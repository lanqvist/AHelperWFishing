"""Addon install, settings, update check, versions and the Lua HUD fixture."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ahelperw_fishing import __version__, addon, settings, updates
from ahelperw_fishing.paths import bundled_addon

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('AHWF_DATA_DIR', str(tmp_path / 'data'))
    return tmp_path / 'data'


@pytest.fixture
def game(tmp_path):
    folder = tmp_path / 'World of Warcraft' / '_forever_'
    (folder / 'Interface' / 'AddOns').mkdir(parents=True)
    return folder


# -- versions -------------------------------------------------------------------
def test_versions_match():
    toc = (bundled_addon() / 'AHelperWFishing.toc').read_text(encoding='utf-8')
    addon_version = re.search(r'^## Version: (\S+)', toc, re.MULTILINE).group(1)
    assert updates.parse_version(addon_version) <= updates.parse_version(__version__)
    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    assert f'## [{__version__}]' in changelog


# -- addon ----------------------------------------------------------------------
def test_install_fresh(game, data_dir):
    result = addon.install(game)
    target = game / 'Interface' / 'AddOns' / 'AHelperWFishing'
    assert result.installed and result.target == target and result.backup is None
    for name in addon.ADDON_FILES:
        assert (target / name).read_bytes() == (bundled_addon() / name).read_bytes()
    assert addon.addon_version(target) == addon.addon_version(bundled_addon())


def test_is_current(game):
    assert not addon.is_current(game)
    addon.install(game)
    assert addon.is_current(game)
    (game / 'Interface' / 'AddOns' / 'AHelperWFishing' / 'Fishing.lua').write_text('-- edited', encoding='utf-8')
    assert not addon.is_current(game)


def test_reinstall_same_version_is_noop(game, data_dir):
    addon.install(game)
    result = addon.install(game)
    assert result.installed and 'актуален' in result.message
    assert not (data_dir / 'Backups').exists()


def test_upgrade_backs_up_previous(game, data_dir):
    target = game / 'Interface' / 'AddOns' / 'AHelperWFishing'
    target.mkdir()
    (target / 'Fishing.lua').write_text('-- old', encoding='utf-8')
    result = addon.install(game)
    assert result.backup is not None and (result.backup / 'Fishing.lua').read_text(encoding='utf-8') == '-- old'
    assert result.backup.parent == data_dir / 'Backups'


def test_backup_failure_does_not_abort(game, tmp_path):
    target = game / 'Interface' / 'AddOns' / 'AHelperWFishing'
    target.mkdir()
    (target / 'Fishing.lua').write_text('-- old', encoding='utf-8')
    blocker = tmp_path / 'file'
    blocker.write_text('x', encoding='utf-8')
    result = addon.install(game, backup_root=blocker / 'sub')
    assert result.installed and result.backup is None


def test_combat_addon_with_fishing_is_respected(game):
    combat = game / 'Interface' / 'AddOns' / 'AHelperWForever'
    combat.mkdir()
    (combat / 'AHelperWForever.toc').write_text('## Title: x\nCore.lua\nFishing.lua\n', encoding='utf-8')
    result = addon.install(game)
    assert not result.installed
    assert not (game / 'Interface' / 'AddOns' / 'AHelperWFishing').exists()


def test_wrong_folder_rejected(tmp_path):
    with pytest.raises(ValueError, match='_forever_'):
        addon.install(tmp_path)


def test_install_cli_codes(game):
    assert addon.install_cli(str(game))[0] == addon.EXIT_OK
    assert addon.install_cli(str(game.parent))[0] == addon.EXIT_ERROR
    with patch.object(addon, 'install', side_effect=PermissionError('denied')):
        assert addon.install_cli(str(game))[0] == addon.EXIT_ACCESS_DENIED
    with patch.object(addon, 'find_game', return_value=None):
        assert addon.install_cli(None)[0] == addon.EXIT_ERROR


def test_is_game_folder(game, tmp_path):
    assert addon.is_game_folder(game)
    assert not addon.is_game_folder(tmp_path)


# -- settings -------------------------------------------------------------------
def test_settings_roundtrip(data_dir):
    prefs = settings.Settings(cast='Shift+Q', interact='9', same_key=False, fps=20, region={'x': 1})
    settings.save(prefs)
    loaded = settings.load()
    assert loaded == prefs and not loaded.problems
    assert not list(data_dir.glob('*.tmp'))


def test_settings_from_previous_version(data_dir):
    data_dir.mkdir(parents=True)
    old = {
        'export': {
            'schema': 'AHelperWForever.Fishing', 'version': 1, 'screen': {'width': 3840, 'height': 2160},
            'rect': {'x': 92, 'y': 734, 'width': 562, 'height': 86}, 'mark': 65, 'source': 'auto',
        },
        'mark': 65.0, 'reserve': 0, 'cast': 'Shift+Q', 'interact': '9', 'mode': 'vision', 'same_key': False,
        'auto_recast': False, 'region': {'x': 947, 'y': 278, 'width': 487, 'height': 189},
        'background': False, 'fps': '30',
    }  # fmt: skip
    (data_dir / 'fishing.json').write_text(json.dumps(old), encoding='utf-8')
    loaded = settings.load()
    assert not loaded.problems
    assert (loaded.cast, loaded.interact, loaded.fps, loaded.check_updates) == ('Shift+Q', '9', 30, True)


def test_invalid_values_fall_back(data_dir):
    loaded = settings.from_dict({'cast': 'F8', 'fps': 25, 'reserve': -1, 'mode': 'x', 'same_key': 'yes'})
    assert set(loaded.problems) == {'cast', 'fps', 'reserve', 'mode', 'same_key'}
    assert loaded == settings.Settings()


def test_corrupt_settings_file(data_dir):
    data_dir.mkdir(parents=True)
    (data_dir / 'fishing.json').write_text('{oops', encoding='utf-8')
    assert settings.load().problems


# -- updates --------------------------------------------------------------------
@pytest.mark.parametrize(
    ('candidate', 'current', 'newer'),
    [('v0.4.1', '0.4.0', True), ('0.4.0', '0.4.0', False), ('v0.3.9', '0.4.0', False),
     ('v1.0', '0.4.0', True), ('nightly', '0.4.0', False)],
)  # fmt: skip
def test_is_newer(candidate, current, newer):
    assert updates.is_newer(candidate, current) is newer


def test_update_check_is_silent_offline():
    called = []
    with patch.object(updates, 'latest_release', side_effect=OSError('offline')):
        updates.check_async(lambda *a: called.append(a)).join(5)
    assert called == []


# -- Lua HUD fixture --------------------------------------------------------------
@pytest.mark.skipif(not shutil.which('luajit') and not shutil.which('lua'), reason='no Lua interpreter')
def test_lua_hud_fixture():
    lua = shutil.which('luajit') or shutil.which('lua')
    script = ROOT / 'tests' / 'test_fishing.lua'
    out = subprocess.run(
        [lua, str(script), str(bundled_addon() / 'Fishing.lua')], cwd=ROOT, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=60, check=False,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr + out.stdout
    assert 'OK' in out.stdout


# -- release tooling --------------------------------------------------------------
def test_checksums_use_lf_and_sha256sum_format(tmp_path):
    import hashlib
    import importlib.util

    spec = importlib.util.spec_from_file_location('ahwf_build', ROOT / 'tools' / 'build.py')
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    files = []
    for name, data in (('AHelperWFishing-Setup-9.9.9.exe', b'setup'), ('AHelperWFishing-9.9.9-portable.zip', b'zip')):
        (tmp_path / name).write_bytes(data)
        files.append(tmp_path / name)
    raw = build.checksums(files, tmp_path).read_bytes()
    assert b'\r' not in raw and raw.endswith(b'\n')
    for line, path in zip(raw.decode('ascii').splitlines(), files, strict=True):
        assert line == f'{hashlib.sha256(path.read_bytes()).hexdigest()} *{path.name}'


# -- command line -----------------------------------------------------------------
@pytest.mark.skipif(sys.platform != 'win32', reason='Windows entry point')
def test_cli_install_addon(game, tmp_path):
    result = tmp_path / 'result.txt'
    env = dict(os.environ)
    out = subprocess.run(
        [sys.executable, '-m', 'ahelperw_fishing', '--install-addon', str(game), '--result-file', str(result)],
        capture_output=True, text=True, timeout=120, env=env, check=False,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr
    assert 'Аддон установлен' in result.read_text(encoding='utf-8')
