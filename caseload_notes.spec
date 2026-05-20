# PyInstaller spec for CaseloadNotes — one-folder distribution.
#
# Build with:
#     python build.py
#
# build.py runs PyInstaller against this spec, then copies the local
# Playwright Chromium install into the bundle so the packaged app
# doesn't need a separate browser download on the user's machine.
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# customtkinter ships theme JSON + assets it loads at runtime — make
# sure those tag along.
ctk_datas = collect_data_files("customtkinter")

a = Analysis(
    ["scripts/launcher.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("notes.yaml", "."),  # bundled default — seeded into user dir on first run
        *ctk_datas,
    ],
    hiddenimports=[
        "customtkinter",
        "darkdetect",
        "pynput.keyboard._win32",
        "pynput.mouse._win32",
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
