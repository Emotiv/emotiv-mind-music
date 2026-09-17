# PyInstaller spec for the macOS .app bundle and the Windows folder build.
#
#   pyinstaller packaging/EmotivMindMusic.spec --noconfirm
#
# Run it from the repository root — the paths below are relative to it.
import os
import sys

from PyInstaller.utils.hooks import collect_all

APP_NAME = "EMOTIV Mind Music"
# SPECPATH is injected by PyInstaller and points at packaging/; every path below
# is built from the repo root so the spec works from any working directory.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
IS_MAC = sys.platform == "darwin"


def at(*parts):
    return os.path.join(ROOT, *parts)


ICON_WIN = at("packaging", "app_icon.ico")
ICON_MAC = at("packaging", "app_icon.icns")

# pywebview loads its platform backend dynamically, so PyInstaller cannot see it
# by static analysis. collect_all pulls the platform modules and their deps
# (pyobjc on macOS, pythonnet/WebView2 on Windows).
webview_datas, webview_binaries, webview_hidden = collect_all("webview")

platform_hidden = (
    ["webview.platforms.cocoa"] if IS_MAC else ["webview.platforms.edgechromium", "clr"]
)

a = Analysis(
    [at("app.py")],
    pathex=[ROOT],
    binaries=webview_binaries,
    datas=webview_datas
    + [
        # The UI is read from disk at runtime; resource_dir() in app.py resolves
        # sys._MEIPASS so these land where it looks for them.
        (at("ui"), "ui"),
        # Cortex serves wss:// with a self-signed chain, so its root CA travels
        # with the app.
        (at("certificates"), "certificates"),
    ],
    hiddenimports=webview_hidden
    + platform_hidden
    + [
        "websockets",
        # The Spotify client is plain standard library, reached through the
        # engine; listed so a refactor that imports it lazily cannot drop it.
        "spotify.auth",
        "spotify.player",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Built from assets/logo.png by packaging/make_icon.py, which the build
    # workflow runs before PyInstaller. Not checked in -- derived artwork, and a
    # stale committed icon is worse than none -- so a checkout that has not run
    # it simply builds without one.
    icon=ICON_WIN if os.path.exists(ICON_WIN) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=ICON_MAC if os.path.exists(ICON_MAC) else None,
        bundle_identifier="com.emotiv.mindmusic",
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            # Cortex runs on localhost, and the Spotify sign-in listens on
            # 127.0.0.1 for the browser's redirect. Recent macOS versions treat
            # both as the local network and block them silently without this.
            "NSLocalNetworkUsageDescription": (
                "EMOTIV Mind Music connects to the EMOTIV Cortex service on this "
                "computer, and receives the Spotify sign-in from your browser."
            ),
        },
    )
