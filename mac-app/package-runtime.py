"""Bundle this Mac's Python and Pillow, without data, credentials, development tools or caches."""
from pathlib import Path
import shutil
import os
import re
import subprocess
import sys
import sysconfig

project = Path(__file__).resolve().parent.parent
resources = project/'Kindle 看板.app/Contents/Resources'
runtime = resources/'runtime'
backend = resources/'backend'
for path in (runtime, backend):
    if path.exists():
        shutil.rmtree(path)
(runtime/'bin').mkdir(parents=True)
base = Path(sys.base_prefix)
executable = Path(sys._base_executable).resolve()
version = f'python{sys.version_info.major}.{sys.version_info.minor}'
shutil.copy2(executable, runtime/'bin'/version)
for name in ('python3', 'python'):
    (runtime/'bin'/name).symlink_to(version)
ignore = shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo', '.DS_Store')
shutil.copytree(base/'lib', runtime/'lib', symlinks=True, ignore=ignore)
# Remove dependency installers and host-specific build metadata from the runtime.
for development in (runtime/'lib/pkgconfig', runtime/'lib'/version/'ensurepip', runtime/'lib/libpython3.12.dylib'):
    if development.is_dir():shutil.rmtree(development)
    elif development.exists():development.unlink()
site = runtime/'lib'/version/'site-packages'
if site.exists():
    shutil.rmtree(site)
site.mkdir()
installed = Path(sysconfig.get_paths()['purelib'])
shutil.copytree(installed/'PIL', site/'PIL', ignore=ignore)
for metadata in installed.glob('pillow-*.dist-info'):
    shutil.copytree(metadata, site/metadata.name, ignore=ignore)
    for provenance in ('direct_url.json','RECORD','INSTALLER','REQUESTED'):
        (site/metadata.name/provenance).unlink(missing_ok=True)
backend.mkdir()
shutil.copy2(project/'kindle_dashboard.py', backend/'kindle_dashboard.py')
shutil.copytree(project/'dashboard', backend/'dashboard', ignore=ignore)
(resources/'project.path').unlink(missing_ok=True)
shutil.copy2(project/'third_party/PYTHON-RUNTIME-LICENSES.rst',resources/'PYTHON-RUNTIME-LICENSES.rst')
shutil.copy2(project/'third_party/PYTHON-RUNTIME-LICENSE-SOURCE.txt',resources/'PYTHON-RUNTIME-LICENSE-SOURCE.txt')
# Sign every native Python/Pillow component before sealing the outer application.
magic = {b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe',
         b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf'}
count = 0
for path in runtime.rglob('*'):
    if path.is_file() and not path.is_symlink():
        with path.open('rb') as stream:
            is_native = stream.read(4) in magic
        if is_native:
            # Wheel build paths can survive in Mach-O install IDs and debug strings.
            commands=subprocess.run(['/usr/bin/otool','-l',str(path)],check=True,capture_output=True,text=True).stdout
            for block in commands.split('Load command '):
                home=re.search(r'(?:name|path) (/(?:Users|home)/[^\n]+?) \(offset ',block)
                if not home:continue
                original=home.group(1)
                if 'cmd LC_ID_DYLIB' in block:
                    subprocess.run(['/usr/bin/install_name_tool','-id','@rpath/'+path.name,str(path)],check=True,capture_output=True)
                elif 'cmd LC_RPATH' in block:
                    subprocess.run(['/usr/bin/install_name_tool','-delete_rpath',original,str(path)],check=True,capture_output=True)
                else:
                    raise ValueError('Unexpected absolute loader dependency in bundled runtime')
            subprocess.run(['/usr/bin/strip','-S',str(path)],check=True,capture_output=True)
            data=path.read_bytes()
            # Same-width neutralization preserves native section offsets; loader paths were handled above.
            data=re.sub(rb'/(?:Users|home)/[^\x00\n\r\'\" ]+',
                        lambda match:b'/build/'+b'_'*(len(match[0])-7),data)
            path.write_bytes(data)
            subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(path)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            count += 1
# sysconfig describes build-time paths, which must not expose the build user's home.
for metadata in (runtime/'lib'/version).glob('_sysconfigdata*.py'):
    data=metadata.read_bytes()
    metadata.write_bytes(re.sub(rb'/(?:Users|home)/[^\x00\n\r\'\" ]+',
                               lambda match:b'/build/'+b'_'*(len(match[0])-7),data))
print(f'Bundled Python {sys.version_info.major}.{sys.version_info.minor}; signed {count} native components')
