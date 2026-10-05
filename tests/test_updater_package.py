"""Bounded private updater packages; every input is a temporary fixture."""
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'updater'))
import package


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repository = 'antigravity-russian'
        self.commit = 'a' * 40
        self.source = self.base / 'source'
        self.source.mkdir()
        self.content = {
            'manifest.json': json.dumps({'format': 1, 'repository': self.repository,
                                         'auto_update_api': 1}).encode(),
            'updater/manager.py': b'# manager\n',
            'updater/adapter.py': b'# adapter\n',
            'updater/package.py': b'# package\n',
            'updater/service.py': b'# service\n',
            'macos/patch.py': b'# macOS core\n',
            'macos/asar.py': b'# ASAR dependency\n',
            'macos/catalog.py': b'# catalog dependency\n',
            'macos/ru.json': '{"Settings":"Настройки"}'.encode('utf-8'),
            'macos/native-ru.json': b'{}',
            'macos/profiles.json': b'{}',
            'macos/ui-runtime.js': b'// runtime\n',
            'portable/patch.py': b'# portable core\n',
            'portable/pe_integrity.py': b'# Windows integrity\n',
            'portable/windows-profiles.json': b'{}',
            'portable/linux-profiles.json': b'{}',
            'common/compatibility.py': b'# shared dependency\n',
            'linux/install.sh': b'#!/bin/sh\n',
            'windows/install.ps1': b'# launcher\n',
            'install.sh': b'#!/bin/sh\n',
        }
        for relative, data in self.content.items():
            destination = self.source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)

    def archive(self, name='input.tar.gz', changes=None, extras=()):
        destination = self.base / name
        content = dict(self.content)
        if changes:
            content.update(changes)
        prefix = self.repository + '-' + self.commit + '/'
        with tarfile.open(destination, 'w:gz') as stream:
            for relative, data in content.items():
                item = tarfile.TarInfo(prefix + relative)
                item.size = len(data)
                stream.addfile(item, io.BytesIO(data))
            for item, data in extras:
                stream.addfile(item, None if data is None else io.BytesIO(data))
        return destination

    def test_snapshot_contains_all_allowed_dependencies_but_not_checkout_extras(self):
        for relative in ('README.md', 'tests/private.py', '.git/config',
                         'macos/__pycache__/cache.py', 'common/node_modules/dependency.py'):
            destination = self.source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b'not part of the runtime package')
        destination = package.snapshot(self.source, self.base / 'packages', self.repository)
        files = {item.relative_to(destination).as_posix(): item.read_bytes()
                 for item in destination.rglob('*') if item.is_file()}
        self.assertEqual(files, self.content)
        if os.name != 'nt':
            self.assertTrue(all(item.stat().st_mode & 0o077 == 0
                                for item in destination.rglob('*')))

    def test_snapshot_rejects_allowed_file_symlink(self):
        original = self.source / 'macos/ru.json'
        original.unlink()
        original.symlink_to(self.source / 'macos/native-ru.json')
        with self.assertRaisesRegex(RuntimeError, 'ссылку'):
            package.snapshot(self.source, self.base / 'packages', self.repository)

    def test_snapshot_rejects_hardlinked_dependency(self):
        original = self.source / 'macos/ru.json'
        alias = self.base / 'hardlink.json'
        os.link(original, alias)
        with self.assertRaisesRegex(RuntimeError, 'Небезопасный исходный файл'):
            package.snapshot(self.source, self.base / 'packages', self.repository)

    def test_valid_commit_pinned_archive_extracted_completely(self):
        destination = self.base / 'extracted'
        self.assertEqual(package.extract_package(self.archive(), destination,
                                                 self.repository, self.commit), destination)
        self.assertEqual({p.relative_to(destination).as_posix(): p.read_bytes()
                          for p in destination.rglob('*') if p.is_file()}, self.content)

    def test_archive_requires_exact_commit_root(self):
        archive = self.archive()
        with self.assertRaisesRegex(RuntimeError, 'путь'):
            package.extract_package(archive, self.base / 'wrong-root', self.repository, 'b' * 40)

    def test_archive_rejects_traversal_absolute_paths_and_links(self):
        prefix = self.repository + '-' + self.commit + '/'
        cases = [(prefix + '../outside.py', None), ('/tmp/outside.py', None),
                 (prefix + 'common/link.py', tarfile.SYMTYPE),
                 (prefix + 'common/hard.py', tarfile.LNKTYPE),
                 (prefix + 'common\\outside.py', None),
                 (prefix + 'common/C:outside.py', None)]
        for index, (name, kind) in enumerate(cases):
            with self.subTest(name=name):
                item = tarfile.TarInfo(name)
                if kind is None:
                    item.size = 1
                    data = b'x'
                else:
                    item.type = kind
                    item.linkname = '../outside.py'
                    data = None
                archive = self.archive('unsafe-' + str(index) + '.tar.gz', extras=((item, data),))
                with self.assertRaisesRegex(RuntimeError, 'путь|Ссылки'):
                    package.extract_package(archive, self.base / ('unsafe-' + str(index)),
                                            self.repository, self.commit)
        self.assertFalse((self.base / 'outside.py').exists())

    def test_archive_duplicate_member_rejected_before_overwrite(self):
        name = self.repository + '-' + self.commit + '/macos/ru.json'
        item = tarfile.TarInfo(name)
        item.size = 8
        archive = self.archive(extras=((item, b'replaced'),))
        destination = self.base / 'duplicate'
        with self.assertRaisesRegex(RuntimeError, 'Повторный'):
            package.extract_package(archive, destination, self.repository, self.commit)
        self.assertEqual((destination / 'macos/ru.json').read_bytes(), self.content['macos/ru.json'])

    def test_bad_manifest_rejected_in_local_snapshot_and_download(self):
        bad = json.dumps({'format': 1, 'repository': 'different-repository',
                          'auto_update_api': 1}).encode()
        (self.source / 'manifest.json').write_bytes(bad)
        with self.assertRaisesRegex(RuntimeError, 'не поддерживает'):
            package.snapshot(self.source, self.base / 'packages', self.repository)
        archive = self.archive(changes={'manifest.json': bad})
        with self.assertRaisesRegex(RuntimeError, 'не поддерживает'):
            package.extract_package(archive, self.base / 'bad-manifest', self.repository, self.commit)

    def test_api_version_and_required_runtime_are_checked(self):
        metadata = json.loads((self.source / 'manifest.json').read_text())
        metadata['auto_update_api'] = 2
        (self.source / 'manifest.json').write_text(json.dumps(metadata))
        with self.assertRaisesRegex(RuntimeError, 'не поддерживает'):
            package.manifest(self.source, self.repository)
        metadata['auto_update_api'] = 1
        (self.source / 'manifest.json').write_text(json.dumps(metadata))
        (self.source / 'updater/adapter.py').unlink()
        with self.assertRaisesRegex(RuntimeError, 'Неполный пакет'):
            package.manifest(self.source, self.repository)

    def test_decompressed_archive_limit_counts_ignored_members(self):
        item = tarfile.TarInfo(self.repository + '-' + self.commit + '/README.md')
        item.size = 4096
        archive = self.archive(extras=((item, b'x' * item.size),))
        destination = self.base / 'large'
        with patch.object(package, 'LIMIT', 2048), self.assertRaisesRegex(RuntimeError, 'размер'):
            package.extract_package(archive, destination, self.repository, self.commit)
        self.assertFalse((destination / 'README.md').exists())

    def test_download_stream_is_bounded_without_network(self):
        destination = self.base / 'download'
        response = io.BytesIO(b'x' * 32)
        with patch.object(package.urllib.request, 'urlopen', return_value=response) as opener:
            with self.assertRaisesRegex(RuntimeError, 'размер'):
                package.download('https://example.invalid/fixture', destination, 16)
        self.assertLessEqual(destination.stat().st_size, 16)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, 'https://example.invalid/fixture')
        self.assertEqual(opener.call_args.kwargs['timeout'], 20)

    def test_refresh_fetches_metadata_and_archive_from_same_immutable_commit(self):
        control = self.base / 'auto'
        package.private_dir(control)
        archive = self.archive()
        calls = []
        def fetch(url, target, limit):
            calls.append((url, limit))
            if url.endswith('/commits/main'):
                target.write_text(json.dumps({'sha': self.commit}))
            else:
                self.assertTrue(url.endswith('/tar.gz/' + self.commit))
                shutil.copyfile(archive, target)
        with patch.object(package, 'download', side_effect=fetch):
            destination = package.refresh(control, {'repository': self.repository})
        self.assertEqual(destination, control / 'packages' / self.commit)
        self.assertEqual(calls, [
            ('https://api.github.com/repos/fadeichev2121/antigravity-russian/commits/main', 1024 * 1024),
            ('https://codeload.github.com/fadeichev2121/antigravity-russian/tar.gz/' + self.commit, package.LIMIT),
        ])
        self.assertEqual((destination / 'macos/ru.json').read_bytes(), self.content['macos/ru.json'])
        calls.clear()
        with patch.object(package, 'download', side_effect=fetch):
            self.assertEqual(package.refresh(control, {'repository': self.repository}), destination)
        self.assertEqual(len(calls), 1, 'A previously pinned valid package needs no new archive download')

    def test_refresh_rejects_invalid_commit_before_archive_download(self):
        control = self.base / 'auto'
        package.private_dir(control)
        calls = []
        def fetch(url, target, limit):
            calls.append(url)
            target.write_text(json.dumps({'sha': '../../different-project'}))
        with patch.object(package, 'download', side_effect=fetch):
            with self.assertRaisesRegex(RuntimeError, 'некорректный commit'):
                package.refresh(control, {'repository': self.repository})
        self.assertEqual(len(calls), 1)
        self.assertEqual(list((control / 'packages').iterdir()), [])

    def test_private_json_rejects_symlink_and_shared_permissions(self):
        destination = self.base / 'state/config.json'
        package.write_json(destination, {'fixture': True})
        alias = self.base / 'linked-config.json'
        alias.symlink_to(destination)
        with self.assertRaisesRegex(RuntimeError, 'ссылку'):
            package.read_json(alias)
        if os.name != 'nt':
            destination.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'приватным'):
                package.read_json(destination)

    @unittest.skipIf(os.name == 'nt', 'POSIX ownership guard')
    def test_private_json_rejects_foreign_owner_without_chown(self):
        destination = self.base / 'state/config.json'
        package.write_json(destination, {'fixture': True})
        own_uid = destination.stat().st_uid
        with patch.object(package.os, 'getuid', return_value=own_uid + 1):
            with self.assertRaisesRegex(RuntimeError, 'принадлежать пользователю'):
                package.read_json(destination)


if __name__ == '__main__':
    unittest.main()
