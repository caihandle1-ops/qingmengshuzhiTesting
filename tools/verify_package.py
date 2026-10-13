"""Verify shipped files and exact export boundaries without touching runtime data."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SUFFIXES = {'.html', '.css', '.js', '.cjs', '.mjs', '.svg', '.png', '.jpg', '.jpeg',
                      '.webp', '.gif', '.mp4', '.webm', '.woff', '.woff2', '.ttf', '.db', '.sqlite'}


def validate_entries(manifest):
    files=manifest['files']
    seen=set()
    for item in files:
        path=Path(item['path'])
        if path.is_absolute() or '..' in path.parts or path.as_posix() in seen:
            raise ValueError('Invalid or duplicate export path')
        seen.add(path.as_posix())
        if path.parts[0] in {'app', '.git', '.streamlit', 'runtime', 'dist'} or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ValueError('Non-technical file in export manifest: '+path.as_posix())
        if path.as_posix() in {'core/charts.py','core/report_pdf.py'}:
            raise ValueError('Product design module in export')
        if path.suffix.lower()=='.pdf' and path.as_posix()!='tests/fixtures/byd_2024_key_financials.pdf':
            raise ValueError('Unlisted PDF in export')
        actual=ROOT/path
        if actual.is_symlink() or not actual.is_file() or hashlib.sha256(actual.read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('Missing or changed export file: '+path.as_posix())
    return len(files)


if __name__=='__main__':
    manifest=json.loads((ROOT/'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
    print(json.dumps({'status':'passed','verified_files':validate_entries(manifest)},ensure_ascii=False))
