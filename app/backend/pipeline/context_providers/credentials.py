"""Windows user-bound DPAPI. There is deliberately no plaintext fallback."""
import ctypes
import os
from ctypes import wintypes


class DPAPIProtector:
    def _convert(self, data, decrypt=False):
        if os.name != 'nt':
            raise RuntimeError('OS credential protection unavailable on this platform.')

        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        # CRYPTPROTECT_UI_FORBIDDEN. No machine scope, exported key or interactive UI.
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise RuntimeError('OS credential protection failed.')
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))

    def protect(self, value):
        return self._convert(value)

    def unprotect(self, value):
        return self._convert(value, decrypt=True)
