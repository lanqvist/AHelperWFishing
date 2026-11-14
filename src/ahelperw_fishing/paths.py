"""Where the app keeps its data. Nothing personal is ever written next to the program.

The folder name ``AHelperWForever`` is kept from earlier versions so existing settings,
recordings and the checked model keep working after an upgrade. ``AHWF_DATA_DIR``
overrides it (tests, portable use).
"""

import os
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get('AHWF_DATA_DIR')
    if override:
        return Path(override)
    base = os.environ.get('LOCALAPPDATA')
    return (Path(base) if base else Path.home() / 'AppData' / 'Local') / 'AHelperWForever'


def settings_file() -> Path:
    return data_dir() / 'fishing.json'


def model_file() -> Path:
    return data_dir() / 'fishing-model.json'


def sessions_dir() -> Path:
    return data_dir() / 'fishing-sessions'


def logs_dir() -> Path:
    return data_dir() / 'logs'


def backups_dir() -> Path:
    # Local app data always exists and is writable; Documents may be redirected or missing.
    return data_dir() / 'Backups'


def bundled_addon() -> Path:
    """The addon shipped with this build (inside the package, also when frozen)."""
    return Path(__file__).resolve().parent / 'addon' / 'AHelperWFishing'


def write_atomic(path: Path, text: str) -> None:
    """Write via a temporary file so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)
