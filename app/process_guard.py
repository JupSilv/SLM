import ctypes
import os
from ctypes import wintypes


MAIN_MUTEX_NAME = r"Local\DeskubuddySLM.Main"
SUPERVISOR_MUTEX_NAME = r"Local\DeskubuddySLM.Supervisor"
ERROR_ALREADY_EXISTS = 183
ERROR_FILE_NOT_FOUND = 2
SYNCHRONIZE = 0x00100000


def _kernel32():
    if os.name != "nt":
        raise OSError("O supervisor de processo requer Windows.")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def acquire_named_mutex(name):
    kernel32 = _kernel32()
    kernel32.CreateMutexW.argtypes = (
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None, False
    return handle, True


def named_mutex_exists(name):
    kernel32 = _kernel32()
    kernel32.OpenMutexW.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, name)
    if not handle:
        error = ctypes.get_last_error()
        if error == ERROR_FILE_NOT_FOUND:
            return False
        raise ctypes.WinError(error)
    kernel32.CloseHandle(handle)
    return True


def release_named_mutex(handle):
    if not handle:
        return
    kernel32 = _kernel32()
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.ReleaseMutex(handle):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise ctypes.WinError(error)
    if not kernel32.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())
