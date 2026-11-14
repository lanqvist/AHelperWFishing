"""Build the release: frozen app (PyInstaller onedir), portable zip, Inno Setup installer, checksums.

Usage (from the repository root):
    uv sync --group build
    uv run python tools/build.py [--skip-installer] [--sign-command "signtool sign ... $f"]

Output in dist/:
    AHelperWFishing-Setup-<version>.exe   installer (per user, no admin rights)
    AHelperWFishing-<version>-portable.zip
    SHA256SUMS.txt

Signing: --sign-command (or env AHWF_SIGN_COMMAND) is any command with ``$f`` for the file,
e.g. signtool with a certificate or with the Azure Artifact Signing dlib. It signs the app
exe before packaging and, through Inno Setup's SignTool, the installer and uninstaller.
No personal data is included: settings, recordings and labels live in %LOCALAPPDATA%.
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'src' / 'ahelperw_fishing'
PACKAGING = ROOT / 'packaging'
BUILD = ROOT / 'build'
DIST = ROOT / 'dist'
APP = 'AHelperWFishing'
# Licenses of everything frozen into the app (dist-info names, lower case).
LICENSED = ('numpy', 'pillow', 'sv_ttk', 'pyinstaller')


def version():
    text = (PACKAGE / '__init__.py').read_text(encoding='utf-8')
    return re.search(r"^__version__ = '([^']+)'", text, re.MULTILINE).group(1)


def check_versions(ver):
    # The addon has its own version (## Version in its TOC): bump it whenever Fishing.lua
    # changes. The app tells a stale addon apart by comparing the files, not the version.
    toc = (PACKAGE / 'addon' / APP / f'{APP}.toc').read_text(encoding='utf-8')
    if not re.search(r'^## Version: \d+\.\d+\.\d+', toc, re.MULTILINE):
        raise SystemExit('Addon TOC has no ## Version')
    if f'## [{ver}]' not in (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'):
        raise SystemExit(f'CHANGELOG.md has no section for {ver}')


def run(cmd, **kwargs):
    print('>', ' '.join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def version_file(ver, target):
    numbers = [int(p) for p in re.findall(r'\d+', ver)[:3]]
    numbers += [0] * (4 - len(numbers))
    tup = tuple(numbers)
    target.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tup}, prodvers={tup}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041904B0', [
      StringStruct('CompanyName', 'lanqvist'),
      StringStruct('FileDescription', 'AHelperW Fishing — помощник рыбалки для WoW Forever'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('InternalName', '{APP}'),
      StringStruct('LegalCopyright', '© 2026 lanqvist'),
      StringStruct('OriginalFilename', '{APP}.exe'),
      StringStruct('ProductName', 'AHelperW Fishing'),
      StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [0x0419, 1200])])
  ]
)
""",
        encoding='utf-8',
    )
    return target


def freeze(ver):
    work = BUILD / 'pyinstaller'
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    sep = os.pathsep
    run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--name', APP,
        '--icon', PACKAGING / 'icon.ico',
        '--version-file', version_file(ver, work / 'version_info.txt'),
        '--paths', ROOT / 'src',
        '--add-data', f'{PACKAGE / "addon"}{sep}ahelperw_fishing/addon',
        '--add-data', f'{PACKAGE / "icon.ico"}{sep}ahelperw_fishing',
        '--collect-data', 'sv_ttk',
        '--exclude-module', 'pytest',
        '--distpath', work / 'dist', '--workpath', work / 'work', '--specpath', work,
        PACKAGING / 'launcher.py',
    ])  # fmt: skip
    frozen = work / 'dist' / APP
    if not (frozen / f'{APP}.exe').is_file():
        raise SystemExit('PyInstaller produced no executable')
    return frozen


def smoke_test(frozen):
    """The frozen app starts, shows its window and installs the addon into a fake game folder."""
    exe = frozen / f'{APP}.exe'
    with tempfile.TemporaryDirectory(prefix='ahwf-smoke-') as temp:
        env = dict(os.environ, AHWF_DATA_DIR=str(Path(temp) / 'data'))
        game = Path(temp) / 'World of Warcraft' / '_forever_'
        (game / 'Interface' / 'AddOns').mkdir(parents=True)
        result = Path(temp) / 'result.txt'
        run([exe, '--install-addon', game, '--result-file', result], env=env, timeout=120)
        installed = game / 'Interface' / 'AddOns' / APP
        for name in ('Fishing.lua', f'{APP}.toc'):
            if (installed / name).read_bytes() != (PACKAGE / 'addon' / APP / name).read_bytes():
                raise SystemExit(f'Smoke test: addon file {name} differs')
        print('smoke: addon ->', result.read_text(encoding='utf-8'))
        if sys.platform == 'win32' and not os.environ.get('AHWF_SKIP_GUI_SMOKE'):
            run([exe, '--smoke-test'], env=env, timeout=120)
            print('smoke: window opened and closed')


def licenses(target):
    target.mkdir(parents=True, exist_ok=True)
    base = Path(sys.base_prefix)
    for name in ('LICENSE.txt', 'LICENSE'):
        if (base / name).exists():
            shutil.copyfile(base / name, target / 'Python-LICENSE.txt')
            break
    site = Path(sysconfig.get_paths()['purelib'])
    for info in site.glob('*.dist-info'):
        name = info.name.split('-')[0].lower()
        if name not in LICENSED:
            continue
        files = [*info.glob('LICENSE*'), *info.glob('COPYING*'), *info.glob('licenses/**/*')]
        for lic in files:
            if lic.is_file():
                shutil.copyfile(lic, target / f'{name}-{lic.name}')
    shutil.copyfile(ROOT / 'THIRD_PARTY_NOTICES.md', target / 'THIRD_PARTY_NOTICES.md')


def stage(ver, frozen):
    """Release folder = portable layout; the installer takes the app from ``frozen``."""
    out = BUILD / 'release' / f'{APP}-{ver}'
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(frozen, out)
    shutil.copytree(PACKAGE / 'addon' / APP, out / 'Addon' / APP)
    readme = (PACKAGING / 'ПРОЧИТАЙ.txt').read_text(encoding='utf-8').replace('{version}', ver)
    (out / 'ПРОЧИТАЙ.txt').write_bytes(readme.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8-sig'))
    (out / 'Подробно').mkdir()
    for doc in ('FISHING.md', 'FISHING_VISION.md'):
        shutil.copyfile(ROOT / 'docs' / doc, out / 'Подробно' / doc)
    shutil.copyfile(ROOT / 'CHANGELOG.md', out / 'Подробно' / 'CHANGELOG.md')
    licenses(out / 'LICENSES')
    return out


def portable_zip(ver, release):
    archive = DIST / f'{APP}-{ver}-portable.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for item in sorted(release.rglob('*')):
            if item.is_file():
                z.write(item, Path(release.name) / item.relative_to(release))
    return archive


def find_iscc():
    candidates = [os.environ.get('ISCC'), shutil.which('iscc'), shutil.which('ISCC')]
    for base in (os.environ.get('LOCALAPPDATA'), os.environ.get('PROGRAMFILES(X86)'), os.environ.get('PROGRAMFILES')):
        if base:
            for sub in ('Programs/Inno Setup 7', 'Programs/Inno Setup 6', 'Inno Setup 7', 'Inno Setup 6'):
                candidates.append(str(Path(base) / sub / 'ISCC.exe'))
    found = next((c for c in candidates if c and Path(c).is_file()), None)
    if not found:
        raise SystemExit('Inno Setup (ISCC.exe) not found: install Inno Setup 6.6+ or set ISCC=<path>')
    return found


def sign(command, path):
    # Same convention as Inno Setup's SignTool: $f is replaced by the quoted file name. The
    # command is passed as one string (Windows command line), so quoted tool paths work.
    print('> sign', path, flush=True)
    subprocess.run(command.replace('$f', f'"{path}"'), check=True)


def installer(ver, frozen, release, sign_command):
    cmd = [find_iscc(), '/Q', f'/DAppVersion={ver}', f'/DAppDir={frozen}', f'/DReleaseDir={release}', f'/O{DIST}']
    if sign_command:
        # Inno Setup's own quote placeholder: a literal " would be escaped away on the command line.
        cmd += ['/DSign', '/Ssigntool=' + sign_command.replace('"', '$q')]
    run([*cmd, PACKAGING / f'{APP}.iss'])
    setup = DIST / f'{APP}-Setup-{ver}.exe'
    if not setup.is_file():
        raise SystemExit('Inno Setup produced no installer')
    return setup


def checksums(files, target_dir=DIST):
    """SHA256SUMS.txt in `sha256sum` format with LF line endings, so `sha256sum -c` works on
    Linux and in Git Bash (a CR would become part of the file name)."""
    lines = [f'{hashlib.sha256(f.read_bytes()).hexdigest()} *{f.name}' for f in files]
    target = Path(target_dir) / 'SHA256SUMS.txt'
    target.write_bytes(('\n'.join(lines) + '\n').encode('ascii'))
    return target


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--skip-installer', action='store_true', help='only the frozen app and the portable zip')
    parser.add_argument('--skip-smoke', action='store_true', help='do not start the frozen app')
    parser.add_argument('--sign-command', default=os.environ.get('AHWF_SIGN_COMMAND'))
    args = parser.parse_args()
    ver = version()
    check_versions(ver)
    DIST.mkdir(exist_ok=True)
    frozen = freeze(ver)
    if args.sign_command:
        sign(args.sign_command, frozen / f'{APP}.exe')
    if not args.skip_smoke:
        smoke_test(frozen)
    release = stage(ver, frozen)
    outputs = [portable_zip(ver, release)]
    if not args.skip_installer:
        outputs.insert(0, installer(ver, frozen, release, args.sign_command))
    outputs.append(checksums(outputs))
    for path in outputs:
        print(f'{path}  ({path.stat().st_size / 1e6:.1f} MB)')


if __name__ == '__main__':
    main()
