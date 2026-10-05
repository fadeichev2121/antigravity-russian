#!/usr/bin/env python3
"""Native-OS installer checks against official files, without app login or launch."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SYSTEM = 'windows' if sys.platform == 'win32' else 'linux' if sys.platform.startswith('linux') else None
sys.path.insert(0, str(ROOT / 'common'))
from asar import Asar


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def run(command, expected=0):
    result = subprocess.run([str(item) for item in command], capture_output=True, encoding='utf-8', errors='replace', timeout=300)
    print(result.stdout, end='', flush=True)
    print(result.stderr, end='', flush=True)
    if (expected == 0 and result.returncode != 0) or (expected != 0 and result.returncode == 0):
        raise AssertionError('Unexpected return code ' + str(result.returncode) + ': ' + str(command[0]))
    return result


def download(url, destination):
    if not url.startswith('https://storage.googleapis.com/antigravity-public/antigravity-hub/'):
        raise RuntimeError('Unapproved fixture URL')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=90) as source, destination.open('wb') as output:
                if not source.geturl().startswith('https://storage.googleapis.com/antigravity-public/'):
                    raise RuntimeError('Unexpected fixture redirect')
                shutil.copyfileobj(source, output, length=1024 * 1024)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(3)


def extract(profile, folder):
    app = folder / 'Antigravity с пробелами'
    app.mkdir()
    archive = folder / ('installer.exe' if SYSTEM == 'windows' else 'package.tar.gz')
    print('Download official fixture: ' + profile['version'] + ' / ' + profile['arch'], flush=True)
    download(profile['source_url'], archive)
    if SYSTEM == 'windows':
        seven = shutil.which('7z')
        if not seven:
            candidates = [Path(os.environ.get(name, 'C:/Program Files')) / '7-Zip/7z.exe' for name in ('ProgramFiles', 'ProgramFiles(x86)')]
            seven = next((str(path) for path in candidates if path.is_file()), None)
        if not seven:
            raise RuntimeError('7-Zip is required to read the official NSIS fixture; the installer will not be executed')
        listing = run([seven, 'l', archive]).stdout
        payload = next(line.split()[-1] for line in listing.splitlines() if '$PLUGINSDIR/app-' in line and '.7z' in line)
        run([seven, 'e', archive, '-i!' + payload, '-o' + str(folder), '-y'])
        packed = folder / Path(payload.replace('\\', '/')).name
        run([seven, 'x', packed, 'resources/app.asar', 'Antigravity.exe', '-o' + str(app), '-y'])
        packed.unlink()
        if digest(app / 'Antigravity.exe') != profile['exe_sha256']:
            raise AssertionError('Official EXE identity mismatch')
    else:
        with tarfile.open(archive, 'r:gz') as source:
            member = next(item for item in source if item.name.endswith('/resources/app.asar'))
            if not member.isfile():
                raise AssertionError('ASAR is not a regular archive member')
            target = app / 'resources/app.asar'
            target.parent.mkdir()
            with source.extractfile(member) as incoming, target.open('xb') as output:
                shutil.copyfileobj(incoming, output)
            target.chmod(0o644)
    archive.unlink()
    if digest(app / 'resources/app.asar') != profile['asar_sha256']:
        raise AssertionError('Official ASAR identity mismatch')
    return app


def launcher(app, state, action, shell=None, standalone=None):
    if SYSTEM == 'linux':
        return ['bash', standalone or ROOT / 'linux/install.sh', action, '--app', app, '--state-dir', state]
    return [shell or 'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            standalone or ROOT / 'windows/install.ps1', action, '-AppPath', app, '-StateDirectory', state]


def exercise(profile_key, profile, folder, report):
    app = extract(profile, folder)
    state = folder / 'state с пробелами'
    asar = app / 'resources/app.asar'
    original = asar.read_bytes()
    original_exe = digest(app / 'Antigravity.exe') if SYSTEM == 'windows' else None
    metadata = asar.stat()
    state_file = state / 'state.json'
    run(launcher(app, state, 'status'))
    run(launcher(app, state, 'install'))
    saved = json.loads(state_file.read_text(encoding='utf-8'))
    assert saved['phase'] == 'installed' and saved['profile'] == profile_key
    assert digest(asar) != profile['asar_sha256']
    assert 'Antigravity RU preload v1' in Asar(asar.read_bytes()).read('dist/preload.js').decode()
    if SYSTEM == 'windows':
        assert digest(app / 'Antigravity.exe') == original_exe
    else:
        assert (asar.stat().st_uid, asar.stat().st_gid, asar.stat().st_mode) == (metadata.st_uid, metadata.st_gid, metadata.st_mode)
    report['checks'].append('install-original-and-preserve-executable-or-file-permissions')
    run(launcher(app, state, 'status'))
    run(launcher(app, state, 'install'))
    assert json.loads(state_file.read_text(encoding='utf-8'))['backup'] == saved['backup']
    report['checks'].append('idempotent-install')

    # Upgrade the dictionary in this disposable checkout; never touch real apps.
    dictionary_path = ROOT / 'macos/ru.json'
    dictionary_bytes = dictionary_path.read_bytes()
    try:
        dictionary = json.loads(dictionary_bytes)
        dictionary['Antigravity CI translation probe'] = 'Проверка обновления перевода'
        dictionary_path.write_text(json.dumps(dictionary, ensure_ascii=False), encoding='utf-8')
        run(launcher(app, state, 'install'))
        upgraded = json.loads(state_file.read_text(encoding='utf-8'))
        assert upgraded['phase'] == 'installed' and upgraded['dictionary_entries'] == saved['dictionary_entries'] + 1
        assert upgraded['backup'] != saved['backup']
        assert digest(state / saved['backup'] / 'original/resources/app.asar') == profile['asar_sha256']
        assert digest(state / upgraded['backup'] / 'original/resources/app.asar') == profile['asar_sha256']
        saved = upgraded
    finally:
        dictionary_path.write_bytes(dictionary_bytes)
    report['checks'].append('translation-upgrade-preserves-original-backups')

    patched = asar.read_bytes()
    asar.write_bytes(patched + b'CI tamper probe')
    for action in ('install', 'restore'):
        snapshot = state_file.read_bytes()
        run(launcher(app, state, action), expected=1)
        assert asar.read_bytes() == patched + b'CI tamper probe'
        assert state_file.read_bytes() == snapshot
    asar.write_bytes(patched)
    report['checks'].append('reject-modified-app-without-overwriting')

    backup = state / saved['backup'] / 'original/resources/app.asar'
    backup.write_bytes(original + b'CI corrupt backup probe')
    snapshot = state_file.read_bytes()
    run(launcher(app, state, 'restore'), expected=1)
    assert asar.read_bytes() == patched and state_file.read_bytes() == snapshot
    backup.write_bytes(original)
    report['checks'].append('reject-corrupted-backup-without-overwriting')

    run(launcher(app, state, 'restore'))
    assert asar.read_bytes() == original
    if SYSTEM == 'windows':
        assert digest(app / 'Antigravity.exe') == original_exe
    assert json.loads(state_file.read_text(encoding='utf-8'))['phase'] == 'restored'
    run(launcher(app, state, 'restore'))
    report['checks'].append('byte-identical-restore-and-idempotent-restore')

    # Reproduce the user's download-and-run entry point, including private ACLs.
    standalone_dir = folder / 'standalone'
    standalone_dir.mkdir()
    source = ROOT / ('windows/install.ps1' if SYSTEM == 'windows' else 'linux/install.sh')
    standalone = standalone_dir / source.name
    shutil.copyfile(source, standalone)
    run(launcher(app, state, 'status', standalone=standalone))
    run(launcher(app, state, 'install', standalone=standalone))
    assert json.loads(state_file.read_text(encoding='utf-8'))['phase'] == 'installed'
    run(launcher(app, state, 'restore', standalone=standalone))
    assert asar.read_bytes() == original
    report['checks'].append('standalone-download-bootstrap')
    if SYSTEM == 'windows' and shutil.which('pwsh'):
        run(launcher(app, state, 'status', shell='pwsh', standalone=standalone))
        report['checks'].append('powershell-7-bootstrap')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', required=True)
    args = parser.parse_args()
    profiles = json.loads((ROOT / 'profiles.json').read_text(encoding='utf-8'))
    profile = profiles[args.profile]
    if profile['platform'] != SYSTEM:
        raise RuntimeError('Run this check on its actual native operating system')
    native_arch = os.environ.get('RUNNER_ARCH', '').lower()
    if native_arch and native_arch != profile['arch']:
        raise RuntimeError('Runner architecture does not match the selected profile')
    report = {'profile': args.profile, 'runner_os': SYSTEM, 'runner_arch': native_arch,
              'python_version': sys.version.split()[0], 'checks': [], 'app_executed': False,
              'google_login_tested': False, 'result': 'failure'}
    output = ROOT / 'test-results'
    output.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='antigravity-ci-', dir=os.environ.get('RUNNER_TEMP')) as temporary:
            exercise(args.profile, profile, Path(temporary), report)
        report['result'] = 'success'
        print('PASS: ' + args.profile + ' (' + str(len(report['checks'])) + ' checks)', flush=True)
    finally:
        (output / (args.profile + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
