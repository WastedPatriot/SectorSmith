"""Agent side of SectorSmith Link. Started by the copy-paste command (dials the controller) or in
listen mode on a machine booted from a SectorSmith USB stick (waits for the controller, shows a code)."""
from __future__ import annotations

import hmac
import socket
import sys
import threading
import time

from ..util import APP_VERSION, get_logger
from .endpoint import LocalEndpoint, Session
from .proto import DEFAULT_PORT, Conn, LinkError, client_context, local_ips, make_cert, new_code, \
    peer_fingerprint, server_context

log = get_logger()


class AgentState:
    def __init__(self):
        self.status = "Starting…"
        self.detail = ""
        self.connected_to = None
        self.stop = threading.Event()
        self.listeners = []

    def set(self, status, detail=None, connected_to=None):
        self.status = status
        if detail is not None:
            self.detail = detail
        self.connected_to = connected_to
        log.info("Agent: %s %s", status, detail or "")
        for fn in self.listeners:
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


def _serve(conn: Conn, ep: LocalEndpoint, state: AgentState):
    sess = Session(ep)
    try:
        while not state.stop.is_set():
            req, blob = conn.recv()
            if req.get("op") == "bye":
                break
            resp, rblob = sess.handle(req, blob)
            conn.send(resp, rblob)
    finally:
        sess.close()  # stops anything the controller left running (it may have gone mid-install)


def run_connect(host: str, port: int, token: str, pin: str, ep: LocalEndpoint, state: AgentState,
                give_up_after=600):
    last_ok = time.monotonic()
    while not state.stop.is_set():
        try:
            state.set("Connecting…", f"to {host}:{port}")
            raw = socket.create_connection((host, port), timeout=10)
            tls = client_context().wrap_socket(raw)
            if not hmac.compare_digest(peer_fingerprint(tls), pin.lower()):
                tls.close()
                state.set("Refused", "The controller's certificate didn't match the command. Not connecting.")
                return 2
            conn = Conn(tls)
            conn.send({"hello": {"token": token, "info": ep.info()}})
            res, _ = conn.recv()
            if not res.get("welcome"):
                state.set("Refused", res.get("error", "Token rejected"))
                return 3
            state.set("Connected", f"Controlled by {res.get('controller')}", connected_to=res.get("controller"))
            last_ok = time.monotonic()
            _serve(conn, ep, state)
            conn.close()
            ep.close()
            state.set("Disconnected", "The controller closed the session.")
            return 0
        except (OSError, LinkError) as e:
            ep.close()
            if time.monotonic() - last_ok > give_up_after:
                state.set("Gave up", str(e))
                return 1
            state.set("Waiting for controller…", f"{e} — retrying")
            state.stop.wait(5)
    return 0


def run_listen(port: int, ep: LocalEndpoint, state: AgentState, code: str | None = None):
    cert, key, fp = make_cert()
    ctx = server_context(cert, key)
    code = code or new_code()
    state.code = code
    state.check = fp[:6].upper()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # must accept the controller from the LAN; TLS + pairing code
    s.bind(("0.0.0.0", port))  # nosec B104
    s.listen(2)
    s.settimeout(1.0)
    if sys.platform == "win32":
        import subprocess
        subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", "name=SectorSmith Agent", "dir=in",
                        "action=allow", "protocol=TCP", f"localport={port}"], capture_output=True)
    ips = ", ".join(local_ips())
    state.set("Waiting for a controller", f"IP {ips}  ·  code {code}  ·  check {state.check}")
    while not state.stop.is_set():
        try:
            raw, addr = s.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            raw.settimeout(20)
            tls = ctx.wrap_socket(raw, server_side=True)
            conn = Conn(tls)
            hello, _ = conn.recv()
            h = hello.get("hello_controller") or {}
            if not hmac.compare_digest(str(h.get("code", "")).upper(), code):
                conn.send({"error": "Wrong pairing code"})
                conn.close()
                state.set("Waiting for a controller", f"Rejected {addr[0]} (wrong code). IP {ips} · code {code}")
                continue
            conn.send({"welcome": True, "info": ep.info(), "version": APP_VERSION})
            tls.settimeout(None)
            state.set("Connected", f"Controlled by {h.get('controller', addr[0])}", connected_to=addr[0])
            try:
                _serve(conn, ep, state)
            except (OSError, LinkError):
                pass
            conn.close()
            ep.close()
            state.set("Waiting for a controller", f"IP {ips}  ·  code {code}  ·  check {state.check}")
        except Exception as e:  # noqa: BLE001
            log.warning("Listen-mode session failed: %s", e)
    s.close()
    return 0


# ---------------------------------------------------------------------------
def _window(state: AgentState, worker):
    """Small status window (if a desktop is available)."""
    import tkinter as tk
    root = tk.Tk()
    root.title("SectorSmith Agent")
    root.geometry("460x330")
    root.configure(background="#191623")
    try:
        from ..ui.mascot import ASSETS, Sprites
        import os
        frames, fps, w, h = Sprites.get(root, "idle", 3)
        root.iconphoto(True, tk.PhotoImage(file=os.path.join(ASSETS, "icon.png")))
    except Exception:  # noqa: BLE001
        frames = []
    cv = tk.Canvas(root, width=170, height=140, background="#191623", highlightthickness=0)
    cv.pack(pady=(14, 0))
    title = tk.Label(root, text="", font=("Segoe UI", 16, "bold"), fg="#F4F1FF", bg="#191623")
    title.pack()
    detail = tk.Label(root, text="", font=("Segoe UI", 11), fg="#A49EBB", bg="#191623", wraplength=420)
    detail.pack(pady=6)
    code = tk.Label(root, text="", font=("Consolas", 22, "bold"), fg="#FF5C99", bg="#191623")
    code.pack()

    def stop():
        state.stop.set()
        root.after(300, root.destroy)
    tk.Button(root, text="Disconnect & close", command=stop, bg="#2B2640", fg="#F4F1FF", relief="flat",
              padx=14, pady=6).pack(pady=10)
    i = [0]

    def tick():
        title.configure(text=state.status)
        detail.configure(text=state.detail)
        if getattr(state, "code", None) and state.status.startswith("Waiting"):
            code.configure(text=f"{state.code}")
        else:
            code.configure(text="")
        if frames:
            cv.delete("all")
            cv.create_image(0, 0, image=frames[i[0] % len(frames)], anchor="nw")
            i[0] += 1
        if not worker.is_alive():
            root.after(4000, root.destroy)
            return
        root.after(140, tick)
    tick()
    root.protocol("WM_DELETE_WINDOW", stop)
    root.mainloop()


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="SectorSmith --agent")
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--connect")
    ap.add_argument("--token")
    ap.add_argument("--pin")
    ap.add_argument("--listen", action="store_true")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--code")
    ap.add_argument("--image", action="append", default=[], help="expose a disk image (testing)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--no-elevate", action="store_true")
    ap.add_argument("--profiles-root")
    a, _ = ap.parse_known_args(argv)
    if a.profiles_root:
        import os
        os.environ["SECTORSMITH_PROFILES_ROOT"] = a.profiles_root
    ep = LocalEndpoint(extra_images=a.image)
    state = AgentState()
    if a.listen:
        target = lambda: run_listen(a.port, ep, state, a.code)  # noqa: E731
    else:
        if not (a.connect and a.token and a.pin):
            print("Usage: SectorSmith --agent --connect HOST:PORT --token T --pin FINGERPRINT  |  --agent --listen")
            return 2
        host, _, port = a.connect.rpartition(":")
        target = lambda: run_connect(host, int(port), a.token, a.pin, ep, state)  # noqa: E731
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("rc", target()), daemon=True)
    worker.start()
    if not a.headless:
        try:
            _window(state, worker)
            state.stop.set()
            worker.join(3)
            return result.get("rc", 0)
        except Exception:  # noqa: BLE001  (no desktop, e.g. SYSTEM via RMM)
            pass
    worker.join()
    return result.get("rc", 0)
