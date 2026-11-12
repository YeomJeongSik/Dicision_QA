"""Store a Decisions key with Windows user-scoped DPAPI."""

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path
from uuid import uuid4


def key_file():
    if os.name != "nt":
        raise OSError("Windows credential storage unavailable")
    root = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    return root / "runs" / "decisions-key.dpapi"


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]


def convert(data, *, encrypt):
    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.addressof(buffer))
    result = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    operation = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    operation.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(DataBlob)]
    operation.restype = wintypes.BOOL
    local_free = ctypes.WinDLL("kernel32").LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    try:
        if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(result.data, result.size)
    finally:
        if not encrypt and result.data:
            ctypes.memset(result.data, 0, result.size)
        if result.data:
            local_free(result.data)
        ctypes.memset(buffer, 0, len(buffer))


def save_key(key):
    path = key_file()
    encrypted = convert(key.encode("ascii"), encrypt=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(encrypted)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_key():
    try:
        encrypted = key_file().read_bytes()
    except FileNotFoundError:
        legacy_root = os.environ.get("LOCALAPPDATA")
        if not legacy_root:
            return ""
        try:
            encrypted = (Path(legacy_root) / "UnrealAutoQA" / "decisions-key.dpapi").read_bytes()
        except FileNotFoundError:
            return ""
    try:
        return convert(encrypted, encrypt=False).decode("ascii")
    except UnicodeError:
        raise OSError("Invalid credential data") from None
