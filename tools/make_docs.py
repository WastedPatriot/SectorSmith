"""Builds docs/images (hero banner, animated sweep GIF, screenshots) for the README.

Linux only, run under a virtual display:
    xvfb-run -s "-screen 0 1360x860x24" python tools/make_docs.py <workdir-from-tests/build_test_disk.py>
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

W = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sstest"
OUT = os.path.join(ROOT, "docs", "images")
RAW = os.path.join(W, "docs_raw")
os.makedirs(OUT, exist_ok=True)
os.makedirs(RAW, exist_ok=True)
ASSETS = os.path.join(ROOT, "sectorsmith", "ui", "assets")
FONT = {"xb": "/usr/share/fonts/opentype/inter/InterDisplay-ExtraBold.otf",
        "b": "/usr/share/fonts/opentype/inter/Inter-Bold.otf",
        "sb": "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf",
        "r": "/usr/share/fonts/opentype/inter/Inter-Regular.otf"}


def font(kind, size):
    return ImageFont.truetype(FONT[kind], size)


def frames(anim, scale):
    import json
    meta = json.load(open(os.path.join(ASSETS, "mossbit.json")))
    n = meta["anims"][anim]["frames"]
    strip = Image.open(os.path.join(ASSETS, f"mossbit_{anim}_x{scale}.png")).convert("RGBA")
    fw = strip.width // n
    return [strip.crop((i * fw, 0, (i + 1) * fw, strip.height)) for i in range(n)], meta["anims"][anim]["fps"]


# --------------------------------------------------------------------------- screenshots
def take_screenshots():
    from tkinter import filedialog, messagebox
    from sectorsmith.ui import main as M, screens as S, theme, workspace as WS, settings as ST
    from sectorsmith.ui import deploy_screens as DS
    from sectorsmith.util import app_dir
    try:
        os.remove(app_dir() / "settings.json")
    except OSError:
        pass
    img = os.path.join(W, "docs.img")
    shutil.copyfile(os.path.join(W, "ntfs.img"), img)  # has deleted files (made by tests/test_core.py)
    lost = os.path.join(W, "docs_lost.img")
    shutil.copyfile(os.path.join(W, "disk.img"), lost)
    with open(lost, "r+b") as f:
        f.write(bytes(34 * 512))
    messagebox.askyesno = lambda *a, **k: True
    filedialog.asksaveasfilename = lambda **k: os.path.join(W, "docs_backup.vhd")
    app = M.MainWindow()
    app.geometry("1360x860+0+0")

    def pump(sec=.4):
        end = time.time() + sec
        while time.time() < end:
            app.update()
            time.sleep(.015)

    def wait():
        pump(.4)
        while app.job is not None:
            pump(.1)
        pump(.6)

    def shot(name):
        pump(.6)
        subprocess.run(["import", "-window", "root", os.path.join(RAW, name + ".png")], check=False)

    def mode(m):
        theme.set_mode(m)
        app.mode.set(m.capitalize())
        pump(.3)

    def pick(path, idx=None):
        for card, _b, t in app.screen.picker.rows:
            if t["dev"].path == path and ((idx is None and t["kind"] == "disk") or
                                          (t["kind"] == "part" and t["part"].index == idx)):
                card.click()
                pump(.2)
                return
        raise AssertionError(path)

    def press(text):
        for w in app.screen.footer.winfo_children():
            if w.cget("text") == text:
                w.invoke()
                pump(.3)
                return
        raise AssertionError(text)

    def type_confirm(phrase):
        for w in app.screen.body.winfo_children():
            for c in w.winfo_children():
                if c.__class__.__name__ == "CTkEntry":
                    c.insert(0, phrase)
        pump(.2)

    mode("light")
    pump(1.2)
    shot("welcome")
    press("Skip")
    app.set_client("Northwind Dental")
    # hide the unreadable sandbox disks so the drive list looks like a real PC
    real = app.inventory()
    app._inventory = []
    app.add_image(img)
    app.add_image(lost)
    app._inventory = [x for x in app.inventory(True) if x[0].is_image]
    _ = real
    pump(4)  # let toasts disappear

    mode("light")
    app.go(S.RecoverWizard)
    pump(.6)
    shot("recover_pick")
    pick(img, 2)
    press("Next")
    shot("recover_type")
    press("Start scan")
    wait()
    shot("recover_results")

    mode("dark")
    app.go(S.WipeWizard)
    pump(.6)
    pick(img, 1)
    press("Next")
    shot("wipe_strength")
    press("Next")
    type_confirm("ERASE PARTITION 1")
    shot("wipe_confirm")
    # progress screen with a slow fake job so the sweeper is visible
    _, panel = app.screen.progress("Erasing...", "NIST 800-88 Clear (1 pass + verify) on Disk 2 · Samsung T7 · 1 TB")
    app.screen._draw_steps(3)

    def fake(prog):
        prog.reset(2 * 1024 ** 3, "Pass 1 of 1: writing zeros")
        prog.set_detail("pattern 0x00")
        for i in range(100):
            prog.check()
            time.sleep(.05)
            prog.update(int(.52 * prog.total * i / 100))
    app.run_job("Erase and certify", fake, lambda r: None, panel)
    pump(4.5)
    shot("progress_dark")
    wait()

    mode("light")
    app.go(S.HealthWizard)
    pump(.6)
    pick(img)
    press("Next")
    wait()
    shot("health_result")

    mode("dark")
    app.go(S.PartitionWizard)
    pump(.6)
    pick(lost)
    press("Next")
    wait()
    shot("partitions_found")

    mode("light")
    tmpd = os.path.join("/tmp", "Desktop")  # short path, it shows on screen
    os.makedirs(tmpd, exist_ok=True)
    names = ["Payroll 2025.xlsx", "Client contracts", "old-passwords.txt"]
    for n in names:
        p = os.path.join(tmpd, n)
        if "." in n:
            open(p, "w").write("x" * 9000)
        else:
            os.makedirs(p, exist_ok=True)
    app.home()
    pump(.5)
    app.handle_drop([os.path.join(tmpd, n) for n in names])
    pump(.8)
    shot("shred_drop")

    app.go(S.CloneWizard)
    pump(.6)
    pick(img)
    press("Next")
    shot("clone_dest")

    mode("dark")
    app.open_guide("wipe")
    pump(.8)
    shot("guide_dark")

    shutil.rmtree(tmpd, ignore_errors=True)

    # the new workspace pages
    mode("light")
    app.go(WS.DrivesScreen)
    pump(1.5)
    shot("drives_light")
    mode("dark")
    app.go(WS.MachinesScreen)
    pump(1)
    shot("machines_dark")
    mode("light")
    app.go(WS.JobsScreen)
    pump(1)
    shot("jobs_light")
    app.go(DS.DeployScreen)
    pump(1.2)
    shot("manage_light")
    app.go(ST.SettingsScreen)
    pump(1)
    shot("settings_light")
    mode("dark")
    app.home()
    pump(.8)
    app.open_palette()
    pump(.8)
    shot("palette_dark")
    app.event_generate("<Escape>")
    pump(.4)

    # Home last, so recent jobs are filled in
    mode("light")
    app.home()
    pump(1.2)
    shot("home_light")
    mode("dark")
    shot("home_dark")
    app.destroy()


# --------------------------------------------------------------------------- composition
def rounded(im, r):
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), r, fill=255)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def framed(name, width=1200, src=None, out=None, dark=None):
    """Screenshot in a soft window frame with shadow (README-ready)."""
    im = Image.open(src or os.path.join(RAW, name + ".png")).convert("RGBA").crop((0, 0, 1360, 860))
    bar_h = 34
    if dark is None:
        dark = "dark" in name or name in ("partitions_found", "wipe_strength", "wipe_confirm")
    bar_bg, fg = ((32, 29, 44, 255), (190, 184, 210)) if dark else ((236, 233, 243, 255), (90, 84, 112))
    win = Image.new("RGBA", (im.width, im.height + bar_h), bar_bg)
    d = ImageDraw.Draw(win)
    icon = Image.open(os.path.join(ASSETS, "mark_32.png")).convert("RGBA").resize((20, 20), Image.LANCZOS)
    win.alpha_composite(icon, (12, 7))
    d.text((40, 17), "SectorSmith", fill=fg, font=font("sb", 14), anchor="lm")
    x = im.width - 22
    d.line((x - 6, 11, x + 6, 23), fill=fg, width=2)
    d.line((x - 6, 23, x + 6, 11), fill=fg, width=2)
    x -= 46
    d.rectangle((x - 6, 11, x + 6, 23), outline=fg, width=2)
    x -= 46
    d.line((x - 6, 17, x + 6, 17), fill=fg, width=2)
    win.paste(im, (0, bar_h))
    win = rounded(win, 14)
    scale = width / win.width
    win = win.resize((width, int(win.height * scale)), Image.LANCZOS)
    pad = 40
    canvas = Image.new("RGBA", (win.width + pad * 2, win.height + pad * 2), (0, 0, 0, 0))
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((pad, pad + 10, pad + win.width, pad + win.height + 6), 16,
                                         fill=(20, 10, 40, 90))
    sh = sh.filter(ImageFilter.GaussianBlur(16))
    canvas.alpha_composite(sh)
    canvas.alpha_composite(win, (pad, pad))
    canvas.save(os.path.join(OUT, (out or name) + ".png"), optimize=True)


def roam_gif():
    """Mossbit strolling across, turning around and sweeping back."""
    walk, _ = frames("walk", 3)
    sweep, _ = frames("sweep", 3)
    sweep_l = [f.transpose(Image.FLIP_LEFT_RIGHT) for f in sweep]
    Wd, Hd = 640, 150
    fw = walk[0].width
    seq = []
    x = -20
    for i in range(48):
        seq.append((walk[i % 6], x))
        x += 7
    for i in range(48):
        seq.append((sweep_l[i % 6], x - 20))
        x -= 6
    imgs = []
    for f, xx in seq:
        im = Image.new("RGBA", (Wd, Hd), (0, 0, 0, 0))
        im.alpha_composite(f, (int(max(-fw // 2, min(Wd - fw // 2, xx))), Hd - f.height))
        imgs.append(im)
    imgs[0].save(os.path.join(OUT, "mossbit_roam.gif"), save_all=True, append_images=imgs[1:], duration=80, loop=0,
                 disposal=2, transparency=0)


def link_shots(folder):
    """Frame screenshots produced by tests/ui_link_smoke.py."""
    mapping = {"connect_linked": ("connect", False), "migrate_from": ("migrate_from", False),
               "migrate_what": ("migrate_what", False), "migrate_apps": ("migrate_apps", False),
               "migrate_done": ("migrate_done", False), "netclone_dest": ("netclone_dest", True),
               "netclone_running": ("netclone_running", True)}
    files = sorted(os.listdir(folder))
    for raw, (out, dark) in mapping.items():
        hits = [f for f in files if f.endswith("_" + raw + ".png")]
        if hits:
            framed(raw, src=os.path.join(folder, hits[0]), out=out, dark=dark)


def hero():
    Wd, Hd = 1600, 640
    bg = Image.new("RGBA", (Wd, Hd))
    px = bg.load()
    for y in range(Hd):
        for x in range(0, Wd):
            t = x / Wd * .6 + y / Hd * .4
            px[x, y] = (int(18 + 30 * t), int(14 + 12 * t), int(30 + 50 * t), 255)
    glow = Image.new("RGBA", (Wd, Hd), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-120, 220, 620, 900), fill=(82, 201, 142, 70))
    gd.ellipse((1050, -260, 1800, 380), fill=(99, 102, 241, 80))
    glow = glow.filter(ImageFilter.GaussianBlur(110))
    bg.alpha_composite(glow)
    # pixel grid texture
    tex = Image.new("RGBA", (Wd, Hd), (0, 0, 0, 0))
    td = ImageDraw.Draw(tex)
    for x in range(0, Wd, 24):
        td.line((x, 0, x, Hd), fill=(255, 255, 255, 7))
    for y in range(0, Hd, 24):
        td.line((0, y, Wd, y), fill=(255, 255, 255, 7))
    bg.alpha_composite(tex)
    # screenshot on the right, tilted card look
    shot = Image.open(os.path.join(RAW, "home_dark.png")).convert("RGBA").crop((0, 0, 1360, 860))
    shot = rounded(shot.resize((800, int(860 * 800 / 1360)), Image.LANCZOS), 16)
    sh = Image.new("RGBA", (Wd, Hd), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle((760, 130, 760 + 800, 130 + shot.height), 18, fill=(0, 0, 0, 150))
    bg.alpha_composite(sh.filter(ImageFilter.GaussianBlur(28)))
    bg.alpha_composite(shot, (740, 110))
    # Mossbit
    fr, _ = frames("happy", 5)
    m = fr[3].resize((fr[3].width * 6 // 5, fr[3].height * 6 // 5), Image.NEAREST)
    bg.alpha_composite(m, (300, Hd - m.height + 10))
    d = ImageDraw.Draw(bg)
    d.text((80, 96), "SectorSmith", font=font("xb", 92), fill=(244, 241, 255))
    d.text((84, 204), "The bench toolkit", font=font("sb", 32), fill=(214, 206, 240))
    d.text((84, 246), "for MSP technicians.", font=font("sb", 32), fill=(214, 206, 240))
    chips = ["Recover", "Erase", "Migrate", "Deploy", "Clone"]
    x = 86
    layer = Image.new("RGBA", (Wd, Hd), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for c in chips:
        w = ld.textlength(c, font=font("b", 19)) + 32
        ld.rounded_rectangle((x, 308, x + w, 346), 19, fill=(99, 102, 241, 56), outline=(129, 140, 248, 200),
                             width=2)
        ld.text((x + w / 2, 327), c, font=font("b", 19), fill=(224, 231, 255, 255), anchor="mm")
        x += w + 10
    bg.alpha_composite(layer)
    bg.convert("RGB").save(os.path.join(OUT, "hero.png"), optimize=True)


def sweep_gif():
    fr, fps = frames("sweep", 3)
    Wd, Hd = 900, 230
    bar_x0, bar_x1, bar_y, bar_h = 40, Wd - 40, 180, 18
    imgs = []
    n = 72
    import random
    rnd = random.Random(3)
    dust = [(rnd.random(), rnd.randint(0, 3), rnd.choice((1, 2))) for _ in range(80)]
    for i in range(n + 10):
        v = min(1.0, i / n)
        im = Image.new("RGB", (Wd, Hd), (18, 16, 25))
        d = ImageDraw.Draw(im)
        d.rectangle((bar_x0 - 3, bar_y - 3, bar_x1 + 3, bar_y + bar_h + 3), fill=(30, 26, 44))
        d.rectangle((bar_x0, bar_y, bar_x1, bar_y + bar_h), fill=(44, 39, 64))
        px = bar_x0 + (bar_x1 - bar_x0) * v
        g = 6
        k = 0
        while bar_x0 + (k + 1) * g <= px:
            t = k * g / (bar_x1 - bar_x0)
            col = tuple(int(a + (b - a) * t) for a, b in zip((82, 201, 142), (154, 130, 255)))
            light = tuple(min(255, int(c + (255 - c) * .35)) for c in col)
            bx = bar_x0 + k * g
            d.rectangle((bx, bar_y, bx + g - 2, bar_y + bar_h), fill=col)
            d.rectangle((bx, bar_y, bx + g - 2, bar_y + 3), fill=light)
            k += 1
        for fx, row, size in dust:
            dx = bar_x0 + (bar_x1 - bar_x0) * fx
            if dx > px + 18:
                y = bar_y - 6 - row * 6
                s = 3 * size
                d.rectangle((dx, y - s, dx + s, y), fill=(164, 158, 187))
        f = fr[i % len(fr)]
        left = int(px - 46 * 3)
        left = max(-18, left)
        top = bar_y - 3 - 42 * 3
        im.paste(f, (left, top), f)
        txt = f"{int(v * 100)}%"
        bx = left + 22 * 3
        by = top - 4
        w = 18 + 11 * len(txt)
        d.rectangle((bx - w / 2 - 3, by - 30, bx + w / 2 + 3, by + 3), fill=(55, 48, 163))
        d.rectangle((bx - w / 2, by - 27, bx + w / 2, by), fill=(99, 102, 241))
        d.text((bx, by - 13), txt, font=font("b", 16), fill=(255, 255, 255), anchor="mm")
        imgs.append(im)
    imgs[0].save(os.path.join(OUT, "sweep.gif"), save_all=True, append_images=imgs[1:], duration=70, loop=0,
                 optimize=True)


def sprite_sheet():
    names = ["idle", "sweep", "happy", "sad", "warn"]
    labels = ["Idle", "Working", "Done!", "Uh-oh", "Careful"]
    cell = 300
    im = Image.new("RGBA", (cell * 5, 330), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i, n in enumerate(names):
        fr, _ = frames(n, 5)
        f = fr[min(3, len(fr) - 1)]
        im.alpha_composite(f, (i * cell + (cell - f.width) // 2, 0))
        d.text((i * cell + cell / 2, 305), labels[i], font=font("b", 22), fill=(118, 112, 140), anchor="mm")
    im.save(os.path.join(OUT, "mossbit_moods.png"), optimize=True)
    # animated idle + happy gif for the README header
    seq = [f for f in frames("idle", 3)[0]] + frames("happy", 3)[0] * 2 + frames("idle", 3)[0]
    gif = []
    for f in seq:
        bg = Image.new("RGBA", f.size, (0, 0, 0, 0))
        bg.alpha_composite(f)
        gif.append(bg)
    gif[0].save(os.path.join(OUT, "mossbit.gif"), save_all=True, append_images=gif[1:], duration=110, loop=0,
                disposal=2, transparency=0)


if __name__ == "__main__":
    if "--compose-only" not in sys.argv:
        take_screenshots()
    for n in ["welcome", "home_light", "home_dark", "recover_pick", "recover_type", "recover_results",
              "wipe_strength", "wipe_confirm", "progress_dark", "health_result", "partitions_found", "shred_drop",
              "clone_dest", "guide_dark", "drives_light", "machines_dark", "jobs_light", "manage_light",
              "settings_light", "palette_dark"]:
        framed(n)
    hero()
    sweep_gif()
    sprite_sheet()
    roam_gif()
    if len(sys.argv) > 2 and os.path.isdir(sys.argv[-1]):
        link_shots(sys.argv[-1])
    print("docs images written to", OUT)
