"""进程锁：内核自动释放，避免残留 PID 和同时认证。"""

import os
import tempfile


class ProcessLock:
    def __init__(self, name):
        self.name = name
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return True
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
            api.CreateMutexW.restype = wintypes.HANDLE
            api.CloseHandle.argtypes = (wintypes.HANDLE,)
            api.CloseHandle.restype = wintypes.BOOL
            handle = api.CreateMutexW(None, False, "Local\\" + self.name)
            error = ctypes.get_last_error()
            if not handle:
                raise ctypes.WinError(error)
            if error == 183:
                api.CloseHandle(handle)
                return False
            self.handle = handle
            self.api = api
            return True
        import fcntl
        fd = os.open(os.path.join(tempfile.gettempdir(), self.name + ".lock"),
                     os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.handle = fd
        return True

    def release(self):
        if self.handle is None:
            return
        if os.name == "nt":
            self.api.CloseHandle(self.handle)
        else:
            os.close(self.handle)
        self.handle = None
