"""No apps or schedules: adapter regressions use private temporary installations."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import shlex
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def adapter_module():
    spec = importlib.util.spec_from_file_location('ag_updater_adapter', ROOT / 'updater/adapter.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AdapterContract(unittest.TestCase):
    def test_commands_keep_signature_consent_explicit(self):
        adapter = adapter_module()
        with patch.object(adapter, 'SYSTEM', 'macos'):
            approved = adapter.command('install', Path('/tmp/App.app'), Path('/tmp/state'), True)
            denied = adapter.command('install', Path('/tmp/App.app'), Path('/tmp/state'), False)
            restore = adapter.command('restore', Path('/tmp/App.app'), Path('/tmp/state'), True)
        self.assertIn('--approve-local-signature', approved)
        self.assertNotIn('--approve-local-signature', denied)
        self.assertNotIn('--approve-local-signature', restore)
        self.assertEqual(approved[1], str(ROOT / 'macos/patch.py'))
        with patch.object(adapter, 'SYSTEM', 'windows'):
            self.assertIn('--approve-exe-signature', adapter.command('install', Path('/tmp/App'), Path('/tmp/state'), True))

    def test_portable_state_checks_every_modified_file_and_backup(self):
        adapter = adapter_module()
        spec = importlib.util.spec_from_file_location('ag_portable_test', ROOT / 'portable/patch.py')
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        core.SYSTEM = 'linux'
        core.FILES = ['resources/app.asar']
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temporary:
            app = Path(temporary) / 'app'
            state_dir = Path(temporary) / 'state'
            (app / 'resources').mkdir(parents=True)
            state_dir.mkdir(mode=0o700)
            (app / 'resources/app.asar').write_bytes(b'patched')
            source_hash, patched_hash = core.sha(b'source'), core.sha(b'patched')
            profile = {'platform': 'linux', 'arch': 'x64', 'version': '2.19.1', 'asar_sha256': source_hash}
            saved = {'schema': 1, 'product': 'antigravity-russian-portable', 'platform': 'linux',
                     'profile': 'linux-x64-2.19.1', 'phase': 'installed', 'app': str(app),
                     'backup': 'backup-' + 'a' * 32, 'files': {'resources/app.asar': {
                         'original': source_hash, 'patched': patched_hash, 'before': source_hash,
                         'mode': 0o644, 'uid': os.getuid(), 'gid': os.getgid()}}}
            record = state_dir / 'state.json'
            record.write_text(json.dumps(saved)); record.chmod(0o600)
            for kind, data in [('original', b'source'), ('patched', b'patched')]:
                file = state_dir / saved['backup'] / kind / 'resources/app.asar'
                file.parent.mkdir(parents=True, mode=0o700)
                file.write_bytes(data); file.chmod(0o600)
            with patch.object(adapter, 'SYSTEM', 'linux'), patch.object(adapter, '_core', return_value=core), \
                    patch.object(core, 'profiles', return_value={saved['profile']: profile}), \
                    patch.object(core, 'require_closed'), patch.object(core, 'match_profile', side_effect=RuntimeError('unknown')), \
                    patch('asar.Asar') as asar:
                asar.return_value.read.return_value = b'{"version": "2.19.1"}'
                good = adapter.probe(app, [state_dir])
                self.assertEqual(good['kind'], 'patched', good)
                self.assertEqual(good['state_dir'], str(state_dir))
                # A modified app is never treated as a registered patch.
                (app / 'resources/app.asar').write_bytes(b'tampered')
                changed = adapter.probe(app, [state_dir])
                self.assertEqual(changed['kind'], 'recovery')
                self.assertEqual(changed['reason'], 'app-changed')
                (app / 'resources/app.asar').write_bytes(b'patched')
                (state_dir / saved['backup'] / 'original/resources/app.asar').write_bytes(b'bad')
                self.assertEqual(adapter.probe(app, [state_dir])['kind'], 'recovery')
                # An old broken backup cannot be restored over a supported update.
                (app / 'resources/app.asar').write_bytes(b'new-source')
                with patch.object(core, 'match_profile', return_value=('linux-x64-2.19.1', profile)):
                    refreshed = adapter.probe(app, [state_dir])
                self.assertEqual(refreshed['kind'], 'recovery', refreshed)
                self.assertNotIn('reason', refreshed)
                # A published same-version rebuild is accepted once historical
                # backup integrity has been checked. It gets a fresh fingerprint.
                (state_dir / saved['backup'] / 'original/resources/app.asar').write_bytes(b'source')
                with patch.object(core, 'match_profile', return_value=('linux-x64-2.19.1', profile)):
                    refreshed = adapter.probe(app, [state_dir])
                self.assertEqual(refreshed['kind'], 'source', refreshed)
                self.assertNotEqual(refreshed['fingerprint'], good['fingerprint'])
                saved['phase'] = 'prepared'
                record.write_text(json.dumps(saved))
                self.assertEqual(adapter.probe(app, [state_dir])['kind'], 'recovery')


    def test_macos_patch_requires_all_four_files_and_bundle_backup(self):
        adapter = adapter_module()
        spec = importlib.util.spec_from_file_location('ag_mac_state_test', ROOT / 'macos/patch.py')
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temporary:
            app, state_dir = Path(temporary) / 'Antigravity.app', Path(temporary) / 'state'
            state_dir.mkdir(mode=0o700)
            backup = state_dir / ('backup-' + 'b' * 32 + '.app')
            backup.mkdir(mode=0o700)
            originals, patched = {}, {}
            for index, name in enumerate(core.MODIFIED_FILES):
                before, after = b'original-' + bytes([index]), b'patched-' + bytes([index])
                for root, data in [(app, after), (backup, before)]:
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                originals[name] = {'sha256': core.digest(before), 'mode': 0o644}
                patched[name] = {'sha256': core.digest(after), 'mode': 0o644}
            version = core.SUPPORTED_VERSIONS[0]
            saved = {'schema': 1, 'app': str(app), 'status': 'installed', 'version': version,
                     'backup': backup.name, 'original_files': originals, 'patched_files': patched}
            record = state_dir / 'state.json'
            record.write_text(json.dumps(saved)); record.chmod(0o600)
            with patch.object(adapter, 'SYSTEM', 'macos'), patch.object(adapter, '_core', return_value=core), \
                    patch.object(core, 'app_info', return_value={'CFBundleShortVersionString': version}), \
                    patch.object(core, 'compatible', side_effect=lambda platform, release, value: value == originals[core.MODIFIED_FILES[0]]['sha256']), \
                    patch.object(core, 'running', return_value=False):
                self.assertEqual(adapter.probe(app, [state_dir])['kind'], 'patched')
                (app / core.MODIFIED_FILES[-1]).write_bytes(b'tampered-signature')
                changed = adapter.probe(app, [state_dir])
                self.assertEqual(changed['kind'], 'recovery')
                self.assertEqual(changed['reason'], 'app-changed')
                (app / core.MODIFIED_FILES[-1]).write_bytes(b'patched-' + bytes([3]))
                (backup / core.MODIFIED_FILES[-1]).unlink()
                self.assertEqual(adapter.probe(app, [state_dir])['kind'], 'recovery')


    def test_portable_incomplete_operation_survives_missing_or_corrupt_asar(self):
        adapter = adapter_module()
        spec = importlib.util.spec_from_file_location('ag_portable_incomplete_test', ROOT / 'portable/patch.py')
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        core.SYSTEM, core.FILES = 'linux', ['resources/app.asar']
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temporary:
            app, state_dir = Path(temporary) / 'app', Path(temporary) / 'state'
            (app / 'resources').mkdir(parents=True)
            state_dir.mkdir(mode=0o700)
            source_hash, patched_hash = core.sha(b'source'), core.sha(b'patched')
            profile = {'platform': 'linux', 'arch': 'x64', 'version': '2.19.1', 'asar_sha256': source_hash}
            saved = {'schema': 1, 'product': 'antigravity-russian-portable', 'platform': 'linux',
                     'profile': 'linux-x64-2.19.1', 'phase': 'prepared', 'app': str(app),
                     'backup': 'backup-' + 'c' * 32, 'files': {'resources/app.asar': {
                         'original': source_hash, 'patched': patched_hash, 'before': source_hash,
                         'mode': 0o644, 'uid': os.getuid(), 'gid': os.getgid()}}}
            record = state_dir / 'state.json'
            with patch.object(adapter, 'SYSTEM', 'linux'), patch.object(adapter, '_core', return_value=core), \
                    patch.object(core, 'profiles', return_value={saved['profile']: profile}), \
                    patch.object(core, 'require_closed'):
                for phase in ('prepared', 'restoring'):
                    for damaged in ('missing', 'corrupt'):
                        with self.subTest(phase=phase, asar=damaged):
                            saved['phase'] = phase
                            record.write_text(json.dumps(saved)); record.chmod(0o600)
                            archive = app / 'resources/app.asar'
                            if damaged == 'missing':
                                archive.unlink(missing_ok=True)
                            else:
                                archive.write_bytes(b'invalid-asar')
                            result = adapter.probe(app, [state_dir])
                            self.assertEqual(result['kind'], 'recovery', result)
                            self.assertEqual(result['state_dir'], str(state_dir))
                            self.assertNotIn('reason', result)

    def test_macos_incomplete_operation_survives_missing_asar(self):
        adapter = adapter_module()
        spec = importlib.util.spec_from_file_location('ag_mac_incomplete_test', ROOT / 'macos/patch.py')
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temporary:
            app, state_dir = Path(temporary) / 'Antigravity.app', Path(temporary) / 'state'
            app.mkdir()
            state_dir.mkdir(mode=0o700)
            originals = {name: {'sha256': core.digest(b'original-' + bytes([index])), 'mode': 0o644}
                         for index, name in enumerate(core.MODIFIED_FILES)}
            version = core.SUPPORTED_VERSIONS[0]
            saved = {'schema': 1, 'app': str(app), 'status': 'prepared', 'version': version,
                     'backup': 'backup-' + 'd' * 32 + '.app', 'original_files': originals,
                     'patched_files': originals}
            record = state_dir / 'state.json'
            record.write_text(json.dumps(saved)); record.chmod(0o600)
            with patch.object(adapter, 'SYSTEM', 'macos'), patch.object(adapter, '_core', return_value=core), \
                    patch.object(core, 'app_info', return_value={'CFBundleShortVersionString': version}), \
                    patch.object(core, 'compatible', return_value=True), patch.object(core, 'running', return_value=False):
                result = adapter.probe(app, [state_dir])
                self.assertEqual(result['kind'], 'recovery', result)
                self.assertEqual(result['state_dir'], str(state_dir))
                self.assertNotIn('reason', result)

    def test_macos_new_source_requires_valid_historical_backup(self):
        adapter = adapter_module()
        spec = importlib.util.spec_from_file_location('ag_mac_new_source_test', ROOT / 'macos/patch.py')
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temporary:
            app, state_dir = Path(temporary) / 'Antigravity.app', Path(temporary) / 'state'
            state_dir.mkdir(mode=0o700)
            backup = state_dir / ('backup-' + 'e' * 32 + '.app')
            backup.mkdir(mode=0o700)
            originals, patched = {}, {}
            for index, name in enumerate(core.MODIFIED_FILES):
                before, after = b'original-' + bytes([index]), b'patched-' + bytes([index])
                for root, data in [(app, b'updated-' + bytes([index])), (backup, before)]:
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                originals[name] = {'sha256': core.digest(before), 'mode': 0o644}
                patched[name] = {'sha256': core.digest(after), 'mode': 0o644}
            version = core.SUPPORTED_VERSIONS[0]
            saved = {'schema': 1, 'app': str(app), 'status': 'installed', 'version': version,
                     'backup': backup.name, 'original_files': originals, 'patched_files': patched}
            record = state_dir / 'state.json'
            record.write_text(json.dumps(saved)); record.chmod(0o600)
            with patch.object(adapter, 'SYSTEM', 'macos'), patch.object(adapter, '_core', return_value=core), \
                    patch.object(core, 'app_info', return_value={'CFBundleShortVersionString': version}), \
                    patch.object(core, 'compatible', return_value=True), patch.object(core, 'signature_kind', return_value='google'), \
                    patch.object(core, 'running', return_value=False):
                (backup / core.MODIFIED_FILES[-1]).write_bytes(b'corrupt')
                result = adapter.probe(app, [state_dir])
                self.assertEqual(result['kind'], 'recovery', result)
                self.assertNotIn('reason', result)
                (backup / core.MODIFIED_FILES[-1]).write_bytes(b'original-' + bytes([3]))
                accepted = adapter.probe(app, [state_dir])
                self.assertEqual(accepted['kind'], 'source', accepted)
                self.assertNotEqual(accepted['fingerprint'], adapter._fingerprint(app, version,
                                    {name: item['sha256'] for name, item in originals.items()}))

    @unittest.skipIf(os.name == 'nt', 'Bash launcher is for macOS/Linux')
    def test_macos_shell_empty_arguments_and_restore_fallback(self):
        script = (ROOT / 'install.sh').read_text()
        functions = script[script.index('run_patch() {'):script.index('\nif [ "$ACTION" != "menu" ]')]
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as temporary:
            stub = Path(temporary) / 'python-stub'
            stub.write_text('#!' + sys.executable + '\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            stub.chmod(0o700)
            variables = 'PYTHON_BIN=' + shlex.quote(str(stub)) + '; ROOT=/temporary; PATCH=/temporary/patch.py; REPO=antigravity-russian; DEFAULT_APP=/temporary/App.app;\n'
            result = subprocess.run(['/bin/bash', '-euc', variables + functions + '\nperform_action status'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), ['/temporary/patch.py', 'status'])
            overrides = '\nrun_auto() { printf "auto:%s\\n" "$*"; return 3; }\nrun_patch() { printf "patch:%s\\n" "$*"; }\nperform_action restore\n'
            result = subprocess.run(['/bin/bash', '-euc', variables + functions + overrides], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['auto:restore --manual', 'patch:restore'])
            refused = overrides.replace('return 3', 'return 1')
            result = subprocess.run(['/bin/bash', '-euc', variables + functions + refused], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout.splitlines(), ['auto:restore --manual'])
            check = '\nrun_auto() { printf "auto:%s\\n" "$*"; }\nperform_action auto-check\n'
            result = subprocess.run(['/bin/bash', '-euc', variables + functions + check], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['auto:check --manual'])

    def test_macos_guard_catches_helper_and_updater_paths(self):
        spec = importlib.util.spec_from_file_location('ag_mac_guard_test', ROOT / 'macos/patch.py')
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        from types import SimpleNamespace
        app = Path('/Applications/Antigravity.app')
        for process in [
            '42 /Applications/Antigravity.app/Contents/Frameworks/Antigravity Helper.app/Contents/MacOS/Antigravity Helper\n',
            '43 /Users/test/Library/Caches/com.google.antigravity.ShipIt/ShipIt\n',
        ]:
            with patch.object(core.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=process)):
                self.assertTrue(core.running(app))
        with patch.object(core.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')):
            with self.assertRaises(RuntimeError):
                core.running(app)


if __name__ == '__main__':
    unittest.main()
