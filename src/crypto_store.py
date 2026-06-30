"""Local data-at-rest encryption for the app's student-data files.

Goal: keep the student-bearing local files (history.db, success_path.db,
note_log.csv, caseload.csv) UNREADABLE on disk when the app isn't running,
unlocked by an app password — without adding a heavyweight native build
dependency and without touching the data-access code (history/success_path/
mongoose_contacts keep reading plain file paths).

Design
------
- Crypto is STDLIB-ONLY (no `cryptography`/`pycryptodome`): a password is
  stretched with ``hashlib.scrypt`` into a 32-byte master key; files are
  encrypted with an HMAC-SHA256 keystream (counter mode) and authenticated
  encrypt-then-MAC with a separate HMAC-SHA256 tag. This is a standard,
  well-understood construction (HMAC-SHA256 as the PRF in CTR + a MAC), not a
  home-grown cipher. Adequate for local file-at-rest protection.
- Lifecycle (owned by the caller): on unlock, each ``X.enc`` is decrypted to
  its plain path ``X``; the app runs against plaintext; on exit each ``X`` is
  re-encrypted to ``X.enc`` and the plaintext shredded. Crash-safe: a plaintext
  newer than its ``.enc`` is kept (it has the latest data) rather than clobbered.
- "Remember on this machine": the master key is sealed with Windows DPAPI
  (tied to the Windows account) and gated on the BOOT SESSION + an age cap, so
  the app can auto-unlock within a session but the sealed key is useless on any
  other machine/account and re-prompts after a reboot (or N days).

Threat model: protects data at rest if the laptop is lost/stolen or the files
are copied off the machine. It does NOT protect against someone already using
your unlocked Windows session while the app is running (plaintext is live then).
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import struct
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional

MAGIC = b"SFEN"          # SalesForce ENcrypted — file/blob header
VERSION = 1
_HEADER_LEN = 4 + 1 + 16 + 32   # magic + version + nonce + tag
# scrypt cost. n=2**15 (~32k) keeps unlock well under a second on a laptop
# while making offline guessing expensive. maxmem sized for n*r*128 headroom.
_KDF = {"n": 1 << 15, "r": 8, "p": 1}
_MAXMEM = 256 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Core cipher (stdlib only)
# --------------------------------------------------------------------------- #
def derive_master(password: str, salt: bytes, kdf: Optional[dict] = None) -> bytes:
    """Stretch `password` + `salt` into a 32-byte master key via scrypt."""
    k = kdf or _KDF
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=k["n"], r=k["r"], p=k["p"], dklen=32, maxmem=_MAXMEM,
    )


def _subkeys(master: bytes) -> tuple[bytes, bytes]:
    """Derive independent encryption + MAC subkeys from the master key."""
    enc = hmac.new(master, b"enc-v1", hashlib.sha256).digest()
    mac = hmac.new(master, b"mac-v1", hashlib.sha256).digest()
    return enc, mac


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    """HMAC-SHA256 counter-mode keystream of length `n`."""
    out = bytearray()
    ctr = 0
    while len(out) < n:
        out += hmac.new(
            enc_key, nonce + struct.pack(">Q", ctr), hashlib.sha256
        ).digest()
        ctr += 1
    return bytes(out[:n])


def _xor(data: bytes, ks: bytes) -> bytes:
    """Fast big-integer XOR (C-level) so multi-MB DBs aren't a Python byte loop."""
    if not data:
        return b""
    return (int.from_bytes(data, "big") ^ int.from_bytes(ks, "big")).to_bytes(
        len(data), "big")


def encrypt(master: bytes, plaintext: bytes) -> bytes:
    """Encrypt-then-MAC. Returns magic|ver|nonce(16)|tag(32)|ciphertext."""
    enc_key, mac_key = _subkeys(master)
    nonce = os.urandom(16)
    ct = _xor(plaintext, _keystream(enc_key, nonce, len(plaintext)))
    head = MAGIC + bytes([VERSION]) + nonce
    tag = hmac.new(mac_key, head + ct, hashlib.sha256).digest()
    return head + tag + ct


def decrypt(master: bytes, blob: bytes) -> bytes:
    """Verify the MAC (constant-time) then decrypt. Raises ValueError on a
    wrong key or any tampering/corruption — callers must treat that as fatal
    (do NOT fall back to deleting the file)."""
    if len(blob) < _HEADER_LEN or blob[:4] != MAGIC:
        raise ValueError("not an encrypted blob")
    ver = blob[4]
    nonce = blob[5:21]
    tag = blob[21:53]
    ct = blob[53:]
    enc_key, mac_key = _subkeys(master)
    head = MAGIC + bytes([ver]) + nonce
    expect = hmac.new(mac_key, head + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expect):
        raise ValueError("authentication failed — wrong password or corrupt file")
    return _xor(ct, _keystream(enc_key, nonce, len(ct)))


def is_encrypted(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == MAGIC


# --------------------------------------------------------------------------- #
# Windows DPAPI (dependency-free via ctypes) — seals the key to this account
# --------------------------------------------------------------------------- #
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _make_blob(data: bytes):
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf   # keep buf alive alongside blob


def _dpapi(fn_name: str, data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in, _buf = _make_blob(data)
    blob_out = _DATA_BLOB()
    fn = getattr(crypt32, fn_name)
    # CryptProtectData/CryptUnprotectData(pDataIn, name, optEntropy, reserved,
    #   promptStruct, flags, pDataOut)  — flags 0; entropy/name None.
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"{fn_name} failed (err={ctypes.get_last_error()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def dpapi_seal(data: bytes) -> bytes:
    return _dpapi("CryptProtectData", data)


def dpapi_unseal(data: bytes) -> bytes:
    return _dpapi("CryptUnprotectData", data)


def boot_id() -> int:
    """A stable id for the current boot session: the (rounded) epoch the machine
    booted, derived from uptime. Changes on every restart; constant within a
    session. 0 if it can't be read (then per-restart gating just falls back to
    the age cap)."""
    try:
        k = ctypes.windll.kernel32
        k.GetTickCount64.restype = ctypes.c_ulonglong
        uptime_s = k.GetTickCount64() / 1000.0
        return int((time.time() - uptime_s) // 5 * 5)   # nearest 5s
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# Vault: password/key management + the on-disk vault.json
# --------------------------------------------------------------------------- #
_VERIFIER_PLAINTEXT = b"salesforce-automation-vault-v1"
_REMEMBER_MAX_AGE_S = 7 * 24 * 3600   # weekly backstop for "remember" modes


class DataVault:
    """Owns vault.json (salt, password verifier, optional DPAPI-sealed key) and
    the unlocked master key. Does NOT know about the managed data files — the
    caller drives decrypt-on-unlock / encrypt-on-exit using `encrypt_file` /
    `decrypt_file`."""

    def __init__(self, vault_path: Path):
        self.path = Path(vault_path)
        self._master: Optional[bytes] = None
        self._meta: dict = {}
        if self.path.exists():
            try:
                self._meta = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._meta = {}

    # -- state ----------------------------------------------------------------
    @property
    def is_setup(self) -> bool:
        return bool(self._meta.get("salt") and self._meta.get("verifier"))

    @property
    def is_unlocked(self) -> bool:
        return self._master is not None

    # -- helpers --------------------------------------------------------------
    def _kdf(self) -> dict:
        return self._meta.get("kdf") or _KDF

    def _write_meta(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._meta, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _verify(self, master: bytes) -> bool:
        try:
            blob = bytes.fromhex(self._meta["verifier"])
            return decrypt(master, blob) == _VERIFIER_PLAINTEXT
        except Exception:
            return False

    # -- setup / unlock -------------------------------------------------------
    def setup(self, password: str) -> None:
        """First-time setup: pick a salt, store a verifier, unlock in-process."""
        salt = os.urandom(16)
        master = derive_master(password, salt, _KDF)
        self._meta = {
            "version": 1,
            "salt": salt.hex(),
            "kdf": _KDF,
            "verifier": encrypt(master, _VERIFIER_PLAINTEXT).hex(),
        }
        self._master = master
        self._write_meta()

    def unlock(self, password: str) -> bool:
        """Unlock with a typed password. Returns True on success."""
        if not self.is_setup:
            return False
        salt = bytes.fromhex(self._meta["salt"])
        master = derive_master(password, salt, self._kdf())
        if not self._verify(master):
            return False
        self._master = master
        return True

    def try_auto_unlock(self, *, allow_remember: bool) -> bool:
        """Unlock WITHOUT a password using the DPAPI-sealed key, if remembering
        is allowed AND the seal is still valid for this boot session / age.
        Returns True on success."""
        if not allow_remember or not self.is_setup:
            return False
        rem = self._meta.get("remember") or {}
        sealed = rem.get("sealed_key")
        if not sealed:
            return False
        if rem.get("boot_id") != boot_id():
            return False
        if (time.time() - float(rem.get("unlocked_at", 0))) > _REMEMBER_MAX_AGE_S:
            return False
        try:
            master = dpapi_unseal(base64.b64decode(sealed))
        except Exception:
            return False
        if not self._verify(master):
            return False
        self._master = master
        return True

    def remember_on_this_machine(self) -> None:
        """Seal the unlocked key for this boot session (call after a successful
        unlock when the mode allows remembering)."""
        if self._master is None:
            return
        try:
            sealed = base64.b64encode(dpapi_seal(self._master)).decode("ascii")
        except Exception:
            return
        self._meta["remember"] = {
            "sealed_key": sealed,
            "boot_id": boot_id(),
            "unlocked_at": time.time(),
        }
        self._write_meta()

    def forget(self) -> None:
        """Drop any DPAPI-sealed key so the next launch must prompt."""
        if self._meta.pop("remember", None) is not None:
            self._write_meta()

    def change_password(self, new_password: str) -> None:
        """Re-key the verifier under a new password (only valid while unlocked).
        NOTE: the master key itself does not change identity for the caller —
        but the files are re-encrypted by the caller on the next exit with the
        new master, so re-derive + re-encrypt the verifier here and adopt the
        new master."""
        salt = os.urandom(16)
        master = derive_master(new_password, salt, _KDF)
        self._meta["salt"] = salt.hex()
        self._meta["kdf"] = _KDF
        self._meta["verifier"] = encrypt(master, _VERIFIER_PLAINTEXT).hex()
        self._meta.pop("remember", None)   # force re-remember under the new key
        self._master = master
        self._write_meta()

    # -- file encryption ------------------------------------------------------
    def encrypt_file(self, plain: Path, enc: Path) -> None:
        """Encrypt `plain` → `enc` atomically (requires an unlocked vault)."""
        if self._master is None:
            raise RuntimeError("vault is locked")
        data = Path(plain).read_bytes()
        blob = encrypt(self._master, data)
        tmp = Path(enc).with_suffix(Path(enc).suffix + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, enc)

    def decrypt_file(self, enc: Path, plain: Path) -> None:
        """Decrypt `enc` → `plain` atomically (requires an unlocked vault).
        Raises on a bad MAC — caller must NOT delete anything on failure."""
        if self._master is None:
            raise RuntimeError("vault is locked")
        blob = Path(enc).read_bytes()
        data = decrypt(self._master, blob)
        tmp = Path(plain).with_suffix(Path(plain).suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, plain)

    def decrypt_bytes_of(self, enc: Path) -> bytes:
        """Decrypt `enc` and return the plaintext bytes (no file written)."""
        if self._master is None:
            raise RuntimeError("vault is locked")
        return decrypt(self._master, Path(enc).read_bytes())


ENC_SUFFIX = ".enc"


def _shred(path: Path) -> None:
    """Best-effort overwrite + delete of a plaintext file. (On SSDs overwrite
    isn't a guaranteed erase, but it beats a bare unlink; the real protection
    is that an encrypted copy already exists.)"""
    try:
        n = path.stat().st_size
        with open(path, "r+b", buffering=0) as f:
            f.write(os.urandom(min(n, 1 << 20)))
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass
    try:
        path.unlink()
    except Exception:
        pass


class ManagedFiles:
    """Drives the decrypt-on-unlock / re-encrypt-on-exit lifecycle for a fixed
    set of plaintext data files, each paired with a sibling ``<name>.enc``.

    Crash-safe + data-loss-averse by construction:
    - decrypt_all only writes plaintext when the encrypted copy is newer-or-
      equal; a plaintext left over from a crash (newer than its .enc) is KEPT.
    - lock_all re-encrypts then VERIFIES the .enc decrypts back to identical
      bytes BEFORE shredding the plaintext. A file whose verify fails is left
      in plaintext (and reported) rather than lost.
    """

    def __init__(self, vault: DataVault, plain_paths):
        self.vault = vault
        self.files = [Path(p) for p in plain_paths]

    @staticmethod
    def enc_path(plain: Path) -> Path:
        return Path(str(plain) + ENC_SUFFIX)

    def has_encrypted(self) -> bool:
        return any(self.enc_path(p).exists() for p in self.files)

    def decrypt_all(self) -> list[str]:
        """Decrypt each .enc → plaintext for the session. Returns a list of
        human-readable error strings (empty when all good)."""
        errors: list[str] = []
        for plain in self.files:
            enc = self.enc_path(plain)
            if not enc.exists():
                continue   # not yet encrypted (pre-migration) — leave plaintext
            try:
                if plain.exists():
                    if enc.stat().st_mtime >= plain.stat().st_mtime:
                        self.vault.decrypt_file(enc, plain)
                    # else: plaintext is newer (crash leftover) — keep it
                else:
                    self.vault.decrypt_file(enc, plain)
            except Exception as e:
                errors.append(f"{plain.name}: {e}")
        return errors

    def lock_all(self) -> list[str]:
        """Re-encrypt each existing plaintext → .enc, verify, then shred the
        plaintext. Returns error strings for any file that could NOT be safely
        encrypted (its plaintext is deliberately left in place)."""
        errors: list[str] = []
        for plain in self.files:
            if not plain.exists():
                continue
            enc = self.enc_path(plain)
            try:
                original = plain.read_bytes()
                self.vault.encrypt_file(plain, enc)
                if self.vault.decrypt_bytes_of(enc) != original:
                    errors.append(f"{plain.name}: verify mismatch — kept plaintext")
                    continue
                _shred(plain)
            except Exception as e:
                errors.append(f"{plain.name}: {e} — kept plaintext")
        return errors

    def migrate_existing(self) -> list[str]:
        """First-time: encrypt any plaintext that has no .enc yet, verifying the
        round-trip. Plaintext is LEFT in place (in use this session; shredded on
        exit). Returns errors; a file that fails verify keeps no .enc."""
        errors: list[str] = []
        for plain in self.files:
            enc = self.enc_path(plain)
            if not plain.exists() or enc.exists():
                continue
            try:
                original = plain.read_bytes()
                self.vault.encrypt_file(plain, enc)
                if self.vault.decrypt_bytes_of(enc) != original:
                    enc.unlink(missing_ok=True)
                    errors.append(f"{plain.name}: verify mismatch — not encrypted")
            except Exception as e:
                errors.append(f"{plain.name}: {e}")
        return errors
