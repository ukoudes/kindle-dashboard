"""Audit release trees and ZIP contents without printing private matched values."""
import argparse
import io
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

FORBIDDEN_PARTS = {'.git','.venv','__pycache__','__MACOSX','.AppleDouble','data','clients'}
FORBIDDEN_SUFFIXES = {'.sqlite','.sqlite3','.db','.log','.jsonl','.pem','.key','.p12','.pfx','.pyc','.pyo'}
SECRET = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{30,})\b')
LOCAL_PATH = re.compile(rb'/(?:Users|home)/[A-Za-z0-9_.-]+/')
IPV4 = re.compile(rb'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')


def audit(paths, private_values=()):
    needles = {str(value).encode() for value in private_values if isinstance(value,str) and len(value)>=8
               and value not in {'127.0.0.1','0.0.0.0','::1','localhost'}}
    home = str(Path.home())
    if len(home)>=8:needles.add(home.encode())
    if len(Path.home().name)>=8:needles.add(Path.home().name.encode())
    findings=[]
    count=0
    total=0
    def finding(name, reason):findings.append({'file':name,'reason':reason})
    def inspect(name, data, mode=0, depth=0):
        nonlocal count,total
        count+=1;total+=len(data)
        if total>400*1024*1024:raise ValueError('Audit content budget exceeded')
        parts=PurePosixPath(name.replace('!','/')).parts
        if any(part in FORBIDDEN_PARTS or part.startswith('._') for part in parts) or PurePosixPath(name).name=='.DS_Store':
            finding(name,'forbidden runtime/history/metadata path')
        if PurePosixPath(name).suffix.lower() in FORBIDDEN_SUFFIXES:finding(name,'forbidden data or credential file')
        if any(needle in data for needle in needles):finding(name,'known private value')
        if LOCAL_PATH.search(data):finding(name,'embedded local home path')
        if SECRET.search(data):finding(name,'credential-shaped content')
        # Standard-library IP classification constants are vendor code, not device addresses.
        if '/Contents/Resources/runtime/' not in name:
            for raw in set(IPV4.findall(data)):
                try:address=ipaddress.ip_address(raw.decode())
                except ValueError:continue
                if any(address in network for network in (ipaddress.ip_network((0x0A000000,8)),ipaddress.ip_network((0xAC100000,12)),ipaddress.ip_network((0xC0A80000,16)))):
                    finding(name,'private-network literal in project content');break
        if stat.S_ISLNK(mode):
            target=data.decode('utf-8')
            if PurePosixPath(target).is_absolute() or '..' in PurePosixPath(target).parts:finding(name,'external or parent symlink')
        if depth<3 and zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if archive.comment:finding(name,'ZIP comment')
                for member in archive.infolist():
                    if member.is_dir():continue
                    if member.file_size>64*1024*1024:raise ValueError('Archive member too large')
                    if member.extra:finding(name+'!'+member.filename,'ZIP extra metadata')
                    if PurePosixPath(member.filename).is_absolute() or '..' in PurePosixPath(member.filename).parts:
                        finding(name+'!'+member.filename,'unsafe archive member')
                    inspect(name+'!'+member.filename,archive.read(member),member.external_attr>>16,depth+1)
    for root in paths:
        root=Path(root)
        files=sorted(root.rglob('*')) if root.is_dir() else [root]
        for file in files:
            if file.is_symlink():
                import os
                inspect(root.name+'/'+file.relative_to(root).as_posix() if root.is_dir() else file.name,os.readlink(file).encode(),stat.S_IFLNK)
            elif file.is_file():
                inspect(root.name+'/'+file.relative_to(root).as_posix() if root.is_dir() else file.name,file.read_bytes(),file.stat().st_mode)
    return {'passed':not findings,'files_checked':count,'bytes_checked':total,'findings':findings,
            'known_private_values_checked':bool(needles),'scope':'release files and nested ZIP members; no filesystem-wide erasure guarantee'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('paths',nargs='+',type=Path)
    parser.add_argument('--private-needles',type=Path)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    values=json.loads(args.private_needles.read_text()) if args.private_needles else []
    result=audit(args.paths,values)
    payload=json.dumps(result,ensure_ascii=False,indent=2)
    if args.report:args.report.write_text(payload+'\n')
    print(payload)
    if not result['passed']:raise SystemExit(1)

if __name__=='__main__':main()
