"""Read the user's Outlook signature HTML directly from disk.

Outlook stores user signatures as `.htm` files in
`%APPDATA%\\Microsoft\\Signatures\\<name>.htm`. The default name for
new mail is recorded in
`HKCU\\Software\\Microsoft\\Office\\<ver>\\Common\\MailSettings\\NewSignature`.

We use this path for **auto-sent** batch emails because the
GetInspector signature-capture we use in interactive Display() mode
leaves the MailItem in a state that programmatic `Send()` refuses
(HRESULT 0x80070057 "parameter is incorrect"). For Display mode the
existing GetInspector trick is kept since it produces a higher-
fidelity result (Outlook attaches signature images automatically).

KNOWN LIMITATION: signature `.htm` files reference embedded images
by relative path (`<img src="<name>_files/image001.png">`). When we
paste this HTML into a new mail, those paths point nowhere — images
will appear broken in the recipient's view. Text + link signatures
work fine. To fix images we'd need to either base64-embed them or
attach with CIDs (similar to inline_images in compose_email). Not
done in v1; flagged for follow-up if users hit it.

Windows-only. All Windows-specific bits are lazy-imported so this
module loads cleanly on Mac/Linux (just returns None there).
"""
import os
import re
import sys
from pathlib import Path
from typing import Optional


# Cached per process. Clear by restarting the launcher.
_signature_cache: dict[str, Optional[str]] = {}


_BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.IGNORECASE | re.DOTALL)


def _signatures_dir() -> Optional[Path]:
    """Outlook signature folder under the user's roaming AppData."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    p = Path(appdata) / "Microsoft" / "Signatures"
    return p if p.exists() else None


def _read_signature_name_from_registry() -> Optional[str]:
    """Look up the user's default new-mail signature name in the
    HKCU registry. Tries Office 16.0 (modern) then 15.0 (legacy)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for ver in ("16.0", "15.0"):
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                rf"Software\Microsoft\Office\{ver}\Common\MailSettings",
            ) as key:
                val, _ = winreg.QueryValueEx(key, "NewSignature")
                if val:
                    return str(val).strip()
        except (OSError, FileNotFoundError):
            continue
    return None


def _read_htm(path: Path) -> str:
    """Outlook signature files are typically Windows-1252 or UTF-8
    with BOM. Try a few encodings, then fall back to latin-1 which
    never errors."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_bytes().decode("latin-1", errors="replace")


def _extract_body(html: str) -> str:
    """Pull `<body>...</body>` content out of an Outlook signature
    HTML file. Falls back to the whole document if no body tag is
    found (some signatures are body-only fragments already)."""
    m = _BODY_RE.search(html)
    if m:
        return m.group(1)
    return html


def find_default_signature_html(name_override: str = "") -> Optional[str]:
    """Return the HTML body of the user's default Outlook signature,
    or None if no signature can be located or read.

    Args:
        name_override: signature filename stem without `.htm`. When
            non-empty, skips auto-detect.

    Lookup order:
        1. `<name_override>.htm` if name_override is given.
        2. Registry's `NewSignature` value (Office 16.0 then 15.0).
        3. If exactly one `.htm` exists in the signatures folder,
           use it (common case for users with a single signature).
        4. None.

    Cached per-process keyed by (name_override or ""). Restart the
    launcher to pick up signature changes.
    """
    cache_key = name_override or ""
    if cache_key in _signature_cache:
        return _signature_cache[cache_key]

    result: Optional[str] = None
    sig_dir = _signatures_dir()
    if sig_dir is not None:
        name = name_override or _read_signature_name_from_registry()
        candidate: Optional[Path] = None
        if name:
            candidate = sig_dir / f"{name}.htm"
            if not candidate.exists():
                candidate = None
        if candidate is None and not name_override:
            htms = sorted(sig_dir.glob("*.htm"))
            if len(htms) == 1:
                candidate = htms[0]
        if candidate is not None:
            try:
                raw = _read_htm(candidate)
                result = _extract_body(raw)
            except Exception:
                result = None

    _signature_cache[cache_key] = result
    return result


def list_signature_names() -> list[str]:
    """List names (filename stem, no extension) of every .htm in the
    user's Outlook Signatures folder. Sorted alphabetically. Returns
    empty list if the folder doesn't exist or isn't readable. Used
    by the eventual editor-UI signature picker."""
    sig_dir = _signatures_dir()
    if sig_dir is None:
        return []
    try:
        return sorted(p.stem for p in sig_dir.glob("*.htm"))
    except Exception:
        return []
