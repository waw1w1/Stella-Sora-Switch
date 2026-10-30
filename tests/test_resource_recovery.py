"""Regression tests using synthetic clients; never write to installed games."""
import hashlib
import importlib.util
import json
from pathlib import Path
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
        backup.write_bytes(b'broken backup')
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


if __name__ == '__main__':
    unittest.main()
