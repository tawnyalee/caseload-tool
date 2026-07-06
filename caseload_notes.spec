# PyInstaller spec for CaseloadNotes — one-folder distribution.
#
# Build with:
#     python build.py
#
# build.py runs PyInstaller against this spec, then copies the local
# Playwright Chromium install into the bundle so the packaged app
# doesn't need a separate browser download on the user's machine.
import os
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# Optional startup splash image + app icon. Included only if present, so the
# build works whether or not the artist files have been dropped in yet.
_res_datas = [
    (p, "resources") for p in ("resources/splash.png", "resources/app.ico")
    if os.path.exists(p)
]
_app_icon = "resources/app.ico" if os.path.exists("resources/app.ico") else None

# customtkinter ships theme JSON + assets it loads at runtime — make
# sure those tag along.
ctk_datas = collect_data_files("customtkinter")

# tzdata provides the IANA timezone database that zoneinfo needs on Windows
# (no system tz DB) — required by the texting schedule math. Its .zi data
# files aren't auto-detected, so collect them explicitly.
tzdata_datas = collect_data_files("tzdata")

a = Analysis(
    ["scripts/launcher.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("default_scenarios.yaml", "."),  # bundled sample — seeded into user dir on first run
        ("default_email_templates", "default_email_templates"),  # sample email templates
        *_res_datas,       # splash.gif / app.ico when present
        *ctk_datas,
        *tzdata_datas,
    ],
    hiddenimports=[
        "customtkinter",
        "darkdetect",
        "pynput.keyboard._win32",
        "pynput.mouse._win32",
        "tzdata",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CaseloadNotes",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI app — no console window
    icon=_app_icon,  # embedded app icon (desktop shortcut / taskbar); None if absent
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="CaseloadNotes",
)
