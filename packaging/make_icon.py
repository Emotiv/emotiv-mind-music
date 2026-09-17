"""Build the application icons from assets/logo.png.

    python packaging/make_icon.py

Writes, next to this file:

    app_icon.ico    Windows: taskbar, Explorer, the installer wizard, and the
                    icon PyInstaller stamps into the .exe. pywebview's window
                    takes its icon from the executable.
    app_icon.icns   macOS: Dock and Finder. Built with iconutil, so this one
                    only appears when run on macOS.

Kept as a script rather than checked-in binaries nobody can regenerate: when
the logo changes, this is the one command that brings every icon along with it.
The build workflow runs it before PyInstaller.

A missing logo is not an error. The build then ships with PyInstaller's default
icon, which is worse than a real one but better than no installer at all.

What the source artwork cannot do on its own, and this script does for it:

  * An icon is square and its artwork may not be. The mark is trimmed to its
    own ink and centred, so padding is measured from what is actually drawn.
  * A transparent logo can vanish against the taskbar behind it. PLATE puts it
    on the app's own dark rounded square; set it to None for artwork that is
    already a finished tile.
  * macOS and Windows want different shapes. Windows icons run to the edge of
    their square; a macOS icon is a rounded rectangle occupying 824 of 1024
    points, and drawing it full-bleed makes the app loom over its neighbours in
    the Dock.
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
SOURCE = os.path.join(ROOT, "assets", "logo.png")

# The interface background, #0c0e12.
PLATE = (12, 14, 18, 255)

# How much of the plate the artwork may fill.
PADDING = 0.80

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

ICNS_SIZES = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]

# macOS Human Interface Guidelines: the rounded rectangle is 824pt in a 1024pt
# canvas with a 185.4pt corner radius. Fractions, so they hold at every size.
MAC_INSET = (1024 - 824) / 2 / 1024
MAC_RADIUS = 185.4 / 824
WIN_RADIUS = 0.18


def load():
    from PIL import Image
    art = Image.open(SOURCE).convert("RGBA")
    bbox = art.split()[3].getbbox()
    return art.crop(bbox) if bbox else art


def render(art, size: int, mac: bool):
    from PIL import Image, ImageDraw
    inset = round(size * MAC_INSET) if mac else 0
    plate_size = size - 2 * inset

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if PLATE:
        radius = max(2, round(plate_size * (MAC_RADIUS if mac else WIN_RADIUS)))
        ImageDraw.Draw(canvas).rounded_rectangle(
            [inset, inset, size - 1 - inset, size - 1 - inset], radius=radius, fill=PLATE)

    scaled = art.copy()
    box = round(plate_size * (PADDING if PLATE else 1.0))
    scaled.thumbnail((box, box), Image.LANCZOS)
    canvas.alpha_composite(scaled, ((size - scaled.width) // 2, (size - scaled.height) // 2))
    return canvas


def write_ico(art):
    target = os.path.join(HERE, "app_icon.ico")
    frames = [render(art, s, mac=False) for s in ICO_SIZES]
    frames[-1].save(target, format="ICO", sizes=[(f.width, f.height) for f in frames],
                    append_images=frames[:-1])
    print(f"wrote {target} ({', '.join(str(s) for s in ICO_SIZES)})")


def write_icns(art):
    if sys.platform != "darwin":
        print("not macOS — skipping app_icon.icns")
        return
    if not shutil.which("iconutil"):
        print("iconutil not found — skipping app_icon.icns", file=sys.stderr)
        return
    iconset = os.path.join(HERE, "app_icon.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset)
    for name, size in ICNS_SIZES:
        render(art, size, mac=True).save(os.path.join(iconset, name))
    target = os.path.join(HERE, "app_icon.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", target], check=True)
    shutil.rmtree(iconset, ignore_errors=True)
    print(f"wrote {target}")


def main() -> int:
    if not os.path.exists(SOURCE):
        print(f"no {SOURCE} — building without a custom icon")
        return 0
    art = load()
    write_ico(art)
    write_icns(art)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
