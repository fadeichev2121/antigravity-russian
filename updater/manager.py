#!/usr/bin/env python3
"""Automatic reapply controller; vendor applications are never launched."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from package import process_options, worker_python, REPOSITORIES, safe_path, private_dir, read_json, write_json, mutex, snapshot, refresh, manifest
import service

ROOT = Path(__file__).resolve().parent.parent
STABLE_SECONDS = 60
REFRESH_SECONDS = 86400

BRIDGE = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + '/updater')
import adapter
operation, payload = sys.argv[2], json.loads(sys.argv[3])
if operation == 'discover': result = adapter.discover(payload)
elif operation == 'default_state': result = adapter.default_state()
elif operation == 'probe': result = adapter.probe(Path(payload[0]), [Path(x) for x in payload[1]])
elif operation == 'command': result = adapter.command(payload[0], Path(payload[1]), Path(payload[2]), payload[3])
else: raise RuntimeError('Invalid adapter operation')
print(json.dumps(result, ensure_ascii=False, default=str))
'''


class Adapter:
    def __init__(self, root): self.root = root
    def call(self, operation, payload):
        result = subprocess.run([worker_python(), '-c', BRIDGE, str(self.root), operation,
                                 json.dumps(payload, ensure_ascii=False)], capture_output=True, text=True, encoding='utf-8', timeout=120, **process_options())
        if result.returncode: raise RuntimeError('Проверка приложения не завершена: ' + result.stderr.strip()[-1500:])
        return json.loads(result.stdout)
    def discover(self, hint): return Path(self.call('discover', hint))
    def default_state(self): return Path(self.call('default_state', None))
    def probe(self, app, states): return self.call('probe', [str(app), [str(p) for p in states]])
    def command(self, action, app, state, approved): return self.call('command', [action, str(app), str(state), approved])


def adapter_for(root):
    return Adapter(safe_path(root))


def default_control(repository):
    if sys.platform == 'darwin': return Path.home() / 'Library/Application Support' / repository / 'auto'
    if sys.platform == 'win32':
        if not os.environ.get('LOCALAPPDATA'): raise RuntimeError('Не найден LOCALAPPDATA.')
        return Path(os.environ['LOCALAPPDATA']) / repository / 'auto'
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / repository / 'auto'


def under(child, parent):
    child, parent = safe_path(child), safe_path(parent)
    try: return os.path.commonpath([os.path.normcase(str(child)), os.path.normcase(str(parent))]) == os.path.normcase(str(parent))
    except ValueError: return False


def config_at(control):
    value = read_json(control / 'config.json')
    if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('repository') not in REPOSITORIES
            or type(value.get('enabled')) is not bool or type(value.get('approved_signature')) is not bool
            or not isinstance(value.get('records'), list) or len(value['records']) > 1000
            or not under(value.get('active_package', ''), control / 'packages')):
        raise RuntimeError('Неверная запись автовосстановления.')
    if value.get('app') is not None and not isinstance(value['app'], str): raise RuntimeError('Неверный путь приложения.')
    if value.get('extra_state') is not None and not isinstance(value['extra_state'], str): raise RuntimeError('Неверный путь старого состояния.')
    for record in value['records']:
        if (not isinstance(record, dict) or not re.fullmatch(r'[a-f0-9]{64}', record.get('fingerprint', ''))
                or not isinstance(record.get('app'), str) or not isinstance(record.get('state_dir'), str)
                or not under(record.get('package', ''), control / 'packages')):
            raise RuntimeError('Неверная запись сохранённой сборки.')
        safe_path(record['app']); safe_path(record['state_dir'])
    observation = value.get('observation')
    if observation is not None and (not isinstance(observation, dict)
          or not re.fullmatch(r'[a-f0-9]{64}', observation.get('fingerprint', ''))
          or not isinstance(observation.get('since'), (int, float))):
        raise RuntimeError('Неверная запись ожидания обновления.')
    if not isinstance(value.get('last_refresh', 0), (int, float)): raise RuntimeError('Неверное время проверки.')
    return value


def report(control, config, status, message, now, **extra):
    if config.get('scheduler_error'): extra['scheduler_warning'] = config['scheduler_error']
    result = dict(status=status, message=message, checked_at=now, **extra)
    write_json(control / 'config.json', config)
    write_json(control / 'result.json', result)
    return result


def states_for(config, adapter):
    # Recorded builds are inspected by their own pinned core, never a new
    # catalog that may no longer contain their historical release profile.
    recorded = {r['state_dir'] for r in config['records']}
    values = []
    if config.get('extra_state') and config['extra_state'] not in recorded: values.append(Path(config['extra_state']))
    default = adapter.default_state()
    if str(default) not in recorded: values.append(default)
    return list(dict.fromkeys(values))


def inspect_current(config):
    adapter = adapter_for(Path(config['active_package']))
    app = safe_path(adapter.discover(config.get('app')))
    for record in reversed(config['records']):
        if record['app'] != str(app): continue
        old = adapter_for(Path(record['package']))
        saved = old.probe(app, [Path(record['state_dir'])])
        if saved['kind'] == 'patched': return old, app, saved
        if saved['kind'] == 'recovery':
            if saved.get('reason') == 'app-changed':
                current = adapter.probe(app, states_for(config, adapter))
                if current['kind'] == 'source': return adapter, app, current
            return old, app, saved
    return adapter, app, adapter.probe(app, states_for(config, adapter))


def execute(argv):
    result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=900, **process_options())
    return result.returncode, (result.stdout + '\n' + result.stderr).strip()[-3000:]


def add_record(config, info, state_dir):
    record = {'app': info['app'], 'fingerprint': info['fingerprint'], 'state_dir': str(state_dir),
              'package': config['active_package'], 'version': info['version'], 'outcome': 'adopted'}
    if not any(r['state_dir'] == str(state_dir) and r['app'] == info['app'] for r in config['records']):
        config['records'].append(record)


def file_snapshot(app):
    # Key resource metadata is cheap; full hashes are required again on every
    # change or before mutation. Don't reread entire macOS bundles every tick.
    names = ['Contents/Resources/app.asar', 'Contents/Info.plist', 'Contents/MacOS/Claude',
             'Contents/MacOS/Antigravity', 'Contents/_CodeSignature/CodeResources',
             'resources/app.asar', 'resources/en-US.json', 'claude.exe', 'Antigravity.exe']
    values = []
    for name in ['.'] + names:
        path = safe_path(app / name)
        try:
            info = path.stat()
            values.append([name, info.st_ino, info.st_dev, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
        except FileNotFoundError: values.append([name, None])
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def same_path(first, second):
    return os.path.normcase(str(safe_path(first))) == os.path.normcase(str(safe_path(second)))


def requested_app_matches(config, adapter, app_hint):
    return not app_hint or same_path(adapter.discover(app_hint), adapter.discover(config.get('app')))


def stop_schedule(control, config):
    # A disabled configuration prevents mutation even if the OS scheduler is
    # temporarily unavailable. Do not block a user's recovery on its removal.
    try:
        service.remove(config['repository'], control, config.get('service'))
        config.pop('scheduler_error', None)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        config['scheduler_error'] = str(error)[-1000:]


def check(control, now=None, manual=False, app_hint=None, state_hint=None):
    now = time.time() if now is None else now
    with mutex(control):
        config = config_at(control)
        if not config['enabled']: return report(control, config, 'disabled', 'Автовосстановление выключено.', now)
        due_refresh = now - config.get('last_refresh', 0) >= REFRESH_SECONDS
        initial_adapter = adapter_for(Path(config['active_package']))
        if not requested_app_matches(config, initial_adapter, app_hint):
            raise RuntimeError('Выбрано другое приложение. Укажи его отдельную папку --control-dir.')
        if state_hint:
            states = states_for(config, initial_adapter) + [Path(r['state_dir']) for r in config['records']]
            if not any(same_path(state_hint, state) for state in states):
                raise RuntimeError('Выбрано другое состояние. Используй папку состояния, указанную при включении.')
        initial_app = safe_path(initial_adapter.discover(config.get('app')))
        observed_metadata = file_snapshot(initial_app)
        if config.get('file_observation') == observed_metadata and not due_refresh and (control / 'result.json').exists():
            previous_result = read_json(control / 'result.json')
            if previous_result.get('status') in {'installed', 'already-patched'}:
                return report(control, config, 'already-patched', 'Перевод установлен; файлы приложения не менялись.', now, app=str(initial_app), state_dir=previous_result.get('state_dir'))
        adapter, app, info = inspect_current(config)
        if under(control, app) or under(app, control): raise RuntimeError('Служебная папка должна быть вне приложения.')
        # Refresh even with supported builds: later vendor updates need newly published profiles.
        # The network is optional and failures never discard a working local package.
        if now - config.get('last_refresh', 0) >= REFRESH_SECONDS:
            config['last_refresh'] = now
            previous = config['active_package']
            try:
                candidate = refresh(control, config)
                config['active_package'] = str(candidate)
                config.pop('refresh_error', None)
                adapter, app, info = inspect_current(config)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                config['active_package'] = previous
                config['refresh_error'] = str(error)[-1000:]
        kind = info['kind']
        if kind != 'source':
            config['observation'] = None
            if kind == 'patched':
                if info.get('state_dir'):
                    add_record(config, info, info['state_dir'])
                    for record in config['records']:
                        if record['state_dir'] == info['state_dir']: record['outcome'] = 'installed'
                config['file_observation'] = file_snapshot(app)
                return report(control, config, 'already-patched', 'Перевод уже установлен.', now, app=str(app), state_dir=info.get('state_dir'))
            status = {'busy': 'waiting-app', 'unknown': 'unsupported', 'recovery': 'needs-recovery'}.get(kind)
            if status is None: raise RuntimeError('Неизвестный результат проверки приложения.')
            return report(control, config, status, info.get('message', kind), now, app=str(app))
        if not re.fullmatch(r'[a-f0-9]{64}', info['fingerprint']): raise RuntimeError('Некорректная контрольная сумма сборки.')
        interrupted = next((r for r in config['records'] if r['app'] == str(app)
                            and r['fingerprint'] == info['fingerprint'] and r.get('outcome') in {'pending', 'failed'}), None)
        if interrupted:
            config['observation'] = None
            return report(control, config, 'needs-recovery', 'Предыдущая попытка прервана. Проверь сохранённое состояние и выбери ручной откат.', now,
                          app=str(app), state_dir=interrupted['state_dir'])
        interactive_admin = manual and sys.stdin.isatty() and sys.platform != 'win32'
        if not info['writable'] and not interactive_admin:
            observation = config.get('observation')
            if not observation or observation['fingerprint'] != info['fingerprint'] or now < observation['since']:
                config['observation'] = {'fingerprint': info['fingerprint'], 'since': now}
            record = next((r for r in config['records'] if r['app'] == str(app)
                           and r['fingerprint'] == info['fingerprint'] and r.get('outcome') == 'manual-required'), None)
            if record: state_dir = Path(record['state_dir'])
            else:
                state_dir = control / 'states' / (info['fingerprint'] + '-' + uuid.uuid4().hex)
                add_record(config, info, state_dir); config['records'][-1]['outcome'] = 'manual-required'
            return report(control, config, 'needs-admin', 'Нужны права на файлы приложения. Автовосстановление не повышает права.', now,
                          app=str(app), state_dir=str(state_dir), manual_command=adapter.command('install', app, state_dir, config['approved_signature']))
        observation = config.get('observation')
        if (not observation or observation['fingerprint'] != info['fingerprint']
                or now < observation['since'] or now - observation['since'] < STABLE_SECONDS):
            if not observation or observation['fingerprint'] != info['fingerprint'] or now < observation['since']:
                config['observation'] = {'fingerprint': info['fingerprint'], 'since': now}
            return report(control, config, 'waiting', 'Жду стабильную сборку после завершения обновления.', now, app=str(app))
        # Re-read immediately before mutation. The core also checks processes and source hashes.
        rediscovered = safe_path(adapter.discover(config.get('app')))
        fresh = adapter.probe(rediscovered, states_for(config, adapter))
        if rediscovered != app or fresh['kind'] != 'source' or fresh['fingerprint'] != info['fingerprint'] or (not fresh['writable'] and not interactive_admin):
            config['observation'] = None
            return report(control, config, 'waiting', 'Установка изменилась; проверю ещё раз позже.', now, app=str(app))
        state_dir = control / 'states' / (info['fingerprint'] + '-' + uuid.uuid4().hex)
        private_dir(state_dir)
        # Journal before invoking the core. An interrupted process can be recovered manually.
        add_record(config, info, state_dir)
        config['records'][-1]['outcome'] = 'pending'
        write_json(control / 'config.json', config)
        argv = adapter.command('install', app, state_dir, config['approved_signature'])
        try:
            if not fresh['writable'] and interactive_admin:
                print('[i] Для выбранного применения нужен доступ администратора; введи пароль в Терминале.', flush=True)
                code = subprocess.run(['sudo', '--'] + argv, timeout=900).returncode
                output = 'Ручное применение завершено.' if code == 0 else 'Ручное применение не завершено.'
            else: code, output = execute(argv)
        except (OSError, subprocess.SubprocessError) as error:
            code, output = 1, str(error)
        config['records'][-1]['outcome'] = 'failed' if code else 'installed'
        config['observation'] = None
        if code:
            return report(control, config, 'failed', 'Повторное применение остановлено. Резервные копии сохранены.', now,
                          app=str(app), state_dir=str(state_dir), detail=output)
        verified = adapter.probe(app, [state_dir])
        if verified['kind'] != 'patched':
            config['records'][-1]['outcome'] = 'failed'
            return report(control, config, 'needs-recovery', 'Ядро завершилось, но перевод не подтверждён. Проверь сохранённое состояние.', now, state_dir=str(state_dir))
        config['file_observation'] = file_snapshot(app)
        return report(control, config, 'installed', 'Русский интерфейс восстановлен.', now,
                      app=str(app), state_dir=str(state_dir), detail=output)


def enable(control, repository, app=None, extra_state=None, approved=False):
    if not approved and (sys.platform == 'darwin' or (sys.platform == 'win32' and repository == 'claude-russian')):
        raise RuntimeError('Нужно явное согласие --approve-signature на повторное применение патча.')
    if under(control, ROOT) or under(ROOT, control): raise RuntimeError('Служебная папка должна быть отдельно от исходников.')
    found_before_copy = safe_path(adapter_for(ROOT).discover(app))
    if under(control, found_before_copy) or under(found_before_copy, control):
        raise RuntimeError('Служебная папка должна быть вне приложения.')
    private_dir(control)
    with mutex(control):
        saved = config_at(control) if (control / 'config.json').exists() else None
        if saved and saved['repository'] != repository: raise RuntimeError('Папка используется другим русификатором.')
        package = snapshot(ROOT, control / 'packages', repository)
        adapter = adapter_for(package)
        found = safe_path(adapter.discover(app))
        if under(control, found) or under(found, control): raise RuntimeError('Служебная папка должна быть вне приложения.')
        # Initial probe verifies location/state but may legitimately be busy or a future build.
        states = [adapter.default_state()] + ([Path(extra_state)] if extra_state else [])
        initial = adapter.probe(found, states)
        if initial['kind'] == 'recovery': raise RuntimeError(initial['message'])
        config = saved or {'schema': 1, 'repository': repository, 'records': []}
        config.update(enabled=False, approved_signature=bool(approved), app=str(found), extra_state=extra_state,
                      active_package=str(package), observation=None, last_refresh=0)
        if initial['kind'] == 'patched' and initial.get('state_dir'): add_record(config, initial, initial['state_dir'])
        # A private immutable runner survives deletion/movement of the downloaded installer.
        runner_hash = hashlib.sha256(b''.join((ROOT / 'updater' / name).read_bytes() for name in ('manager.py', 'package.py', 'service.py'))).hexdigest()
        runner_parent = control / 'runners' / runner_hash
        runner_files = {name: (ROOT / 'updater' / name).read_bytes() for name in ('manager.py', 'package.py', 'service.py')}
        ready = runner_parent.exists() and all(safe_path(runner_parent / name).is_file() and
                    (runner_parent / name).read_bytes() == data for name, data in runner_files.items())
        if not ready:
            staging = private_dir(control / 'runners' / ('.staging-' + uuid.uuid4().hex))
            for name in ('manager.py', 'package.py', 'service.py'):
                target = staging / name
                with target.open('xb') as dst:
                    dst.write(runner_files[name]); dst.flush(); os.fsync(dst.fileno())
                if os.name != 'nt': target.chmod(0o600)
            if runner_parent.exists():
                safe_path(runner_parent).rename(runner_parent.with_name(runner_hash + '.incomplete-' + uuid.uuid4().hex))
            staging.rename(runner_parent)
        config['runner'] = str(runner_parent / 'manager.py')
        # Disabled config before registration ensures an immediate first run cannot mutate early.
        config['service'] = service.record_for(repository, control)
        write_json(control / 'config.json', config)
        try:
            schedule = service.register(repository, control, Path(sys.executable), runner_parent / 'manager.py')
        except (OSError, RuntimeError, subprocess.SubprocessError):
            config['enabled'] = False; write_json(control / 'config.json', config)
            try: service.remove(repository, control, config['service'])
            except (OSError, RuntimeError, subprocess.SubprocessError): pass
            raise
        config.update(service=schedule, enabled=True)
        config.pop('scheduler_error', None)
        write_json(control / 'config.json', config)
        return report(control, config, 'enabled', 'Автовосстановление включено. Проверка каждые две минуты после входа в систему.', time.time())


def disable(control):
    if not (control / 'config.json').exists(): return {'status': 'disabled', 'message': 'Автовосстановление ещё не включалось.'}
    with mutex(control):
        config = config_at(control); config['enabled'] = False; config['observation'] = None
        write_json(control / 'config.json', config)
        stop_schedule(control, config)
        return report(control, config, 'disabled', 'Автовосстановление выключено; перевод и копии сохранены.', time.time())


def restore(control, manual=False, app_hint=None, state_hint=None):
    if not (control / 'config.json').exists(): return 3, {'message': 'Нет записи автовосстановления.'}
    with mutex(control):
        config = config_at(control)
        if not requested_app_matches(config, adapter_for(Path(config['active_package'])), app_hint):
            return 3, {'message': 'Выбранное приложение имеет отдельное состояние отката.'}
        config['enabled'] = False; config['observation'] = None
        write_json(control / 'config.json', config)
        stop_schedule(control, config)
        adapter, app, info = inspect_current(config)
        if state_hint and (not info.get('state_dir') or not same_path(state_hint, info['state_dir'])):
            return 3, report(control, config, 'disabled', 'Автовосстановление выключено; используй выбранное вручную состояние отката.', time.time())
        if info['kind'] == 'source':
            matching = [r for r in config['records'] if r['app'] == str(app) and r['fingerprint'] == info['fingerprint']]
            if matching:
                for record in matching: record['outcome'] = 'restored'
                return 0, report(control, config, 'restored', 'Исходные файлы уже на месте; незавершённая попытка снята с повторных проверок. Копии сохранены.', time.time())
        if info['kind'] not in {'patched', 'recovery'} or not info.get('state_dir'):
            return 3, report(control, config, 'disabled', 'Для текущих файлов нет актуальной автоматической записи отката.', time.time())
        record = next((r for r in config['records'] if r['state_dir'] == info['state_dir'] and r['app'] == str(app)), None)
        if record: adapter = adapter_for(Path(record['package']))
        argv = adapter.command('restore', app, Path(info['state_dir']), config['approved_signature'])
        if not info['writable']:
            if manual and sys.stdin.isatty() and sys.platform != 'win32':
                print('[i] Для выбранного отката нужен доступ администратора; введи пароль в Терминале.', flush=True)
                code = subprocess.run(['sudo', '--'] + argv, timeout=900).returncode
                output = 'Ручной откат завершён.' if code == 0 else 'Ручной откат не завершён.'
            else:
                return 1, report(control, config, 'needs-admin', 'Нужны права на файлы приложения для отката.', time.time(), manual_command=argv)
        else: code, output = execute(argv)
        if code == 0 and record: record['outcome'] = 'restored'
        return code, report(control, config, 'restored' if code == 0 else 'needs-recovery', output, time.time(), state_dir=info['state_dir'])


def main():
    parser = argparse.ArgumentParser(description='Автовосстановление русского интерфейса после обновления')
    parser.add_argument('action', choices=['enable', 'disable', 'status', 'check', 'restore'])
    parser.add_argument('--control-dir'); parser.add_argument('--app'); parser.add_argument('--state-dir')
    parser.add_argument('--approve-signature', action='store_true'); parser.add_argument('--manual', action='store_true')
    parser.add_argument('--quiet', action='store_true'); parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt' and os.geteuid() == 0: raise RuntimeError('Автовосстановление запускается из обычного аккаунта без sudo.')
    os.umask(0o077)
    if args.control_dir:
        control = safe_path(args.control_dir)
        if (control / 'config.json').exists(): repository = config_at(control)['repository']
        else: repository = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))['repository']
    else:
        repository = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))['repository']
        control = safe_path(default_control(repository))
    code = 0
    if args.action == 'enable': result = enable(control, repository, args.app, args.state_dir, args.approve_signature)
    elif args.action == 'disable': result = disable(control)
    elif args.action == 'restore': code, result = restore(control, args.manual, args.app, args.state_dir)
    elif args.action == 'check':
        if not (control / 'config.json').exists(): result = {'status': 'disabled', 'message': 'Сначала включи автовосстановление.'}
        else: result = check(control, manual=args.manual, app_hint=args.app, state_hint=args.state_dir)
    else:
        if not (control / 'config.json').exists(): result = {'status': 'disabled', 'message': 'Автовосстановление ещё не включалось.'}
        else:
            config = config_at(control)
            result = read_json(control / 'result.json') if (control / 'result.json').exists() else {'message': 'Проверка ещё не выполнялась.'}
            result = dict(result, enabled=config['enabled'], control_dir=str(control), records=config['records'])
    if not args.quiet:
        if args.json: print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result.get('message', 'Проверка завершена.'))
            if 'enabled' in result: print('Автовосстановление: ' + ('включено' if result['enabled'] else 'выключено'))
            if result.get('app'): print('Приложение: ' + result['app'])
            if result.get('state_dir'): print('Каталог отката: ' + result['state_dir'])
            if result.get('detail'): print(result['detail'])
            if result.get('scheduler_warning'):
                print('[i] Автовосстановление выключено в настройках, но остановка расписания не подтверждена: ' + result['scheduler_warning'])
            if result.get('manual_command'):
                import shlex
                command = subprocess.list2cmdline(result['manual_command']) if os.name == 'nt' else shlex.join(result['manual_command'])
                print('Для ручного применения из терминала с нужными правами:')
                print(command)
            if result.get('control_dir'): print('Настройки и копии: ' + result['control_dir'])
            if result.get('checked_at'): print('Последняя проверка: ' + time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(result['checked_at'])))
    return code


if __name__ == '__main__':
    try: sys.exit(main())
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('[Ошибка] ' + str(error), file=sys.stderr); sys.exit(1)
