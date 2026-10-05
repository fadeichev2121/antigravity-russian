"""Private storage and bounded, commit-pinned source package updates."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
import ssl
import urllib.error

REPOSITORIES = {'claude-russian', 'antigravity-russian'}
LIMIT = 25 * 1024 * 1024


def process_options():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def worker_python():
    # A GUI Python runner has no console streams. Use console Python for
    # captured adapter/core output; CREATE_NO_WINDOW keeps these children quiet.
    executable = Path(sys.executable)
    console = executable.with_name('python.exe')
    if executable.name.lower() == 'pythonw.exe' and console.is_file(): return str(console)
    return sys.executable


def safe_path(value):
    path = Path(os.path.abspath(os.path.expanduser(str(value))))
    if os.name == 'nt' and str(path).startswith('\\\\'):
        raise RuntimeError('Сетевой служебный путь не поддерживается.')
    for item in (path, *path.parents):
        try: info = item.lstat()
        except FileNotFoundError: continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RuntimeError('Служебный путь содержит ссылку: ' + str(item))
    return path


def private_dir(value):
    path = safe_path(value)
    missing = []
    item = path
    while not item.exists():
        missing.append(item); item = item.parent
    for item in reversed(missing): item.mkdir(mode=0o700)
    if not path.is_dir(): raise RuntimeError('Ожидалась служебная папка: ' + str(path))
    if os.name != 'nt':
        for item in list(reversed(missing)) + [path]:
            if item.stat().st_uid != os.getuid(): raise RuntimeError('Чужой владелец служебной папки.')
            item.chmod(0o700)
    else:
        # Restrict only our own newly-created directories, never application ACLs.
        command = r"""
$ErrorActionPreference='Stop'
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$targets=ConvertFrom-Json $env:RU_AUTO_DIRS
foreach ($p in $targets) {
$old=[IO.Directory]::GetAccessControl($p)
if ($p -eq $env:RU_AUTO_EXISTING -and $old.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw 'Foreign state directory owner' }
$acl=New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true,$false); $acl.SetOwner($sid)
$rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl',('ContainerInherit,ObjectInherit'),'None','Allow')
$acl.AddAccessRule($rule); [IO.Directory]::SetAccessControl($p,$acl)
}
"""
        env = dict(os.environ, RU_AUTO_DIRS=json.dumps([str(x) for x in reversed(missing)] + [str(path)]),
                   RU_AUTO_EXISTING='' if path in missing else str(path))
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                       env=env, check=True, capture_output=True, **process_options())
    return path


def read_json(path):
    path = safe_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
        raise RuntimeError('Небезопасный служебный файл: ' + str(path))
    if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise RuntimeError('Служебный файл должен быть приватным и принадлежать пользователю.')
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    path = safe_path(path); private_dir(path.parent)
    if path.exists(): read_json(path)
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


@contextmanager
def mutex(control):
    private_dir(control)
    path = safe_path(control / 'run.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    locked = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError('Небезопасная блокировка.')
        if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise RuntimeError('Небезопасная блокировка.')
        if os.name == 'nt':
            import msvcrt
            if info.st_size == 0: os.write(fd, b'0')
            os.lseek(fd, 0, 0)
            try: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError: raise RuntimeError('Автовосстановление уже выполняется. Повтори действие позже.')
        else:
            import fcntl
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('Автовосстановление уже выполняется. Повтори действие позже.')
        locked = True
        yield
    finally:
        if locked and os.name == 'nt':
            os.lseek(fd, 0, 0); msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def manifest(root, repository):
    # Local checkout manifests need not have private file permissions.
    path = safe_path(root / 'manifest.json')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise RuntimeError('Некорректный манифест пакета.')
    data = json.loads(path.read_text(encoding='utf-8'))
    if repository not in REPOSITORIES or data.get('repository') != repository or data.get('format') != 1 or data.get('auto_update_api') != 1:
        raise RuntimeError('Пакет не поддерживает это автовосстановление.')
    for name in ('updater/adapter.py', 'updater/manager.py', 'updater/package.py', 'updater/service.py', 'macos/patch.py', 'portable/patch.py'):
        item = safe_path(root / name)
        if not item.is_file(): raise RuntimeError('Неполный пакет русификатора: ' + name)
    return data


def allowed(name):
    return name == 'manifest.json' or (name.split('/')[0] in {'macos', 'portable', 'common', 'updater', 'windows', 'linux'}
                                       and Path(name).suffix in {'.py', '.json', '.js', '.sh', '.ps1'}) or name == 'install.sh'


def snapshot(root, packages, repository):
    manifest(root, repository); private_dir(packages)
    target = packages / ('local-' + uuid.uuid4().hex)
    private_dir(target); total = 0; count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__', 'packages', 'node_modules'}]
        for name in files:
            source = Path(directory) / name
            relative = source.relative_to(root).as_posix()
            if not allowed(relative): continue
            safe_path(source); info = source.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError('Небезопасный исходный файл пакета.')
            total += info.st_size; count += 1
            if total > LIMIT or count > 2000: raise RuntimeError('Пакет русификатора слишком большой.')
            dest = target / relative; private_dir(dest.parent)
            with source.open('rb') as src, dest.open('xb') as out: shutil.copyfileobj(src, out)
            if os.name != 'nt': dest.chmod(0o600)
    manifest(target, repository)
    return target


def download(url, target, limit):
    request = urllib.request.Request(url, headers={'User-Agent': 'russian-interface-auto', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response, target.open('xb') as output:
            count = 0
            while True:
                data = response.read(1024 * 256)
                if not data: break
                count += len(data)
                if count > limit: raise RuntimeError('Ответ сервера превышает допустимый размер.')
                output.write(data)
    except urllib.error.URLError as error:
        # macOS system curl can use the system trust store when a standalone Python
        # has no CA bundle. TLS verification remains enabled in both paths.
        if not isinstance(error.reason, ssl.SSLError) or not shutil.which('curl'): raise
        if target.exists(): safe_path(target).unlink()
        subprocess.run(['curl', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https',
                        '--connect-timeout', '10', '--max-time', '30', '--max-filesize', str(limit),
                        '--output', str(target), url], capture_output=True, check=True, timeout=35, **process_options())
        if target.stat().st_size > limit: raise RuntimeError('Ответ сервера превышает допустимый размер.')
    if os.name != 'nt': target.chmod(0o600)


def extract_package(archive, destination, repository, commit):
    if repository not in REPOSITORIES or not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise RuntimeError('Некорректная версия пакета.')
    safe_path(destination)
    if destination.exists(): raise RuntimeError('Папка извлечения уже существует.')
    private_dir(destination)
    with tarfile.open(archive, 'r|gz') as source:
        seen = set(); count = 0; total = 0
        for item in source:
            count += 1; total += item.size
            if count > 2000 or total > LIMIT:
                raise RuntimeError('Неожиданный размер архива.')
            parts = PurePosixPath(item.name).parts
            if (not parts or parts[0] != repository + '-' + commit or '..' in parts
                    or any('\\' in p or ':' in p or '\x00' in p for p in parts)):
                raise RuntimeError('Некорректный путь в архиве.')
            if item.isdir(): continue
            if not item.isfile() or len(parts) < 2: raise RuntimeError('Ссылки в архиве запрещены.')
            relative = '/'.join(parts[1:])
            if relative in seen: raise RuntimeError('Повторный путь в архиве.')
            seen.add(relative)
            if not allowed(relative): continue
            dest = destination / relative; private_dir(dest.parent)
            with source.extractfile(item) as src, dest.open('xb') as out: shutil.copyfileobj(src, out)
            if os.name != 'nt': dest.chmod(0o600)
    manifest(destination, repository)
    return destination


def refresh(control, config):
    repository = config['repository']
    if repository not in REPOSITORIES: raise RuntimeError('Неизвестный репозиторий.')
    private_dir(control / 'packages')
    # The API and archive reference the same immutable commit, never two main snapshots.
    with tempfile.TemporaryDirectory(prefix='download-', dir=control) as temporary:
        folder = Path(temporary)
        metadata = folder / 'commit.json'
        download('https://api.github.com/repos/fadeichev2121/' + repository + '/commits/main', metadata, 1024 * 1024)
        commit = json.loads(metadata.read_text(encoding='utf-8')).get('sha', '')
        if not re.fullmatch(r'[a-f0-9]{40}', commit): raise RuntimeError('GitHub вернул некорректный commit.')
        target = control / 'packages' / commit
        if target.exists(): manifest(target, repository); return target
        archive = folder / 'sources.tar.gz'
        download('https://codeload.github.com/fadeichev2121/' + repository + '/tar.gz/' + commit, archive, LIMIT)
        extracted = extract_package(archive, folder / 'extracted', repository, commit)
        extracted.rename(target)
        return target
