"""Finding the game folder and installing the bundled HUD addon into it.

Used by the app ("Установить аддон") and by the installer, which runs
``AHelperWFishing.exe --install-addon <_forever_>`` and, when the game folder is
write-protected, repeats it elevated. Only the AHelperWFishing folder is ever written;
a previous copy is backed up to %LOCALAPPDATA% first.
"""

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import backups_dir, bundled_addon

ADDON = 'AHelperWFishing'
ADDON_FILES = ('AHelperWFishing.toc', 'Fishing.lua')
GAME_FOLDER = '_forever_'
COMBAT_ADDON = 'AHelperWForever'

# Exit codes of --install-addon (also read by the installer script).
EXIT_OK, EXIT_ERROR, EXIT_SKIPPED, EXIT_ACCESS_DENIED = 0, 1, 2, 5


@dataclass
class AddonResult:
    installed: bool
    target: Path | None
    message: str
    backup: Path | None = None


def _wow_install_paths():
    try:
        import winreg
    except ImportError:
        return []
    found = []
    for key in (
        r'SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft',
        r'SOFTWARE\Blizzard Entertainment\World of Warcraft',
    ):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                found.append(Path(winreg.QueryValueEx(k, 'InstallPath')[0]))
        except OSError:
            continue
    return found


def game_candidates():
    """Likely _forever_ folders: registry install path, standard paths, other drives."""
    candidates = []
    for path in _wow_install_paths():
        candidates += [path / GAME_FOLDER, path.parent / GAME_FOLDER]
    for base in (os.environ.get('PROGRAMFILES(X86)'), os.environ.get('PROGRAMFILES')):
        if base:
            candidates.append(Path(base) / 'World of Warcraft' / GAME_FOLDER)
    for letter in 'CDEFGHIJ':
        root = Path(f'{letter}:\\')
        candidates += [
            root / 'World of Warcraft' / GAME_FOLDER,
            root / 'Games' / 'World of Warcraft' / GAME_FOLDER,
            root / 'Battle.net' / 'World of Warcraft' / GAME_FOLDER,
            root / 'Program Files (x86)' / 'World of Warcraft' / GAME_FOLDER,
        ]
    return list(dict.fromkeys(candidates))


def is_game_folder(path):
    try:
        path = Path(path)
        return path.name.lower() == GAME_FOLDER and ((path / 'Interface').is_dir() or (path / 'Wow.exe').exists())
    except OSError:
        return False


def find_game():
    return next((c for c in game_candidates() if is_game_folder(c)), None)


def addon_version(folder):
    """``## Version`` of an addon TOC, or None."""
    try:
        text = (Path(folder) / f'{ADDON}.toc').read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return None
    match = re.search(r'^##\s*Version:\s*(\S+)', text, re.MULTILINE)
    return match.group(1) if match else None


def installed_addon(game):
    return Path(game) / 'Interface' / 'AddOns' / ADDON


def combat_has_fishing(addons):
    """The combat addon already embeds the fishing HUD (a second one would show two HUDs)."""
    toc = Path(addons) / COMBAT_ADDON / f'{COMBAT_ADDON}.toc'
    try:
        return toc.is_file() and any(
            line.strip() == 'Fishing.lua' for line in toc.read_text(encoding='utf-8', errors='ignore').splitlines()
        )
    except OSError:
        return False


def backup(target, root=None):
    """Copy the files of an existing addon folder; best effort (returns None on failure)."""
    try:
        root = Path(root or backups_dir())
        stamp = time.strftime('%Y%m%d-%H%M%S') + f'-{int(time.time() * 1000) % 1000:03d}'
        destination = root / f'{ADDON}-{stamp}'
        destination.mkdir(parents=True)
        for item in Path(target).iterdir():
            if item.is_file():
                shutil.copy2(item, destination / item.name)
        return destination
    except OSError:
        return None


def install(game, source=None, backup_root=None):
    """Install the bundled addon into ``<game>/Interface/AddOns/AHelperWFishing``.

    Raises ValueError for a wrong folder and PermissionError when the folder is protected.
    """
    game = Path(game)
    source = Path(source or bundled_addon())
    if game.name.lower() != GAME_FOLDER:
        raise ValueError(f'Выбери именно папку {GAME_FOLDER} (в ней Wow.exe и Interface)')
    if not game.is_dir():
        raise ValueError(f'Папка игры не найдена: {game}')
    missing = [name for name in ADDON_FILES if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError(f'В сборке нет файлов аддона: {", ".join(missing)}')
    addons = game / 'Interface' / 'AddOns'
    if combat_has_fishing(addons):
        return AddonResult(False, None, f'HUD рыбалки уже встроен в {COMBAT_ADDON} — второй аддон не ставлю.')
    addons.mkdir(parents=True, exist_ok=True)
    target = addons / ADDON
    saved = None
    if target.is_dir() and any(target.iterdir()):
        if all(_same(source / n, target / n) for n in ADDON_FILES):
            return AddonResult(True, target, f'Аддон уже актуален: {target}')
        saved = backup(target, backup_root)
    target.mkdir(exist_ok=True)
    for name in ADDON_FILES:
        shutil.copyfile(source / name, target / name)
        if not _same(source / name, target / name):
            raise OSError(f'Проверка копии не прошла: {name}')
    note = f' Прежняя версия сохранена: {saved}' if saved else ''
    return AddonResult(True, target, f'Аддон установлен: {target}.{note}', saved)


def _same(a, b):
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def install_cli(game):
    """``--install-addon``: returns (exit code, message). Never raises."""
    game = game or find_game()
    if not game:
        return EXIT_ERROR, 'Папка игры _forever_ не найдена'
    try:
        result = install(game)
    except PermissionError as exc:
        return EXIT_ACCESS_DENIED, f'Нет прав на запись: {exc}'
    except (OSError, ValueError) as exc:
        return EXIT_ERROR, str(exc)
    return (EXIT_OK if result.installed else EXIT_SKIPPED), result.message
