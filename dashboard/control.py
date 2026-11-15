"""Local service lifecycle, independent of the HTTP server. macOS controller backend."""
import argparse
try:
    import fcntl
except ImportError:
    fcntl=None
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from .http_guard import NoRedirect
from .paths import default_data_dir

ROOT=Path(__file__).resolve().parent.parent


class Controller:
    def __init__(self,root=ROOT,data_dir=None,port=8000):
        self.root=Path(root).resolve()
        self.data=Path(data_dir).resolve() if data_dir else default_data_dir(self.root)
        self.port=port
        self.pid_file=self.data/'server.pid'
        self.log_file=self.data/'server.log'
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        self.instance=hashlib.sha256(str((self.data/'hub.json').resolve()).encode()).hexdigest()[:16]
        self.children=[]

    def owned(self,pid):
        if type(pid) is not int or pid<=1:return False
        try:
            command=subprocess.run(['/bin/ps','-p',str(pid),'-o','command='],capture_output=True,text=True,timeout=2).stdout.strip()
            script=re.escape(str(self.root/'kindle_dashboard.py'))
            if not re.search(r'(?:^|\s)'+script+r'\s+serve(?:\s|$)',command):return False
            # A PID from another instance of this project must never be stopped.
            port=re.search(r'(?:^|\s)--port(?:=|\s+)(\d+)(?=\s|$)',command)
            if (int(port[1]) if port else 8000)!=self.port:return False
            if re.search(r'(?:^|\s)--data-dir(?:=|\s+)',command):
                return bool(re.search(r'(?:^|\s)--data-dir(?:=|\s+)'+re.escape(str(self.data))+r'(?=\s+--|$)',command))
            return self.data==self.root/'data'
        except (OSError,subprocess.TimeoutExpired):return False

    def saved_pid(self):
        try:return int(self.pid_file.read_text().strip())
        except (OSError,ValueError):return None

    def status(self):
        pid=self.saved_pid();health=None
        try:
            with self.opener.open(f'http://127.0.0.1:{self.port}/health',timeout=.7) as response:
                health=json.loads(response.read(4096))
        except (OSError,ValueError):pass
        identity=isinstance(health,dict) and health.get('service')=='kindle-dashboard' and health.get('instance')==self.instance
        if identity:pid=health.get('pid')
        owned=self.owned(pid)
        running=owned and isinstance(health,dict) and health.get('ok') is True and identity
        with socket.socket() as connection:
            connection.settimeout(.2)
            occupied=connection.connect_ex(('127.0.0.1',self.port))==0
        state='running' if running else 'unresponsive' if owned else 'conflict' if occupied else 'stopped'
        ip=None
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as connection:
            try:connection.connect(('192.0.2.1',80));ip=connection.getsockname()[0]
            except OSError:pass
        return {'state':state,'running':running,'can_stop':owned,'pid':pid if owned else None,
                'kindle_url':f'http://{ip}:{self.port}/kindle' if ip and ip!='127.0.0.1' else None,
                'dashboard_url':f'http://localhost:{self.port}',
                'message':{'running':'看板服务正在运行','stopped':'看板服务已停止',
                'conflict':'端口被其他程序使用','unresponsive':'服务没有响应，可停止后重新启动'}[state]}

    @contextmanager
    def lock(self):
        if fcntl is None:raise RuntimeError('控制应用目前仅支持 macOS')
        self.data.mkdir(parents=True,exist_ok=True)
        with (self.data/'service-control.lock').open('a') as stream:
            deadline=time.monotonic()+5
            while True:
                try:
                    fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise RuntimeError('另一个控制操作尚未完成，请稍后重试')
                    time.sleep(.1)
            yield

    def start(self,extra_args=()):
        with self.lock():
            status=self.status()
            if status['state']!='stopped':return status
            python=Path(os.environ.get('KINDLE_DASHBOARD_PYTHON',str(self.root/'.venv/bin/python')))
            if not python.exists():raise RuntimeError('首次使用请先打开 start-mac.command 安装运行环境')
            with self.log_file.open('ab') as output:
                process=subprocess.Popen([str(python),str(self.root/'kindle_dashboard.py'),'serve',
                    '--port',str(self.port),'--data-dir',str(self.data),*extra_args],cwd=self.root,
                    stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
            self.children.append(process)
            self.pid_file.write_text(str(process.pid)+'\n')
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                if process.poll() is not None:raise RuntimeError('启动失败，请查看运行日志')
                status=self.status()
                if status['running']:return status
                time.sleep(.15)
            raise RuntimeError('服务启动尚未完成，请稍后刷新状态或查看日志')

    def stop(self):
        with self.lock():
            status=self.status();pid=status['pid']
            if not status['can_stop']:return status
            # Recheck ownership immediately before signalling; never stop another port user.
            if not self.owned(pid):return self.status()
            try:os.kill(pid,signal.SIGTERM)
            except ProcessLookupError:pass
            deadline=time.monotonic()+10
            while self.owned(pid) and time.monotonic()<deadline:time.sleep(.15)
            status=self.status()
            for child in self.children[:]:
                if child.poll() is not None:
                    child.wait();self.children.remove(child)
            if status['state']=='stopped':self.pid_file.unlink(missing_ok=True)
            return status


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['status','start','stop','restart'])
    args=parser.parse_args();controller=Controller()
    try:
        if args.action=='restart':
            controller.stop();result=controller.start()
        else:result=getattr(controller,args.action)()
        print(json.dumps(result,ensure_ascii=False))
    except (OSError,RuntimeError) as exc:
        result=controller.status();result['error']=str(exc)
        print(json.dumps(result,ensure_ascii=False))
        raise SystemExit(1)


if __name__=='__main__':main()
