"""Offline rebuild of this hub's local Codex counters, preserving other sources.

Stop the dashboard first. Both databases are backed up before changing any records.
Run: python -m dashboard.repair
"""
import argparse
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .collector import Collector
from .paths import default_data_dir
from .storage import Store, prepare_private_database


def rebuild_local_usage(data_dir):
    data_dir = Path(data_dir)
    config = json.loads((data_dir/'hub.json').read_text())
    device = config['local_device']
    root = Path(os.path.expandvars(device['codex_dir'])).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('未找到本机日志目录，未修改统计')
    with tempfile.TemporaryDirectory(dir=data_dir) as temp:
        replacement = Path(temp)/'agent.sqlite3'
        collector = Collector(replacement, {'codex_dir':str(root),
            'codex_billing_type':device.get('codex_billing_type','plan')})
        try:
            status = collector.scan()
            count = collector.db.execute('SELECT COUNT(*) FROM queue').fetchone()[0]
            if status['errors'] or not count:
                raise ValueError('日志读取异常或没有有效记录，未修改统计')
            store = Store(data_dir/'hub.sqlite3')
            backup = data_dir/'backups'/datetime.now(timezone.utc).strftime('usage-repair-%Y%m%dT%H%M%S%fZ')
            backup.mkdir(parents=True)
            for name in ('hub.sqlite3','local-agent.sqlite3'):
                source = data_dir/name
                if source.exists():
                    prepare_private_database(backup/name)
                    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(backup/name)) as dest:
                        src.backup(dest)
            agent_path = data_dir/'local-agent.sqlite3'
            if agent_path.exists():
                with closing(sqlite3.connect(agent_path)) as old:
                    # Keep generic import queues and cursors outside the Codex log root.
                    for ident,event,sent in old.execute('SELECT id,event,sent FROM queue'):
                        if json.loads(event).get('source') != 'Codex':
                            collector.db.execute('INSERT OR IGNORE INTO queue VALUES (?,?,?)',(ident,event,sent))
                    for path,pos,context in old.execute('SELECT path,pos,context FROM cursors'):
                        if not Path(path).resolve().is_relative_to(root):
                            collector.db.execute('INSERT OR IGNORE INTO cursors VALUES (?,?,?)',(path,pos,context))
            with store.connect() as db:
                before = db.execute("SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM events WHERE device_id=? AND source='Codex'",(device['id'],)).fetchone()[0]
                db.execute("DELETE FROM events WHERE device_id=? AND source='Codex'",(device['id'],))
                db.execute("DELETE FROM event_details WHERE device_id=? AND id NOT IN (SELECT id FROM events WHERE device_id=?)",(device['id'],device['id']))
                for (raw,) in collector.db.execute('SELECT event FROM queue'):
                    e = json.loads(raw)
                    if e['source'] != 'Codex':
                        continue
                    store.insert_events(db,device['id'],[e])
                after = db.execute("SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM events WHERE device_id=? AND source='Codex'",(device['id'],)).fetchone()[0]
            # Rebuilt Codex rows are already in the hub. Other queues keep delivery state.
            for ident,raw in collector.db.execute('SELECT id,event FROM queue').fetchall():
                if json.loads(raw)['source']=='Codex':
                    collector.db.execute('UPDATE queue SET sent=1 WHERE id=?',(ident,))
            collector.db.commit()
        finally:
            collector.close()
        os.replace(replacement, data_dir/'local-agent.sqlite3')
    report={'before_tokens':before,'after_tokens':after,'token_change':after-before,
            'removed_inherited_tokens':max(0,before-after),'records':count}
    (backup/'report.json').write_text(json.dumps(report,indent=2))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='停服后重建本机 Codex 用量；自动保留备份')
    parser.add_argument('--data-dir',type=Path,default=default_data_dir())
    print(json.dumps(rebuild_local_usage(parser.parse_args().data_dir),ensure_ascii=False))
