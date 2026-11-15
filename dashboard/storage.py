"""Validated, idempotent event storage. No conversation content is accepted."""
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def prepare_private_database(path):
    """Preserve file contents and restrict POSIX database/sidecar permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    if os.name != 'nt':
        path.chmod(0o600)
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(path)+suffix)
            if sidecar.exists():
                sidecar.chmod(0o600)
    return path


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp 必须是带时区的 ISO 日期")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp 缺少时区")
    if dt > datetime.now(timezone.utc) + timedelta(minutes=10):
        raise ValueError("记录时间超前，请检查电脑时钟")
    return dt.astimezone(timezone.utc).isoformat()


def text_field(value, limit=120):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("文本字段为空或过长")
    return value


def validate_event(event):
    if not isinstance(event, dict):
        raise ValueError("记录必须是对象")
    out = {k: text_field(event[k], 200 if k == "id" else 120)
           for k in ("id", "source", "model")}
    out["timestamp"] = timestamp(event["timestamp"])
    for k in ("input_tokens", "cached_input_tokens", "output_tokens"):
        n = event.get(k, 0)
        if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 10**12:
            raise ValueError(f"{k} 必须是非负整数")
        out[k] = n
    if out["cached_input_tokens"] > out["input_tokens"]:
        raise ValueError("缓存 Tokens 不能超过输入 Tokens")
    cost = event.get("cost_usd")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))
                             or not math.isfinite(cost) or not 0 <= cost <= 10**8):
        raise ValueError("cost_usd 无效")
    out["cost_usd"] = cost
    billing = event.get('billing_type', 'plan' if out['source']=='Codex' else 'unknown')
    kind = event.get('cost_kind', 'reported' if cost is not None else 'unknown')
    if billing not in ('plan','api','unknown') or kind not in ('reported','estimated','unknown'):
        raise ValueError('无效的计费类型')
    if cost is not None and kind=='unknown':raise ValueError('金额必须声明为来源费用或估算')
    if billing == 'plan' and cost is not None and kind != 'estimated':
        raise ValueError('订阅用量金额必须标为估算')
    out.update(billing_type=billing,cost_kind=kind)
    conversation_kind=event.get('conversation_kind','conversation')
    if conversation_kind not in ('conversation','background'):raise ValueError('无效的会话类型')
    out['conversation_kind']=conversation_kind
    for key,limit in (('session_id',200),('title',100)):
        out[key] = text_field(event[key],limit) if event.get(key) is not None else None
    return out


class Store:
    def __init__(self, path):
        self.path = prepare_private_database(path)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    device_id TEXT NOT NULL, id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    source TEXT NOT NULL, model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL, cost_usd REAL,
                    PRIMARY KEY(device_id, id));
                CREATE INDEX IF NOT EXISTS event_time ON events(timestamp);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL,
                    seen TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS integration_state (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS event_details (
                    device_id TEXT NOT NULL, id TEXT NOT NULL,
                    billing_type TEXT NOT NULL, cost_kind TEXT NOT NULL,
                    session_id TEXT, title TEXT, PRIMARY KEY(device_id,id));
            """)
            if 'conversation_kind' not in {r[1] for r in db.execute('PRAGMA table_info(event_details)')}:
                db.execute("ALTER TABLE event_details ADD COLUMN conversation_kind TEXT NOT NULL DEFAULT 'conversation'")
            db.execute("""INSERT OR IGNORE INTO event_details(device_id,id,billing_type,cost_kind,session_id,title)
                SELECT device_id,id,CASE WHEN source='Codex' THEN 'plan' ELSE 'unknown' END,
                CASE WHEN cost_usd IS NULL THEN 'unknown' ELSE 'reported' END,NULL,NULL FROM events""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, device, events, status):
        if not isinstance(events, list) or len(events) > 500:
            raise ValueError("每批最多 500 条记录")
        cleaned = [validate_event(e) for e in events]
        # Validate the whole batch before opening the transaction.
        device_id = text_field(device["id"])
        name = text_field(device["name"])
        platform = text_field(device["platform"])
        if not isinstance(status, dict):
            raise ValueError("status 必须是对象")
        # Only retain bounded operational metadata, never arbitrary caller payloads.
        safe_status = {
            "state": str(status.get("state", "ok"))[:20],
            "files": int(status.get("files", 0)),
            "errors": int(status.get("errors", 0)),
            "pending": int(status.get("pending", 0)),
        }
        with self.connect() as db:
            inserted = self.insert_events(db,device_id,cleaned)
            db.execute("INSERT OR REPLACE INTO devices VALUES (?,?,?,?,?)", (
                device_id, name, platform, utcnow(), json.dumps(safe_status)))
        return inserted

    @staticmethod
    def insert_events(db,device_id,events):
        inserted=0
        for e in events:
            result=db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?)',(
                device_id,e['id'],e['timestamp'],e['source'],e['model'],e['input_tokens'],
                e['cached_input_tokens'],e['output_tokens'],e['cost_usd']))
            inserted += result.rowcount
            if result.rowcount:
                db.execute('INSERT OR REPLACE INTO event_details VALUES (?,?,?,?,?,?,?)',(
                    device_id,e['id'],e.get('billing_type','plan' if e['source']=='Codex' else 'unknown'),
                    e.get('cost_kind','unknown'),e.get('session_id'),e.get('title'),e.get('conversation_kind','conversation')))
        return inserted

    def channel_summary(self,billing_type,offset=480,device_id=None,source=None):
        now=datetime.now(timezone.utc)
        today=now.astimezone(timezone(timedelta(minutes=offset))).replace(hour=0,minute=0,second=0,microsecond=0)
        end=(today+timedelta(days=1)).astimezone(timezone.utc).isoformat()
        where='m.billing_type=?';params=[billing_type]
        if device_id is not None:where+=' AND e.device_id=?';params.append(device_id)
        if source is not None:where+=' AND e.source=?';params.append(source)
        joined='events e JOIN event_details m ON e.device_id=m.device_id AND e.id=m.id'
        result={'billing_type':billing_type}
        with self.connect() as db:
            for key,start in (('today',today),('history',today-timedelta(days=29))):
                row=dict(db.execute(f'''SELECT COUNT(*) records,SUM(e.input_tokens+e.output_tokens) tokens,
                    SUM(e.cost_usd) cost_usd,COUNT(e.cost_usd) priced,
                    SUM(CASE WHEN m.cost_kind='estimated' THEN 1 ELSE 0 END) estimates
                    FROM {joined} WHERE {where} AND e.timestamp>=? AND e.timestamp<?''',
                    [*params,start.astimezone(timezone.utc).isoformat(),end]).fetchone())
                # Partial prices are never presented as a complete bill.
                if row['priced'] != row['records'] or not row['records']:row['cost_usd']=None
                result[key]=row
            result['connected']=bool(db.execute(f'SELECT 1 FROM {joined} WHERE {where} LIMIT 1',params).fetchone())
            result['source_devices']=[dict(r) for r in db.execute(
                f'SELECT DISTINCT e.device_id,e.source FROM {joined} WHERE {where}',params)]
            result['conversations']=[dict(r) for r in db.execute(f'''WITH grouped AS (
                SELECT e.device_id,e.source,e.model,m.title,m.session_id,d.name device_name,
                SUM(e.input_tokens+e.output_tokens) OVER (PARTITION BY e.device_id,e.source,COALESCE(m.session_id,e.id)) tokens,
                e.timestamp last_usage,
                ROW_NUMBER() OVER (PARTITION BY e.device_id,e.source,COALESCE(m.session_id,e.id)
                    ORDER BY e.timestamp DESC,e.id DESC) position
                FROM {joined} JOIN devices d ON d.id=e.device_id WHERE {where} AND m.conversation_kind='conversation')
                SELECT device_id,source,model,title,session_id,device_name,tokens,last_usage
                FROM grouped WHERE position=1 ORDER BY last_usage DESC LIMIT 12''',params)]
        return result

    def load_quota_state(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM integration_state WHERE name='codexbar'").fetchone()
        return json.loads(row[0]) if row else {}

    def save_quota_state(self, state):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO integration_state VALUES ('codexbar',?)", (json.dumps(state),))

    def load_token_history(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM integration_state WHERE name='token_history'").fetchone()
        return json.loads(row[0]) if row else {}

    def save_token_history(self,state):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO integration_state VALUES ('token_history',?)",(json.dumps(state),))

    def summary(self, offset_minutes=480, expected=()):
        now = datetime.now(timezone.utc)
        tz = timezone(timedelta(minutes=offset_minutes))
        today = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start = today.astimezone(timezone.utc).isoformat()
        with self.connect() as db:
            totals = dict(db.execute("""SELECT COUNT(*) records,
                COALESCE(SUM(input_tokens+output_tokens),0) tokens,
                COALESCE(SUM(input_tokens),0) input_tokens,
                COALESCE(SUM(output_tokens),0) output_tokens,
                COALESCE(SUM(cached_input_tokens),0) cached_tokens,
                SUM(cost_usd) cost_usd, COUNT(cost_usd) cost_records,
                MAX(timestamp) latest_event FROM events WHERE timestamp>=?""", (start,)).fetchone())
            all_tokens = db.execute("SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM events").fetchone()[0]
            recent = [dict(r) for r in db.execute("""SELECT e.*, d.name device_name
                FROM events e JOIN devices d ON e.device_id=d.id
                ORDER BY timestamp DESC LIMIT 12""")]
            ranking = [dict(r) for r in db.execute("""SELECT source,model,
                SUM(input_tokens+output_tokens) tokens, COUNT(*) records
                FROM events WHERE timestamp>=? GROUP BY source,model ORDER BY tokens DESC LIMIT 10""", (start,))]
            devices = [dict(r) for r in db.execute("SELECT * FROM devices ORDER BY name")]
            device_usage = {r["device_id"]:dict(r) for r in db.execute("""SELECT device_id,
                SUM(CASE WHEN timestamp>=? THEN input_tokens+output_tokens ELSE 0 END) today_tokens,
                MAX(timestamp) last_usage FROM events GROUP BY device_id""",(start,))}
            trend = []
            for days in range(6, -1, -1):
                a = (today-timedelta(days=days)).astimezone(timezone.utc).isoformat()
                b = (today-timedelta(days=days-1)).astimezone(timezone.utc).isoformat()
                n = db.execute("SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM events WHERE timestamp>=? AND timestamp<?", (a,b)).fetchone()[0]
                trend.append({"date": (today-timedelta(days=days)).strftime("%m/%d"), "tokens": n})
        seen = {d["id"] for d in devices}
        for d in expected:
            if d["id"] not in seen:
                devices.append({"id":d["id"], "name":d["name"], "platform":d["platform"], "seen":None,"status":"{}"})
        for d in devices:
            age = (now-datetime.fromisoformat(d["seen"])).total_seconds() if d["seen"] else None
            d["online"] = age is not None and age < 120
            d["age_seconds"] = round(age) if age is not None else None
            d["status"] = json.loads(d["status"])
            usage = device_usage.get(d["id"],{})
            d["today_tokens"] = usage.get("today_tokens",0)
            d["last_usage"] = usage.get("last_usage")
        totals["all_tokens"] = all_tokens
        return {"generated_at": now.isoformat(), "timezone_offset_minutes":offset_minutes,
                "totals":totals,"devices":devices,"recent":recent,"ranking":ranking,"trend":trend,
                "quota": None, "notice":"仅统计已接入的本地记录；订阅额度尚未接入。"}
