#!/usr/bin/env python3
"""Kindle AI usage hub and cross-platform collector. Python 3.10+."""
import argparse
import json
import os
import platform
import secrets
import socket
import signal
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

from dashboard.collector import default_collector, run_agent
from dashboard.paths import default_data_dir
from dashboard.storage import Store
from dashboard.codexbar import CodexBarBridge, TokenHistoryBridge, DEFAULT_CONFIG, quota_view, history_view
from dashboard.display import DEFAULT_WIDTH, DEFAULT_HEIGHT

ROOT=Path(__file__).resolve().parent


def write_json(path, data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    descriptor=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as output:
        if os.name!='nt':os.fchmod(output.fileno(),0o600)
        output.write(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    os.replace(temp,path)


def lan_ip():
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
        try:
            s.connect(('192.0.2.1',80))
            return s.getsockname()[0]
        except OSError:
            return '127.0.0.1'


def load_hub(path):
    path=Path(path)
    if not path.exists():
        cfg={'timezone_offset_minutes':480,
             'local_device':{'id':str(uuid.uuid4()),'name':'MacBook' if platform.system()=='Darwin' else socket.gethostname(),
                             'platform':platform.system(),**default_collector(),'interval':20},
             'devices':[], 'codexbar':DEFAULT_CONFIG.copy()}
        write_json(path,cfg)
    cfg=json.loads(path.read_text(encoding='utf-8'))
    if 'codexbar' not in cfg:
        cfg['codexbar']=DEFAULT_CONFIG.copy()
        write_json(path,cfg)
    return cfg


def positive(value):
    n=int(value)
    if n<5:
        raise argparse.ArgumentTypeError('至少 5 秒')
    return n


def main():
    parser=argparse.ArgumentParser(description='AI USAGE · Windows / Mac / Kindle 双端联动')
    sub=parser.add_subparsers(dest='command')
    serve=sub.add_parser('serve',help='启动汇总服务，并采集本机')
    serve.add_argument('--port',type=int,default=8000)
    serve.add_argument('--host',default='0.0.0.0')
    serve.add_argument('--refresh',type=positive,default=60)
    serve.add_argument('--width',type=int,default=DEFAULT_WIDTH)
    serve.add_argument('--height',type=int,default=DEFAULT_HEIGHT)
    serve.add_argument('--landscape',action='store_true')
    serve.add_argument('--no-collect',action='store_true',help='仅汇总，不读取本机日志')
    serve.add_argument('--no-codexbar',action='store_true',help='关闭本次运行的 CodexBar 额度采集')
    serve.add_argument('--data-dir',type=Path,default=default_data_dir(ROOT))
    pair=sub.add_parser('pair',help='为另一台电脑生成专属配对配置')
    pair.add_argument('--name',required=True)
    pair.add_argument('--platform',choices=['Windows','Darwin','Linux'],default='Windows')
    pair.add_argument('--server-url',required=True,help='例如 http://192.0.2.10:8000')
    pair.add_argument('--output',type=Path,default=default_data_dir(ROOT).parent/'clients'/'agent.json')
    pair.add_argument('--data-dir',type=Path,default=default_data_dir(ROOT))
    agent=sub.add_parser('agent',help='仅采集本机并上传到汇总电脑；无需 Pillow')
    agent.add_argument('--config',type=Path,required=True)
    agent.add_argument('--once',action='store_true')
    agent.add_argument('--relay-port',type=int,help='同时为 Kindle 转发看板，例如 8001；无需 Pillow')
    agent.add_argument('--data-dir',type=Path,default=default_data_dir(ROOT))
    render=sub.add_parser('render',help='导出真实数据预览；没有数据时显示空状态')
    render.add_argument('--output',type=Path,default=default_data_dir(ROOT).parent/'preview.png')
    render.add_argument('--width',type=int,default=DEFAULT_WIDTH)
    render.add_argument('--height',type=int,default=DEFAULT_HEIGHT)
    render.add_argument('--view',choices=['usage','api','quota','devices'],default='usage')
    render.add_argument('--data-dir',type=Path,default=default_data_dir(ROOT))
    args=parser.parse_args()
    if args.command is None:
        args=parser.parse_args(['serve'])
    if args.command=='pair':
        if args.output.exists():
            parser.error('输出配置已存在，请换一个 --output，避免覆盖正在使用的身份')
        parsed=urlparse(args.server_url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username:
            parser.error('server-url 必须是 http(s)://主机:端口，不带路径或登录信息')
        if not args.name.strip() or len(args.name)>120:
            parser.error('设备名长度应为 1–120 个字符')
        cfg=load_hub(args.data_dir/'hub.json')
        device={'id':str(uuid.uuid4()),'name':args.name,'platform':args.platform,'token':secrets.token_urlsafe(32)}
        cfg['devices'].append(device)
        write_json(args.data_dir/'hub.json',cfg)
        client={**device,'server_url':args.server_url.rstrip('/'),'codex_dir':'~/.codex/sessions','jsonl_paths':[],'interval':20}
        write_json(args.output,client)
        print(f'已生成专属配置：{args.output.resolve()}\n将此配置和项目复制到另一台电脑；凭据请勿公开。')
        return
    if args.command=='agent':
        config=json.loads(args.config.read_text(encoding='utf-8'))
        for key in ('id','name','platform','server_url','token'):
            if not isinstance(config.get(key),str) or not config[key]:
                parser.error(f'配置缺少 {key}')
        if urlparse(config['server_url']).scheme not in ('http','https'):
            parser.error('服务地址必须使用 http 或 https')
        if args.relay_port is not None:
            if args.once or not 1<=args.relay_port<=65535:
                parser.error('--relay-port 范围为 1–65535，不能与 --once 一起使用')
            from dashboard.relay import RelayServer, hotspot_addresses
            relay=RelayServer(('0.0.0.0',args.relay_port),lambda:json.loads(args.config.read_text(encoding='utf-8')))
            stop=threading.Event()
            worker=threading.Thread(target=run_agent,
                args=(config,args.data_dir/('agent-'+str(uuid.UUID(config['id']))+'.sqlite3')),
                kwargs={'stop':stop},daemon=True)
            worker.start()
            print(f"\nWindows 看板转发已启动，端口 {args.relay_port}。\n"
                  "保持设备处于用户信任且能够互通的网络中。",flush=True)
            addresses=hotspot_addresses()
            if addresses:
                for ip in addresses:
                    print(f'Kindle 热点地址：http://{ip}:{args.relay_port}/kindle',flush=True)
            else:
                print(f'Kindle 地址：http://Windows热点网关地址:{args.relay_port}/kindle\n'
                      '请在 Kindle 的 Wi-Fi 详情中确认网关，或在 Windows 运行 ipconfig 查看热点网卡地址。',flush=True)
            print(f'汇总端地址：{config["server_url"]}\n按 Ctrl+C 停止。',flush=True)
            try:
                relay.serve_forever(poll_interval=0.3)
            except KeyboardInterrupt:
                pass
            finally:
                stop.set()
                relay.server_close()
                worker.join(timeout=5)
            return
        print(f"采集器已启动：{config['name']}，每 {config.get('interval',20)} 秒同步。",flush=True)
        try:
            ok=run_agent(config,args.data_dir/('agent-'+str(uuid.UUID(config['id']))+'.sqlite3'),once=args.once)
            if args.once and not ok:
                raise SystemExit(1)
        except KeyboardInterrupt:
            print('采集器已停止；未上传记录保留在本机。')
        return
    cfg=load_hub(args.data_dir/'hub.json')
    store=Store(args.data_dir/'hub.sqlite3')
    if not 400<=args.width<=1600 or not 400<=args.height<=2200:
        parser.error('画布范围：宽 400–1600，高 400–2200')
    if args.command=='render':
        from dashboard.render import render_dashboard
        summary=store.summary(cfg.get('timezone_offset_minutes',480),[cfg['local_device'],*cfg['devices']])
        summary['quotas']=quota_view(store.load_quota_state(),cfg.get('codexbar',{}).get('enabled',True))
        summary['token_history']=history_view(store.load_token_history(),cfg.get('codexbar',{}).get('enabled',True))
        from dashboard.panels import build_panels
        build_panels(store,summary,cfg)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        render_dashboard(args.width,args.height,summary,args.view).save(args.output)
        print(f'预览已保存：{args.output.resolve()}')
        return
    if not 0<=args.port<=65535:
        parser.error('端口范围应为 0–65535')
    if args.landscape:
        args.width,args.height=DEFAULT_HEIGHT,DEFAULT_WIDTH
    from dashboard.web import DashboardServer
    server=DashboardServer((args.host,args.port),store,args.data_dir/'hub.json',args.refresh,args.width,args.height)
    server.codexbar_enabled = not args.no_codexbar
    stop=threading.Event()
    worker=None
    if not args.no_collect:
        worker=threading.Thread(target=run_agent,args=(cfg['local_device'],args.data_dir/'local-agent.sqlite3'),
                                kwargs={'stop':stop,'sink':store.ingest},daemon=True)
        worker.start()
    quota_worker = None
    history_worker = None
    if not args.no_codexbar:
        quota_worker = threading.Thread(target=CodexBarBridge(store, server.config).run, args=(stop,), daemon=True)
        quota_worker.start()
        history_worker = threading.Thread(target=TokenHistoryBridge(store,server.config).run,args=(stop,),daemon=True)
        history_worker.start()
    port=server.server_address[1]
    ip=lan_ip()
    print(f'\nAI USAGE 已启动\n电脑面板：http://localhost:{port}\nKindle：http://{ip}:{port}/kindle\n'
          f"采集：{'关闭' if args.no_collect else cfg['local_device']['name']}\n"
          '另一台电脑请使用 pair 生成的配置启动 agent。\n按 Ctrl+C 停止。',flush=True)
    def terminate(signum,frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,terminate)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        if worker:
            worker.join(timeout=5)
        if quota_worker:
            quota_worker.join(timeout=5)
        if history_worker:
            history_worker.join(timeout=5)


if __name__=='__main__':
    main()
