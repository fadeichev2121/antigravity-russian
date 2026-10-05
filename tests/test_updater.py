"""Auto-reapply behavior without running apps, schedulers or the network."""
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'updater'))

class ReapplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.control = self.root / 'auto'
        import manager
        self.m = manager
        from package import private_dir, write_json
        private_dir(self.control)
        self.app = self.root / 'Original App'
        self.app.mkdir(); (self.app / 'resources').mkdir()
        (self.app / 'resources/app.asar').write_bytes(b'original fixture')
        self.config = {'schema': 1, 'repository': 'antigravity-russian', 'enabled': True,
                       'approved_signature': True, 'app': str(self.app),
                       'active_package': str(self.control / 'packages/local-test'), 'records': [], 'extra_state': None,
                       'last_refresh': 1000, 'observation': None}
        private_dir(self.control / 'packages/local-test')
        write_json(self.control / 'config.json', self.config)
        self.calls = []
    def tearDown(self): self.tmp.cleanup()
    def probe(self, app, state_dirs):
        return {'app': str(app), 'fingerprint': self.fingerprint, 'version': self.version,
                'kind': self.kind, 'writable': self.writable, 'state_dir': None,
                'message': self.kind}
    def run_check(self, now):
        from types import SimpleNamespace
        adapter = SimpleNamespace(discover=lambda hint: self.app, probe=self.probe,
                                  default_state=lambda: self.root / 'old-state',
                                  command=lambda action, app, state_dir, approved:
                                      [action, str(app), str(state_dir), str(approved)])
        def execute(argv):
            self.calls.append(argv); self.kind = 'patched'
            return (0, 'installed')
        with patch.object(self.m, 'adapter_for', return_value=adapter), \
             patch.object(self.m, 'execute', side_effect=execute), \
             patch.object(self.m, 'refresh', side_effect=OSError('offline')):
            return self.m.check(self.control, now=now)
    def setup_source(self):
        self.fingerprint, self.version, self.kind, self.writable = 'a'*64, '1.0', 'source', True
    def test_stable_closed_source_then_single_apply(self):
        self.setup_source()
        self.assertEqual(self.run_check(1000)['status'], 'waiting')
        self.assertEqual(self.run_check(1030)['status'], 'waiting')
        self.assertEqual(self.run_check(1061)['status'], 'installed')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][-1], 'True')
    def test_update_uses_new_state_and_preserves_old(self):
        self.setup_source(); self.run_check(1000); self.run_check(1061)
        first = self.calls[-1][2]
        marker = Path(first) / 'original.marker'
        marker.parent.mkdir(parents=True, exist_ok=True); marker.write_text('old backup')
        self.fingerprint, self.version, self.kind = 'b'*64, '2.0', 'source'
        (self.app / 'resources/app.asar').write_bytes(b'updated fixture')
        self.run_check(1200); self.run_check(1261)
        self.assertNotEqual(first, self.calls[-1][2])
        self.assertEqual(marker.read_text(), 'old backup')
    def test_busy_or_unknown_never_apply_and_resets_stability(self):
        self.setup_source(); self.run_check(1000)
        self.kind = 'busy'; self.run_check(1100)
        self.kind = 'source'; self.run_check(1200)
        self.assertEqual(self.calls, [])
        self.kind = 'unknown'
        self.assertEqual(self.run_check(1300)['status'], 'unsupported')
        self.assertEqual(self.calls, [])
    def test_no_permissions_or_incomplete_state_do_not_apply(self):
        self.setup_source(); self.writable = False
        self.assertEqual(self.run_check(1000)['status'], 'needs-admin')
        self.writable = True; self.kind = 'recovery'
        self.assertEqual(self.run_check(1100)['status'], 'needs-recovery')
        self.assertEqual(self.calls, [])
    def test_disable_prevents_reapply(self):
        self.setup_source(); self.config['enabled'] = False
        from package import write_json
        write_json(self.control / 'config.json', self.config)
        self.assertEqual(self.run_check(1000)['status'], 'disabled')
        self.assertEqual(self.calls, [])
    def test_changed_files_between_probe_and_install_reset_wait(self):
        self.setup_source(); self.run_check(1000)
        original = self.probe; count = [0]
        def changing(app, state_dirs):
            count[0] += 1; value = original(app, state_dirs)
            if count[0] > 1: value['fingerprint'] = 'c'*64
            return value
        with patch.object(self, 'probe', side_effect=changing):
            self.assertEqual(self.run_check(1061)['status'], 'waiting')
        self.assertEqual(self.calls, [])
    def test_same_build_replaced_by_vendor_gets_new_backup_generation(self):
        self.setup_source(); self.run_check(1000); self.run_check(1061)
        first = self.calls[-1][2]
        self.kind = 'source'; (self.app / 'resources/app.asar').write_bytes(b'same build restored')
        self.run_check(1200); self.run_check(1261)
        self.assertNotEqual(first, self.calls[-1][2])
    def test_unchanged_installed_files_skip_expensive_probe(self):
        self.setup_source(); self.run_check(1000); self.run_check(1061)
        with patch.object(self, 'probe', side_effect=AssertionError('large hashes should not be reread')):
            self.assertEqual(self.run_check(1200)['status'], 'already-patched')
    def test_uncertain_failure_never_retried_until_explicit_restore(self):
        self.setup_source(); self.run_check(1000)
        from types import SimpleNamespace
        adapter = SimpleNamespace(discover=lambda hint: self.app, probe=self.probe,
                                  default_state=lambda: self.root / 'old-state',
                                  command=lambda *args: ['fixture'])
        with patch.object(self.m, 'adapter_for', return_value=adapter), \
             patch.object(self.m, 'execute', side_effect=TimeoutError('interrupted')):
            self.assertEqual(self.m.check(self.control, now=1061)['status'], 'failed')
        self.assertEqual(self.run_check(1200)['status'], 'needs-recovery')
        with patch.object(self.m, 'adapter_for', return_value=adapter), patch.object(self.m.service, 'remove'):
            code, result = self.m.restore(self.control)
        self.assertEqual(code, 0); self.assertEqual(result['status'], 'restored')
        from package import read_json, write_json
        config = read_json(self.control / 'config.json'); config['enabled'] = True
        write_json(self.control / 'config.json', config)
        self.run_check(1400)
        self.assertEqual(self.run_check(1461)['status'], 'installed')
    def fixture_adapter(self):
        from types import SimpleNamespace
        return SimpleNamespace(discover=lambda hint: Path(hint) if hint else self.app,
                               probe=self.probe, default_state=lambda: self.root / 'old-state',
                               command=lambda action, app, state, approved: [action, str(app), str(state)])
    def test_restore_explicit_other_app_does_not_touch_tracked_app(self):
        self.setup_source(); self.kind = 'patched'
        other = self.root / 'Other App'; other.mkdir()
        with patch.object(self.m, 'adapter_for', return_value=self.fixture_adapter()), \
             patch.object(self.m.service, 'remove') as remove, patch.object(self.m, 'execute') as execute:
            code, _ = self.m.restore(self.control, app_hint=str(other))
        self.assertEqual(code, 3); remove.assert_not_called(); execute.assert_not_called()
        self.assertTrue(self.m.config_at(self.control)['enabled'])
    def test_restore_explicit_other_state_disables_then_uses_legacy_fallback(self):
        self.setup_source(); self.kind = 'patched'
        with patch.object(self.m, 'adapter_for', return_value=self.fixture_adapter()), \
             patch.object(self.m.service, 'remove'), patch.object(self.m, 'execute') as execute:
            code, _ = self.m.restore(self.control, state_hint=str(self.root / 'explicit-state'))
        self.assertEqual(code, 3); execute.assert_not_called()
        self.assertFalse(self.m.config_at(self.control)['enabled'])
    def test_check_rejects_conflicting_explicit_app(self):
        self.setup_source()
        with patch.object(self.m, 'adapter_for', return_value=self.fixture_adapter()), \
             patch.object(self.m, 'execute') as execute:
            with self.assertRaisesRegex(RuntimeError, 'приложени'):
                self.m.check(self.control, now=1000, app_hint=str(self.root / 'Other App'))
        execute.assert_not_called()
    def test_check_rejects_conflicting_explicit_state(self):
        self.setup_source()
        with patch.object(self.m, 'adapter_for', return_value=self.fixture_adapter()), \
             patch.object(self.m, 'execute') as execute:
            with self.assertRaisesRegex(RuntimeError, 'состояни'):
                self.m.check(self.control, now=1000, state_hint=str(self.root / 'other-state'))
        execute.assert_not_called()
    def test_scheduler_removal_error_cannot_block_explicit_rollback(self):
        self.setup_source(); state = self.root / 'saved-state'
        def probe(app, states):
            return dict(self.probe(app, states), kind='patched', state_dir=str(state))
        adapter = self.fixture_adapter(); adapter.probe = probe
        with patch.object(self.m, 'adapter_for', return_value=adapter), \
             patch.object(self.m.service, 'remove', side_effect=RuntimeError('scheduler unavailable')), \
             patch.object(self.m, 'execute', return_value=(0, 'restored')) as execute:
            code, result = self.m.restore(self.control)
        self.assertEqual(code, 0); execute.assert_called_once()
        self.assertFalse(self.m.config_at(self.control)['enabled'])
        self.assertIn('scheduler unavailable', result['scheduler_warning'])
    def test_new_profile_can_replace_same_version_typed_app_changed(self):
        self.setup_source()
        old_package = self.control / 'packages/old'; old_package.mkdir()
        record = {'app': str(self.app), 'fingerprint': 'b'*64, 'state_dir': str(self.root / 'saved'),
                  'package': str(old_package), 'version': '1.0', 'outcome': 'installed'}
        self.config['records'] = [record]
        old = self.fixture_adapter()
        old.probe = lambda *args: dict(self.probe(*args), kind='recovery', reason='app-changed')
        current = self.fixture_adapter()
        with patch.object(self.m, 'adapter_for', side_effect=lambda path: old if path == old_package else current):
            selected, _, info = self.m.inspect_current(self.config)
        self.assertIs(selected, current); self.assertEqual(info['kind'], 'source')
    def test_config_lock_prevents_parallel_checks(self):
        self.setup_source()
        from package import mutex
        with mutex(self.control):
            with self.assertRaises(RuntimeError): self.run_check(1000)
        self.assertEqual(self.calls, [])
    def test_invalid_archive_path_or_link_rejected(self):
        from package import extract_package
        for name, symlink in [('repo/../../escape', False), ('repo/updater/manager.py', True)]:
            archive = self.root / ('bad-link.tar.gz' if symlink else 'bad-path.tar.gz')
            with tarfile.open(archive, 'w:gz') as tar:
                item = tarfile.TarInfo(name)
                if symlink: item.type = tarfile.SYMTYPE; item.linkname = '/tmp/target'
                else: item.size = 1
                tar.addfile(item, None if symlink else io.BytesIO(b'x'))
            with self.assertRaises((RuntimeError, ValueError)):
                extract_package(archive, self.root / ('extract-link' if symlink else 'extract-path'), 'antigravity-russian', 'f'*40)
    def test_windows_schedule_repeats_before_the_next_login(self):
        import xml.etree.ElementTree as ET
        from service import definitions
        definition = definitions('antigravity-russian', self.control, Path('/python.exe'), Path('/manager.py'))['windows']
        root = ET.fromstring(definition)
        trigger = root.find('{*}Triggers/{*}TimeTrigger')
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.find('{*}Repetition/{*}Interval').text, 'PT2M')
        self.assertIsNotNone(trigger.find('{*}StartBoundary').text)
    def test_windows_utf16_definition_can_be_replaced_on_reenable(self):
        from service import definitions, write_job
        data = definitions('antigravity-russian', self.control, Path('/python.exe'), Path('/manager.py'))
        path = self.control / 'task.xml'
        write_job(path, data['windows'].encode('utf-16'), data['id'])
        write_job(path, data['windows'].encode('utf-16'), data['id'])
        self.assertEqual(path.read_bytes(), data['windows'].encode('utf-16'))
    def test_pythonw_runner_uses_console_python_for_captured_children(self):
        import package
        path = self.root / 'pythonw.exe'; path.write_bytes(b'fixture')
        console = self.root / 'python.exe'; console.write_bytes(b'fixture')
        with patch.object(package.sys, 'executable', str(path)):
            self.assertEqual(package.worker_python(), str(console))
    def test_linux_cleanup_does_not_disable_a_foreign_timer(self):
        import service
        key = service.identifier('antigravity-russian', self.control)
        directory = self.root / 'config/systemd/user'; directory.mkdir(parents=True)
        (directory / (key + '.timer')).write_text('[Unit]\nDescription=unrelated\n')
        record = {'kind': 'systemd', 'id': key}
        with patch.object(service.sys, 'platform', 'linux'), \
             patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')}), \
             patch.object(service, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Чуж'):
                service.remove('antigravity-russian', self.control, record)
        run.assert_not_called()
    def test_mac_cleanup_checks_definition_before_stopping_job(self):
        import service, plistlib
        key = service.identifier('antigravity-russian', self.control)
        directory = self.root / 'Library/LaunchAgents'; directory.mkdir(parents=True)
        (directory / (key + '.plist')).write_bytes(plistlib.dumps({'Label': 'unrelated'}))
        with patch.object(service.sys, 'platform', 'darwin'), \
             patch.object(service.Path, 'home', return_value=self.root), \
             patch.object(service.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Чуж'):
                service.remove('antigravity-russian', self.control, {'kind': 'launchd', 'id': key})
        run.assert_not_called()
    def test_scheduler_uses_current_user_and_quoted_argv(self):
        from service import definitions
        result = definitions('antigravity-russian', self.control, Path('/path with space/python'), Path('/path with space/runner/manager.py'))
        self.assertEqual(result['macos']['ProgramArguments'][0], '/path with space/python')
        self.assertEqual(result['macos']['StartInterval'], 120)
        self.assertNotIn('UserName', result['macos'])
        self.assertIn('Limited', result['windows'])
        self.assertIn('120', result['linux_timer'])
        self.assertNotIn('sudo', result['linux_service'])

if __name__ == '__main__': unittest.main()
