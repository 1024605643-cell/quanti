"""Windows-user-bound encrypted settings; never bundled with the application."""
import ctypes
import json
import os
from pathlib import Path
from ctypes import wintypes

APP_DIR = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'QuantiDesktop'


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def protect(data, decrypt=False):
    if os.name != 'nt':
        raise OSError('Encrypted desktop settings require Windows')
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(result.data)


def load_settings(directory=APP_DIR):
    path = directory / 'settings.dpapi'
    return json.loads(protect(path.read_bytes(), True)) if path.exists() else {}


def save_settings(settings, directory=APP_DIR):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'settings.dpapi'
    temp = path.with_suffix('.tmp')
    temp.write_bytes(protect(json.dumps(settings).encode()))
    temp.replace(path)
