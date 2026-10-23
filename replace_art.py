"""Version-checked, reversible Stella Sora local artwork replacement."""
from __future__ import annotations

import argparse
import base64
import collections
import copy
import datetime
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.runtime'))
STATE = ROOT / 'backups/active.json'
ART = re.compile(r'^(?:char_(?:2d_|avg_2d_|l2d_|2dsp_)|npc_(?:2d_|l2d_)|disc_|cg_|icon-|image(?:avg)?-)')
COVER = re.compile(r'^ui_gachacover_cover_\d+\.unity3d$')
COVER_TEXTURE = re.compile(r'^assets/assetbundles/ui_gachacover/cover_\d+/(?:sprite|textures?)/(?:(?:bg|zs|fx)_gacha_)[a-z0-9_]+\.png$')
UI_ART = {'ui_big_sprites.unity3d': re.compile(r'^assets/assetbundles/ui/big_sprites/zs_startowerhandbook_entrance\d+\.png$')}
HASH_CACHE = {}


def hash_key(path):
    st = path.stat()
    return (str(path), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


def digest(path, algorithm='sha256'):
    path = Path(path)
    key = hash_key(path)
    if key not in HASH_CACHE:
        # Both are required by backup safety and the official update manifest.
        hashes = {name: hashlib.new(name) for name in ('sha256', 'md5')}
        with path.open('rb') as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
                for h in hashes.values():
                    h.update(chunk)
        if hash_key(path) != key:
            raise RuntimeError('校验期间文件发生变化：' + str(path))
        HASH_CACHE[key] = {name: h.hexdigest() for name, h in hashes.items()}
    return HASH_CACHE[key][algorithm]


def verified_move(src, dst, expected):
    # Only reuse hashes for this exact verified file, within this process.
    if digest(src) != expected:
        raise RuntimeError('切换文件校验失败：' + str(src))
    hashes = HASH_CACHE[hash_key(src)]
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    HASH_CACHE[hash_key(dst)] = hashes


def switch_slot(cn, rel, kind):
    safe(cn, rel)
    key = hashlib.sha256(rel.replace('\\', '/').encode('utf-8')).hexdigest()
    return safe(cn, '.art-switch/' + key + '.' + kind)


def restore_entry(cn, entry):
    target = safe(cn, entry['path'])
    original_sha = entry['original']
    parked = switch_slot(cn, entry['path'], 'original')
    patched = switch_slot(cn, entry['path'], 'patched')
    if original_sha:
        # Validate the return source before taking the live file away.
        use_parked = (entry.get('switch') and parked.is_file()
                      and digest(parked) == original_sha)
        backup = safe(ROOT / 'backups/objects', original_sha)
        if not use_parked and (not backup.is_file() or digest(backup) != original_sha):
            raise RuntimeError('原件校验失败：' + entry['path'])
    if target.is_file():
        verified_move(target, patched, entry['patched'])
    if original_sha:
        if use_parked:
            verified_move(parked, target, original_sha)
        else:
            atomic_copy(backup, target)


def safe(root, rel):
    p = (root / rel).resolve()
    if not p.is_relative_to(root.resolve()) or p == root.resolve():
        raise RuntimeError(f'路径越界：{rel}')
    return p


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8', newline='\n') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + '.art-' + uuid.uuid4().hex + '.tmp')
    try:
        shutil.copyfile(src, tmp)
        with tmp.open('r+b') as f:
            os.fsync(f.fileno())
        if digest(src) != digest(tmp):
            raise RuntimeError(f'复制校验失败：{dst.name}')
        verified_move(tmp, dst, digest(src))
    finally:
        if tmp.exists():
            tmp.unlink()


def validate_roots(cn, jp):
    if cn == jp or not (cn / 'xtlr.exe').is_file() or not (jp / 'StellaSora.exe').is_file():
        raise ValueError('请选择国服 xtlr.exe 所在文件夹，以及日服 StellaSora.exe 所在文件夹（不是启动器目录）。')


def choose_folder(label, exe):
    print(f'请选择{label}目录（包含 {exe} 的文件夹）。', flush=True)
    # Embedded Python has no Tk; use the Windows folder picker without extra dependencies.
    script = """
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '__TITLE__'
$dialog.ShowNewFolderButton = $false
try {
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Write($dialog.SelectedPath)
    } else { exit 2 }
} finally { $dialog.Dispose() }
""".replace('__TITLE__', f'选择{label}目录（包含 {exe} 的文件夹）')
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-EncodedCommand', encoded],
                            capture_output=True, encoding='utf-8', errors='replace', creationflags=0x08000000)
    if result.returncode == 2:
        raise RuntimeError('已取消选择，原配置和游戏文件未修改。')
    if result.returncode or not result.stdout.strip():
        print('无法打开目录窗口，请粘贴文件夹路径；直接回车取消。')
        selected = input(f'{label}目录：').strip().strip('"')
        if not selected:
            raise RuntimeError('已取消选择，原配置和游戏文件未修改。')
    else:
        selected = result.stdout.strip()
    return Path(selected).resolve()


def configure_roots():
    cn = choose_folder('国服', 'xtlr.exe')
    if not (cn / 'xtlr.exe').is_file():
        raise RuntimeError('所选国服目录没有 xtlr.exe，未保存配置。')
    jp = choose_folder('日服', 'StellaSora.exe')
    validate_roots(cn, jp)
    state = read_state()
    if state and Path(state['cn']).resolve() != cn:
        raise RuntimeError('旧国服目录仍有未恢复的替换，请先一键恢复，再切换国服目录。未保存配置。')
    cfg = {}
    for key, path in (('cn', cn), ('jp', jp)):
        try:
            cfg[key] = Path(os.path.relpath(path, ROOT)).as_posix()
        except ValueError:  # Different Windows drives cannot use relative paths.
            cfg[key] = path.as_posix()
    save_json(ROOT / 'settings.json', cfg)
    print(f'目录已保存。\n国服：{cn}\n日服：{jp}', flush=True)
    return cn, jp


def roots():
    try:
        cfg = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8-sig'))
        if not all(isinstance(cfg[k], str) and cfg[k].strip() for k in ('cn', 'jp')):
            raise ValueError('目录配置为空。')
        cn, jp = [(ROOT / cfg[k]).resolve() for k in ('cn', 'jp')]
        validate_roots(cn, jp)
    except (OSError, ValueError, KeyError, TypeError):
        print('尚未设置游戏目录，或原路径已失效，需要选择两服目录。', flush=True)
        return configure_roots()
    return cn, jp


def ensure_closed():
    script = "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(xtlr|StellaSora)\\.exe$' -or $_.ExecutablePath -match 'StellaSora_(CN|JP)_Gamelauncher' } | Select-Object -ExpandProperty Name"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], capture_output=True, creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError('无法检查游戏进程，未修改资源。')
    if result.stdout.strip():
        raise RuntimeError('请退出国服、日服游戏及启动器后再运行。')


def manifest(root):
    p = root / 'Persistent_Store/ss_win.mani'
    meta, rows = {}, {}
    for line in p.read_text(encoding='utf-8-sig').splitlines():
        if line.startswith('$'):
            k, _, v = line.partition(':')
            if k in ('$CLIENT_VER', '$GAME_VER', '$RES_VER'):
                meta[k[1:]] = v
        else:
            bits = line.split('|')
            if len(bits) >= 4 and bits[0].endswith('.unity3d'):
                if Path(bits[0]).name != bits[0]:
                    raise RuntimeError('资源文件名格式发生变化，停止。')
                rows[bits[0]] = {'md5': bits[1].zfill(32).lower(), 'size': int(bits[2])}
    if not rows or len(meta) != 3:
        raise RuntimeError('资源清单格式变化或不完整。')
    return meta, rows, digest(p)


def layers(root, name):
    data = 'xtlr_Data' if (root / 'xtlr.exe').exists() else 'StellaSora_Data'
    return [root / 'Patch_Store/AssetBundles' / name,
            root / 'Persistent_Store/AssetBundles' / name,
            root / data / 'StreamingAssets/InstallResource' / name]


def original(root, name, row):
    for p in layers(root, name):
        if p.is_file() and p.stat().st_size == row['size'] and digest(p, 'md5') == row['md5']:
            return p
    raise RuntimeError(f'{root.name} 缺少与当前清单匹配的 {name}；请先完成更新或资源修复。')


def read_state():
    return json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else None


def client_values(path):
    if not path.is_file():
        return {}
    return dict(line.split(':', 1) for line in path.read_text(encoding='utf-8-sig').splitlines() if ':' in line)


def snapshot_client_state(cn):
    entries = []
    for name in ('file_versions.dat', 'prefs.dat'):
        path = cn / name
        if path.is_file():
            sha = digest(path)
            backup = ROOT / 'backups/objects' / sha
            if not backup.exists():
                atomic_copy(path, backup)
            if digest(backup) != sha:
                raise RuntimeError('客户端状态备份校验失败：' + name)
            entries.append({'path': name, 'original': sha})
    return entries


def recover_update_bases(cn, state):
    """Recover missing originals only when the current official manifest agrees."""
    _, rows, _ = manifest(cn)
    recovered = 0
    for e in state['entries']:
        p = safe(cn, e['path'])
        row = rows.get(p.name)
        if p.exists() or not e['original'] or not row:
            continue
        b = safe(ROOT / 'backups/objects', e['original'])
        if not b.is_file() or digest(b) != e['original']:
            raise RuntimeError('备份损坏，停止补回：' + e['path'])
        if b.stat().st_size == row['size'] and digest(b, 'md5') == row['md5']:
            atomic_copy(b, p)
            recovered += 1
    return recovered


def restore_file_index_entries(cn, state):
    """Restore exact snapshot lines for verified originals, retaining other updates."""
    entry = next((e for e in state.get('client_state', []) if e['path'] == 'file_versions.dat'), None)
    path = cn / 'file_versions.dat'
    if not entry or not path.is_file():
        return 0
    backup = safe(ROOT / 'backups/objects', entry['original'])
    if not backup.is_file() or digest(backup) != entry['original']:
        raise RuntimeError('客户端索引备份损坏，停止恢复。')
    originals = {}
    for e in state['entries']:
        p = safe(cn, e['path'])
        if e['original'] and p.is_file() and digest(p) == e['original']:
            key = ('\\' + str(p.relative_to(cn)).replace('/', '\\')).encode('utf-8')
            originals[key] = p
    replacements = {}
    for line in backup.read_bytes().splitlines(keepends=True):
        key, sep, value = line.rstrip(b'\r\n').partition(b'|')
        if key in originals and sep and value.decode('ascii').lower().zfill(32) == digest(originals[key], 'md5'):
            replacements[key] = line
    current = path.read_bytes()
    output, seen, changed = [], set(), 0
    for line in current.splitlines(keepends=True):
        key = line.rstrip(b'\r\n').partition(b'|')[0]
        replacement = replacements.get(key, line)
        if key in replacements:
            if key in seen:
                changed += 1
                continue
            seen.add(key)
        changed += int(replacement != line)
        output.append(replacement)
    result = b''.join(output)
    for key, line in replacements.items():
        if key not in seen:
            if result and not result.endswith(b'\n'):
                result += b'\n'
            result += line
            changed += 1
    if changed:
        # Keep a recoverable copy and use binary I/O: CRLF regeneration breaks the client.
        saved = ROOT / 'backups/objects' / digest(path)
        if not saved.exists():
            atomic_copy(path, saved)
        tmp = ROOT / '.work' / ('index-' + uuid.uuid4().hex)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.write_bytes(result)
            atomic_copy(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        print(f'已恢复 {changed} 条原文件索引，保留其他客户端记录。', flush=True)
    return changed


def check_restored_bases(cn, state):
    _, rows, _ = manifest(cn)
    missing = []
    for name in sorted({Path(e['path']).name for e in state['entries']}):
        if name in rows:
            try:
                original(cn, name, rows[name])
            except RuntimeError:
                missing.append(name)
    if missing:
        raise RuntimeError('恢复后仍缺少官方更新所需原件，请先修复游戏资源：' + '、'.join(missing[:12]))


def restore(cn):
    state = read_state()
    if not state:
        # Older versions archived incomplete recoveries. Permit a scoped retry.
        history = ROOT / 'backups/history'
        for record in sorted(history.glob('*.json'), reverse=True):
            previous = json.loads(record.read_text(encoding='utf-8'))
            if previous.get('cn') != str(cn):
                continue
            count = recover_update_bases(cn, previous)
            index_count = restore_file_index_entries(cn, previous)
            check_restored_bases(cn, previous)
            print(f'当前无已应用替换；历史恢复检查通过，补回 {count} 个原件、{index_count} 条索引。', flush=True)
            return
        print('没有待恢复的替换记录或历史备份。', flush=True)
        return
    if state['cn'] != str(cn):
        raise RuntimeError('备份属于另一国服目录，停止恢复。')
    restored, preserved = 0, []
    print('校验恢复备份……', flush=True)
    # Validate every backup before restoring any file.
    for e in state['entries'] + state.get('client_state', []):
        if e['original']:
            b = safe(ROOT / 'backups/objects', e['original'])
            if not b.is_file() or digest(b) != e['original']:
                raise RuntimeError('备份损坏，停止恢复：' + e['path'])
    print('核对原件、切换副本与官方更新状态……', flush=True)
    _, current_rows, current_manifest = manifest(cn)
    prefs_snapshot = next((e for e in state.get('client_state', []) if e['path'] == 'prefs.dat'), None)
    old_prefs = client_values(safe(ROOT / 'backups/objects', prefs_snapshot['original'])) if prefs_snapshot else {}
    new_prefs = client_values(cn / 'prefs.dat')
    no_update = (current_manifest == state['fingerprints'][0] and prefs_snapshot
                 and old_prefs.get('LOCAL_BIN_DIFF_PATCH_RESOURCE_VERSION') == new_prefs.get('LOCAL_BIN_DIFF_PATCH_RESOURCE_VERSION')
                 and old_prefs.get('PACKAGE_TAGS') == new_prefs.get('PACKAGE_TAGS'))
    # Finish interrupted two-rename switches before normal update recovery.
    for e in state['entries']:
        parked = switch_slot(cn, e['path'], 'original')
        if e.get('switch') and not safe(cn, e['path']).exists() and e['original'] and parked.is_file():
            row = current_rows.get(Path(e['path']).name)
            matches_current = (row and parked.stat().st_size == row['size']
                               and digest(parked, 'md5') == row['md5'])
            if no_update or matches_current:
                restore_entry(cn, e)
                restored += 1
    restored += recover_update_bases(cn, state)
    for e in state['entries']:
        p = safe(cn, e['path'])
        current = digest(p) if p.is_file() else None
        if current == e['patched']:
            restore_entry(cn, e)
            restored += 1
        elif current != e['original']:
            preserved.append(e['path'])
    # Return to the exact pre-replacement state, never regenerate client data.
    # A newer official update or a changed language package must be retained.
    snapshots = state.get('client_state', [])
    prefs_entry = next((e for e in snapshots if e['path'] == 'prefs.dat'), None)
    before = client_values(safe(ROOT / 'backups/objects', prefs_entry['original'])) if prefs_entry else {}
    now = client_values(cn / 'prefs.dat')
    try:
        patch_not_advanced = (int(now.get('LOCAL_BIN_DIFF_PATCH_RESOURCE_VERSION', '0'))
                              <= int(before.get('LOCAL_BIN_DIFF_PATCH_RESOURCE_VERSION', '0')))
    except ValueError:
        patch_not_advanced = False
    mani = cn / 'Persistent_Store/ss_win.mani'
    can_restore_state = (not preserved and snapshots
                         and mani.is_file() and digest(mani) == state['fingerprints'][0]
                         and now.get('PACKAGE_TAGS') == before.get('PACKAGE_TAGS')
                         and patch_not_advanced)
    if can_restore_state:
        for e in snapshots:
            atomic_copy(safe(ROOT / 'backups/objects', e['original']), safe(cn, e['path']))
        print('客户端缓存与版本记录已按替换前的原始字节恢复。', flush=True)
    elif snapshots:
        print('检测到后续更新或客户端状态变化，保留当前客户端记录。', flush=True)
        restore_file_index_entries(cn, state)
    # Never archive an incomplete recovery as success; keep active.json for retries.
    check_restored_bases(cn, state)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    save_json(ROOT / f'backups/history/{stamp}.json', dict(state, restored=restored, preserved_updates=preserved))
    STATE.unlink()
    print(f'恢复完成：{restored} 个文件；保留更新器或其他程序改动的文件 {len(preserved)} 个。', flush=True)


def inspect_bundle(p):
    import UnityPy
    env = UnityPy.load(str(p))
    if not env.assets:
        raise RuntimeError('无法解析资源包：' + p.name)
    ab = next(o for o in env.objects if o.type.name == 'AssetBundle')
    tree = ab.read_typetree()
    return env, ab, tree


def normalize_art_path(path):
    """Normalize region folders and whitespace immediately before extensions."""
    return '/'.join(re.sub(r'\s+(?=\.|$)', '', part) for part in path.split('/')).replace('/cen/', '/noncen/')


def normalized_entries(entries):
    result = {}
    for path, value in entries.items():
        key = normalize_art_path(path)
        if key in result and result[key][0] != path:
            raise RuntimeError('日服公开路径归一后存在歧义：' + path)
        result[key] = (path, value)
    return result


def verify_texture_patch(ce, ct, output, changed, unchanged, extra_objects=None):
    output.write_bytes(ce.file.save(packer='lz4'))
    ve, _, vt = inspect_bundle(output)
    actual = {o.path_id: o for o in ve.objects}
    if vt != ct or set(actual) != {o.path_id for o in ce.objects}:
        raise RuntimeError('贴图补丁改变对象集合或公开入口：' + output.name)
    for path_id, expected in unchanged.items():
        if hashlib.sha256(actual[path_id].get_raw_data()).hexdigest() != expected:
            raise RuntimeError('贴图补丁改变非目标对象：' + output.name)
    for path_id, expected in (extra_objects or {}).items():
        if hashlib.sha256(actual[path_id].get_raw_data()).hexdigest() != expected:
            raise RuntimeError('剧情Sprite保存校验失败：' + output.name)
    for entry in changed:
        a = actual[entry['id']].read()
        if [a.m_Width, a.m_Height] != entry['size'] or hashlib.sha256(a.get_image_data()).hexdigest() != entry['image_sha256']:
            raise RuntimeError('贴图补丁图像保存校验失败：' + output.name)


def patch_cover_textures(cp, jpfile, output, texture_pattern=COVER_TEXTURE):
    """Keep the CN UI bundle and replace only matched artwork texture payloads."""
    from PIL import Image
    ce, _, ct = inspect_bundle(cp)
    je, _, jt = inspect_bundle(jpfile)
    if len(ce.assets) != 1 or len(je.assets) != 1 or ce.assets[0].unity_version != je.assets[0].unity_version:
        raise RuntimeError('招募封面资源格式不兼容：' + cp.name)
    cnobjects = {o.path_id: o for o in ce.objects}
    jpobjects = {o.path_id: o for o in je.objects}
    # Sprite and Texture2D can share the same container path; keep the texture entry.
    jpkeys = {path: info for path, info in jt['m_Container']
              if info['asset']['m_FileID'] == 0
              and jpobjects.get(info['asset']['m_PathID']) is not None
              and jpobjects[info['asset']['m_PathID']].type.name == 'Texture2D'}
    unchanged = {o.path_id: hashlib.sha256(o.get_raw_data()).hexdigest() for o in ce.objects}
    changed, ignored = [], []
    for path, info in ct['m_Container']:
        # Localized labels and prefabs are deliberately not copied.
        if not texture_pattern.fullmatch(path) or re.search(r'(?:lang|title|text|logo)|_(?:cn|jp|en)(?:_|\.|$)', Path(path).name):
            continue
        ptr = info['asset']
        other = jpkeys.get(path, {}).get('asset')
        if ptr['m_FileID'] != 0 or not other or other['m_FileID'] != 0:
            continue
        co, jo = cnobjects.get(ptr['m_PathID']), jpobjects.get(other['m_PathID'])
        if co is None or jo is None or co.type.name != 'Texture2D' or jo.type.name != 'Texture2D':
            continue
        if co.path_id not in unchanged:
            continue
        c, j = co.read(), jo.read()
        cb, jb = bytes(c.get_image_data()), bytes(j.get_image_data())
        layout = ('m_Width', 'm_Height', 'm_TextureFormat', 'm_MipCount', 'm_ColorSpace')
        same_layout = all(getattr(c, k, None) == getattr(j, k, None) for k in layout)
        if same_layout and cb == jb:
            continue
        # A radically different canvas may be a different asset despite its name.
        ratio = (c.m_Width / c.m_Height) / (j.m_Width / j.m_Height)
        if not 0.98 <= ratio <= 1.02:
            ignored.append({'path': path, 'reason': '两服画布比例不兼容'})
            continue
        original_size = (c.m_Width, c.m_Height)
        if same_layout:
            c.image_data = jb
            if c.m_StreamData is not None:
                c.m_StreamData.path, c.m_StreamData.offset, c.m_StreamData.size = '', 0, 0
        else:
            pixels = j.image
            if pixels.size != original_size:
                pixels = pixels.resize(original_size, Image.Resampling.LANCZOS)
            c.set_image(pixels, target_format=c.m_TextureFormat, mipmap_count=c.m_MipCount or 1)
        c.save()
        changed.append({'path': path, 'id': co.path_id, 'size': list(original_size),
                        'image_sha256': hashlib.sha256(c.get_image_data()).hexdigest(),
                        'resized': original_size != (j.m_Width, j.m_Height)})
        del unchanged[co.path_id]
    if not changed:
        return None
    verify_texture_patch(ce, ct, output, changed, unchanged)
    return {'textures': changed, 'ignored_textures': ignored,
            'deps': [d + '.unity3d' for d in ct['m_Dependencies']]}


def patch_story_atlas(cp, jpfile, output):
    """Keep CN story objects and dependencies; transplant compatible atlas artwork."""
    ce, _, ct = inspect_bundle(cp)
    je, _, _ = inspect_bundle(jpfile)
    if len(ce.assets) != 1 or len(je.assets) != 1:
        return None
    cf, jf = ce.assets[0], je.assets[0]
    if cf.unity_version != jf.unity_version or [x.path for x in cf.externals] != [x.path for x in jf.externals]:
        return None
    def inventory(env):
        objects, names = {}, {}
        for obj in env.objects:
            if obj.type.name not in ('Texture2D', 'Sprite', 'SpriteAtlas'):
                continue
            tree = obj.read_typetree()
            name = tree['m_Name']
            if obj.type.name == 'Texture2D':
                if not name.startswith('sactx-'):
                    return None
                name = re.sub(r'-[0-9a-f]{8}$', '', name)
            key = (obj.type.name, name)
            if key in objects:
                return None
            objects[key] = obj
            names[obj.path_id] = key
        return objects, names
    ci, ji = inventory(ce), inventory(je)
    if ci is None or ji is None or not ci[0] or set(ci[0]) != set(ji[0]):
        return None
    co, cnames = ci
    jo, jnames = ji
    def normalized_tree(value, names):
        if isinstance(value, dict):
            if value.get('m_FileID') == 0 and value.get('m_PathID') in names:
                return names[value['m_PathID']]
            return {k: normalized_tree(v, names) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalized_tree(v, names) for v in value]
        return value
    meshes, payloads = [], []
    mesh_fields = ('m_IndexBuffer', 'm_SubMeshes', 'm_VertexData')
    for key, obj in co.items():
        other = jo[key]
        ctree, jtree = obj.read_typetree(), other.read_typetree()
        if key[0] == 'Texture2D':
            c, j = obj.read(), other.read()
            layout = ('m_Width', 'm_Height', 'm_TextureFormat', 'm_MipCount', 'm_ColorSpace')
            if any(getattr(c, field, None) != getattr(j, field, None) for field in layout):
                return None
            cb, jb = bytes(c.get_image_data()), bytes(j.get_image_data())
            if cb != jb:
                payloads.append((obj, c, jb))
            continue
        clean_c, clean_j = normalized_tree(ctree, cnames), normalized_tree(jtree, jnames)
        if clean_c == clean_j:
            continue
        # Atlas UVs, packing, sprite pivots and references must already agree.
        if key[0] != 'Sprite' or 'm_RD' not in ctree or 'm_RD' not in jtree:
            return None
        for field in mesh_fields:
            if field not in ctree['m_RD'] or field not in jtree['m_RD']:
                return None
            clean_c['m_RD'][field] = copy.deepcopy(clean_j['m_RD'][field])
            ctree['m_RD'][field] = copy.deepcopy(jtree['m_RD'][field])
        if clean_c != clean_j:
            return None
        meshes.append((obj, ctree))
    if not payloads:
        return None
    unchanged = {o.path_id: hashlib.sha256(o.get_raw_data()).hexdigest() for o in ce.objects}
    changed, mesh_hashes = [], {}
    for obj, c, payload in payloads:
        c.image_data = payload
        if c.m_StreamData is not None:
            c.m_StreamData.path, c.m_StreamData.offset, c.m_StreamData.size = '', 0, 0
        c.save()
        changed.append({'name': c.m_Name, 'id': obj.path_id, 'size': [c.m_Width, c.m_Height],
                        'image_sha256': hashlib.sha256(payload).hexdigest()})
        del unchanged[obj.path_id]
    for obj, tree in meshes:
        payload = obj.save_typetree(tree)
        mesh_hashes[obj.path_id] = hashlib.sha256(payload).hexdigest()
        del unchanged[obj.path_id]
    verify_texture_patch(ce, ct, output, changed, unchanged, mesh_hashes)
    return {'textures': changed, 'updated_sprite_meshes': len(meshes),
            'deps': [d + '.unity3d' for d in ct['m_Dependencies']]}


def object_references(value):
    if isinstance(value, dict):
        if 'm_FileID' in value and 'm_PathID' in value:
            if value['m_PathID']:
                yield value
        else:
            for child in value.values():
                yield from object_references(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from object_references(child)


def preserve_npc_motions(name, ce, ct, je, jt, missing):
    """Retain actual CN-only motions when they are compatible with the JP model."""
    norm = normalize_art_path
    pattern = r'^assets/assetbundles/actor2d_l2d/cen/npc/\d+/live2d/mtn/[a-z0-9_]+\.(?:anim|fade\.asset|motion3\.json)$'
    if not name.startswith('npc_l2d_') or not all(re.fullmatch(pattern, p) for p in missing):
        return None
    if len(ce.assets) != 1 or len(je.assets) != 1:
        return None
    cf, jf = ce.assets[0], je.assets[0]
    if cf.unity_version != jf.unity_version or [e.path for e in cf.externals] != [e.path for e in jf.externals]:
        return None
    ck, jk = dict(ct['m_Container']), dict(jt['m_Container'])
    sources, trees, type_indices = {}, {}, {}
    for path in missing:
        ptr = ck[path]['asset']
        if ptr['m_FileID'] != 0 or ptr['m_PathID'] in jf.objects:
            return None
        obj = cf.objects[ptr['m_PathID']]
        if obj.type.name not in ('AnimationClip', 'TextAsset', 'MonoBehaviour'):
            return None
        tree = obj.read_typetree()
        if obj.type.name == 'MonoBehaviour' and 'ParameterIds' not in tree:
            return None
        compatible = [i for i, typ in enumerate(jf.types)
                      if typ.class_id == obj.serialized_type.class_id
                      and typ.old_type_hash == obj.serialized_type.old_type_hash
                      and typ.script_id == obj.serialized_type.script_id]
        if len(compatible) != 1:
            return None
        sources[obj.path_id], trees[obj.path_id], type_indices[obj.path_id] = obj, tree, compatible[0]
    # Curves may only target parameters already used by the JP model's own motions.
    jp_params, jp_paths = set(), set()
    for obj in je.objects:
        if obj.type.name == 'MonoBehaviour':
            jp_params.update(obj.read_typetree().get('ParameterIds', []))
        elif obj.type.name == 'AnimationClip':
            jp_paths.update(c['path'] for c in obj.read_typetree().get('m_FloatCurves', []))
    def reference_ok(ptr):
        pid = ptr['m_PathID']
        if not pid:
            return True
        if ptr['m_FileID'] != 0:
            return False
        return pid in sources or (pid in jf.objects and pid in cf.objects
                                  and cf.objects[pid].get_raw_data() == jf.objects[pid].get_raw_data())
    for tree in trees.values():
        if not set(tree.get('ParameterIds', [])).issubset(jp_params):
            return None
        if not {c['path'] for c in tree.get('m_FloatCurves', [])}.issubset(jp_paths):
            return None
        if not all(reference_ok(p) for p in object_references(tree)):
            return None
    list_updates = []
    imported_fades = {pid for pid, tree in trees.items() if 'ParameterIds' in tree}
    for path, info in ct['m_Container']:
        if not path.endswith('.fademotionlist.asset'):
            continue
        other = jk.get(norm(path))
        if not other:
            return None
        cobj, jobj = cf.objects[info['asset']['m_PathID']], jf.objects[other['asset']['m_PathID']]
        ctree, jtree = cobj.read_typetree(), jobj.read_typetree()
        fade_ptrs, instance_ids = ctree.get('CubismFadeMotionObjects', []), ctree.get('MotionInstanceIds', [])
        if len(fade_ptrs) != len(instance_ids):
            return None
        copied = set()
        for ptr, instance_id in zip(fade_ptrs, instance_ids):
            if ptr['m_PathID'] in imported_fades:
                if instance_id in jtree['MotionInstanceIds']:
                    return None
                jtree['MotionInstanceIds'].append(instance_id)
                jtree['CubismFadeMotionObjects'].append(copy.deepcopy(ptr))
                copied.add(ptr['m_PathID'])
        if copied:
            list_updates.append((jobj, jtree, copied))
    if imported_fades != set().union(*(v[2] for v in list_updates)):
        return None
    additions = []
    for path in missing:
        info = copy.deepcopy(ck[path])
        preload = copy.deepcopy(ct['m_PreloadTable'][info['preloadIndex']:info['preloadIndex'] + info['preloadSize']])
        if len(preload) != info['preloadSize'] or not all(reference_ok(p) for p in preload):
            return None
        additions.append((path, info, preload))
    # All validation is complete before mutating the in-memory JP bundle.
    for pid, obj in sources.items():
        cloned = copy.copy(obj)
        cloned.assets_file = jf
        cloned.type_id = type_indices[pid]
        cloned.serialized_type = jf.types[cloned.type_id]
        cloned.data = obj.get_raw_data()
        jf.objects[pid] = cloned
    for jobj, tree, _ in list_updates:
        jobj.save_typetree(tree)
    for path, info, preload in additions:
        info['preloadIndex'] = len(jt['m_PreloadTable'])
        jt['m_PreloadTable'].extend(preload)
        jt['m_Container'].append((norm(path), info))
    return [{'path': path, 'id': ck[path]['asset']['m_PathID'],
             'sha256': hashlib.sha256(sources[ck[path]['asset']['m_PathID']].get_raw_data()).hexdigest()}
            for path in missing]


def build_plan(cn, jp):
    cm, cr, ch = manifest(cn)
    jm, jr, jh = manifest(jp)
    for k in ('CLIENT_VER', 'GAME_VER'):
        if cm[k] != jm[k]:
            raise RuntimeError(f'两服 {k} 不一致（国服 {cm[k]} / 日服 {jm[k]}），未替换；请同步更新。')
    print(f"客户端 {cm['CLIENT_VER']}，资源 {cm['GAME_VER']}；核对美术资源……", flush=True)
    names = sorted(n for n in cr.keys() & jr.keys() if (ART.match(n) or COVER.fullmatch(n) or n in UI_ART) and cr[n] != jr[n])
    work = ROOT / '.work/staging' / uuid.uuid4().hex
    work.mkdir(parents=True)
    plan, skipped = {}, {}
    norm = normalize_art_path
    for i, name in enumerate(names, 1):
        cp, jpfile = original(cn, name, cr[name]), original(jp, name, jr[name])
        if COVER.fullmatch(name) or name in UI_ART:
            output = work / name
            details = patch_cover_textures(cp, jpfile, output, UI_ART.get(name, COVER_TEXTURE))
            if details:
                plan[name] = dict(details, output=str(output), source=str(jpfile.relative_to(jp)),
                                  aliases=0, sha256=digest(output), mode='国服界面仅替换美术贴图')
                print(f"界面美术 {name}：替换 {len(details['textures'])} 张贴图，其他对象保持国服。", flush=True)
            gc.collect()
            continue
        ce, ca, ct = inspect_bundle(cp)
        je, ja, jt = inspect_bundle(jpfile)
        if name.startswith('char_avg_2d_') and any(d + '.unity3d' not in cr for d in jt['m_Dependencies']):
            output = work / name
            details = patch_story_atlas(cp, jpfile, output)
            if details:
                unlisted = [d for d in details['deps'] if d not in cr]
                details['deps'] = [d for d in details['deps'] if d in cr]
                plan[name] = dict(details, output=str(output), source=str(jpfile.relative_to(jp)),
                                  aliases=0, sha256=digest(output), mode='保留国服剧情对象、依赖及图集布局',
                                  preserved_cn_unlisted_dependencies=unlisted)
                print(f"剧情图集 {name}：替换 {len(details['textures'])} 张贴图，适配 {details['updated_sprite_meshes']} 个Sprite网格。", flush=True)
                del ce, ca, ct, je, ja, jt
                gc.collect()
                continue
        cnkeys, jpkeys = dict(ct['m_Container']), dict(jt['m_Container'])
        normalized = normalized_entries(jpkeys)
        missing = [k for k in cnkeys if norm(k) not in normalized]
        preserved_motions = preserve_npc_motions(name, ce, ct, je, jt, missing) if missing else None
        if preserved_motions:
            jpkeys = dict(jt['m_Container'])
            normalized = normalized_entries(jpkeys)
            missing = [k for k in cnkeys if norm(k) not in normalized]
            print(f'{name}：保留 {len(preserved_motions)} 个兼容国服动作资源，采用日服美术。', flush=True)
        # Never guess substitute motions or silently drop CN public entry points.
        if missing:
            skipped[name] = {'reason': '日服缺少国服公开资源入口', 'missing': missing}
            continue
        if sorted(a.name for a in ce.assets) != sorted(a.name for a in je.assets):
            skipped[name] = {'reason': '两服资源内部标识不一致'}
            continue
        if any(a.unity_version != b.unity_version for a, b in zip(ce.assets, je.assets)):
            raise RuntimeError('两服 Unity 资源格式版本不一致：' + name)
        aliases = 0
        jp_object_hashes = {o.path_id: hashlib.sha256(o.data if o.data is not None else o.get_raw_data()).hexdigest()
                            for o in je.objects if o.path_id != ja.path_id}
        for k in cnkeys:
            if k not in jpkeys:
                jt['m_Container'].append((k, copy.deepcopy(normalized[norm(k)][1])))
                aliases += 1
        output = work / name
        if aliases or preserved_motions:
            jt['m_Container'].sort(key=lambda pair: pair[0])
            ja.save_typetree(jt)
            output.write_bytes(je.file.save(packer='lz4'))
            ve, va, vt = inspect_bundle(output)
            if not set(cnkeys).issubset(dict(vt['m_Container'])):
                raise RuntimeError('国服资源路径兼容校验失败：' + name)
            saved = {o.path_id: o for o in ve.objects}
            if set(saved) != {o.path_id for o in je.objects} or any(
                    hashlib.sha256(saved[pid].get_raw_data()).hexdigest() != sha
                    for pid, sha in jp_object_hashes.items()):
                raise RuntimeError('路径兼容补丁改变日服模型或其他对象：' + name)
            if preserved_motions:
                for motion in preserved_motions:
                    if hashlib.sha256(saved[motion['id']].get_raw_data()).hexdigest() != motion['sha256']:
                        raise RuntimeError('国服补充动作保存校验失败：' + name)
            del ve, va, vt
        else:
            shutil.copyfile(jpfile, output)
        deps = [x + '.unity3d' for x in jt['m_Dependencies']]
        # A new dependency absent from CN's manifest can break runtime loading.
        if any(d not in cr for d in deps):
            skipped[name] = {'reason': '日服依赖包在国服资源清单中缺失'}
            continue
        plan[name] = {'output': str(output), 'source': str(jpfile.relative_to(jp)),
                      'aliases': aliases, 'deps': deps, 'sha256': digest(output),
                      'preserved_cn_motions': preserved_motions or []}
        if i % 10 == 0 or i == len(names):
            print(f'资源结构核对 {i}/{len(names)}', flush=True)
        del ce, ca, ct, je, ja, jt
        gc.collect()
    # Remove entire dependent groups when one of their replacement bundles is unsafe.
    while True:
        rejected = [n for n, p in plan.items() if any(d in skipped for d in p['deps'])]
        if not rejected:
            break
        for n in rejected:
            skipped[n] = {'reason': '依赖已跳过的不兼容美术包'}
            del plan[n]
    # Retain the CN loading index; add only dependencies needed by selected artwork.
    # Copying the JP index wholesale would also change unrelated CN game resources.
    we, wa, wt = inspect_bundle(original(cn, 'win.unity3d', cr['win.unity3d']))
    mo = next(o for o in we.objects if o.type.name == 'AssetBundleManifest')
    mt = mo.read_typetree()
    before = copy.deepcopy(mt)
    name_to_id = {n: i for i, n in mt['AssetBundleNames']}
    infos = dict(mt['AssetBundleInfos'])
    changed_ids = set()
    for n, p in plan.items():
        idx = name_to_id[n.removesuffix('.unity3d')]
        deps = {name_to_id[d.removesuffix('.unity3d')] for d in p['deps']}
        deps.update(infos[idx]['AssetBundleDependencies'])
        if deps != set(infos[idx]['AssetBundleDependencies']):
            infos[idx]['AssetBundleDependencies'] = sorted(deps)
            changed_ids.add(idx)
    if changed_ids:
        for idx, entry in before['AssetBundleInfos']:
            if idx not in changed_ids and entry != infos[idx]:
                raise RuntimeError('无关资源的加载索引发生变化，停止。')
        mo.save_typetree(mt)
        output = work / 'win.unity3d'
        output.write_bytes(we.file.save(packer='lz4'))
        ve, _, _ = inspect_bundle(output)
        actual = next(o for o in ve.objects if o.type.name == 'AssetBundleManifest').read_typetree()
        if actual != mt:
            raise RuntimeError('美术依赖索引保存校验失败。')
        plan['win.unity3d'] = {'output': str(output), 'source': '国服美术加载索引局部兼容',
                               'aliases': 0, 'deps': [], 'sha256': digest(output),
                               'updated_art_dependencies': len(changed_ids)}
    report = {'versions': {'cn': cm, 'jp': jm}, 'fingerprints': [ch, jh], 'recipe': digest(Path(__file__)),
              'bundles': plan, 'skipped': skipped,
              'scope': '角色立绘、剧情图集、Live2D、NPC、秘纹、CG、图标、招募封面及星塔手册美术贴图；保留国服脚本、配置表、语音、界面对象和程序'}
    save_json(ROOT / 'reports/latest.json', report)
    print(f"计划：{len(plan) - int('win.unity3d' in plan)} 个美术包，{int('win.unity3d' in plan)} 个局部兼容索引；跳过 {len(skipped)} 个不兼容包。", flush=True)
    for n, entry in skipped.items():
        print('跳过 ' + n + '：' + entry['reason'], flush=True)
    return report


def reusable_plan(cn, jp):
    cache = ROOT / '.work/validated-plan.json'
    cm, cr, ch = manifest(cn)
    jm, jr, jh = manifest(jp)
    if cache.is_file():
        try:
            report = json.loads(cache.read_text(encoding='utf-8'))
            valid = (report['fingerprints'] == [ch, jh]
                     and report['recipe'] == digest(Path(__file__)))
            if valid:
                for name in cr.keys() & jr.keys():
                    if (ART.match(name) or COVER.fullmatch(name) or name in UI_ART) and cr[name] != jr[name]:
                        original(cn, name, cr[name])
                        original(jp, name, jr[name])
                original(cn, 'win.unity3d', cr['win.unity3d'])
                for item in report['bundles'].values():
                    output = Path(item['output'])
                    if not output.resolve().is_relative_to((ROOT / '.work').resolve()):
                        valid = False
                        break
                    if not output.is_file() or digest(output) != item['sha256']:
                        valid = False
                        break
            if valid:
                print('复用已验证美术补丁，源文件与补丁内容校验通过。', flush=True)
                return report
        except (KeyError, ValueError, OSError):
            pass
    report = build_plan(cn, jp)
    save_json(cache, report)
    return report


def verify(cn, verbose=True):
    state = read_state()
    if not state:
        if verbose:
            print('当前没有已应用的替换。')
        return False
    if state['cn'] != str(cn):
        raise RuntimeError('替换记录属于另一个目录。')
    bad = []
    for e in state['entries']:
        p = safe(cn, e['path'])
        if not p.is_file() or digest(p) != e['patched']:
            bad.append(e['path'])
    if verbose:
        print(f"文件校验：{len(state['entries']) - len(bad)}/{len(state['entries'])} 一致。")
        for path in bad[:20]:
            print('已变化：' + path)
    return not bad


def apply(cn, jp):
    # A mismatched JP update must not undo a working CN artwork replacement.
    cm, _, _ = manifest(cn)
    jm, _, _ = manifest(jp)
    for key in ('CLIENT_VER', 'GAME_VER'):
        if cm[key] != jm[key]:
            raise RuntimeError(f'两服 {key} 不一致（国服 {cm[key]} / 日服 {jm[key]}），保留现有替换；请先运行一键恢复，正常更新两服后再替换。')
    old = read_state()
    if old and old.get('complete') and old.get('recipe') == digest(Path(__file__)) and old['fingerprints'] == [manifest(cn)[2], manifest(jp)[2]] and verify(cn, False):
        print('当前版本已经替换，全部文件校验一致，无需重复写入。')
        return
    if old:
        restore(cn)
    report = reusable_plan(cn, jp)
    if not report['bundles']:
        print('本版本没有可安全替换的差异美术包。')
        return
    state = {'cn': str(cn), 'fingerprints': report['fingerprints'], 'versions': report['versions'], 'recipe': report['recipe'],
             'complete': False, 'entries': []}
    print('核对目标文件、备份与同盘切换缓存……', flush=True)
    changes = []
    for name, p in report['bundles'].items():
        targets = {target for target in layers(cn, name) if target.is_file()}
        # Persistent is the writable game resource tier; also replace existing shadow copies.
        targets.add(cn / 'Persistent_Store/AssetBundles' / name)
        for target in sorted(targets):
            previous = digest(target) if target.exists() else None
            if previous != p['sha256']:
                changes.append((Path(p['output']), target, previous, p['sha256']))
    needed = 0
    for source, target, previous, patched in changes:
        slot = switch_slot(cn, str(target.relative_to(cn)), 'patched')
        if not slot.is_file() or digest(slot) != patched:
            needed += source.stat().st_size
        if previous and not (ROOT / 'backups/objects' / previous).exists():
            needed += target.stat().st_size
    if shutil.disk_usage(cn).free < needed + 512 * 1024 * 1024:
        raise RuntimeError('磁盘空间不足以安全备份和替换。')
    ensure_closed()
    if report['fingerprints'] != [manifest(cn)[2], manifest(jp)[2]]:
        raise RuntimeError('准备过程中客户端已更新，请重试。')
    state['client_state'] = snapshot_client_state(cn)
    # Save all originals and the complete recovery journal before first game-file write.
    for source, target, previous, patched in changes:
        if previous:
            backup = ROOT / 'backups/objects' / previous
            if not backup.exists():
                atomic_copy(target, backup)
            if digest(backup) != previous:
                raise RuntimeError('原文件备份校验失败：' + target.name)
        rel = str(target.relative_to(cn))
        slot = switch_slot(cn, rel, 'patched')
        if not slot.is_file() or digest(slot) != patched:
            atomic_copy(source, slot)
        state['entries'].append({'path': rel, 'original': previous, 'patched': patched, 'switch': True})
    save_json(STATE, state)
    try:
        for index, (source, target, previous, patched) in enumerate(changes, 1):
            if (digest(target) if target.exists() else None) != previous:
                raise RuntimeError('文件被其他程序改动：' + target.name)
            rel = str(target.relative_to(cn))
            if previous:
                verified_move(target, switch_slot(cn, rel, 'original'), previous)
            verified_move(switch_slot(cn, rel, 'patched'), target, patched)
            if index % 30 == 0:
                print(f'切换并校验 {index}/{len(changes)}', flush=True)
        if not verify(cn):
            raise RuntimeError('写入后的校验失败')
        state['complete'] = True
        save_json(STATE, state)
    except BaseException:
        restore(cn)
        raise
    print(f"替换完成：{len(report['bundles'])} 个资源包（含必要加载索引），共 {len(changes)} 个文件位置。原文件已备份。", flush=True)
    print('文件与资源结构校验不等于游戏内显示验证；请进游戏检查角色立绘和Live2D。')


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['apply', 'restore', 'verify', 'plan', 'configure'], default='apply', nargs='?')
    args = parser.parse_args()
    (ROOT / '.work').mkdir(exist_ok=True)
    # Lock lifetime follows process lifetime, including crashes.
    import msvcrt
    with (ROOT / '.work/tool.lock').open('a+b') as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('替换工具正在运行，请勿重复启动。')
        if args.action == 'configure':
            configure_roots()
            return 0
        cn, jp = roots()
        if args.action != 'verify':
            ensure_closed()
        if args.action == 'apply':
            apply(cn, jp)
        elif args.action == 'restore':
            restore(cn)
        elif args.action == 'verify':
            if not verify(cn):
                return 2
        else:
            if read_state():
                raise RuntimeError('请先恢复原文件再生成新的预览；已应用内容请使用校验入口。')
            build_plan(cn, jp)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('\n未完成：' + str(exc), flush=True)
        (ROOT / 'reports').mkdir(exist_ok=True)
        (ROOT / 'reports/error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        sys.exit(1)
