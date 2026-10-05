"""Read-only release/state adapter for the Antigravity patch cores."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SYSTEM = 'macos' if sys.platform == 'darwin' else 'windows' if sys.platform == 'win32' else 'linux'
_CORE = None


def _core():
    global _CORE
    if _CORE is None:
        path = ROOT / ('macos/patch.py' if SYSTEM == 'macos' else 'portable/patch.py')
        sys.path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location('_antigravity_updater_core', path)
        _CORE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_CORE)
    return _CORE


def default_state():
    core = _core()
    if SYSTEM == 'macos':
        return Path(core.original_user().pw_dir) / 'Library/Application Support/antigravity-russian/state'
    return core.identity()[2]


def discover(app_hint=None):
    core = _core()
    if SYSTEM == 'macos':
        return core.safe_path(core.absolute_path(app_hint or core.DEFAULT_APP, core.original_user()))
    return core.discover(SimpleNamespace(app=str(app_hint) if app_hint else None), None)


def command(action, app, state_dir, approved=False):
    if action not in {'install', 'restore', 'status'}:
        raise ValueError('Неизвестное действие ядра патча.')
    entry = ROOT / ('macos/patch.py' if SYSTEM == 'macos' else 'portable/patch.py')
    argv = [sys.executable, str(entry), action, '--app', str(app), '--state-dir', str(state_dir)]
    if approved and action == 'install':
        argv.append('--approve-local-signature' if SYSTEM == 'macos' else '--approve-exe-signature')
    return argv


def _fingerprint(app, version, files):
    body = json.dumps({'app': os.path.normcase(str(app)), 'version': version, 'files': files}, sort_keys=True).encode()
    return hashlib.sha256(body).hexdigest()


def _state_for_app(core, directory, app):
    """Validate records even when they belong to another installation."""
    directory = core.safe_path(Path(directory), must_exist=False) if SYSTEM == 'macos' else core.safe_path(Path(directory))
    state_file = directory / 'state.json'
    if not state_file.exists() and not state_file.is_symlink():
        return None
    if SYSTEM == 'macos':
        user = core.original_user()
        core.ensure_private_state(state_file, user)
        if state_file.stat().st_size > 128 * 1024:
            raise RuntimeError('Некорректный размер файла состояния.')
        raw = json.loads(state_file.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or not isinstance(raw.get('app'), str):
            raise RuntimeError('Некорректная запись состояния.')
        recorded_app = Path(raw['app'])
        if not recorded_app.is_absolute() or str(recorded_app) != os.path.abspath(raw['app']):
            raise RuntimeError('Некорректный путь приложения в состоянии.')
        core.safe_path(recorded_app, must_exist=False)
        saved = core.read_state(directory, recorded_app, user)
    else:
        saved = core.load_state(directory, core.identity()[0])
    if saved and os.path.normcase(saved['app']) == os.path.normcase(str(app)):
        return saved
    return None


def _validated_backup(core, directory, saved):
    """A vendor update never excuses damage to the previous rollback copy."""
    if SYSTEM == 'macos':
        backup = core.safe_path(Path(directory) / saved['backup'])
        if not backup.is_dir():
            raise RuntimeError('Полная резервная копия приложения отсутствует.')
        core.verify_records(backup, saved['original_files'])
        source = {name: value['sha256'] for name, value in saved['original_files'].items()}
        return saved['version'], source
    uid = core.identity()[0]
    for name in core.FILES:
        for kind in ('original', 'patched'):
            core.backup_bytes(Path(directory), saved, name, kind, uid)
    source = {name: value['original'] for name, value in saved['files'].items()}
    return core.profiles()[saved['profile']]['version'], source


def probe(app, state_dirs=()):
    core = _core()
    app = Path(app)
    observed = {}
    version = ''
    result = {'app': str(app), 'fingerprint': _fingerprint(app, '', {}), 'version': '',
              'kind': 'unknown', 'writable': False, 'state_dir': None, 'message': ''}
    try:
        app = core.safe_path(app)
        result['app'] = str(app)
        try:
            if SYSTEM == 'macos':
                if core.running(app):
                    raise RuntimeError('Antigravity или его обновление ещё работает.')
            else:
                core.require_closed(app)
        except Exception as error:
            result.update(kind='busy', message=str(error))
            return result
        observation_error = None
        try:
            if SYSTEM == 'macos':
                info = core.app_info(app)
                version = info['CFBundleShortVersionString']
                observed = {name: value['sha256'] for name, value in core.records(app).items()}
                try:
                    core.writable_app(app)
                    result['writable'] = True
                except (RuntimeError, OSError):
                    pass
            else:
                core.require_layout(app)
                observed = {name: core.file_hash(app / name) for name in core.FILES}
                from asar import Asar
                version = json.loads(Asar((app / 'resources/app.asar').read_bytes()).read('package.json'))['version']
                result['writable'] = all(os.access(app / name, os.W_OK) and os.access((app / name).parent, os.W_OK)
                                         for name in core.FILES)
        except Exception as error:
            # An interrupted replacement can leave absent or unreadable files.
            # Check its journal before treating this as an unsupported build.
            observation_error = error
        result.update(version=version, fingerprint=_fingerprint(app, version, observed))
        mismatched_state = None
        for directory in state_dirs:
            try:
                saved = _state_for_app(core, directory, app)
                if not saved:
                    continue
                phase = saved['status'] if SYSTEM == 'macos' else saved['phase']
                if phase in {'prepared', 'restoring'}:
                    result.update(kind='recovery', state_dir=str(directory), message='Предыдущая операция не завершена; нужен ручной откат.')
                    return result
                if phase != 'installed':
                    continue
                recorded_version, source = _validated_backup(core, directory, saved)
                expected = ({name: value['sha256'] for name, value in saved['patched_files'].items()}
                            if SYSTEM == 'macos' else {name: value['patched'] for name, value in saved['files'].items()})
                if observation_error or observed != expected:
                    if not version or version == recorded_version:
                        mismatched_state = (directory, observation_error or 'Файлы приложения изменились после установки перевода.')
                    # A known source from a published profile is accepted below,
                    # only after the historical backup has passed validation.
                    continue
                if SYSTEM == 'macos':
                    core.verify_records(app, saved['patched_files'])
                else:
                    core.require_hashes(app, saved, ['patched'])
                result.update(kind='patched', state_dir=str(directory), version=recorded_version,
                              fingerprint=_fingerprint(app, recorded_version, source), message='Перевод установлен; исходная копия проверена.')
                return result
            except Exception as error:
                result.update(kind='recovery', state_dir=str(directory), message=str(error))
                return result
        try:
            if observation_error:
                raise observation_error
            if SYSTEM == 'macos':
                if not core.compatible('macos', version, observed[core.MODIFIED_FILES[0]]):
                    raise RuntimeError('Для этой исходной сборки ещё не опубликован профиль совместимости.')
                core.signature_kind(app)
            else:
                core.match_profile(app)
        except Exception as error:
            if mismatched_state:
                result.update(kind='recovery', reason='app-changed', state_dir=str(mismatched_state[0]),
                              message=str(mismatched_state[1]))
            else:
                result.update(kind='unknown', message=str(error))
            return result
        result.update(kind='source', message='Поддерживаемая исходная сборка; перевод можно применить после закрытия приложения.')
    except Exception as error:
        result.update(kind='unknown', message=str(error))
    return result
