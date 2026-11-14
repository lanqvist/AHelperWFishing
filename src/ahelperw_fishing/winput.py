"""Keyboard pulses for the game window. No process memory, no hooks.

``pulse`` uses SendInput (the game must be the foreground window); ``post_pulse`` posts
WM_KEYDOWN/WM_KEYUP to one window (background mode, plain keys only). Both refuse to send
while the user physically holds a modifier or the same key, and both re-check the stop
flag, F8 and the focus guard right before sending.
"""

import ctypes as ct
import time
from ctypes import wintypes as wt

from .keys import STOP_VK

_U = ct.WinDLL('user32', use_last_error=True)
_ULONG_PTR = ct.c_size_t

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 16, 17, 18, 91, 92
_MODIFIER_VK = {'CTRL': VK_CONTROL, 'ALT': VK_MENU, 'SHIFT': VK_SHIFT}
_HELD_MODIFIERS = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)
# Keys that need KEYEVENTF_EXTENDEDKEY: navigation block, arrows, Insert/Delete, Divide, right Ctrl/Alt.
EXTENDED = frozenset({33, 34, 35, 36, 37, 38, 39, 40, 45, 46, 111, 163, 165})
_KEYEVENTF_EXTENDEDKEY, _KEYEVENTF_KEYUP = 0x1, 0x2
_WM_KEYDOWN, _WM_KEYUP = 0x0100, 0x0101


class _MouseInput(ct.Structure):
    _fields_ = [
        ('dx', wt.LONG),
        ('dy', wt.LONG),
        ('mouseData', wt.DWORD),
        ('flags', wt.DWORD),
        ('time', wt.DWORD),
        ('extra', _ULONG_PTR),
    ]


class _KeyboardInput(ct.Structure):
    _fields_ = [
        ('vk', wt.WORD),
        ('scan', wt.WORD),
        ('flags', wt.DWORD),
        ('time', wt.DWORD),
        ('extra', _ULONG_PTR),
    ]


class _HardwareInput(ct.Structure):
    _fields_ = [('message', wt.DWORD), ('low', wt.WORD), ('high', wt.WORD)]


class _Payload(ct.Union):
    # The union must be as large as its largest member (MOUSEINPUT) for SendInput's size check.
    _fields_ = [('mouse', _MouseInput), ('key', _KeyboardInput), ('hardware', _HardwareInput)]


class _Input(ct.Structure):
    _fields_ = [('type', wt.DWORD), ('payload', _Payload)]


_U.SendInput.argtypes = [wt.UINT, ct.POINTER(_Input), ct.c_int]
_U.SendInput.restype = wt.UINT
_U.GetAsyncKeyState.argtypes = [ct.c_int]
_U.GetAsyncKeyState.restype = ct.c_short
_U.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_U.PostMessageW.restype = wt.BOOL
_U.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
_U.MapVirtualKeyW.restype = wt.UINT


def pressed(vk):
    return bool(_U.GetAsyncKeyState(vk) & 0x8000)


def stop_pressed():
    return pressed(STOP_VK)


def _keyboard(vk, up=False):
    item = _Input()
    item.type = 1  # INPUT_KEYBOARD
    flags = (_KEYEVENTF_KEYUP if up else 0) | (_KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0)
    item.payload.key = _KeyboardInput(vk, 0, flags, 0, 0)
    return item


def _blocked(vk, guard, halt):
    """Reason not to send now, or None."""
    if any(pressed(m) for m in _HELD_MODIFIERS):
        return 'physical_modifier_held'
    if pressed(vk):
        return 'physical_key_held'
    if halt.is_set() or stop_pressed() or not guard():
        return 'focus_or_stop'
    return None


def pulse(binding, focus_guard, halt):
    """Press and release ``binding`` (with its modifiers) via SendInput. Returns 'sent' or a reason."""
    if binding['device'] != 'keyboard':
        raise ValueError('Поддерживаются только клавиши клавиатуры')
    vk = binding['vk']
    mods = [_MODIFIER_VK[m] for m in binding['modifiers']]
    reason = _blocked(vk, focus_guard, halt)
    if reason:
        return reason
    releases = [_keyboard(vk, True)] + [_keyboard(m, True) for m in reversed(mods)]
    events = [_keyboard(m) for m in mods] + [_keyboard(vk)] + releases
    inputs = (_Input * len(events))(*events)
    written = _U.SendInput(len(inputs), inputs, ct.sizeof(_Input))
    if written != len(inputs):
        # A partial insertion may have left our modifiers down: release only the keys of this
        # pulse and stop, without privilege escalation.
        if written:
            cleanup = (_Input * len(releases))(*releases)
            _U.SendInput(len(cleanup), cleanup, ct.sizeof(_Input))
        raise OSError(ct.get_last_error(), 'Windows не приняла полный пакет клавиш')
    return 'sent'


def post_pulse(hwnd, binding, guard, halt):
    """Key press delivered to one window's message queue (works while it is not focused).

    Only plain keys: the game reads Shift/Ctrl/Alt from the real keyboard state, so a posted
    modifier would be ignored and a physically held one would change the key.
    """
    if binding['device'] != 'keyboard' or binding['modifiers']:
        return 'background_needs_plain_key'
    vk = binding['vk']
    reason = _blocked(vk, guard, halt)
    if reason:
        return reason
    scan = _U.MapVirtualKeyW(vk, 0)
    down = 1 | (scan << 16) | ((1 << 24) if vk in EXTENDED else 0)
    if not _U.PostMessageW(hwnd, _WM_KEYDOWN, vk, down):
        raise OSError(ct.get_last_error(), 'Окно игры не приняло нажатие')
    time.sleep(0.03)
    if not _U.PostMessageW(hwnd, _WM_KEYUP, vk, down | (1 << 30) | (1 << 31)):
        raise OSError(ct.get_last_error(), 'Окно игры не приняло отпускание клавиши')
    return 'sent'
