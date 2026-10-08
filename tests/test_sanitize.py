"""Hardware erase (ATA Secure Erase, NVMe Sanitize/Format) against fake drives, the NIST mapping and fallback, and
the branded wipe certificate with its QR code. Needs no test disk and no admin rights.

Usage: python tests/test_sanitize.py [workdir]
"""
import os
import re
import shutil
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import qr_decode  # noqa: E402
from sectorsmith import device as D, qr, report, wipe  # noqa: E402
from sectorsmith.device import Device, DeviceError  # noqa: E402
from sectorsmith.util import Progress  # noqa: E402

W = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="ss_sanitize_")
os.makedirs(W, exist_ok=True)
OK = []
SIZE = 8 * 1024 * 1024


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    OK.append(bool(cond))


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return e
    return None


# --- byte samples --------------------------------------------------------------------------------
def ata_string(text, nwords):
    raw = text.ljust(nwords * 2).encode()
    return [struct.unpack(">H", raw[i:i + 2])[0] for i in range(0, len(raw), 2)]


def ata_identify(sec=0x0021, w82=0x746B, w89=0x8002, w90=0x001E, model="Samsung SSD 870 EVO 500GB",
                 serial="S6PXNM0T123456A", fw="SVT02B6Q", w59=0x1000, rotation=1):
    """IDENTIFY DEVICE as a SATA SSD returns it (strings byte-swapped, integrity word with checksum)."""
    w = [0] * 256
    w[0] = 0x0040
    w[10:20] = ata_string(serial, 10)
    w[23:27] = ata_string(fw, 4)
    w[27:47] = ata_string(model, 20)
    w[59], w[82], w[89], w[90], w[128], w[217] = w59, w82, w89, w90, sec, rotation
    data = bytearray(struct.pack("<256H", *w))
    data[510] = 0xA5
    data[511] = (-sum(data[:511])) % 256
    return bytes(data)


def nvme_identify(oacs=0x0017, sanicap=0x00000003, fna=0x04, model="Samsung SSD 980 PRO 1TB",
                  serial="S5GXNX0R654321B", fw="5B2QGXA7"):
    d = bytearray(4096)
    struct.pack_into("<H", d, 0, 0x144D)
    d[4:24] = serial.ljust(20).encode()
    d[24:64] = model.ljust(40).encode()
    d[64:72] = fw.ljust(8).encode()
    struct.pack_into("<H", d, 256, oacs)
    struct.pack_into("<I", d, 328, sanicap)
    d[524] = fna
    return bytes(d)


SEC_SUPPORTED, SEC_ENABLED, SEC_LOCKED, SEC_FROZEN, SEC_EXPIRED, SEC_ENHANCED = 1, 2, 4, 8, 16, 32


class FakeATA(D.PassThrough):
    """A SATA drive's security feature set, erasing the backing file when Secure Erase runs."""

    def __init__(self, path, sec=SEC_SUPPORTED | SEC_ENHANCED, refuse_erase=False, refuse_disable=False):
        self.path, self.sec = path, sec
        self.refuse_erase, self.refuse_disable = refuse_erase, refuse_disable
        self.log, self.password, self.closed = [], None, False
        self.prepared = False
        self.enhanced = None

    def ata_command(self, command, features=0, count=0, data_out=None, data_in=0, timeout=30):
        self.log.append(command)
        if command == D.ATA_IDENTIFY:
            return ata_identify(sec=self.sec)
        if self.sec & SEC_FROZEN and command in (0xF1, 0xF3, 0xF4, 0xF6):
            raise OSError(5, "aborted: frozen")
        pwd = data_out[2:34] if data_out else None
        if command == D.ATA_SECURITY_SET_PASSWORD:
            self.password = pwd
            self.sec |= SEC_ENABLED
        elif command == D.ATA_SECURITY_ERASE_PREPARE:
            self.prepared = True
        elif command == D.ATA_SECURITY_ERASE_UNIT:
            if not self.prepared or pwd != self.password or self.refuse_erase:
                raise OSError(5, "aborted")
            self.enhanced = bool(data_out[0] & 2)
            with open(self.path, "r+b") as f:
                f.write(bytes(os.path.getsize(self.path)))
            self.sec &= ~SEC_ENABLED
            self.password = None
        elif command == D.ATA_SECURITY_DISABLE_PASSWORD:
            if self.refuse_disable or pwd != self.password:
                raise OSError(5, "aborted")
            self.sec &= ~SEC_ENABLED
            self.password = None
        return b""

    def close(self):
        self.closed = True


class FakeNVMe(D.PassThrough):
    def __init__(self, path, ident=None, works=True, refuse=False):
        self.path, self.ident, self.works, self.refuse = path, ident or nvme_identify(), works, refuse
        self.log = []

    def nvme_identify_controller(self):
        self.log.append("identify")
        return self.ident

    def nvme_sanitize(self, method, timeout):
        self.log.append(method)
        if self.refuse:
            raise OSError(1, "Incorrect function")
        if self.works:  # crypto erase: everything reads back as noise
            with open(self.path, "r+b") as f:
                f.write(os.urandom(os.path.getsize(self.path)))


def drive(name, bus="SATA", system=False, fill=b"\x5a"):
    p = os.path.join(W, name)
    with open(p, "wb") as f:
        f.write(fill * SIZE)
    return Device(path=p, name="Disk 7", size=SIZE, bus=bus, model="Test drive", serial="SER123",
                  is_system=system)


def content(dev):
    with open(dev.path, "rb") as f:
        return f.read()


NIST_CLEAR = wipe.METHODS["NIST 800-88 Clear (1 pass + verify)"]

# --- ATA IDENTIFY parsing ------------------------------------------------------------------------
a = wipe.parse_ata_identify(ata_identify())
check(a["model"] == "Samsung SSD 870 EVO 500GB" and a["serial"] == "S6PXNM0T123456A" and a["firmware"] == "SVT02B6Q",
      "IDENTIFY: model, serial and firmware strings un-swapped")
check(a["checksum_ok"] and a["security_supported"] and a["enhanced_supported"] and not a["frozen"]
      and not a["security_enabled"] and not a["locked"], "IDENTIFY: security words read")
check(a["erase_minutes"] == 4 and a["enhanced_minutes"] == 60, "IDENTIFY: erase times (extended and legacy format)")
check(a["solid_state"] and a["sanitize_supported"], "IDENTIFY: solid state and sanitize bits")
bad = bytearray(ata_identify())
bad[100] ^= 1
check(not wipe.parse_ata_identify(bytes(bad))["checksum_ok"], "IDENTIFY: integrity word catches a corrupt sector")
check(raises(lambda: wipe.parse_ata_identify(b"\0" * 100), ValueError) is not None, "IDENTIFY: short data refused")
f = wipe.parse_ata_identify(ata_identify(sec=SEC_SUPPORTED | SEC_FROZEN | SEC_ENHANCED))
check(f["frozen"], "IDENTIFY: frozen bit")
check(wipe.parse_ata_identify(ata_identify(sec=0, w82=0x4000))["security_supported"] is False,
      "IDENTIFY: no security feature set")

# --- method choice -------------------------------------------------------------------------------
p = wipe.plan_from_ata(a)
check(p.available and p.command == "enhanced" and p.name == "ATA Secure Erase (enhanced)" and p.minutes == 60,
      "ATA: enhanced Secure Erase preferred")
p = wipe.plan_from_ata(wipe.parse_ata_identify(ata_identify(sec=SEC_SUPPORTED)))
check(p.available and p.command == "normal" and p.minutes == 4, "ATA: normal Secure Erase when enhanced is missing")
p = wipe.plan_from_ata(f)
check(not p.available and p.frozen and "sleep" in p.reason and "plug" in p.reason, "ATA: frozen says sleep or replug")
p = wipe.plan_from_ata(wipe.parse_ata_identify(ata_identify(sec=SEC_SUPPORTED | SEC_EXPIRED)))
check(p.frozen and "off and on" in p.reason, "ATA: attempt counter expired needs a power cycle")
p = wipe.plan_from_ata(wipe.parse_ata_identify(ata_identify(sec=SEC_SUPPORTED | SEC_ENABLED)))
check(not p.available and not p.frozen and "password" in p.reason, "ATA: existing password blocks it")
p = wipe.plan_from_ata(wipe.parse_ata_identify(ata_identify(sec=SEC_SUPPORTED | SEC_ENABLED | SEC_LOCKED)))
check(not p.available and "locked" in p.reason, "ATA: locked drive")
p = wipe.plan_from_ata(wipe.parse_ata_identify(ata_identify(sec=0, w82=0x4000)))
check(not p.available and "doesn't support" in p.reason, "ATA: unsupported")

n = wipe.parse_nvme_identify(nvme_identify())
check(n["model"] == "Samsung SSD 980 PRO 1TB" and n["serial"] == "S5GXNX0R654321B" and n["firmware"] == "5B2QGXA7",
      "NVMe identify: strings")
check(n["format_supported"] and n["sanitize_crypto"] and n["sanitize_block"] and not n["sanitize_overwrite"]
      and n["crypto_erase_supported"], "NVMe identify: OACS, SANICAP and FNA bits")
plans = [wipe.plan_from_nvme(wipe.parse_nvme_identify(nvme_identify(oacs=o, sanicap=s)))
         for o, s in ((0x17, 3), (0x17, 2), (0x17, 0), (0x05, 0))]
check([x.command for x in plans] == ["crypto", "block", "format", ""] and not plans[3].available,
      "NVMe: crypto sanitize, then block sanitize, then Format, else none")
check(raises(lambda: wipe.parse_nvme_identify(b"\0" * 512), ValueError) is not None, "NVMe identify: short data refused")

# --- NIST mapping --------------------------------------------------------------------------------
check(wipe.nist_category(True) == "Purge" and wipe.nist_category(False) == "Clear", "NIST: hardware Purge, overwrite Clear")
check(wipe.plan_from_ata(a).nist == "Purge" and wipe.plan_from_ata(f).nist == "Clear", "NIST: plan category")

# --- capability check without a drive that can do it -------------------------------------------
from sectorsmith.device import open_image  # noqa: E402
img = drive("plain.img")
check("Image" in wipe.hardware_plan(open_image(img.path)).reason, "image files have no hardware erase")
usb = drive("usb.img", bus="USB")
check(not wipe.hardware_plan(usb, FakeATA(usb.path)).available and "USB" in wipe.hardware_plan(usb).reason,
      "USB bridge: not attempted")
if sys.platform != "win32":
    check(raises(lambda: D.open_passthrough(img), DeviceError) is not None, "pass-through is Windows only")
    check("Windows" in wipe.hardware_plan(img).reason, "capability check off Windows says why")

# --- system disk is refused before any command is sent -------------------------------------------
sysd = drive("system.img", system=True)
fake = FakeATA(sysd.path)
before = content(sysd)
e = raises(lambda: wipe.erase_disk(sysd, NIST_CLEAR, Progress(1), pt=fake), DeviceError)
check(e is not None and "system disk" in str(e) and fake.log == [] and content(sysd) == before,
      "system disk refused, no command sent, nothing written")
check("system disk" in wipe.hardware_plan(sysd, fake).reason and fake.log == [], "system disk: plan refuses too")
e = raises(lambda: wipe.hardware_erase(sysd, wipe.plan_from_ata(a), Progress(1), fake), DeviceError)
check(e is not None and fake.log == [] and content(sysd) == before, "system disk: direct hardware erase refused")

# --- ATA Secure Erase end to end ------------------------------------------------------------------
d1 = drive("ata.img")
fake = FakeATA(d1.path)
r = wipe.erase_disk(d1, NIST_CLEAR, Progress(1), pt=fake)
check(r["hardware"] and r["nist"] == "Purge" and r["method"] == "ATA Secure Erase (enhanced)" and fake.enhanced,
      "ATA: enhanced Secure Erase ran, recorded as hardware Purge")
check(fake.log == [0xEC, 0xF1, 0xF3, 0xF4, 0xEC], f"ATA: command order identify, set password, prepare, erase, "
                                                  f"identify ({[hex(c) for c in fake.log]})")
check(r["verify_ok"] and r["verify_mismatched_blocks"] == 0 and "32 of 32" in r["verification"]
      and "zeros" in r["verification"], "ATA: sampled markers gone, verification recorded")
check(not fake.sec & SEC_ENABLED and content(d1) == bytes(SIZE), "ATA: drive erased and left without a password")

# frozen: report, don't erase, don't fall back by itself
d2 = drive("frozen.img")
fake = FakeATA(d2.path, sec=SEC_SUPPORTED | SEC_FROZEN | SEC_ENHANCED)
before = content(d2)
e = raises(lambda: wipe.erase_disk(d2, NIST_CLEAR, Progress(1), pt=fake), wipe.DriveFrozen)
check(e is not None and "sleep" in str(e) and fake.log == [0xEC] and content(d2) == before,
      "frozen: DriveFrozen raised after IDENTIFY only, drive untouched")
r = wipe.erase_disk(d2, NIST_CLEAR, Progress(1), hardware=False, pt=fake)
check(not r["hardware"] and r["nist"] == "Clear" and r["verify_ok"] and content(d2) == bytes(SIZE),
      "frozen: operator can choose the overwrite instead")

# unsupported: falls back to overwrite and says so
d3 = drive("nosec.img")


class NoSecurity(FakeATA):
    def ata_command(self, command, **kw):
        if command == D.ATA_IDENTIFY:
            self.log.append(command)
            return ata_identify(sec=0, w82=0x4000)
        return super().ata_command(command, **kw)


fake = NoSecurity(d3.path)
r = wipe.erase_disk(d3, NIST_CLEAR, Progress(1), pt=fake)
check(not r["hardware"] and r["nist"] == "Clear" and "doesn't support" in r["fallback_reason"]
      and "Overwritten instead" in r["fallback_reason"] and r["method"] == NIST_CLEAR.name,
      "unsupported: overwrite used, reason recorded")
check(r["verified"] and r["verify_ok"] and content(d3) == bytes(SIZE) and fake.log == [0xEC],
      "unsupported: overwrite verified, no security command sent")

# drive refuses the erase: password removed again, overwrite instead
d4 = drive("refuse.img")
fake = FakeATA(d4.path, refuse_erase=True)
r = wipe.erase_disk(d4, NIST_CLEAR, Progress(1), pt=fake)
check(not r["hardware"] and "Secure Erase failed" in r["fallback_reason"] and fake.log[-1] == 0xF6
      and not fake.sec & SEC_ENABLED and content(d4) == bytes(SIZE),
      "refused erase: temporary password disabled, overwrite ran")

# refuses the erase and the unlock: stop, and say what the password is
d5 = drive("stuck.img")
fake = FakeATA(d5.path, refuse_erase=True, refuse_disable=True)
e = raises(lambda: wipe.erase_disk(d5, NIST_CLEAR, Progress(1), pt=fake), wipe.HardwareEraseFailed)
check(e is not None and not e.fallback_ok and wipe.ATA_TEMP_PASSWORD in str(e), "stuck password: stops and names it")

# partition: always overwrite
d6 = drive("part.img")
fake = FakeATA(d6.path)
r = wipe.erase_disk(d6, NIST_CLEAR, Progress(1), start_lba=2048, sectors=4096, pt=fake)
data = content(d6)
check(not r["hardware"] and "whole drives" in r["fallback_reason"] and fake.log == []
      and data[2048 * 512:6144 * 512] == bytes(4096 * 512) and data[:512] == b"\x5a" * 512,
      "partition: overwritten only in range, no hardware command")

# --- NVMe ------------------------------------------------------------------------------------------
d7 = drive("nvme.img", bus="NVMe")
fake = FakeNVMe(d7.path)
r = wipe.erase_disk(d7, NIST_CLEAR, Progress(1), pt=fake)
check(r["hardware"] and r["method"] == "NVMe Sanitize (crypto erase)" and fake.log == ["identify", "crypto"]
      and r["verify_ok"] and "other data" in r["verification"], "NVMe: crypto sanitize ran and verified")
d8 = drive("nvme_format.img", bus="NVMe")
fake = FakeNVMe(d8.path, nvme_identify(sanicap=0))
r = wipe.erase_disk(d8, NIST_CLEAR, Progress(1), pt=fake)
check(r["hardware"] and fake.log[-1] == "format" and r["nist"] == "Purge", "NVMe: Format with secure erase")
d9 = drive("nvme_lies.img", bus="NVMe")
fake = FakeNVMe(d9.path, works=False)
r = wipe.erase_disk(d9, NIST_CLEAR, Progress(1), pt=fake)
check(r["hardware"] and not r["verify_ok"] and r["verify_mismatched_blocks"] == 32,
      "NVMe: a drive that reports success but erased nothing fails verification")
d10 = drive("nvme_refuse.img", bus="NVMe")
fake = FakeNVMe(d10.path, refuse=True)
r = wipe.erase_disk(d10, NIST_CLEAR, Progress(1), pt=fake)
check(not r["hardware"] and "refused" in r["fallback_reason"] and content(d10) == bytes(SIZE),
      "NVMe: Windows or the drive refuses, overwrite instead")
d11 = drive("nvme_none.img", bus="NVMe")
r = wipe.erase_disk(d11, NIST_CLEAR, Progress(1), pt=FakeNVMe(d11.path, nvme_identify(oacs=0, sanicap=0)))
check(not r["hardware"] and "neither" in r["fallback_reason"], "NVMe: no sanitize or format, overwrite instead")

# cancel before the command goes out leaves the drive as it was apart from the marks
d12 = drive("cancel.img")
fake = FakeATA(d12.path)
pr = Progress(1)
pr.cancel()
e = raises(lambda: wipe.erase_disk(d12, NIST_CLEAR, pr, pt=fake), wipe.Cancelled)
check(e is not None and 0xF4 not in fake.log, "cancel before sending: no erase command")

# --- QR code -------------------------------------------------------------------------------------
for text in ("A", "SECTORSMITH-WIPE 1A2B-3C4D-5E6F SHA256:" + "ab" * 32, "x" * 200, bytes(range(256))[:150]):
    got, info = qr_decode.decode_matrix(qr.qr_matrix(text))
    want = text.encode() if isinstance(text, str) else text
    check(got == want, f"QR round trip, {len(want)} bytes, version {info['version']} mask {info['mask']}")
for mask in range(8):
    got, info = qr_decode.decode_matrix(qr.qr_matrix("mask test", mask=mask))
    check(got == b"mask test" and info["mask"] == mask, f"QR mask {mask}")
check(raises(lambda: qr.qr_matrix("x" * 300), ValueError) is not None, "QR: too long refused")
try:  # an independent reference encoder, when installed: our decoder must read its codes too
    import segno
    s = segno.make("SECTORSMITH reference", error="m", mode="byte", micro=False, boost_error=False)
    got, _ = qr_decode.decode_matrix([[1 if x else 0 for x in r] for r in s.matrix])
    check(got == b"SECTORSMITH reference", "QR: test decoder reads the segno reference encoder")
except ImportError:
    print("SKIP segno not installed (reference cross-check)")

# --- certificate ---------------------------------------------------------------------------------
def tiny_png():
    raw = b"\x00\x00\x00\x00"  # one black pixel, filter byte + RGB

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


logo = os.path.join(W, "logo.png")
with open(logo, "wb") as fh:
    fh.write(tiny_png())
info = dict(write_errors=0, verify_mismatched_blocks=0, verified=True, verify_ok=True, passes=1,
            bytes=SIZE, hardware=True, nist="Purge", method="ATA Secure Erase (enhanced)",
            verification="32 of 32 sampled sectors no longer hold the marks written before the erase",
            client="Northwind Dental", ticket="48213", technician="Sam Patel", machine="BENCH-PC-02",
            model="Samsung SSD 870 EVO 500GB", serial="S6PXNM0T123456A", size=500107862016, bus="SATA",
            drive_firmware="SVT02B6Q", scope="Disk 2 (whole drive)", start=1760000000.0, end=1760000125.0)
branding = {"company": "Example IT Services Ltd", "logo": logo}
html_doc, cid, digest = report.render_certificate(info, branding)
for want in ("Example IT Services Ltd", "data:image/png;base64,", "Northwind Dental", "48213", "Sam Patel",
             "BENCH-PC-02", "Samsung SSD 870 EVO 500GB", "S6PXNM0T123456A", "465.76 GB", "500,107,862,016 bytes",
             "SATA", "SVT02B6Q", "ATA Secure Erase (enhanced)", "Hardware (drive&#x27;s own erase)", "Purge",
             "32 of 32 sampled sectors", "Passed: erased and verified", cid, digest, "Certificate of Data Erasure",
             "size: A4"):
    check(want in html_doc, f"certificate shows {want[:40]}")
check(f"Started</th><td>{report._stamp(1760000000.0)}" in html_doc and "Duration</th><td>00:02:05" in html_doc,
      "certificate shows start, end and duration")
check(html_doc.count("<svg") == 1 and "mossbit" not in html_doc.lower(), "one QR, no mascot")
from sectorsmith.ui import theme  # noqa: E402
app_colours = {v.lower() for v in theme.PALETTE.values() if isinstance(v, str) and v.startswith("#") and
               v.lower() not in ("#fff", "#ffffff", "#000", "#000000", "#111111", "#111")}
used = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{3,6}\b", html_doc)}
check(not (used & app_colours), f"no app colours on the certificate ({sorted(used & app_colours)})")
svg = re.search(r"<svg.*?</svg>", html_doc, re.S).group(0)
got, qinfo = qr_decode.decode_matrix(qr_decode.matrix_from_svg(svg))
check(got.decode() == f"SECTORSMITH-WIPE {cid} SHA256:{digest}", f"certificate QR decodes to ID and hash "
                                                                  f"(version {qinfo['version']})")
ok, again = report.verify_certificate(html_doc)
check(ok and again == digest, "record hash recalculates from the embedded record")
tampered = html_doc.replace('"drive_serial":"S6PXNM0T123456A"', '"drive_serial":"S6PXNM0T999999Z"')
check(tampered != html_doc and not report.verify_certificate(tampered)[0], "changing the record breaks the hash")
p = os.path.join(W, "cert.html")
cid2 = report.wipe_certificate(p, dict(info, cert_id="TEST-0000-0001"), branding)
with open(p, encoding="utf-8") as fh:
    cert_text = fh.read()
check(cid2 == "TEST-0000-0001" and "TEST-0000-0001" in cert_text, "certificate written to file")

# overwrite fallback, no branding, errors
info2 = dict(info, hardware=False, nist="Clear", method=NIST_CLEAR.name, write_errors=3, verify_mismatched_blocks=1,
             fallback_reason="This drive doesn't support ATA Secure Erase. Overwritten instead.", client="",
             ticket="")
doc2, _, _ = report.render_certificate(info2, None)
check("Software overwrite" in doc2 and ">Clear<" in doc2 and "Completed with errors" in doc2
      and "Overwritten instead" in doc2 and 'class="logo"' not in doc2, "fallback certificate: Clear, errors, note")

shutil.rmtree(W, ignore_errors=True)
print(f"\n{sum(OK)}/{len(OK)} sanitize checks passed")
sys.exit(0 if all(OK) else 1)
