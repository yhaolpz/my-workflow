#!/usr/bin/env python3
"""Create an installable skill ZIP from explicit source directories only."""
from pathlib import Path
import zipfile

root=Path(__file__).resolve().parents[1]
target=root/'dist/my-workflow-v0.1.0.zip'
target.parent.mkdir(exist_ok=True)
files=[root/p for p in ('SKILL.md','README.md','LICENSE')]
for name in ('agents','assets','references','templates','scripts','docs'):
    files.extend(p for p in (root/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo'))
with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
    for p in sorted(files):archive.write(p,Path('my-workflow')/p.relative_to(root))
with zipfile.ZipFile(target) as archive:
    assert archive.testzip() is None
    assert 'my-workflow/SKILL.md' in archive.namelist()
print(target)
