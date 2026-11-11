"""Package a clean build.ps1 output directory and write its SHA-256 checksum."""
import argparse
import hashlib
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build_dir', type=Path, help='Folder containing ABI Sound Scout.exe')
    parser.add_argument('output', type=Path, help='Destination .zip outside build_dir')
    args = parser.parse_args()
    source, target = args.build_dir.resolve(), args.output.resolve()
    if target.is_relative_to(source):
        parser.error('The archive must be outside the build directory')
    for required in ('ABI Sound Scout.exe', '_internal', 'data/catalog.json', 'licenses'):
        if not (source / required).exists():
            parser.error(f'Missing required build file: {required}')
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, 'w', compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source.rglob('*')):
            relative = path.relative_to(source)
            if path.is_file() and '__pycache__' not in relative.parts:
                archive.write(path, str(Path('ABI Sound Scout') / relative))
    with ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('Archive integrity check failed')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.zip.sha256').write_text(f'{digest}  {target.name}\n', encoding='ascii')
    print(f'{target.name}: {target.stat().st_size} bytes; sha256={digest}')


if __name__ == '__main__':
    main()
