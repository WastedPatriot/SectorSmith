"""Controller side of SectorSmith Link: accepts agents that dial in with the copy-paste command,
serves the agent .exe to them, and can also dial out to a machine waiting in USB/listen mode."""
from __future__ import annotations

import hashlib
import hmac
import http.server
import os
import secrets
import socket
import subprocess
import sys
import threading

from ..util import APP_VERSION, get_logger
from .endpoint import RemoteEndpoint
from .proto import DEFAULT_PORT, Conn, LinkError, client_context, local_ips, make_cert, peer_fingerprint, \
    server_context

log = get_logger()
FW_RULE = "SectorSmith Link"


def _netsh(args):
    if sys.platform != "win32":
        return
    try:
        subprocess.run(["netsh", "advfirewall", "firewall"] + args, capture_output=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


class LinkServer:
    def __init__(self, on_connect=None, on_disconnect=None, port=DEFAULT_PORT):
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.port = port
        self.token = None
        self.fingerprint = None
        self.machines: list[RemoteEndpoint] = []
        self._sock = None
        self._http = None
        self.running = False
        self.exe_path = sys.executable if getattr(sys, "frozen", False) else None
        self.exe_sha = None
        self.dl_token = secrets.token_urlsafe(12)

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self.running:
            return
        cert, key, self.fingerprint = make_cert()
        self.ctx = server_context(cert, key)
        self.token = secrets.token_hex(16)
        for p in range(self.port, self.port + 20, 2):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                # agents dial in from the LAN; TLS + pinned cert + token
                s.bind(("0.0.0.0", p))  # nosec B104
            except OSError:
                s.close()
                continue
            self.port = p
            break
        else:
            raise LinkError("No free port for SectorSmith Link")
        s.listen(8)
        self._sock = s
        self.running = True
        _netsh(["delete", "rule", f"name={FW_RULE}"])
        _netsh(["add", "rule", f"name={FW_RULE}", "dir=in", "action=allow", "protocol=TCP",
                f"localport={self.port}-{self.port + 1}", "profile=private,domain"])
        threading.Thread(target=self._accept_loop, daemon=True).start()
        if self.exe_path:
            self._start_http()
        log.info("Link server listening on %s", self.port)

    def stop(self):
        self.running = False
        for m in list(self.machines):
            m.close()
        try:
            self._sock.close()
        except (OSError, AttributeError):
            pass
        if self._http:
            self._http.shutdown()
        _netsh(["delete", "rule", f"name={FW_RULE}"])

    # ------------------------------------------------------------------ exe download
    def _start_http(self):
        h = hashlib.sha256()
        with open(self.exe_path, "rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                h.update(chunk)
        self.exe_sha = h.hexdigest().upper()
        server = self
        want = f"/{self.dl_token}/SectorSmith.exe"

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path != want:
                    self.send_error(404)
                    return
                size = os.path.getsize(server.exe_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with open(server.exe_path, "rb") as f:
                    while True:
                        b = f.read(1024 * 1024)
                        if not b:
                            break
                        self.wfile.write(b)

            def log_message(self, *a):
                pass

        # serves the agent exe to the LAN; verified by SHA-256
        self._http = http.server.ThreadingHTTPServer(("0.0.0.0", self.port + 1), H)  # nosec B104
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    def command(self, ip: str) -> str:
        """The copy-paste command for the other machine (PowerShell, run as admin / RMM shell)."""
        args = f"'--agent','--connect','{ip}:{self.port}','--token','{self.token}','--pin','{self.fingerprint}'"
        if self.exe_path:
            url = f"http://{ip}:{self.port + 1}/{self.dl_token}/SectorSmith.exe"
            return (f"$f=Join-Path $env:TEMP 'SectorSmithAgent.exe'; "
                    f"Invoke-WebRequest -UseBasicParsing '{url}' -OutFile $f; "
                    f"if((Get-FileHash $f -Algorithm SHA256).Hash -ne '{self.exe_sha}'){{throw 'Checksum mismatch'}}; "
                    f"Start-Process $f -ArgumentList {args}")
        # running from source: SectorSmith must already be on the other machine
        return (f"python -m sectorsmith --agent --connect {ip}:{self.port} --token {self.token} "
                f"--pin {self.fingerprint}")

    def addresses(self):
        return local_ips()

    # ------------------------------------------------------------------ agents dialling in
    def _accept_loop(self):
        while self.running:
            try:
                raw, addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handshake, args=(raw, addr), daemon=True).start()

    def _handshake(self, raw, addr):
        try:
            raw.settimeout(20)
            tls = self.ctx.wrap_socket(raw, server_side=True)
            conn = Conn(tls)
            tls.settimeout(20)
            hello, _ = conn.recv()
            h = hello.get("hello") or {}
            if not hmac.compare_digest(str(h.get("token", "")), self.token):
                conn.send({"error": "bad token"})
                conn.close()
                log.warning("Rejected link from %s (bad token)", addr[0])
                return
            conn.send({"welcome": True, "controller": socket.gethostname(), "version": APP_VERSION})
            tls.settimeout(None)
            self._register(RemoteEndpoint(conn, h.get("info", {}), addr[0]))
        except Exception as e:  # noqa: BLE001
            log.warning("Link handshake from %s failed: %s", addr[0], e)
            try:
                raw.close()
            except OSError:
                pass

    def _register(self, ep: RemoteEndpoint):
        # replace an older connection from the same machine
        for old in [m for m in self.machines if m.label == ep.label]:
            old.close()
            self.machines.remove(old)
        self.machines.append(ep)
        log.info("Linked machine %s (%s)", ep.label, ep.address)
        threading.Thread(target=self._watch, args=(ep,), daemon=True).start()
        if self.on_connect:
            self.on_connect(ep)

    def _watch(self, ep):
        import time
        while ep.alive:
            time.sleep(5)
            try:
                ep.ping()
            except Exception:  # noqa: BLE001
                ep.alive = False
        if ep in self.machines:
            self.machines.remove(ep)
        if self.on_disconnect:
            self.on_disconnect(ep)

    def disconnect(self, ep):
        ep.close()

    # ------------------------------------------------------------------ dial out (USB / listen mode)
    def connect_to_waiting(self, host: str, code: str, port: int = DEFAULT_PORT) -> RemoteEndpoint:
        raw = socket.create_connection((host, port), timeout=10)
        tls = client_context().wrap_socket(raw)
        fp = peer_fingerprint(tls)
        conn = Conn(tls)
        tls.settimeout(20)
        conn.send({"hello_controller": {"code": code.strip().upper(), "controller": socket.gethostname()}})
        res, _ = conn.recv()
        if not res.get("welcome"):
            conn.close()
            raise LinkError(res.get("error", "Pairing code was not accepted"))
        tls.settimeout(None)
        ep = RemoteEndpoint(conn, res.get("info", {}), host)
        ep.check = fp[:6].upper()
        self._register(ep)
        return ep
