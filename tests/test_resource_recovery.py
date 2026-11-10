"""Regression tests using synthetic clients; never write to installed games."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


TOOL = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('art_switch', TOOL / 'replace_art.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class ResourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        (TOOL / '.work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TOOL / '.work')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cn = self.root / 'client'
        self.cn.mkdir()
        (self.cn / 'xtlr.exe').touch()
        (self.cn / 'Persistent_Store').mkdir()
        self.prefs = self.cn / 'prefs.dat'
        self.prefs.write_bytes(b'LOCAL_GAME_VERSION:2.0.1\nRESOURCE_SUPPLEMENT_MODE_ENABLE:0\nPACKAGE_TAGS:Voice_CN\n')
        self.index = self.cn / 'file_versions.dat'
        self.index.write_bytes(b'\\unrelated|keep-this\n')
        self.payload = b'official bundle'
        self.md5 = hashlib.md5(self.payload).hexdigest()
        self.name = 'never_replaced.unity3d'
        self.mani = self.cn / 'Persistent_Store/ss_win.mani'
        self.mani.write_text('$CLIENT_VER:2.0.0\n$GAME_VER:2.0.1\n$RES_VER:123\n'
                             f'{self.name}|{self.md5}|{len(self.payload)}|0|\n', encoding='utf-8')
        self.target = self.cn / 'Persistent_Store/AssetBundles' / self.name
        self.key = b'\\Persistent_Store\\AssetBundles\\never_replaced.unity3d'
        self.addCleanup(patch.stopall)
        patch.object(a, 'ROOT', self.root).start()
        patch.object(a, 'STATE', self.root / 'backups/active.json').start()
        patch.object(a, 'ensure_closed').start()
        a.HASH_CACHE.clear()
        patch.object(a, 'PERSISTENT_HASHES', None).start()

    def install(self, data=None):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(self.payload if data is None else data)

    def test_no_history_missing_resource_requests_client_repair(self):
        before = self.prefs.read_bytes()
        a.restore(self.cn, prepare_update=True)
        self.assertEqual(self.prefs.read_bytes(), before.replace(b'ENABLE:0', b'ENABLE:1'))
        backup = self.root / 'backups/objects' / hashlib.sha256(before).hexdigest()
        self.assertEqual(backup.read_bytes(), before)
        self.assertFalse(self.target.exists())
        with self.assertRaisesRegex(RuntimeError, '资源补全'):
            a.apply(self.cn, self.root / 'unused-jp')

    def test_same_size_corruption_is_detected(self):
        self.install(b'x' * len(self.payload))
        a.prepare_official_update(self.cn)
        self.assertIn(b'ENABLE:1', self.prefs.read_bytes())
        self.assertEqual(self.target.read_bytes(), b'x' * len(self.payload))

    def active_state(self):
        self.install(b'patched')
        original = hashlib.sha256(self.payload).hexdigest()
        backup = self.root / 'backups/objects' / original
        backup.parent.mkdir(parents=True)
        backup.write_bytes(self.payload)
        state = {'cn': str(self.cn), 'fingerprints': [a.digest(self.mani)],
                 'entries': [{'path': str(self.target.relative_to(self.cn)),
                              'original': original, 'patched': a.digest(self.target)}]}
        a.STATE.write_text(json.dumps(state), encoding='utf-8')
        return backup

    def test_active_restore_repairs_unrelated_missing_bundle(self):
        self.active_state()
        with self.mani.open('a', encoding='utf-8') as f:
            f.write(f'unrelated_missing.unity3d|{self.md5}|{len(self.payload)}|0|\n')
        a.restore(self.cn, prepare_update=True)
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertFalse(a.STATE.exists())
        self.assertIn(b'ENABLE:1', self.prefs.read_bytes())
        self.assertEqual(len(list((self.root / 'backups/history').glob('*.json'))), 1)

    def test_damaged_backup_keeps_active_state_and_live_files(self):
        backup = self.active_state()
        a.load_hash_cache()
        a.digest(backup)
        a.save_hash_cache()
        before = backup.stat()
        backup.write_bytes(b'x' * before.st_size)
        os.utime(backup, ns=(before.st_atime_ns, before.st_mtime_ns))
        a.load_hash_cache()
        with self.assertRaisesRegex(RuntimeError, '备份损坏'):
            a.restore(self.cn, prepare_update=True)
        self.assertTrue(a.STATE.exists())
        self.assertEqual(self.target.read_bytes(), b'patched')
        self.assertIn(b'ENABLE:0', self.prefs.read_bytes())

    def test_missing_index_repaired_and_second_run_is_identical(self):
        self.install()
        prefs = self.prefs.read_bytes()
        a.prepare_official_update(self.cn)
        expected = b'\\unrelated|keep-this\n' + self.key + b'|' + self.md5.encode() + b'\n'
        self.assertEqual(self.index.read_bytes(), expected)
        a.prepare_official_update(self.cn)
        self.assertEqual(self.index.read_bytes(), expected)
        self.assertEqual(self.prefs.read_bytes(), prefs)

    def test_valid_snapshot_line_preserves_original_bytes(self):
        self.install()
        data = self.key + b'|' + self.md5.upper().encode() + b'\r\n'
        self.index.write_bytes(data)
        a.prepare_official_update(self.cn)
        self.assertEqual(self.index.read_bytes(), data)

    def test_wrong_and_duplicate_index_entries_repaired(self):
        self.install()
        self.index.write_bytes((self.key + b'|bad\n') * 2 + b'\\other|preserve\n')
        a.prepare_official_update(self.cn)
        self.assertEqual(self.index.read_bytes(), self.key + b'|' + self.md5.encode() + b'\n\\other|preserve\n')

    def test_prefs_bom_crlf_and_absent_setting(self):
        for before, expected in [
            (b'\xef\xbb\xbfRESOURCE_SUPPLEMENT_MODE_ENABLE:0\r\nOTHER:value\r\n',
             b'\xef\xbb\xbfRESOURCE_SUPPLEMENT_MODE_ENABLE:1\r\nOTHER:value\r\n'),
            (b'OTHER:value', b'OTHER:value\nRESOURCE_SUPPLEMENT_MODE_ENABLE:1\n'),
        ]:
            with self.subTest(before=before):
                self.prefs.write_bytes(before)
                a.prepare_official_update(self.cn)
                self.assertEqual(self.prefs.read_bytes(), expected)

    def test_existing_repair_request_not_cancelled_by_healthy_scan(self):
        self.install()
        self.prefs.write_bytes(b'RESOURCE_SUPPLEMENT_MODE_ENABLE:1\n')
        a.prepare_official_update(self.cn)
        self.assertEqual(self.prefs.read_bytes(), b'RESOURCE_SUPPLEMENT_MODE_ENABLE:1\n')

    def test_manifest_change_or_running_game_prevents_write(self):
        self.install()
        before = self.index.read_bytes()
        with patch.object(a, 'ensure_closed', side_effect=RuntimeError('running')):
            with self.assertRaisesRegex(RuntimeError, 'running'):
                a.prepare_official_update(self.cn)
        self.assertEqual(self.index.read_bytes(), before)
        real_manifest = a.manifest
        count = 0

        def changed(cn):
            nonlocal count
            count += 1
            meta, rows, digest = real_manifest(cn)
            return meta, rows, digest if count == 1 else 'changed'

        with patch.object(a, 'manifest', side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, '清单发生变化'):
                a.prepare_official_update(self.cn)
        self.assertEqual(self.index.read_bytes(), before)

    def prepare_switch(self):
        self.install()
        jp = self.root / 'jp'
        (jp / 'Persistent_Store').mkdir(parents=True)
        (jp / 'StellaSora.exe').touch()
        shutil.copyfile(self.mani, jp / 'Persistent_Store/ss_win.mani')
        output = self.root / '.work/patch'
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(b'replacement artwork')
        report = {'fingerprints': [a.manifest(self.cn)[2], a.manifest(jp)[2]],
                  'versions': {}, 'recipe': a.digest(Path(a.__file__)),
                  'bundles': {self.name: {'output': str(output), 'sha256': a.digest(output)}}}
        patch.object(a, 'reusable_plan', return_value=report).start()
        return jp

    def test_switch_round_trip_across_cache_reloads(self):
        jp = self.prepare_switch()
        prefs, index = self.prefs.read_bytes(), self.index.read_bytes()
        for _ in range(2):
            a.load_hash_cache()
            a.apply(self.cn, jp)
            self.assertEqual(self.target.read_bytes(), b'replacement artwork')
            self.assertTrue(a.verify(self.cn))
            a.save_hash_cache()
            a.load_hash_cache()
            a.restore(self.cn)
            self.assertEqual(self.target.read_bytes(), self.payload)
            self.assertEqual(self.prefs.read_bytes(), prefs)
            self.assertEqual(self.index.read_bytes(), index)
            self.assertFalse(a.STATE.exists())
            a.save_hash_cache()

    def test_interrupted_switch_recovers_original_with_cache(self):
        jp = self.prepare_switch()
        a.load_hash_cache()
        a.apply(self.cn, jp)
        entry = a.read_state()['entries'][0]
        # Simulate interruption between the two renames during restore.
        a.verified_move(self.target, a.switch_slot(self.cn, entry['path'], 'patched'), entry['patched'])
        a.save_hash_cache()
        a.load_hash_cache()
        a.restore(self.cn)
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertFalse(a.STATE.exists())


class FastValidationTests(unittest.TestCase):
    def setUp(self):
        (TOOL / '.work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TOOL / '.work')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(a, 'ROOT', self.root).start()
        patch.object(a, 'PERSISTENT_HASHES', None).start()
        self.path = self.root / 'bundle'
        self.path.write_bytes(b'original payload')
        a.load_hash_cache()

    def seed(self):
        expected = a.digest(self.path)
        a.save_hash_cache()
        a.load_hash_cache()
        return expected

    def require_ntfs(self):
        if a.hash_key(self.path)[-1] is None:
            self.skipTest('Cross-run reuse requires local NTFS change timestamps')

    def test_unchanged_content_reused_after_reload(self):
        self.require_ntfs()
        expected = self.seed()
        with patch.object(Path, 'open', side_effect=AssertionError('must not reread payload')):
            self.assertEqual(a.digest(self.path), expected)

    def test_same_size_edit_with_reset_mtime_invalidates_cache(self):
        self.require_ntfs()
        expected = self.seed()
        before = self.path.stat()
        self.path.write_bytes(b'x' * before.st_size)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertNotEqual(a.digest(self.path), expected)
        self.assertEqual(a.digest(self.path), hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_replaced_file_with_reset_mtime_invalidates_cache(self):
        expected = self.seed()
        before = self.path.stat()
        other = self.root / 'replacement'
        other.write_bytes(b'x' * before.st_size)
        os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(other, self.path)
        self.assertNotEqual(a.digest(self.path), expected)

    def test_full_check_rereads_content(self):
        expected = self.seed()
        a.load_hash_cache(full=True)
        self.assertEqual(a.digest(self.path), expected)
        self.assertEqual(a.HASH_STATS['bytes'], self.path.stat().st_size)

    def test_full_check_preserves_other_valid_cache_entries(self):
        self.require_ntfs()
        other = self.root / 'other'
        other.write_bytes(b'another resource')
        expected = a.digest(other)
        self.seed()
        a.load_hash_cache(full=True)
        a.digest(self.path)
        a.save_hash_cache()
        a.load_hash_cache()
        with patch.object(Path, 'open', side_effect=AssertionError('must retain other cached resources')):
            self.assertEqual(a.digest(other), expected)

    def test_unavailable_change_time_disables_cross_run_reuse(self):
        expected = self.seed()
        with patch.object(a, 'file_change_time', return_value=None):
            self.assertEqual(a.digest(self.path), expected)
            self.assertEqual(a.HASH_STATS['bytes'], self.path.stat().st_size)

    def test_open_writer_disables_cached_validation(self):
        self.require_ntfs()
        self.seed()
        with self.path.open('r+b') as writer:
            writer.write(b'changed!')
            writer.flush()
            self.assertIsNone(a.hash_key(self.path)[-1])
            self.assertEqual(a.digest(self.path), hashlib.sha256(self.path.read_bytes()).hexdigest())
            self.assertEqual(a.HASH_STATS['bytes'], self.path.stat().st_size)

    def test_corrupt_cache_falls_back_to_content(self):
        for value in ('not JSON', '{"version":1,"files":[]}',
                      '{"version":1,"files":{"bad":null}}'):
            with self.subTest(value=value):
                (self.root / '.work').mkdir(exist_ok=True)
                (self.root / '.work/file-hashes.json').write_text(value, encoding='utf-8')
                a.load_hash_cache()
                self.assertEqual(a.digest(self.path), hashlib.sha256(self.path.read_bytes()).hexdigest())
                self.assertEqual(a.HASH_STATS['read'], 1)

    def test_change_during_read_is_rejected(self):
        real_open = Path.open
        target = self.path

        class ChangingFile:
            def __enter__(self):
                self.file = real_open(target, 'rb')
                return self.file

            def __exit__(self, *args):
                self.file.close()
                with real_open(target, 'wb') as f:
                    f.write(b'changed after read')

        with patch.object(Path, 'open', return_value=ChangingFile()):
            with self.assertRaisesRegex(RuntimeError, '校验期间文件发生变化'):
                a.digest(target)
        self.assertFalse(a.HASH_CACHE)

    def test_rename_reuses_verified_content_across_runs(self):
        self.require_ntfs()
        expected = self.seed()
        target = self.root / 'moved'
        a.verified_move(self.path, target, expected)
        a.save_hash_cache()
        a.load_hash_cache()
        with patch.object(Path, 'open', side_effect=AssertionError('must not reread payload')):
            self.assertEqual(a.digest(target), expected)
        self.assertFalse(self.path.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows native process guard')
    def test_running_game_name_blocks_native_guard(self):
        # Run a renamed command interpreter waiting on stdin, never the game.
        executable = self.root / 'xtlr.exe'
        shutil.copyfile(Path(os.environ['SystemRoot']) / 'System32/cmd.exe', executable)
        process = subprocess.Popen([str(executable), '/d', '/c', 'set /p art_test='],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=0x08000000)
        try:
            self.assertIsNone(process.poll())
            with self.assertRaisesRegex(RuntimeError, '请退出'):
                a.ensure_closed()
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdin.close()


if __name__ == '__main__':
    unittest.main()
