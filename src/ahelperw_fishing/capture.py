"""Screen capture of exact rectangles; every returned image owns its pixel data.

Backends: 'gdi' (BitBlt of exactly the requested rectangle, Windows only; the memory DC,
bitmap and pixel buffer are reused while the size stays the same, so a 30 fps loop does
not allocate GDI objects per frame), 'mss' (if installed) and 'pillow' (ImageGrab; on
multi-monitor systems it can copy the whole virtual desktop, ~80-120 ms per frame at 4K,
so it is the last fallback).
"""

import sys
import time

from PIL import Image, ImageGrab

if sys.platform == 'win32':
    import ctypes as ct
    from ctypes import wintypes as wt

    _G = ct.WinDLL('gdi32', use_last_error=True)
    _U = ct.WinDLL('user32', use_last_error=True)
    _U.GetDC.argtypes = [wt.HWND]
    _U.GetDC.restype = wt.HDC
    _U.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    _G.CreateCompatibleDC.argtypes = [wt.HDC]
    _G.CreateCompatibleDC.restype = wt.HDC
    _G.CreateCompatibleBitmap.argtypes = [wt.HDC, ct.c_int, ct.c_int]
    _G.CreateCompatibleBitmap.restype = wt.HBITMAP
    _G.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    _G.SelectObject.restype = wt.HGDIOBJ
    _G.BitBlt.argtypes = [wt.HDC, ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.HDC, ct.c_int, ct.c_int, wt.DWORD]
    _G.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ct.c_void_p, ct.c_void_p, wt.UINT]
    _G.DeleteObject.argtypes = [wt.HGDIOBJ]
    _G.DeleteDC.argtypes = [wt.HDC]

    class _BitmapInfo(ct.Structure):
        _fields_ = [
            ('size', wt.DWORD),
            ('width', wt.LONG),
            ('height', wt.LONG),
            ('planes', wt.WORD),
            ('bits', wt.WORD),
            ('compression', wt.DWORD),
            ('image_size', wt.DWORD),
            ('xppm', wt.LONG),
            ('yppm', wt.LONG),
            ('used', wt.DWORD),
            ('important', wt.DWORD),
        ]


_SRCCOPY_CAPTUREBLT = 0x00CC0020 | 0x40000000  # CAPTUREBLT keeps layered windows; the game is a normal one


class GdiGrabber:
    """BitBlt grabber that keeps one memory DC/bitmap for the last requested size."""

    def __init__(self):
        self._size = None
        self._memory = self._bitmap = self._old = None
        self._buffer = self._info = None

    def _prepare(self, screen, width, height):
        if self._size == (width, height):
            return
        self.close()
        memory = _G.CreateCompatibleDC(screen)
        bitmap = _G.CreateCompatibleBitmap(screen, width, height) if memory else None
        if not memory or not bitmap:
            if memory:
                _G.DeleteDC(memory)
            raise OSError('Не удалось создать буфер захвата')
        self._memory, self._bitmap = memory, bitmap
        self._old = _G.SelectObject(memory, bitmap)
        self._info = _BitmapInfo(ct.sizeof(_BitmapInfo), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        self._buffer = ct.create_string_buffer(width * height * 4)
        self._size = (width, height)

    def grab(self, bounds):
        left, top, right, bottom = bounds
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise ValueError('Пустая область захвата')
        screen = _U.GetDC(None)
        if not screen:
            raise OSError('GetDC failed')
        try:
            self._prepare(screen, width, height)
            if not _G.BitBlt(self._memory, 0, 0, width, height, screen, left, top, _SRCCOPY_CAPTUREBLT):
                raise OSError('BitBlt failed')
            if _G.GetDIBits(self._memory, self._bitmap, 0, height, self._buffer, ct.byref(self._info), 0) != height:
                raise OSError('GetDIBits failed')
            # copy(): the buffer is reused by the next grab.
            return Image.frombuffer('RGB', (width, height), self._buffer, 'raw', 'BGRX', 0, 1).copy()
        finally:
            _U.ReleaseDC(None, screen)

    def close(self):
        if self._memory:
            if self._old:
                _G.SelectObject(self._memory, self._old)
            if self._bitmap:
                _G.DeleteObject(self._bitmap)
            _G.DeleteDC(self._memory)
        self._size = None
        self._memory = self._bitmap = self._old = None
        self._buffer = self._info = None


def gdi_grab(bounds):
    """One-off GDI capture (allocates and frees its own buffers)."""
    grabber = GdiGrabber()
    try:
        return grabber.grab(bounds)
    finally:
        grabber.close()


class HudCapture:
    """Captures screen rectangles with the fastest backend that works, falling back once."""

    def __init__(self, preferred='gdi'):
        if preferred not in ('gdi', 'mss', 'pillow'):
            raise ValueError('captureBackend: gdi, mss или pillow')
        if preferred == 'gdi' and sys.platform != 'win32':
            preferred = 'pillow'
        self.preferred = preferred
        self.backend = preferred
        self.session = None
        self.fallback_error = None
        self.last_ms = None
        # One grabber per rectangle size in use (HUD strip, water zone, full window).
        self._grabbers = {}

    def grab(self, bounds):
        started = time.perf_counter()
        try:
            return self._grab(bounds)
        finally:
            self.last_ms = (time.perf_counter() - started) * 1000

    def _gdi(self, bounds):
        size = (bounds[2] - bounds[0], bounds[3] - bounds[1])
        grabber = self._grabbers.get(size)
        if grabber is None:
            if len(self._grabbers) >= 4:  # window/zone changed: drop old buffers
                self._close_grabbers()
            grabber = self._grabbers[size] = GdiGrabber()
        return grabber.grab(bounds)

    def _grab(self, bounds):
        if self.backend == 'gdi':
            try:
                return self._gdi(bounds)
            except ValueError:
                raise
            except Exception as exc:
                self.fallback_error = str(exc)
                self._close_grabbers()
                self.backend = 'mss'
        if self.backend == 'mss':
            try:
                if self.session is None:
                    from mss import MSS  # optional dependency

                    self.session = MSS()
                shot = self.session.grab(bounds)
                # frombytes copies pixels; a later grab cannot overwrite a frame in use.
                return Image.frombytes('RGB', shot.size, shot.bgra, 'raw', 'BGRX')
            except Exception as exc:
                self.fallback_error = str(exc)
                self._close_session()
                self.backend = 'pillow'
        return ImageGrab.grab(bbox=bounds, all_screens=True)

    def _close_grabbers(self):
        for grabber in self._grabbers.values():
            grabber.close()
        self._grabbers.clear()

    def _close_session(self):
        if self.session is not None:
            try:
                self.session.close()
            finally:
                self.session = None

    def close(self):
        self._close_grabbers()
        self._close_session()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
