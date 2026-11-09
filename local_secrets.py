"""Protect this application's local Telegram secrets; never log plaintext values."""
import base64
import ctypes
import os
from pathlib import Path

from library_store import DATA


class Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _windows(data, decrypt=False):
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "TG Workspace", None, None, None, 1, ctypes.byref(result))
    if not ok:
        raise RuntimeError("无法访问当前 Windows 用户的本地加密存储")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def _fernet():
    from cryptography.fernet import Fernet
    path = Path(DATA) / "telegram-secret.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    return Fernet(path.read_bytes())


def seal(value):
    data = value.encode("utf-8")
    if os.name == "nt":
        return "dpapi:" + base64.b64encode(_windows(data)).decode()
    return "fernet:" + _fernet().encrypt(data).decode()


def unseal(value):
    kind, data = value.split(":", 1)
    if kind == "dpapi" and os.name == "nt":
        return _windows(base64.b64decode(data), True).decode("utf-8")
    if kind == "fernet":
        return _fernet().decrypt(data.encode()).decode("utf-8")
    raise RuntimeError("此本地凭据无法在当前操作系统中解密")
