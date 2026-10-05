#!/usr/bin/env python3
"""Patch original Windows and Linux folders; keep every replaced byte."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'common'))
from asar import Asar
from translation import make_patch
from profiles import load_profiles

PACKAGE_VERSION = '2.0.1'

SYSTEM = 'windows' if sys.platform == 'win32' else 'linux' if sys.platform.startswith('linux') else None
FILES = ['resources/app.asar'] + (['Antigravity.exe'] if SYSTEM == 'windows' else [])


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def safe_path(path):
    """Reject symlinks and Windows junctions on every component."""
    path = Path(os.path.abspath(path))
    if SYSTEM == 'windows' and str(path).startswith('\\\\'):
        raise RuntimeError('Сетевые пути не поддерживаются.')
    for component in [path] + list(path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RuntimeError('Путь содержит ссылку или junction: ' + str(component))
    return path


def identity():
    if SYSTEM == 'linux':
        import pwd
        uid = int(os.environ.get('SUDO_UID', os.getuid())) if os.geteuid() == 0 else os.getuid()
        account = pwd.getpwuid(uid)
        if uid == 0:
            raise RuntimeError('Запускай меню из обычного аккаунта; sudo требуется только выбранному действию.')
        return uid, account.pw_gid, Path(account.pw_dir) / '.local/state/antigravity-russian'
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        raise RuntimeError('LOCALAPPDATA не найден. Запускай из своего обычного аккаунта Windows.')
    return None, None, Path(local) / 'antigravity-russian/state'


def private_directory(path, uid, gid):
    safe_path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)
        if SYSTEM == 'linux' and os.geteuid() == 0:
            os.chown(item, uid, gid)
    if not path.is_dir():
        raise RuntimeError('Папка состояния отсутствует: ' + str(path))
    if SYSTEM == 'linux':
        if path.stat().st_uid != uid:
            raise RuntimeError('Папка состояния принадлежит другому пользователю.')
        path.chmod(0o700)
    else:
        # Restrict this project's backup/state directory; never change app ACLs.
        command = """
$ErrorActionPreference='Stop'
$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
$targets=@(ConvertFrom-Json -InputObject $env:ANTIGRAVITY_RU_PRIVATE_DIRECTORIES)
foreach ($p in $targets) {
$old=[System.IO.Directory]::GetAccessControl($p)
if ($env:ANTIGRAVITY_RU_NEW_DIRECTORY -ne '1' -and $old.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw ('State directory belongs to another user: ' + $p) }
$acl=New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true,$false)
$acl.SetOwner($sid)
$rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,[System.Security.AccessControl.FileSystemRights]::FullControl,([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit),[System.Security.AccessControl.PropagationFlags]::None,[System.Security.AccessControl.AccessControlType]::Allow)
$acl.AddAccessRule($rule)
[System.IO.Directory]::SetAccessControl($p,$acl)
}
"""
        # Intermediate parents must get the same private owner/ACL too. An
        # elevated Windows token otherwise makes them Administrators-owned.
        targets = list(reversed(missing)) if missing else [path]
        environment = dict(os.environ, ANTIGRAVITY_RU_PRIVATE_DIRECTORIES=json.dumps([str(item) for item in targets]),
                           ANTIGRAVITY_RU_NEW_DIRECTORY='1' if path in missing else '0')
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                       env=environment, check=True, stdout=subprocess.DEVNULL)


def owned_file(path, uid):
    safe_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError('Ожидался обычный файл с одной ссылкой: ' + str(path))
    if SYSTEM == 'linux' and (info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError('Файл состояния должен принадлежать пользователю и иметь права 0600.')
    return info


def write_private(path, data, uid, gid):
    safe_path(path)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        if SYSTEM == 'linux' and os.geteuid() == 0:
            os.fchown(stream.fileno(), uid, gid)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def save_state(directory, state, uid, gid):
    raw = json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
    pending = directory / ('state-' + uuid.uuid4().hex + '.tmp')
    write_private(pending, raw, uid, gid)
    os.replace(pending, directory / 'state.json')


def profiles():
    return load_profiles()


def load_state(directory, uid, app=None):
    path = directory / 'state.json'
    safe_path(path)
    if not path.exists():
        return None
    info = owned_file(path, uid)
    if info.st_size > 128 * 1024:
        raise RuntimeError('Файл состояния слишком большой.')
    state = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(state, dict) or state.get('schema') != 1 or state.get('product') != 'antigravity-russian-portable'
            or state.get('platform') != SYSTEM or state.get('phase') not in {'prepared', 'installed', 'restoring', 'restored'}):
        raise RuntimeError('Неизвестный формат состояния.')
    selected = profiles().get(state.get('profile'))
    if not selected or selected['platform'] != SYSTEM:
        raise RuntimeError('Неизвестная сборка в состоянии.')
    original_app = state.get('app')
    if not isinstance(original_app, str) or str(safe_path(original_app)) != original_app:
        raise RuntimeError('Некорректный путь приложения в состоянии.')
    if app is not None and os.path.normcase(str(app)) != os.path.normcase(original_app):
        raise RuntimeError('Путь --app не совпадает с сохранённой установкой.')
    backup_name = state.get('backup')
    if not isinstance(backup_name, str) or not re.fullmatch(r'backup-[0-9a-f]{32}', backup_name):
        raise RuntimeError('Некорректный путь резервной копии.')
    safe_path(directory / backup_name)
    if set(state.get('files', {})) != set(FILES):
        raise RuntimeError('Некорректный список изменённых файлов.')
    for name, record in state['files'].items():
        for key in ('original', 'patched', 'before'):
            if not re.fullmatch(r'[a-f0-9]{64}', str(record.get(key, ''))):
                raise RuntimeError('Некорректная контрольная сумма.')
        for key in ('mode', 'uid', 'gid'):
            if type(record.get(key)) is not int or record[key] < 0:
                raise RuntimeError('Некорректные права файла в состоянии.')
        if record['mode'] > 0o777:
            raise RuntimeError('Особые права файлов не поддерживаются.')
    if state['files']['resources/app.asar']['original'] != selected['asar_sha256']:
        raise RuntimeError('Резервная копия относится к другой сборке.')
    if SYSTEM == 'windows' and state['files']['Antigravity.exe']['original'] != selected['exe_sha256']:
        raise RuntimeError('EXE относится к другой сборке.')
    return state


@contextmanager
def lock(directory, uid, gid):
    private_directory(directory, uid, gid)
    path = directory / 'state.lock'
    safe_path(path)
    if not path.exists():
        try:
            write_private(path, b'0', uid, gid)
        except FileExistsError:
            pass
    owned_file(path, uid)
    with path.open('r+b') as stream:
        try:
            if SYSTEM == 'windows':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('Другой установщик уже работает с этой папкой состояния.')
        try:
            yield
        finally:
            if SYSTEM == 'windows':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def discover(args, state):
    if args.app:
        return safe_path(os.path.expanduser(args.app))
    if state:
        return safe_path(state['app'])
    if SYSTEM == 'linux':
        import pwd
        owner = int(os.environ.get('SUDO_UID', os.getuid())) if os.geteuid() == 0 else os.getuid()
        candidates = [Path(pwd.getpwuid(owner).pw_dir) / '.local/share/antigravity', Path('/opt/Antigravity'), Path('/opt/antigravity')]
    else:
        local = Path(os.environ['LOCALAPPDATA'])
        candidates = [local / 'Programs/Antigravity', local / 'Antigravity']
        for variable in ('ProgramFiles', 'ProgramFiles(x86)'):
            if os.environ.get(variable):
                candidates.append(Path(os.environ[variable]) / 'Antigravity')
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r'Software\Microsoft\Windows\CurrentVersion\Uninstall\121a0be4-63bd-531e-acf8-fc3924c7e984', 0, winreg.KEY_READ | view) as key:
                        location, kind = winreg.QueryValueEx(key, 'InstallLocation')
                        if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and location:
                            candidates.append(Path(os.path.expandvars(location)))
                except FileNotFoundError:
                    pass
                except PermissionError:
                    continue

    found = list({os.path.normcase(str(safe_path(p))): safe_path(p) for p in candidates if (p / 'resources/app.asar').is_file()}.values())
    if len(found) != 1:
        raise RuntimeError('Укажи --app с папкой Antigravity, где находится resources/app.asar. Автоматически найдено установок: ' + str(len(found)))
    return found[0]


def separate_state(directory, app):
    for other in (app, ROOT):
        left, right = os.path.normcase(str(directory)), os.path.normcase(str(other))
        try:
            common = os.path.commonpath([left, right])
        except ValueError:
            continue
        if common == left or common == right:
            raise RuntimeError('Папка состояния должна находиться отдельно от приложения и файлов установщика.')


def require_layout(app):
    safe_path(app)
    if SYSTEM == 'windows' and any(x.lower() == 'windowsapps' for x in app.parts):
        raise RuntimeError('Пакеты MSIX/Microsoft Store в WindowsApps нельзя менять этим патчем. Используй обычную EXE-установку Antigravity.')
    for name in FILES:
        path = safe_path(app / name)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o7000:
            raise RuntimeError('Неподдерживаемый файл приложения: ' + name)
    if SYSTEM == 'windows':
        for parent in [app] + list(app.parents):
            if (parent / 'AppxManifest.xml').exists() or (parent / 'AppxSignature.p7x').exists():
                raise RuntimeError('Обнаружен MSIX-пакет. Его подпись и регистрацию установщик не меняет.')


def require_closed(app):
    """Inspect executable paths; never kill apps or alter updater settings."""
    if SYSTEM == 'windows':
        command = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); $p=@(Get-CimInstance Win32_Process | Select-Object ProcessId,Name,ExecutablePath); ConvertTo-Json -InputObject $p -Compress"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, encoding='utf-8', check=True)
        items = json.loads(result.stdout)
        prefix = os.path.normcase(str(app) + os.sep)
        updater = os.path.normcase(str(app.parent / 'Update.exe'))
        for item in items:
            executable = item.get('ExecutablePath')
            name = (item.get('Name') or '').lower()
            path = os.path.normcase(executable) if executable else ''
            if path.startswith(prefix) or path == updater or (not path and name in {'antigravity.exe', 'antigravity-x64.exe', 'antigravity-arm64.exe'}):
                raise RuntimeError('Antigravity или его обновление ещё работает: ' + str(item['Name']) + ' (PID ' + str(item['ProcessId']) + '). Заверши Antigravity через меню выхода и повтори действие.')
    else:
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit() or int(proc.name) == os.getpid():
                continue
            try:
                command = (proc / 'comm').read_text().strip()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                raise RuntimeError('Не удалось прочитать список процессов. Повтори выбранное действие с sudo.')
            if command in {'apt', 'apt-get', 'dpkg', 'unattended-upgr'}:
                raise RuntimeError('Менеджер пакетов сейчас работает. Дождись завершения обновления.')
            try:
                exe = os.readlink(proc / 'exe').removesuffix(' (deleted)')
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                if command.lower().startswith(('antigravity',)):
                    raise RuntimeError('Не удалось определить путь процесса Antigravity. Полностью закрой Antigravity или повтори с sudo.')
                continue
            if exe.startswith(str(app) + '/'):
                raise RuntimeError('Antigravity ещё работает (PID ' + proc.name + '). Полностью выйди из приложения и повтори действие.')


def match_profile(app):
    hashes = {name: file_hash(app / name) for name in FILES}
    version = json.loads(Asar((app / 'resources/app.asar').read_bytes()).read('package.json'))['version']
    architecture = None
    if SYSTEM == 'linux':
        binary = safe_path(app / 'antigravity')
        with binary.open('rb') as source:
            header = source.read(20)
        if len(header) != 20 or header[:5] != b'\x7fELF\x02' or header[5] not in {1, 2}:
            raise RuntimeError('Не удалось определить архитектуру Linux-приложения.')
        machine = int.from_bytes(header[18:20], 'little' if header[5] == 1 else 'big')
        architecture = {62: 'x64', 183: 'arm64'}.get(machine)
        if architecture is None:
            raise RuntimeError('Архитектура Linux-приложения не поддерживается.')
    for key, profile in profiles().items():
        if profile['platform'] == SYSTEM and profile['version'] == version and hashes['resources/app.asar'] == profile['asar_sha256']:
            if architecture is not None and profile['arch'] != architecture:
                continue
            if SYSTEM == 'windows' and hashes['Antigravity.exe'] != profile['exe_sha256']:
                continue
            return key, profile
    raise RuntimeError('Эта исходная сборка пока не поддерживается. Выбери статус; неизвестные файлы не изменены.')


def build(originals, profile):
    expected = {'resources/app.asar': profile['asar_sha256']}
    if SYSTEM == 'windows':
        expected['Antigravity.exe'] = profile['exe_sha256']
    if any(sha(originals[name]) != expected[name] for name in FILES):
        raise RuntimeError('Исходные файлы не соответствуют профилю сборки.')
    dictionary = json.loads((ROOT / 'macos/ru.json').read_text(encoding='utf-8'))
    patched_asar, header_hash = make_patch(originals['resources/app.asar'], dictionary)
    patched = {'resources/app.asar': patched_asar}
    if SYSTEM == 'windows':
        if profile.get('integrity_update'):
            from pe_integrity import replace_integrity
            size = struct.unpack('<I', originals['resources/app.asar'][12:16])[0]
            old_header = sha(originals['resources/app.asar'][16:16 + size])
            patched['Antigravity.exe'] = replace_integrity(originals['Antigravity.exe'], old_header, header_hash)
        else:
            patched['Antigravity.exe'] = originals['Antigravity.exe']
    return patched, len(dictionary)


def require_hashes(app, state, choices):
    for name, record in state['files'].items():
        if file_hash(safe_path(app / name)) not in {record[x] for x in choices}:
            raise RuntimeError('Файл изменился после установки/подготовки: ' + name + '. Автоматическая перезапись остановлена.')


def backup_bytes(directory, state, name, kind, uid):
    path = safe_path(directory / state['backup'] / kind / name)
    owned_file(path, uid)
    data = path.read_bytes()
    if sha(data) != state['files'][name][kind]:
        raise RuntimeError('Резервная копия повреждена: ' + name)
    return data


def replace_file(target, data, record):
    safe_path(target)
    descriptor, temporary = tempfile.mkstemp(prefix='.antigravity-ru-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            if SYSTEM == 'linux':
                os.fchmod(stream.fileno(), record['mode'])
                if os.geteuid() == 0:
                    os.fchown(stream.fileno(), record['uid'], record['gid'])
        os.replace(temporary, target)
    except Exception:
        # Keep the prepared file for diagnosis; the original backup remains.
        print('Сохранён промежуточный файл: ' + temporary, file=sys.stderr)
        raise


def install(args, app, directory, uid, gid, state):
    require_layout(app)
    require_closed(app)
    upgrading = bool(state and state['phase'] == 'installed')
    if upgrading:
        matches_saved = all(file_hash(app / name) == record['patched'] for name, record in state['files'].items())
        if not matches_saved:
            # An updater may have replaced the app. Accept only an entirely
            # known original release, never arbitrary modified files.
            match_profile(app)
            upgrading = False
            print('[i] Найдена поддерживаемая исходная сборка после обновления. Предыдущая резервная копия сохранится.')
    if upgrading:
        key, profile = state['profile'], profiles()[state['profile']]
        originals = {name: backup_bytes(directory, state, name, 'original', uid) for name in FILES}
    else:
        if state and state['phase'] not in {'restored', 'installed'}:
            raise RuntimeError('Предыдущая операция не завершена. Сначала выбери откат с тем же --app и --state-dir.')
        key, profile = match_profile(app)
        originals = {name: (app / name).read_bytes() for name in FILES}
    if SYSTEM == 'windows' and profile.get('integrity_update') and not args.approve_exe_signature:
        raise RuntimeError('Нужно согласие --approve-exe-signature: после изменения EXE его подпись Google будет недействительной.')
    patched, count = build(originals, profile)
    before = {name: file_hash(app / name) for name in FILES}
    if upgrading and all(before[name] == sha(patched[name]) for name in FILES):
        print('[OK] Установлена актуальная версия русского патча: ' + str(app))
        return
    if state:
        # Keep the previous registration for diagnostics; original backup bytes
        # are also copied into the next transaction, never taken from patched app.
        write_private(directory / ('record-' + uuid.uuid4().hex + '.json'), json.dumps(state, ensure_ascii=False, indent=2).encode(), uid, gid)
    backup_name = 'backup-' + uuid.uuid4().hex
    backup = directory / backup_name
    private_directory(backup, uid, gid)
    records = {}
    for name in FILES:
        info = (app / name).stat()
        records[name] = {'original': sha(originals[name]), 'patched': sha(patched[name]), 'before': before[name],
                         'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}
        for kind, data in (('original', originals[name]), ('patched', patched[name])):
            path = backup / kind / name
            private_directory(path.parent, uid, gid)
            write_private(path, data, uid, gid)
    state = {'schema': 1, 'product': 'antigravity-russian-portable', 'platform': SYSTEM, 'profile': key,
             'phase': 'prepared', 'app': str(app), 'backup': backup_name, 'files': records,
             'dictionary_entries': count, 'package_version': PACKAGE_VERSION, 'installed_at': datetime.now(timezone.utc).isoformat()}
    save_state(directory, state, uid, gid)
    require_closed(app)
    require_hashes(app, state, ['before'])
    for name in FILES:
        require_hashes(app, state, ['before', 'patched'])
        if file_hash(app / name) != records[name]['patched']:
            replace_file(app / name, patched[name], records[name])
    state['phase'] = 'installed'
    save_state(directory, state, uid, gid)
    print('[OK] Патч применён к исходной установке: ' + str(app))
    print('Резервная копия всех заменённых файлов: ' + str(backup))
    print('Antigravity не запущен. Запусти его самостоятельно.')


def restore(app, directory, uid, gid, state):
    if not state:
        raise RuntimeError('Запись установки не найдена в указанной папке состояния.')
    require_layout(app)
    require_closed(app)
    require_hashes(app, state, ['original', 'patched', 'before'])
    originals = {name: backup_bytes(directory, state, name, 'original', uid) for name in FILES}
    if state['phase'] == 'restored':
        require_hashes(app, state, ['original'])
        print('[OK] Исходные файлы уже восстановлены.')
        return
    # Backup contains both byte variants; preserve interrupted transactions too.
    for name in FILES:
        backup_bytes(directory, state, name, 'patched', uid)
    state['phase'] = 'restoring'
    save_state(directory, state, uid, gid)
    for name in FILES:
        require_hashes(app, state, ['original', 'patched', 'before'])
        if file_hash(app / name) != state['files'][name]['original']:
            replace_file(app / name, originals[name], state['files'][name])
    state['phase'] = 'restored'
    save_state(directory, state, uid, gid)
    print('[OK] Исходные файлы восстановлены: ' + str(app))
    print('Резервные копии сохранены. Профиль и чаты не изменены.')


def status(app, directory, state):
    print('Приложение: ' + str(app))
    print('Состояние: ' + str(directory))
    supported = [p['version'] + ' (' + p['arch'] + ')' for p in profiles().values() if p['platform'] == SYSTEM]
    print('Поддерживаемые сборки: ' + ', '.join(supported))
    require_layout(app)
    print('SHA256 app.asar: ' + file_hash(app / 'resources/app.asar'))
    if state:
        print('Фаза установки: ' + state['phase'])
        print('Версия пакета: ' + state['package_version'])
        print('Профиль сборки: ' + state['profile'])
        print('Резервная копия: ' + str(directory / state['backup']))
        try:
            require_hashes(app, state, ['original', 'patched', 'before'] if state['phase'] in {'prepared', 'restoring'} else ['patched'] if state['phase'] == 'installed' else ['original'])
        except RuntimeError:
            if state['phase'] != 'installed':
                raise
            key, _ = match_profile(app)
            print('[i] Установлена поддерживаемая исходная сборка: ' + key)
            print('Выбери установку для повторного перевода. Старый откат не применяется поверх обновлённого приложения.')
            return
        print('[OK] Файлы соответствуют сохранённому состоянию.')
    else:
        key, profile = match_profile(app)
        print('[OK] Совместимая исходная сборка: ' + key)


def main():
    if sys.version_info < (3, 9) or SYSTEM is None:
        raise RuntimeError('Нужны Windows/Linux и Python 3.9+. Для macOS используй основной install.sh.')
    parser = argparse.ArgumentParser(description='Русский интерфейс установленного Antigravity для Windows/Linux')
    parser.add_argument('action', choices=['install', 'status', 'restore'])
    parser.add_argument('--app', help='Папка установленного Antigravity: resources и исполняемый файл')
    parser.add_argument('--state-dir')
    parser.add_argument('--approve-exe-signature', action='store_true')
    args = parser.parse_args()
    uid, gid, default = identity()
    directory = safe_path(os.path.expanduser(args.state_dir)) if args.state_dir else safe_path(default)
    initial_state = load_state(directory, uid)
    initial_app = discover(args, initial_state)
    separate_state(directory, initial_app)
    if args.action == 'status':
        state, app = initial_state, initial_app
        if state:
            load_state(directory, uid, app)
        status(app, directory, state)
        return
    with lock(directory, uid, gid):
        state = load_state(directory, uid)
        app = discover(args, state)
        separate_state(directory, app)
        if app != initial_app:
            raise RuntimeError('Установка изменилась во время подготовки. Повтори действие.')
        if state:
            load_state(directory, uid, app)
        if args.action == 'install':
            install(args, app, directory, uid, gid, state)
        else:
            restore(app, directory, uid, gid, state)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, struct.error, subprocess.SubprocessError) as error:
        print('[Ошибка] ' + str(error), file=sys.stderr)
        print('Если подготовка уже началась, резервные копии сохранены. После закрытия Antigravity выбери откат с той же папкой состояния.', file=sys.stderr)
        sys.exit(1)
