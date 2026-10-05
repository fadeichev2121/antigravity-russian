"""Registration and durable runners without real schedules or vendor apps."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'updater'))
import manager
import package


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / 'downloaded installer'
        self.control = self.base / 'private persistent updater'
        self.app = self.base / 'fixture original app'
        self.app.mkdir()
        (self.app / 'resources').mkdir()
        (self.app / 'resources/app.asar').write_bytes(b'fixture resource; never patched')
        self.default_state = self.base / 'readonly nonexistent state'
        for relative in ('updater/manager.py', 'updater/package.py', 'updater/service.py'):
            destination = self.source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)
        (self.source / 'manifest.json').write_text(json.dumps({
            'format': 1, 'repository': 'antigravity-russian', 'auto_update_api': 1,
        }))
        for relative in ('macos/patch.py', 'portable/patch.py'):
            destination = self.source / relative
            destination.parent.mkdir()
            destination.write_text("raise AssertionError('The patch core must never run in these tests')\n")
        # The manager's real subprocess bridge imports this fixture adapter.
        # Its only operations return metadata about the temporary installation.
        adapter = '''from pathlib import Path
import sys
def default_state():
    return Path({state})
def discover(hint=None):
    return Path(hint or {app})
def probe(app, states):
    return dict(app=str(app), kind='unknown', fingerprint='', version='',
                writable=False, state_dir=None, message='Fixture build is unsupported')
def command(*args):
    raise AssertionError('These fixtures never authorize an application mutation')
'''.format(state=repr(str(self.default_state)), app=repr(str(self.app)))
        (self.source / 'updater/adapter.py').write_text(adapter)
        self.addCleanup(patch.stopall)
        patch.object(manager, 'ROOT', self.source).start()
        self.register = patch.object(manager.service, 'register', side_effect=self.register_fixture).start()
        self.remove = patch.object(manager.service, 'remove').start()
        self.app_before = (self.app / 'resources/app.asar').read_bytes()

    def register_fixture(self, repository, control, python, runner):
        # Registration can immediately start a job: no application mutation is
        # authorized until the registration step succeeds.
        config = package.read_json(control / 'config.json')
        self.assertFalse(config['enabled'])
        self.assertTrue(runner.is_file())
        self.assertFalse(manager.under(runner, self.source))
        return manager.service.record_for(repository, control)

    def enable(self):
        return manager.enable(self.control, 'antigravity-russian', str(self.app), approved=True)

    def config(self):
        return manager.config_at(self.control)

    def test_enable_copies_runtime_and_real_adapter_bridge_is_readonly(self):
        self.assertEqual(self.enable()['status'], 'enabled')
        config = self.config()
        self.assertTrue(config['enabled'])
        self.register.assert_called_once()
        root = Path(config['active_package'])
        adapter = manager.adapter_for(root)
        self.assertEqual(adapter.default_state(), self.default_state)
        self.assertEqual(adapter.discover(str(self.app)), self.app)
        self.assertEqual(adapter.probe(self.app, [self.default_state])['kind'], 'unknown')
        self.assertFalse(self.default_state.exists())
        self.assertEqual((self.app / 'resources/app.asar').read_bytes(), self.app_before)
        self.assertTrue((root / 'macos/patch.py').is_file())
        self.assertTrue((root / 'portable/patch.py').is_file())

    def test_pinned_runner_and_package_survive_original_installer_removal(self):
        self.enable()
        config = self.config()
        runner = Path(config['runner'])
        pinned_package = Path(config['active_package'])
        config['last_refresh'] = time.time()
        package.write_json(self.control / 'config.json', config)
        # Delete only this test's temporary downloaded source, never a checkout.
        shutil.rmtree(self.source)
        self.assertTrue(runner.is_file())
        self.assertTrue((pinned_package / 'updater/adapter.py').is_file())
        status = subprocess.run([sys.executable, str(runner), 'status', '--control-dir',
                                 str(self.control), '--json'], capture_output=True, text=True,
                                encoding='utf-8', timeout=30)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertTrue(json.loads(status.stdout)['enabled'])
        # Execute the copied runner, but explicitly prohibit a network call.
        driver = '''import importlib.util, sys
from pathlib import Path
runner=Path(sys.argv[1]); sys.path.insert(0,str(runner.parent))
spec=importlib.util.spec_from_file_location('private_runner',runner)
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
def no_network(*args): raise AssertionError('No external network is allowed')
module.refresh=no_network
result=module.check(Path(sys.argv[2]))
import json
print(json.dumps(result))
'''
        result = subprocess.run([sys.executable, '-c', driver, str(runner), str(self.control)],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'unsupported')
        self.assertEqual((self.app / 'resources/app.asar').read_bytes(), self.app_before)
        self.assertFalse(self.default_state.exists())

    def test_explicit_reenable_replaces_runner_without_destroying_pinned_history(self):
        self.enable()
        first = self.config()
        first_runner = Path(first['runner'])
        first_bytes = first_runner.read_bytes()
        old_package = Path(first['active_package'])
        historical_state = self.control / 'states/old-build'
        package.private_dir(historical_state)
        marker = historical_state / 'backup.marker'
        marker.write_bytes(b'old original bytes')
        first['records'] = [{'app': str(self.app), 'fingerprint': 'c' * 64,
                             'state_dir': str(historical_state), 'package': str(old_package),
                             'version': 'fixture historical version', 'outcome': 'installed'}]
        package.write_json(self.control / 'config.json', first)
        with (self.source / 'updater/manager.py').open('a') as out:
            out.write('\n# explicit upgrade fixture\n')
        self.enable()
        second = self.config()
        self.assertNotEqual(second['runner'], first['runner'])
        self.assertNotEqual(second['active_package'], first['active_package'])
        self.assertEqual(first_runner.read_bytes(), first_bytes)
        self.assertTrue(old_package.is_dir())
        self.assertEqual(second['records'], first['records'])
        self.assertEqual(marker.read_bytes(), b'old original bytes')
        self.assertEqual(self.register.call_count, 2)

    def test_failed_registration_keeps_disabled_config_and_attempts_cleanup(self):
        self.register.side_effect = RuntimeError('fixture registration denied')
        with self.assertRaisesRegex(RuntimeError, 'registration denied'):
            self.enable()
        config = self.config()
        self.assertFalse(config['enabled'])
        self.remove.assert_called_once()
        self.assertTrue(Path(config['runner']).is_file())
        self.assertEqual((self.app / 'resources/app.asar').read_bytes(), self.app_before)

    def test_interrupted_runner_copy_is_repaired_or_rejected_before_registration(self):
        names = ('manager.py', 'package.py', 'service.py')
        source = {name: (self.source / 'updater' / name).read_bytes() for name in names}
        digest = hashlib.sha256(b''.join(source[name] for name in names)).hexdigest()
        runner = self.control / 'runners' / digest
        package.private_dir(runner)
        (runner / 'manager.py').write_bytes(b'# interrupted partial copy\n')
        def require_complete_runner(repository, control, python, entrypoint):
            for name in names:
                self.assertTrue((entrypoint.parent / name).is_file(),
                                'A partial runner must never reach scheduler registration')
                self.assertEqual((entrypoint.parent / name).read_bytes(), source[name],
                                 'Every registered runner byte must match its pinned source')
            return self.register_fixture(repository, control, python, entrypoint)
        self.register.side_effect = require_complete_runner
        try:
            self.enable()
        except RuntimeError:
            # Refusing the invalid immutable runner also fails closed; a real
            # schedule must not be pointed at incomplete or changed code.
            self.register.assert_not_called()
            if (self.control / 'config.json').exists():
                self.assertFalse(self.config()['enabled'])
        else:
            self.register.assert_called_once()
            self.assertTrue(self.config()['enabled'])
            for name in names:
                self.assertEqual((runner / name).read_bytes(), source[name])
        self.assertEqual((self.app / 'resources/app.asar').read_bytes(), self.app_before)

    def test_config_symlink_rejected_without_registration(self):
        self.enable()
        config_path = self.control / 'config.json'
        retained = self.control / 'retained-config.json'
        config_path.rename(retained)
        config_path.symlink_to(retained)
        self.register.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'ссылку'):
            self.enable()
        self.register.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'POSIX ownership guard')
    def test_config_foreign_owner_rejected_without_chown_or_registration(self):
        self.enable()
        self.register.reset_mock()
        config_path = self.control / 'config.json'
        own_uid = config_path.stat().st_uid
        with patch.object(package.os, 'getuid', return_value=own_uid + 1):
            with self.assertRaisesRegex(RuntimeError, 'принадлежать пользователю'):
                manager.config_at(self.control)
        self.register.assert_not_called()

    def test_disable_retains_packages_and_history_and_is_idempotent(self):
        self.enable()
        config = self.config()
        retained = Path(config['active_package'])
        runner = Path(config['runner'])
        self.assertEqual(manager.disable(self.control)['status'], 'disabled')
        self.assertEqual(manager.disable(self.control)['status'], 'disabled')
        self.assertFalse(self.config()['enabled'])
        self.assertTrue(retained.is_dir())
        self.assertTrue(runner.is_file())
        self.assertEqual((self.app / 'resources/app.asar').read_bytes(), self.app_before)

    def test_control_inside_source_or_app_rejected_before_scheduler(self):
        with self.assertRaisesRegex(RuntimeError, 'отдельно от исходников'):
            manager.enable(self.source / 'auto', 'antigravity-russian', str(self.app), approved=True)
        with self.assertRaisesRegex(RuntimeError, 'вне приложения'):
            manager.enable(self.app / 'auto', 'antigravity-russian', str(self.app), approved=True)
        self.register.assert_not_called()
        self.assertFalse((self.app / 'auto').exists())
        self.assertEqual((self.app / 'resources/app.asar').read_bytes(), self.app_before)


if __name__ == '__main__':
    unittest.main()
