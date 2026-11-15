"""Create public artifacts from an explicit source allowlist, with normalized ZIP metadata."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

try:
    from .privacy_audit import audit
except ImportError:
    from privacy_audit import audit

ROOT=Path(__file__).resolve().parent.parent
VERSION='1.1.0'
SOURCE_FILES=['README.md','WINDOWS-QUICKSTART.md','SECURITY.md','LICENSE-NOTICE.md',
              'THIRD-PARTY-NOTICES.md','GITHUB-RELEASE-GUIDE.md','RELEASE-NOTES.md','.gitignore',
              'third_party/PYTHON-RUNTIME-LICENSES.rst','third_party/PYTHON-RUNTIME-LICENSE-SOURCE.txt',
              'requirements.txt','kindle_dashboard.py','start-mac.command','start-agent-mac.command',
              'start-agent-windows.cmd','start-windows.cmd']
SOURCE_DIRS=('dashboard','mac-app','tests','scripts')
EXTENSIONS={'.py','.swift','.command','.cmd','.html','.mjs'}


def source_files():
    result=[ROOT/name for name in SOURCE_FILES]
    for folder in SOURCE_DIRS:
        for path in sorted((ROOT/folder).rglob('*')):
            if path.is_symlink():raise ValueError('Source symlink is not publishable')
            if path.is_file() and '__pycache__' not in path.parts and (path.suffix in EXTENSIONS or path.relative_to(ROOT).as_posix()=='mac-app/Usage.txt'):
                result.append(path)
    return sorted(set(result))


def archive_files(output, members):
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for path,name in sorted(members,key=lambda item:item[1]):
            info=zipfile.ZipInfo(name,date_time=(2020,1,1,0,0,0))
            info.create_system=3
            info.extra=b'';info.comment=b''
            if path.is_symlink():
                info.external_attr=(stat.S_IFLNK|0o777)<<16
                data=os.readlink(path).encode()
                info.compress_type=zipfile.ZIP_STORED
            else:
                executable=bool(path.stat().st_mode&0o111)
                info.external_attr=(stat.S_IFREG|(0o755 if executable else 0o644))<<16
                data=path.read_bytes()
                info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,data)


def main():
    app=ROOT/'Kindle 看板.app'
    if not app.is_dir():raise SystemExit('Build the standalone app first')
    subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(app)],check=True)
    output=ROOT/'dist';output.mkdir(exist_ok=True)
    # Only remove outputs owned by this script, never arbitrary files in dist.
    names=[f'Kindle-Dashboard-v{VERSION}-macOS-arm64.zip',f'kindle-dashboard-v{VERSION}-source.zip']
    source_members=[(path,'kindle-dashboard-v'+VERSION+'/'+path.relative_to(ROOT).as_posix()) for path in source_files()]
    app_members=[(path,'Kindle 看板.app/'+path.relative_to(app).as_posix()) for path in app.rglob('*') if path.is_file() or path.is_symlink()]
    app_members += [(ROOT/name,name) for name in ('THIRD-PARTY-NOTICES.md','LICENSE-NOTICE.md','RELEASE-NOTES.md')]
    archive_files(output/names[0],app_members)
    archive_files(output/names[1],source_members)
    result=audit([output/name for name in names])
    (output/'PRIVACY-REPORT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    if not result['passed']:
        for name in names:(output/name).unlink(missing_ok=True)
        raise SystemExit('Privacy audit failed; archives removed. Inspect the report without publishing it.')
    (output/'SHA256SUMS.txt').write_text(''.join(hashlib.sha256((output/name).read_bytes()).hexdigest()+'  '+name+'\n' for name in names))
    for name in ('GITHUB-RELEASE-GUIDE.md','RELEASE-NOTES.md'):
        shutil.copyfile(ROOT/name,output/name)
    print('Release archives generated; normalized metadata; initial audit passed')

if __name__=='__main__':main()
