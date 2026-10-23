"""Build clean source and portable ZIPs without touching local game state."""
import argparse
import hashlib
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
ENTRIES = ('一键替换.cmd', '一键恢复.cmd', '校验替换.cmd', '选择游戏目录.cmd')
RUNTIME_FILES = ('replace_art.py', 'requirements.txt', *ENTRIES)
SOURCE_FILES = (*RUNTIME_FILES, 'README.md', '.gitignore', 'build_release.py')
# Compiler intermediates and Python caches are not runtime dependencies.
BUILD_SUFFIXES = {'.pyc', '.pyo', '.iobj', '.ipdb', '.pdb', '.obj', '.exp', '.lib'}
# The tool processes Unity texture bytes, not AVIF/WebP, fonts, ICC profiles or Tk.
# Keep Pillow's core, PNG/JPEG support and all Unity texture codecs/CPU variants.
UNUSED_PIL_EXTENSIONS = {'_avif', '_webp', '_imagingft', '_imagingcms', '_imagingtk'}
UNUSED_PYTHON_FILES = {'_sqlite3.pyd', 'sqlite3.dll', '_msi.pyd', 'winsound.pyd', 'pythonw.exe'}
AUDIO_MODULE = '.runtime/UnityPy/export/AudioClipConverter.py'
AUDIO_SOURCE_SHA256 = '9e7faa0728462c3cab784cc614304508771ae34207ee793ed0812d0bd3a43b8c'


def lazy_audio_module():
    """Patch only the audited UnityPy 1.25.3 file, leaving local dependencies intact."""
    data = (ROOT / AUDIO_MODULE).read_bytes()
    if hashlib.sha256(data).hexdigest() != AUDIO_SOURCE_SHA256:
        raise RuntimeError('UnityPy 音频模块已变化，请重新核对延迟导入补丁后再打包。')
    text = data.decode('utf-8').replace('\r\n', '\n')
    text = text.replace('import fmod_toolkit\n', '', 1)
    text = text.replace('    return fmod_toolkit.raw_to_wav(',
                        '    # Portable artwork build: load FMOD only for audio decoding.\n'
                        '    import fmod_toolkit\n\n'
                        '    return fmod_toolkit.raw_to_wav(', 1)
    return text.encode('utf-8')


def runtime_file(path):
    relative = path.relative_to(ROOT)
    if not path.is_file() or '__pycache__' in relative.parts or path.suffix.lower() in BUILD_SUFFIXES:
        return False
    if relative.parts[:2] == ('.runtime', 'bin'):
        return False  # Dependency CLI launchers are never called by the tool.
    if relative.parts[0] == '.runtime' and relative.parts[1] in {'fmod_toolkit', 'pyfmodex'}:
        return False  # Audio export is outside the artwork tool's scope.
    if relative.parts[:2] == ('.runtime', 'PIL') and path.suffix == '.pyd':
        return path.name.split('.')[0] not in UNUSED_PIL_EXTENSIONS
    return not (relative.parts[0] == '.python' and path.name in UNUSED_PYTHON_FILES)


def build(output, portable):
    audio_module = lazy_audio_module() if portable else None
    files = [(ROOT / name, name) for name in (RUNTIME_FILES if portable else SOURCE_FILES)]
    if portable:
        for folder in ('.python', '.runtime'):
            base = ROOT / folder
            if not base.is_dir():
                raise FileNotFoundError(f'缺少内置环境：{base}')
            files.extend((p, p.relative_to(ROOT).as_posix()) for p in sorted(base.rglob('*'))
                         if runtime_file(p))
        for required in ('.python/python.exe', '.python/python311._pth', '.runtime/UnityPy/__init__.py'):
            if not (ROOT / required).is_file():
                raise FileNotFoundError(required)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.zip.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path, name in files:
                if name == AUDIO_MODULE:
                    archive.writestr(name, audio_module)
                else:
                    archive.write(path, name)
            if portable:
                archive.writestr('settings.json', '{"cn": "", "jp": ""}\n')
        with zipfile.ZipFile(temporary) as archive:
            bad = archive.testzip()
            if bad:
                raise RuntimeError('压缩包校验失败：' + bad)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'{output.name}: {output.stat().st_size / 1024 / 1024:.2f} MiB')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    build(args.output / '日服替换小开关-源码.zip', portable=False)
    build(args.output / '日服替换小开关-精简版.zip', portable=True)
