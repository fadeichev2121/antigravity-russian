"""Unix launchers pass only explicit target flags to the auto controller."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipIf(os.name == 'nt', 'Unix launcher fixture')
class LinuxTargetRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve(); self.source = self.base / 'package'
        for name in ('linux', 'portable', 'updater'):
            (self.source / name).mkdir(parents=True)
        shutil.copyfile(ROOT / 'linux/install.sh', self.source / 'linux/install.sh')
        (self.source / 'manifest.json').write_text('{"repository":"antigravity-russian","format":1}')
        driver = "import json,os,sys\nwith open(os.environ['UPDATER_TEST_LOG'],'a') as out: out.write(json.dumps(sys.argv)+'\\n')\n"
        for name in ('portable/patch.py', 'updater/manager.py'):
            (self.source / name).write_text(driver)
        self.bin = self.base / 'bin'; self.bin.mkdir(); self.log = self.base / 'calls.jsonl'
        for name, output in (('uname', 'Linux'), ('id', '1000')):
            path = self.bin / name; path.write_text('#!/bin/sh\nprintf "%s\\n" ' + output + '\n'); path.chmod(0o700)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'], UPDATER_TEST_LOG=str(self.log))
    def launch(self, action, flags=()):
        result = subprocess.run(['/bin/bash', str(self.source / 'linux/install.sh'), action, *flags],
                                env=self.env, input='', text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.log.read_text().splitlines()[-1])
    def test_custom_enable_then_no_target_auto_actions_use_saved_config(self):
        selected = str(self.base / 'Custom App')
        enabled = self.launch('auto-enable', ('--app', selected))
        self.assertIn(selected, enabled)
        for action in ('auto-check', 'restore', 'auto-status'):
            with self.subTest(action=action):
                argv = self.launch(action)
                self.assertNotIn('--app', argv)
                self.assertTrue(argv[0].endswith('manager.py'))
    def test_explicit_target_and_control_are_forwarded(self):
        selected, control = str(self.base / 'Explicit App'), str(self.base / 'control')
        argv = self.launch('auto-check', ('--app', selected, '--control-dir', control))
        self.assertIn(selected, argv); self.assertIn(control, argv)
    def test_normal_patch_keeps_default_target(self):
        argv = self.launch('status')
        self.assertTrue(argv[0].endswith('patch.py'))
        self.assertIn('--app', argv)

if __name__ == '__main__': unittest.main()
