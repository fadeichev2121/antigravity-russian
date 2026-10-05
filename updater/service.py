"""Opt-in schedules confined to the current user's session."""
import hashlib
from datetime import datetime, timedelta
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from package import safe_path, process_options

WINDOWS_OWNER_GUARD = r"""
$ErrorActionPreference='Stop'
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$old=Get-ScheduledTask -TaskName $env:RU_AUTO_TASK_NAME -ErrorAction SilentlyContinue
if ($old) {
    try { $owner=([Security.Principal.SecurityIdentifier]::new($old.Principal.UserId)).Value }
    catch { $owner=([Security.Principal.NTAccount]::new($old.Principal.UserId)).Translate([Security.Principal.SecurityIdentifier]).Value }
    if (-not $old.Description.StartsWith($env:RU_AUTO_TASK_NAME + ':') -or $owner -ne $sid) {
        throw 'Task name belongs to another schedule'
    }
}
"""


def identifier(repository, control):
    return repository + '-auto-' + hashlib.sha256(str(control).encode()).hexdigest()[:12]


def systemd_quote(value):
    value = str(value)
    if any(ord(ch) < 32 for ch in value): raise RuntimeError('Недопустимый символ в пути расписания.')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def definitions(repository, control, python, runner, sid='CURRENT_USER'):
    key = identifier(repository, control)
    argv = [str(python), str(runner), 'check', '--quiet', '--control-dir', str(control)]
    arguments = subprocess.list2cmdline(argv[1:])
    windows_python = python.with_name('pythonw.exe') if python.name.lower() == 'python.exe' and python.with_name('pythonw.exe').is_file() else python
    windows = '''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>{key}: русский интерфейс после обновлений</Description></RegistrationInfo>
<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{sid}</UserId></LogonTrigger>
<TimeTrigger><Repetition><Interval>PT2M</Interval></Repetition><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
<Principals><Principal id="User"><UserId>{sid}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>Limited</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT15M</ExecutionTimeLimit><Enabled>true</Enabled></Settings>
<Actions Context="User"><Exec><Command>{python}</Command><Arguments>{arguments}</Arguments></Exec></Actions>
</Task>'''.format(key=escape(key), sid=escape(sid), python=escape(str(windows_python)), arguments=escape(arguments),
                  start=(datetime.now().astimezone() + timedelta(seconds=30)).isoformat(timespec='seconds'))
    return {'id': key,
            'macos': {'Label': key, 'ProgramArguments': argv, 'StartInterval': 120, 'RunAtLoad': True,
                      'ProcessType': 'Background', 'StandardOutPath': str(control / 'service.log'),
                      'StandardErrorPath': str(control / 'service.log')},
            'windows': windows,
            'linux_service': '[Unit]\nDescription=' + key + '\n\n[Service]\nType=oneshot\nExecStart=' + ' '.join(systemd_quote(x) for x in argv) + '\nTimeoutStartSec=15min\nUMask=0077\n',
            'linux_timer': '[Unit]\nDescription=' + key + '\n\n[Timer]\nOnStartupSec=30\nOnUnitInactiveSec=120\nAccuracySec=5s\n\n[Install]\nWantedBy=timers.target\n'}


def write_job(path, data, key):
    path = safe_path(path)
    # Scheduler directories can contain unrelated user files; don't change their permissions.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe_path(path.parent)
    if os.name != 'nt' and path.parent.stat().st_uid != os.getuid():
        raise RuntimeError('Чужой владелец папки расписаний.')
    if path.exists():
        if not path.is_file() or path.stat().st_size > 65536:
            raise RuntimeError('Имя расписания занято другим файлом.')
        require_owned_definition(path, key)
    fd, name = tempfile.mkstemp(prefix='.ru-auto-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out: out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def require_owned_definition(path, key):
    path = safe_path(path)
    if not path.is_file() or path.stat().st_size > 65536:
        raise RuntimeError('Чужое или повреждённое определение расписания.')
    raw = path.read_bytes()
    if path.suffix == '.xml':
        try: ours = ET.fromstring(raw).find('{*}RegistrationInfo/{*}Description').text.startswith(key + ':')
        except (ET.ParseError, AttributeError): ours = False
    elif path.suffix == '.plist':
        try: ours = plistlib.loads(raw).get('Label') == key
        except (ValueError, plistlib.InvalidFileException): ours = False
    else: ours = ('Description=' + key).encode() in raw.splitlines()
    if not ours: raise RuntimeError('Чужое определение расписания; остановка отменена.')


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=30, **process_options(), **kwargs)


def register(repository, control, python, runner):
    data = definitions(repository, control, python, runner)
    key = data['id']
    if sys.platform == 'darwin':
        path = Path.home() / 'Library/LaunchAgents' / (key + '.plist')
        write_job(path, plistlib.dumps(data['macos']), key)
        domain = 'gui/' + str(os.getuid())
        existing = subprocess.run(['/bin/launchctl', 'print', domain + '/' + key], capture_output=True)
        if existing.returncode == 0: run(['/bin/launchctl', 'bootout', domain + '/' + key])
        run(['/bin/launchctl', 'bootstrap', domain, str(path)])
        run(['/bin/launchctl', 'print', domain + '/' + key])
        return {'kind': 'launchd', 'id': key, 'path': str(path)}
    if sys.platform == 'win32':
        command = WINDOWS_OWNER_GUARD + r"""
$xml=[IO.File]::ReadAllText($env:RU_AUTO_TASK_XML).Replace('CURRENT_USER',$sid)
Register-ScheduledTask -TaskName $env:RU_AUTO_TASK_NAME -Xml $xml -Force | Out-Null
Get-ScheduledTask -TaskName $env:RU_AUTO_TASK_NAME -ErrorAction Stop | Out-Null
Start-ScheduledTask -TaskName $env:RU_AUTO_TASK_NAME
"""
        path = control / 'task.xml'
        write_job(path, data['windows'].encode('utf-16'), key)
        env = dict(os.environ, RU_AUTO_TASK_XML=str(path), RU_AUTO_TASK_NAME=key)
        run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], env=env)
        return {'kind': 'task', 'id': key}
    if sys.platform.startswith('linux'):
        home = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
        directory = safe_path(home / 'systemd/user')
        service, timer = directory / (key + '.service'), directory / (key + '.timer')
        write_job(service, data['linux_service'].encode(), key)
        write_job(timer, data['linux_timer'].encode(), key)
        run(['systemctl', '--user', 'daemon-reload'])
        run(['systemctl', '--user', 'enable', '--now', key + '.timer'])
        run(['systemctl', '--user', 'is-active', key + '.timer'])
        return {'kind': 'systemd', 'id': key, 'path': str(timer), 'service_path': str(service)}
    raise RuntimeError('Нет расписания для этой ОС.')


def remove(repository, control, record):
    key = identifier(repository, control)
    if not record: return
    if record.get('id') != key: raise RuntimeError('Запись расписания относится к другой установке.')
    if record['kind'] == 'launchd' and sys.platform == 'darwin':
        path = safe_path(Path.home() / 'Library/LaunchAgents' / (key + '.plist'))
        if not path.exists(): return
        require_owned_definition(path, key)
        target = 'gui/' + str(os.getuid()) + '/' + key
        if subprocess.run(['/bin/launchctl', 'print', target], capture_output=True).returncode == 0:
            run(['/bin/launchctl', 'bootout', target])
        if path.exists():
            # Keep a recoverable disabled definition rather than deleting files.
            disabled = path.with_suffix('.plist.disabled')
            if disabled.exists():
                disabled = path.with_name(path.name + '.disabled-' + __import__('uuid').uuid4().hex)
            path.rename(disabled)
    elif record['kind'] == 'task' and sys.platform == 'win32':
        command = WINDOWS_OWNER_GUARD + "\nif ($old) { Disable-ScheduledTask -TaskName $env:RU_AUTO_TASK_NAME | Out-Null }"
        run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], env=dict(os.environ, RU_AUTO_TASK_NAME=key))
    elif record['kind'] == 'systemd' and sys.platform.startswith('linux'):
        home = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
        directory = safe_path(home / 'systemd/user')
        paths = [safe_path(directory / (key + suffix)) for suffix in ('.service', '.timer')]
        existing = [path for path in paths if path.exists()]
        if not existing: return
        for path in existing: require_owned_definition(path, key)
        run(['systemctl', '--user', 'disable', '--now', key + '.timer'])
    else:
        raise RuntimeError('Расписание относится к другой ОС.')


def record_for(repository, control):
    key = identifier(repository, control)
    if sys.platform == 'darwin':
        return {'kind': 'launchd', 'id': key, 'path': str(Path.home() / 'Library/LaunchAgents' / (key + '.plist'))}
    if sys.platform == 'win32': return {'kind': 'task', 'id': key}
    if sys.platform.startswith('linux'):
        directory = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'systemd/user'
        return {'kind': 'systemd', 'id': key, 'path': str(directory / (key + '.timer')), 'service_path': str(directory / (key + '.service'))}
    raise RuntimeError('Нет расписания для этой ОС.')
