"""Incremental Codex and generic JSONL reader with a persistent delivery queue."""
import hashlib
import json
import os
import sqlite3
import re
from contextlib import closing
import time
import urllib.error
import urllib.request
from pathlib import Path

from .storage import timestamp, validate_event, prepare_private_database
from .http_guard import NoRedirect


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Collector:
    def __init__(self, path, config):
        self.config = config
        self.titles = {}
        self.background=set()
        prepare_private_database(path)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cursors (path TEXT PRIMARY KEY, pos INTEGER, context TEXT);
            CREATE TABLE IF NOT EXISTS queue (id TEXT PRIMARY KEY, event TEXT, sent INTEGER DEFAULT 0);
        """)

    def close(self):
        self.db.close()

    def _metadata(self,context):
        session=context['session']
        title=self.titles.get(session,'Codex 对话')
        return {'session_id':'codex-session:'+digest(session),'title':title,
                'conversation_kind':'background' if session in self.background or context.get('background') else 'conversation',
                'billing_type':self.config.get('codex_billing_type','plan')}

    def _load_titles(self):
        self.titles={}
        self.background=set()
        value=self.config.get('codex_dir')
        if not value:return
        root=Path(os.path.expandvars(value)).expanduser().resolve().parent
        # Read only display titles. Conversation bodies and authentication stay in Codex.
        for path in sorted(root.glob('state_*.sqlite'),reverse=True):
            try:
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
                    columns={r[1] for r in db.execute('PRAGMA table_info(threads)')}
                    if not {'id','title'}<=columns:continue
                    label="COALESCE(NULLIF(name,''),title)" if 'name' in columns else 'title'
                    source='source' if 'source' in columns else "'null'"
                    for session,title,origin in db.execute('SELECT id,'+label+','+source+' FROM threads'):
                        try:
                            origin=json.loads(origin)
                            if isinstance(origin,dict) and 'subagent' in origin:self.background.add(session)
                        except (ValueError,TypeError):pass
                        if isinstance(title,str) and title.strip():
                            self.titles[session]=re.sub(r'\s+',' ',''.join(c for c in title if c.isprintable() or c.isspace())).strip()[:100]
                break
            except (sqlite3.Error,OSError,ValueError):
                continue

    def _record(self, row, context, kind):
        if kind == "generic":
            return validate_event(row)
        payload = row.get("payload") or {}
        if row.get("type") == "session_meta":
            context["session"] = payload.get("id") or payload.get("session_id") or context["session"]
            origin=payload.get('source')
            context['background']=isinstance(origin,dict) and 'subagent' in origin
        if row.get("type") == "turn_context":
            context["model"] = payload.get("model") or "unknown"
        if row.get('type') == 'compacted':
            record = payload.get('latest_token_usage_record') or {}
            response = payload.get('compaction_response_id')
            if (not isinstance(response,str) or not response or len(response)>500
                    or record.get('response_id') != response
                    or record.get('thread_id') != context['session']):
                return None
            # Modern Codex records compaction separately: token_count keeps its
            # pre-compaction cumulative baseline. Do not add all token_usage_record
            # rows, which would also duplicate every ordinary model request.
            counts = record.get('usage')
            if not isinstance(counts,dict):
                raise ValueError('missing compaction usage')
            return validate_event({'id':'codex:compaction:'+digest(response),
                'timestamp':row['timestamp'],'source':'Codex','model':context.get('model','unknown'),
                **self._metadata(context),
                **{k:counts.get(k) for k in ('input_tokens','cached_input_tokens','output_tokens')},
                'cost_usd':None})
        if row.get("type") != "event_msg" or payload.get("type") != "token_count":
            return None
        usage = (payload.get("info") or {}).get("total_token_usage")
        if not isinstance(usage, dict):
            return None
        current = {k: usage.get(k) for k in
                   ("input_tokens", "cached_input_tokens", "output_tokens")}
        if (any(type(n) is not int or not 0 <= n <= 10**12 for n in current.values())
                or current['cached_input_tokens'] > current['input_tokens']):
            raise ValueError('invalid cumulative token counters')
        # Validate before advancing the baseline. A rejected timestamp/count must
        # not swallow the next valid increment or create a false counter reset.
        measured_at = timestamp(row['timestamp'])
        previous = context.get("usage", {})
        if not previous:
            last = (payload.get("info") or {}).get("last_token_usage")
            if (not isinstance(last, dict) or any(type(last.get(k)) is not int
                    or not 0 <= last[k] <= current[k] for k in current)):
                # A truncated/resumed log's first cumulative value is not new usage.
                # Without a valid per-turn increment, establish a baseline only.
                context["usage"] = current
                return None
            previous = {k:current[k]-last[k] for k in current}
        # Compare cumulative counters, not last_token_usage: quota-only events repeat it.
        delta = {k: max(0, current[k]-previous.get(k, 0)) for k in current}
        epoch = context.get('epoch', 0)
        if any(current[k] < previous.get(k, 0) for k in current):
            # A reset starts a new accounting epoch, while identical snapshots do nothing.
            epoch += 1
            delta = current.copy()
        if delta["input_tokens"] + delta["output_tokens"] == 0:
            context['usage'] = current
            return None
        delta["cached_input_tokens"] = min(delta["cached_input_tokens"], delta["input_tokens"])
        ident = json.dumps([context["session"], epoch, current], sort_keys=True)
        event = validate_event({"id":"codex:"+digest(ident), "timestamp":measured_at,
                               "source":"Codex", "model":context.get("model", "unknown"),
                               **self._metadata(context),
                               **delta, "cost_usd":None})
        context.update(usage=current, epoch=epoch)
        return event

    def scan(self):
        self._load_titles()
        files = []
        codex = self.config.get("codex_dir")
        if codex:
            root = Path(os.path.expandvars(codex)).expanduser()
            if root.is_dir():
                files += [(p,"codex") for p in sorted(root.rglob("*.jsonl"))]
        for value in self.config.get("jsonl_paths", []):
            p = Path(os.path.expandvars(value)).expanduser()
            if p.is_dir():
                files += [(f,"generic") for f in sorted(p.rglob("*.jsonl"))]
            elif p.is_file():
                files.append((p,"generic"))
        errors = 0
        for path, kind in files:
            key = str(path.resolve())
            cursor = self.db.execute("SELECT pos,context FROM cursors WHERE path=?", (key,)).fetchone()
            pos, context = (cursor[0],json.loads(cursor[1])) if cursor else (0,{"session":digest(key)})
            try:
                if path.stat().st_size < pos:
                    pos, context = 0, {"session":digest(key)}
                if path.stat().st_size == pos:
                    continue
                with path.open("rb") as stream:
                    stream.seek(pos)
                    # Chunk transactions keep a first historical import bounded and crash-safe.
                    lines = 0
                    while True:
                        # Image-bearing Codex records can exceed 4 MiB. Keep the
                        # read bounded without flagging ordinary screenshots as corrupt.
                        record_limit = 64*1024*1024
                        line = stream.readline(record_limit+1)
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            if len(line) > record_limit:
                                # Skip an oversized record without retaining its contents.
                                while line and not line.endswith(b"\n"):
                                    line = stream.readline(1024*1024)
                                errors += 1
                                pos = stream.tell()
                                continue
                            break  # A writer may still be appending this record.
                        pos = stream.tell()
                        try:
                            row = json.loads(line)
                            event = self._record(row, context, kind)
                            if event:
                                self.db.execute("INSERT OR IGNORE INTO queue(id,event) VALUES (?,?)", (event["id"],json.dumps(event)))
                        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                            errors += 1
                        lines += 1
                        if lines % 1000 == 0:
                            self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?)",(key,pos,json.dumps(context)))
                            self.db.commit()
                self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?)",(key,pos,json.dumps(context)))
                self.db.commit()
            except OSError:
                errors += 1
        pending = self.db.execute("SELECT COUNT(*) FROM queue WHERE sent=0").fetchone()[0]
        return {"state":"warning" if errors else ("ok" if files else "no_source"),
                "files":len(files),"errors":errors,"pending":pending}

    def batch(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT event FROM queue WHERE sent=0 ORDER BY rowid LIMIT 500")]

    def acknowledge(self, events):
        self.db.executemany("UPDATE queue SET sent=1 WHERE id=?", [(e["id"],) for e in events])
        self.db.commit()


def default_collector():
    return {"codex_dir":str(Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex")))/"sessions"),
            "jsonl_paths":[]}


def run_agent(config, db_path, stop=None, sink=None, once=False):
    collector = Collector(db_path, config)
    device = {k:config[k] for k in ("id","name","platform")}
    last_error = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        while stop is None or not stop.is_set():
            status = collector.scan()
            try:
                # Drain queued history without waiting an interval between batches.
                while True:
                    batch = collector.batch()
                    status["pending"] = max(0, status["pending"]-len(batch))
                    if sink:
                        sink(device, batch, status)
                    else:
                        body = json.dumps({"device":device,"events":batch,"status":status}).encode()
                        req = urllib.request.Request(config["server_url"].rstrip("/")+"/api/ingest",data=body,
                            headers={"Content-Type":"application/json", "Authorization":"Bearer "+config["token"]})
                        with opener.open(req, timeout=15) as response:
                            result = json.load(response)
                            if result.get("accepted") != len(batch):
                                raise ValueError("服务端未确认整批数据")
                    collector.acknowledge(batch)
                    if len(batch)<500:
                        break
                if last_error:
                    print("连接已恢复，缓存记录已补传。", flush=True)
                last_error = None
            except (OSError, ValueError, urllib.error.URLError) as exc:
                # Do not print URLs, credentials, or remote response bodies.
                if last_error != type(exc).__name__:
                    print("暂时无法上报；记录已保存在本机，稍后自动重试。",flush=True)
                last_error = type(exc).__name__
            if once:
                return last_error is None
            interval = max(5, int(config.get("interval",20)))
            if stop:
                stop.wait(interval)
            else:
                time.sleep(interval)
    finally:
        collector.close()
