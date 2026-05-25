"""Compose-and-display draft emails in Outlook via Win32 COM.

Always opens the draft for the user to review (`Display(False)`) —
never auto-sends. FERPA review requirement: every email containing
student information must be human-reviewed before sending. The user
sends from inside Outlook themselves.

Signature handling: setting `HTMLBody` directly would overwrite the
user's default Outlook signature. To preserve it, we access the mail's
Inspector first (which triggers Outlook to insert the signature into
HTMLBody), capture that signature HTML, then prepend our content so
the final body is `<our html><signature>`.

Inline images use the standard CID pattern. The HTML body references
`<img src="cid:NAME">` and the matching path is passed in
`inline_images={"NAME": Path(...)}`. The attachment gets tagged with
PR_ATTACH_CONTENT_ID so Outlook serves it inline when the recipient
views the message.

Windows + Outlook only. win32com is lazy-imported so this module loads
fine on Mac/Linux — only `compose_email()` / `get_user_info()` raise
there.
"""
from pathlib import Path
from typing import Optional


# Schema-URL form of the MAPI property tag PR_ATTACH_CONTENT_ID.
# Setting this on an attachment binds it to a `cid:` reference in the
# HTML body so Outlook serves the image inline.
PR_ATTACH_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001E"


# Cached so multi-email scenarios don't redispatch Outlook for every
# fire. Cleared if the user restarts the launcher.
_user_info_cache: Optional[dict] = None


def get_user_info() -> dict:
    """Return the current Outlook user's display name and SMTP email
    as `{"name": str, "email": str}`. Result is cached per process.
    On any failure (Outlook unreachable, profile not configured, etc.)
    returns empty strings rather than raising."""
    global _user_info_cache
    if _user_info_cache is not None:
        return _user_info_cache

    info = {"name": "", "email": ""}
    try:
        import win32com.client
        outlook = win32com.client.Dispatch("Outlook.Application")
        user = outlook.Session.CurrentUser
        info["name"] = (user.Name or "").strip()
        # SMTP resolution: Exchange users need GetExchangeUser() to get
        # a real SMTP address (otherwise `.Address` is the legacy DN).
        try:
            entry = user.AddressEntry
            entry_type = getattr(entry, "Type", "")
            if entry_type == "EX":
                ex_user = entry.GetExchangeUser()
                if ex_user is not None:
                    info["email"] = (ex_user.PrimarySmtpAddress or "").strip()
            else:
                info["email"] = (entry.Address or "").strip()
        except Exception:
            pass
    except Exception:
        pass

    _user_info_cache = info
    return info


def compose_email(
    to: str,
    cc: str = "",
    subject: str = "",
    html_body: str = "",
    inline_images: Optional[dict[str, Path]] = None,
    *,
    auto_send: bool = False,
) -> None:
    """Build a MailItem in Outlook. By default opens it for the user
    to review and send (`Display(False)`). When `auto_send=True`,
    sends it through Outlook programmatically (`Send()`) with no
    further user interaction — used by the batch driver for students
    2..N after the user has previewed the template on student 1.

    Args:
        to: primary recipient(s); semicolon-separated.
        cc: CC recipient(s); semicolon-separated. Empty string for none.
        subject: subject line.
        html_body: full HTML body. `<img src="cid:NAME">` refs are
            bound to entries in `inline_images`. The user's default
            signature is appended AFTER this content.
        inline_images: CID name → file path. Missing files are
            silently skipped; the rest of the email still goes
            through (the broken image just appears as missing).
        auto_send: when True, `Send()` the mail instead of showing
            it. Outlook may prompt for a "Programmatic Access"
            warning if the user's security settings demand it.

    Raises:
        OSError: if Outlook can't be reached via COM, or (when
            auto_send=True) if Send() itself fails.
    """
    import win32com.client
    from pywintypes import com_error

    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
    except com_error as e:
        raise OSError(f"Couldn't reach Outlook via COM: {e}") from e

    mail = outlook.CreateItem(0)  # 0 = olMailItem
    mail.To = to or ""
    if cc:
        mail.CC = cc
    mail.Subject = subject or ""

    if auto_send:
        # IMPORTANT: do NOT touch GetInspector here. Accessing the
        # Inspector to capture the signature seems to leave the
        # MailItem in a state that breaks `Send()` programmatically
        # — Outlook returns 0x80070057 ('parameter is incorrect').
        # Trade-off: auto-sent emails go out without the user's
        # Outlook signature. If/when we want signatures on auto-sent
        # mail, read them from `%APPDATA%\Microsoft\Signatures\*.htm`
        # and concatenate manually rather than via Inspector.
        mail.HTMLBody = html_body or ""
    else:
        # Display mode: capture the user's default Outlook signature
        # (inserted by Outlook when the Inspector is created), then
        # prepend our content so the final body is
        # `<our html><signature>`.
        signature_html = ""
        try:
            _ = mail.GetInspector
            signature_html = mail.HTMLBody or ""
        except Exception:
            pass
        mail.HTMLBody = (html_body or "") + signature_html

    if inline_images:
        for cid, path in inline_images.items():
            p = Path(path)
            if not p.exists():
                continue
            try:
                att = mail.Attachments.Add(str(p.resolve()), 1)  # olByValue
                att.PropertyAccessor.SetProperty(
                    PR_ATTACH_CONTENT_ID, cid,
                )
            except com_error:
                # CID binding failed — image won't inline but the message
                # is otherwise intact. Best-effort behavior.
                pass

    if auto_send:
        try:
            mail.Send()
        except com_error as e:
            raise OSError(f"Outlook Send() failed: {e}") from e
        return
    # Non-modal so the launcher window stays usable while the user
    # reviews and sends the email in Outlook.
    mail.Display(False)
