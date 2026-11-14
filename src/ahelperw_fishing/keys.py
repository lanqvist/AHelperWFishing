"""Keys the user can assign to casting and interacting. F8 is reserved for stopping."""

STOP_KEY = 'F8'
STOP_VK = 0x77  # VK_F8

BASE_KEYS = list('1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ') + [f'F{i}' for i in range(1, 13) if f'F{i}' != STOP_KEY]
# Game bindings with one modifier, e.g. Shift+Q. F8 stays reserved in any form.
KEYS = BASE_KEYS + [f'{m}+{k}' for m in ('Shift', 'Ctrl', 'Alt') for k in BASE_KEYS]


def binding(key):
    """Key name from KEYS -> {'device': 'keyboard', 'vk': int, 'modifiers': [...]}."""
    if key not in KEYS:
        raise ValueError(f'Выбери клавишу из списка; {STOP_KEY} зарезервирована для остановки')
    modifier, _, base = key.rpartition('+')
    vk = 0x6F + int(base[1:]) if base.startswith('F') and len(base) > 1 else ord(base)
    return {'device': 'keyboard', 'vk': vk, 'modifiers': [modifier.upper()] if modifier else []}
