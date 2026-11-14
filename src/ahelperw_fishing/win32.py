"""Win32 helpers: the game window, visibility, single instance, DPI and window theme."""

import ctypes as ct
import winreg
from ctypes import wintypes as wt
from pathlib import Path

_U = ct.WinDLL('user32', use_last_error=True)
_K = ct.WinDLL('kernel32', use_last_error=True)

_U.GetForegroundWindow.restype = wt.HWND
_U.GetWindowThreadProcessId.argtypes = [wt.HWND, ct.POINTER(wt.DWORD)]
_U.GetClientRect.argtypes = [wt.HWND, ct.POINTER(wt.RECT)]
_U.ClientToScreen.argtypes = [wt.HWND, ct.POINTER(wt.POINT)]
_U.IsWindow.argtypes = [wt.HWND]
_U.IsIconic.argtypes = [wt.HWND]
_U.WindowFromPoint.argtypes = [wt.POINT]
_U.WindowFromPoint.restype = wt.HWND
_U.GetAncestor.argtypes = [wt.HWND, wt.UINT]
_U.GetAncestor.restype = wt.HWND
_U.GetParent.argtypes = [wt.HWND]
_U.GetParent.restype = wt.HWND
_U.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
_K.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_K.OpenProcess.restype = wt.HANDLE
_K.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ct.POINTER(wt.DWORD)]
_K.CloseHandle.argtypes = [wt.HANDLE]
_K.CreateMutexW.argtypes = [ct.c_void_p, wt.BOOL, wt.LPCWSTR]
_K.CreateMutexW.restype = wt.HANDLE

GAME_FOLDER = '_forever_'
GAME_EXECUTABLES = ('wow.exe', 'wowt.exe', 'wowb.exe')
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_GA_ROOT = 2
_ERROR_ALREADY_EXISTS = 183
# Same name as the combat reader: fishing and combat must never send keys at the same time.
# The installer uses it (AppMutex) to ask the user to close a running copy.
INSTANCE_MUTEX = 'Local\\AHelperWForever.Reader.Installed'

Window = tuple[int, int, int, int, int]  # (hwnd, client x, client y, width, height)


def process_path(hwnd):
    pid = wt.DWORD()
    _U.GetWindowThreadProcessId(hwnd, ct.byref(pid))
    handle = _K.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        buf, length = ct.create_unicode_buffer(32768), wt.DWORD(32768)
        if not _K.QueryFullProcessImageNameW(handle, 0, buf, ct.byref(length)):
            return None
        return Path(buf.value)
    finally:
        _K.CloseHandle(handle)


def is_game_process(hwnd):
    path = process_path(hwnd) if hwnd else None
    return bool(path) and path.name.lower() in GAME_EXECUTABLES and path.parent.name.lower() == GAME_FOLDER


def game_window(screen=None, hwnd=None) -> Window:
    """Forever client area as (hwnd, x, y, width, height).

    Without hwnd the foreground window must be Forever. With hwnd (background mode) that
    exact window, focused or not, as long as it exists and is not minimised.
    """
    if hwnd is None:
        hwnd = _U.GetForegroundWindow()
        if not is_game_process(hwnd):
            raise ValueError('Forever не в фокусе')
    elif not _U.IsWindow(hwnd) or _U.IsIconic(hwnd) or not is_game_process(hwnd):
        raise ValueError('Окно Forever закрыто или свёрнуто')
    rect, point = wt.RECT(), wt.POINT()
    if not _U.GetClientRect(hwnd, ct.byref(rect)) or not _U.ClientToScreen(hwnd, ct.byref(point)):
        raise ValueError('Окно игры недоступно')
    if screen is not None and (rect.right, rect.bottom) != (screen['width'], screen['height']):
        raise ValueError('Размер окна игры изменился; найди HUD заново')
    return hwnd, point.x, point.y, rect.right, rect.bottom


def foreground_window():
    return _U.GetForegroundWindow()


def visible(hwnd, points):
    """Every screen point shows this window (not covered by another one)."""
    for x, y in points:
        top = _U.WindowFromPoint(wt.POINT(int(x), int(y)))
        if not top or _U.GetAncestor(top, _GA_ROOT) != hwnd:
            return False
    return True


class SingleInstance:
    """Named mutex held for the lifetime of the app; ``acquired`` is False if another copy runs."""

    def __init__(self, name=INSTANCE_MUTEX):
        self.handle = _K.CreateMutexW(None, False, name)
        self.acquired = bool(self.handle) and ct.get_last_error() != _ERROR_ALREADY_EXISTS

    def close(self):
        if self.handle:
            _K.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def message_box(text, title, icon=0x40):
    _U.MessageBoxW(None, text, title, icon)


def enable_dpi_awareness():
    """Per-monitor v2 DPI awareness: screen coordinates are physical pixels, as the capture expects."""
    try:
        _U.SetProcessDpiAwarenessContext.argtypes = [ct.c_void_p]
        if _U.SetProcessDpiAwarenessContext(ct.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return
    except AttributeError:
        pass
    try:
        ct.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def system_uses_dark_theme():
    """Windows "app mode" (Settings -> Personalization -> Colors)."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'
        ) as key:
            return winreg.QueryValueEx(key, 'AppsUseLightTheme')[0] == 0
    except OSError:
        return False


def set_dark_title_bar(tk_window, dark):
    """Match the native title bar to the app theme (Windows 10 20H1+ / 11)."""
    try:
        tk_window.update_idletasks()
        hwnd = _U.GetParent(tk_window.winfo_id()) or tk_window.winfo_id()
        value = ct.c_int(1 if dark else 0)
        dwm = ct.windll.dwmapi
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new id, then pre-20H1 id)
            if dwm.DwmSetWindowAttribute(wt.HWND(hwnd), attribute, ct.byref(value), ct.sizeof(value)) == 0:
                return True
    except (AttributeError, OSError):
        pass
    return False
