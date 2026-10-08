"""SectorSmith Link wire protocol: TLS (self-signed, fingerprint-pinned) + length-prefixed frames.

Frame:  >I length  B kind  payload
kind:   J = JSON, Z = zlib-compressed JSON, B = binary blob (always follows a J/Z frame that says "blob": n)
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
import socket
import ssl
import struct
import tempfile
import zlib

DEFAULT_PORT = 47310
MAX_FRAME = 64 * 1024 * 1024


class LinkError(Exception):
    pass


class RemoteError(LinkError):
    def __init__(self, kind, msg):
        super().__init__(f"{kind}: {msg}")
        self.kind = kind


# ---------------------------------------------------------------------------
def make_cert(common_name="SectorSmith Link"):
    """Ephemeral self-signed EC certificate. Returns (cert_pem, key_pem, sha256_fingerprint_hex)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=7)).sign(key, hashes.SHA256()))
    der = cert.public_bytes(serialization.Encoding.DER)
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()),
            hashlib.sha256(der).hexdigest())


def server_context(cert_pem: bytes, key_pem: bytes) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    d = tempfile.mkdtemp(prefix="sslink")
    cp, kp = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    try:
        with open(cp, "wb") as f:
            f.write(cert_pem)
        with open(kp, "wb") as f:
            f.write(key_pem)
        ctx.load_cert_chain(cp, kp)
    finally:
        for p in (cp, kp):
            try:
                os.remove(p)
            except OSError:
                pass
        os.rmdir(d)
    return ctx


def client_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # we pin the exact certificate fingerprint instead
    return ctx


def peer_fingerprint(sock: ssl.SSLSocket) -> str:
    return hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()


def new_token() -> str:
    return secrets.token_hex(16)


def new_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def local_ips() -> list[str]:
    ips = []
    try:  # address of the default route first
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips or ["127.0.0.1"]


# ---------------------------------------------------------------------------
class Conn:
    """Thread-unsafe framed connection; callers serialise access."""

    def __init__(self, sock):
        self.sock = sock
        self.sock.settimeout(None)
        self.rx = 0
        self.tx = 0

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(min(n - len(buf), 1024 * 1024))
            if not chunk:
                raise LinkError("Connection closed")
            buf += chunk
        self.rx += n
        return bytes(buf)

    def _send_frame(self, kind: bytes, payload: bytes):
        self.sock.sendall(struct.pack(">IB", len(payload), kind[0]) + payload)
        self.tx += len(payload) + 5

    def send(self, obj: dict, blob: bytes | None = None):
        if blob is not None:
            obj = dict(obj, blob=len(blob))
        data = json.dumps(obj, separators=(",", ":")).encode()
        if len(data) > 64 * 1024:
            self._send_frame(b"Z", zlib.compress(data, 3))
        else:
            self._send_frame(b"J", data)
        if blob is not None:
            self._send_frame(b"B", blob)

    def recv(self):
        n, kind = struct.unpack(">IB", self._recv_exact(5))
        if n > MAX_FRAME:
            raise LinkError("Frame too large")
        payload = self._recv_exact(n)
        if kind == ord("Z"):
            payload = zlib.decompress(payload)
        elif kind != ord("J"):
            raise LinkError("Protocol error: expected a message frame")
        obj = json.loads(payload)
        blob = None
        if "blob" in obj:
            m, k2 = struct.unpack(">IB", self._recv_exact(5))
            if k2 != ord("B") or m != obj["blob"]:
                raise LinkError("Protocol error: bad blob frame")
            blob = self._recv_exact(m)
        return obj, blob

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
