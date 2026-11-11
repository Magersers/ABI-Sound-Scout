"""Keep one shortcut listener per interactive Windows session."""
import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class SingleInstance:
    def __init__(self, name=r'Local\ABI_Sound_Scout_Desktop'):
        ctypes.set_last_error(0)
        self.handle = kernel32.CreateMutexW(None, False, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.already_running = ctypes.get_last_error() == 183

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None
