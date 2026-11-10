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

    def test_parallel_preparation_failure_never_changes_live_files(self):
        jp = self.prepare_switch()
        report = a.reusable_plan.return_value
        template = report['bundles'][self.name]
        targets = [self.target]
        for index in range(16):
            name = f'char_2d_{index}.unity3d'
            target = self.target.with_name(name)
            target.write_bytes(self.payload)
            targets.append(target)
            with self.mani.open('a', encoding='utf-8') as f:
                f.write(f'{name}|{self.md5}|{len(self.payload)}|0|\n')
            report['bundles'][name] = dict(template)
        shutil.copyfile(self.mani, jp / 'Persistent_Store/ss_win.mani')
        report['fingerprints'] = [a.manifest(self.cn)[2], a.manifest(jp)[2]]
        failed_slot = a.switch_slot(self.cn, str(targets[-1].relative_to(self.cn)), 'patched')
        real_copy = a.atomic_copy

        def fail_one(src, dst):
            if dst == failed_slot:
                raise OSError('simulated copy failure')
            return real_copy(src, dst)

        with patch.object(a, 'atomic_copy', side_effect=fail_one):
            with self.assertRaisesRegex(OSError, 'simulated copy failure'):
                a.apply(self.cn, jp)
        self.assertFalse(a.STATE.exists())
        self.assertTrue(all(p.read_bytes() == self.payload for p in targets))

    def test_missing_unrelated_resource_blocks_write_after_parallel_planning(self):
        jp = self.prepare_switch()
        with self.mani.open('a', encoding='utf-8') as f:
            f.write(f'missing.unity3d|{self.md5}|{len(self.payload)}|0|\n')
        with self.assertRaisesRegex(RuntimeError, '官方资源缺失或损坏'):
            a.apply(self.cn, jp)
        self.assertFalse(a.STATE.exists())
        self.assertFalse((self.cn / '.art-switch').exists())
        self.assertEqual(self.target.read_bytes(), self.payload)

    @unittest.skipUnless(os.name == 'nt', 'Windows filename case insensitivity')
    def test_directory_scan_keeps_manifest_path_casing(self):
        self.install()
        self.target.rename(self.target.with_name(self.name.upper()))
        a.prepare_official_update(self.cn)
        self.assertIn(self.key + b'|' + self.md5.encode(), self.index.read_bytes())


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

    def test_parallel_requests_read_one_file_only_once(self):
        results = a.io_map(a.digest, [self.path] * 32)
        self.assertEqual(set(results), {hashlib.sha256(self.path.read_bytes()).hexdigest()})
        self.assertEqual(a.HASH_STATS['read'], 1)

    def test_worker_hash_preserves_other_verified_algorithm(self):
        md5 = a.digest(self.path, 'md5')
        sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        a.remember_hashes(a.hash_key(self.path), {'sha256': sha256})
        with patch.object(Path, 'open', side_effect=AssertionError('already verified')):
            self.assertEqual(a.digest(self.path, 'md5'), md5)
            self.assertEqual(a.digest(self.path), sha256)

    @unittest.skipUnless(os.name == 'nt', 'Windows long-path namespace')
    def test_long_path_prefix_does_not_hide_or_invent_path_escape(self):
        for root in (self.root, Path(r'\\server\share\client')):
            for relative, allowed in (('child', True), ('../outside', False), ('.', False)):
                normal = Path(os.path.normpath(root / relative))
                extended = Path('\\\\?\\UNC\\' + str(normal)[2:]) if str(normal).startswith('\\\\') else Path('\\\\?\\' + str(normal))
                with self.subTest(root=root, relative=relative):
                    with patch.object(Path, 'resolve', side_effect=[root, extended]):
                        if allowed:
                            self.assertEqual(a.safe(root, relative), extended)
                        else:
                            with self.assertRaisesRegex(RuntimeError, '路径越界'):
                                a.safe(root, relative)

    def test_parallel_directory_creation_keeps_safe_path_checks(self):
        for iteration in range(20):
            client = self.root / str(iteration)
            client.mkdir()
            def create(index):
                path = a.safe(client, f'.art-switch/{index}.patched')
                path.parent.mkdir(exist_ok=True)
                return path
            self.assertEqual(len(a.io_map(create, range(32))), 32)
        with self.assertRaisesRegex(RuntimeError, '路径越界'):
            a.safe(self.root, '../outside')

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
        with patch.object(a, 'windows_file_signature', return_value=None):
            self.assertEqual(a.digest(self.path), expected)
            self.assertEqual(a.HASH_STATS['bytes'], self.path.stat().st_size)

    def test_native_signature_matches_existing_cache_identity(self):
        self.require_ntfs()
        st = self.path.stat()
        self.assertEqual(a.hash_key(self.path)[1:6],
                         (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev))

    def test_unavailable_change_time_never_reuses_same_process_hash(self):
        if os.name != 'nt':
            self.skipTest('Windows metadata fallback')
        with patch.object(a, 'windows_file_signature', return_value=None):
            expected = a.digest(self.path)
            before = self.path.stat()
            self.path.write_bytes(b'x' * before.st_size)
            os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertNotEqual(a.digest(self.path), expected)
            self.assertEqual(a.HASH_STATS['read'], 2)

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


class IncrementalBundleTests(unittest.TestCase):
    def setUp(self):
        (TOOL / '.work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TOOL / '.work')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(a, 'ROOT', self.root).start()
        patch.object(a, 'PERSISTENT_HASHES', None).start()
        self.cn, self.jp = self.root / 'cn', self.root / 'jp'
        for client, exe in ((self.cn, 'xtlr.exe'), (self.jp, 'StellaSora.exe')):
            (client / 'Persistent_Store/AssetBundles').mkdir(parents=True)
            (client / exe).touch()
        self.cr, self.jr = {'dependency.unity3d': {}}, {}
        self.names = ['char_2d_1.unity3d', 'char_2d_2.unity3d']
        for name in self.names:
            self.install(name)
        self.direct = False
        self.builder = patch.object(a, 'build_bundle', side_effect=self.build).start()
        patch.object(a, 'run_bundle_jobs', side_effect=lambda jobs: list(map(a.run_bundle_job, jobs))).start()
        a.load_hash_cache()

    def install(self, name):
        for client, rows, prefix in ((self.cn, self.cr, b'CN'), (self.jp, self.jr, b'JP')):
            content = prefix + name.encode()
            (client / 'Persistent_Store/AssetBundles' / name).write_bytes(content)
            rows[name] = {'md5': hashlib.md5(content).hexdigest(), 'size': len(content)}

    def build(self, job):
        name, cp, jpfile, jp, names, output = job
        dependencies = {'dependency.unity3d': 'dependency.unity3d' in names}
        if not dependencies['dependency.unity3d']:
            return None, {'reason': 'missing dependency'}, dependencies
        if self.direct:
            output = Path(jpfile)
        else:
            output = Path(output)
            output.write_bytes(Path(cp).read_bytes() + Path(jpfile).read_bytes())
        return {'output': str(output), 'source': str(Path(jpfile).relative_to(jp)),
                'sha256': a.digest(output), 'deps': ['dependency.unity3d'],
                'direct': self.direct}, None, dependencies

    def plan(self):
        return a.incremental_bundles(self.cn, self.jp, self.cr, self.jr, self.names)

    def test_unrelated_manifest_change_reuses_all_bundles(self):
        first, _ = self.plan()
        self.cr['unrelated_new_bundle.unity3d'] = {}
        second, _ = self.plan()
        self.assertEqual(first, second)
        self.assertEqual(self.builder.call_count, 2)

    def test_one_changed_source_only_rebuilds_one_bundle(self):
        first, _ = self.plan()
        path = self.cn / 'Persistent_Store/AssetBundles' / self.names[0]
        content = b'new artwork version'
        path.write_bytes(content)
        self.cr[path.name] = {'md5': hashlib.md5(content).hexdigest(), 'size': len(content)}
        second, _ = self.plan()
        self.assertEqual(self.builder.call_count, 3)
        self.assertNotEqual(first[path.name]['sha256'], second[path.name]['sha256'])
        self.assertEqual(first[self.names[1]], second[self.names[1]])

    def test_new_bundle_is_discovered_without_rebuilding_old_ones(self):
        self.plan()
        self.names.append('char_2d_3.unity3d')
        self.install(self.names[-1])
        plan, _ = self.plan()
        self.assertEqual(set(plan), set(self.names))
        self.assertEqual(self.builder.call_count, 3)

    def test_removed_and_returning_dependency_invalidates_analysis(self):
        self.plan()
        del self.cr['dependency.unity3d']
        plan, skipped = self.plan()
        self.assertFalse(plan)
        self.assertEqual(set(skipped), set(self.names))
        self.assertEqual(self.builder.call_count, 4)
        self.cr['dependency.unity3d'] = {}
        plan, skipped = self.plan()
        self.assertEqual(set(plan), set(self.names))
        self.assertFalse(skipped)
        self.assertEqual(self.builder.call_count, 6)

    def test_corrupt_output_rebuilt_without_overwriting_old_artifact(self):
        first, _ = self.plan()
        output = Path(first[self.names[0]]['output'])
        before = output.stat()
        output.write_bytes(b'x' * before.st_size)
        os.utime(output, ns=(before.st_atime_ns, before.st_mtime_ns))
        second, _ = self.plan()
        self.assertEqual(self.builder.call_count, 3)
        self.assertNotEqual(second[self.names[0]]['output'], str(output))
        self.assertEqual(first[self.names[0]]['sha256'], second[self.names[0]]['sha256'])
        self.assertEqual(output.read_bytes(), b'x' * before.st_size)

    def test_source_corruption_is_not_hidden_by_cached_analysis(self):
        self.plan()
        path = self.cn / 'Persistent_Store/AssetBundles' / self.names[0]
        path.write_bytes(b'x' * path.stat().st_size)
        with self.assertRaisesRegex(RuntimeError, '缺少与当前清单匹配'):
            self.plan()
        self.assertEqual(self.builder.call_count, 2)

    def test_tool_change_invalidates_cached_analysis(self):
        self.plan()
        real_digest = a.digest
        with patch.object(a, 'digest', side_effect=lambda path, algorithm='sha256':
                          'new recipe' if Path(path) == Path(a.__file__) else real_digest(path, algorithm)):
            self.plan()
        self.assertEqual(self.builder.call_count, 4)

    def test_direct_source_relocated_to_updated_layer(self):
        self.direct = True
        self.plan()
        name = self.names[0]
        old = self.jp / 'Persistent_Store/AssetBundles' / name
        current = self.jp / 'Patch_Store/AssetBundles' / name
        current.parent.mkdir(parents=True)
        old.rename(current)
        plan, _ = self.plan()
        self.assertEqual(self.builder.call_count, 2)
        self.assertEqual(plan[name]['output'], str(current))
        self.assertTrue(a.valid_bundle_output(plan[name], self.jp))

    def test_direct_output_cannot_escape_japanese_resource_layers(self):
        outside = self.root / 'outside'
        outside.write_bytes(b'payload')
        item = {'output': str(outside), 'source': '../outside', 'direct': True,
                'sha256': a.digest(outside)}
        self.assertFalse(a.valid_bundle_output(item, self.jp))


class LoadingIndexCacheTests(unittest.TestCase):
    def setUp(self):
        (TOOL / '.work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TOOL / '.work')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(a, 'ROOT', self.root).start()
        patch.object(a, 'PERSISTENT_HASHES', None).start()
        self.cn = self.root / 'cn'
        (self.cn / 'Persistent_Store/AssetBundles').mkdir(parents=True)
        (self.cn / 'xtlr.exe').touch()
        self.source = self.cn / 'Persistent_Store/AssetBundles/win.unity3d'
        self.source.write_bytes(b'official loading index')
        self.rows = {'win.unity3d': self.row()}
        self.plan = {'char_2d_1.unity3d': {'deps': ['dependency.unity3d'], 'sha256': 'art version 1'}}
        self.builder = patch.object(a, 'build_loading_index', side_effect=self.build).start()
        a.load_hash_cache()

    def row(self):
        data = self.source.read_bytes()
        return {'md5': hashlib.md5(data).hexdigest(), 'size': len(data)}

    def build(self, source, plan, output):
        output.write_bytes(source.read_bytes() + b'patched dependencies')
        return {'output': str(output), 'deps': [], 'sha256': a.digest(output)}

    def test_image_or_unrelated_manifest_change_reuses_index(self):
        before = a.loading_index(self.cn, self.rows, self.plan)
        self.plan['char_2d_1.unity3d']['sha256'] = 'art version 2'
        self.rows['unrelated.unity3d'] = {}
        after = a.loading_index(self.cn, self.rows, self.plan)
        self.assertEqual(before, after)
        self.assertEqual(self.builder.call_count, 1)

    def test_dependency_and_selected_bundle_changes_rebuild_index(self):
        a.loading_index(self.cn, self.rows, self.plan)
        self.plan['char_2d_1.unity3d']['deps'].append('new_dependency.unity3d')
        a.loading_index(self.cn, self.rows, self.plan)
        self.plan['char_2d_2.unity3d'] = {'deps': []}
        a.loading_index(self.cn, self.rows, self.plan)
        self.assertEqual(self.builder.call_count, 3)

    def test_updated_official_index_rebuilds_cache(self):
        a.loading_index(self.cn, self.rows, self.plan)
        self.source.write_bytes(b'new official index')
        self.rows['win.unity3d'] = self.row()
        a.loading_index(self.cn, self.rows, self.plan)
        self.assertEqual(self.builder.call_count, 2)

    def test_corrupt_source_cannot_be_hidden_by_index_cache(self):
        a.loading_index(self.cn, self.rows, self.plan)
        self.source.write_bytes(b'x' * self.source.stat().st_size)
        with self.assertRaisesRegex(RuntimeError, '缺少与当前清单匹配'):
            a.loading_index(self.cn, self.rows, self.plan)

    def test_corrupt_output_is_rebuilt(self):
        before = a.loading_index(self.cn, self.rows, self.plan)
        Path(before['output']).write_bytes(b'broken')
        after = a.loading_index(self.cn, self.rows, self.plan)
        self.assertEqual(self.builder.call_count, 2)
        self.assertNotEqual(before['output'], after['output'])
        self.assertEqual(before['sha256'], after['sha256'])

    def test_no_patch_result_is_invalidated_when_dependencies_change(self):
        self.builder.side_effect = None
        self.builder.return_value = None
        self.assertIsNone(a.loading_index(self.cn, self.rows, self.plan))
        self.assertIsNone(a.loading_index(self.cn, self.rows, self.plan))
        self.assertEqual(self.builder.call_count, 1)
        self.plan['new.unity3d'] = {'deps': []}
        a.loading_index(self.cn, self.rows, self.plan)
        self.assertEqual(self.builder.call_count, 2)


class CompressionTests(unittest.TestCase):
    def test_fast_lz4_is_lossless_and_restores_library_configuration(self):
        import sys
        sys.path.insert(0, str(TOOL / '.runtime'))
        import lz4.block
        from UnityPy.helpers import CompressionHelper
        payload = b'unchanged texture and model bytes' * 4096
        before = CompressionHelper.COMPRESSION_MAP[2]

        class Bundle:
            def save(self, packer):
                self.packer = packer
                return CompressionHelper.COMPRESSION_MAP[2](payload)

        bundle = Bundle()
        packed = a.save_bundle(bundle)
        self.assertEqual(bundle.packer, 'lz4')
        self.assertEqual(lz4.block.decompress(packed, uncompressed_size=len(payload)), payload)
        self.assertIs(CompressionHelper.COMPRESSION_MAP[2], before)
        with patch.object(bundle, 'save', side_effect=RuntimeError('serialize failed')):
            with self.assertRaisesRegex(RuntimeError, 'serialize failed'):
                a.save_bundle(bundle)
        self.assertIs(CompressionHelper.COMPRESSION_MAP[2], before)


if __name__ == '__main__':
    unittest.main()
