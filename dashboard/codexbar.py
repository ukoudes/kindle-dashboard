"""CodexBar dashboard-v1 adapter. Only anonymous quota measurements cross this seam.

Quota snapshots are account-wide state, never additive device usage events.
Raw stdout, identity, errors and credits are deliberately discarded.
"""
import json
import math
import os
import re
import shutil
import signal
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .storage import utcnow


DEFAULT_CONFIG = {"enabled": True, "interval": 120, "timeout": 25}
NAMES = {"codex": "Codex", "claude": "Claude", "cursor": "Cursor", "gemini": "Gemini"}
LABELS = {"session": "时段额度", "weekly": "周额度", "monthly": "月额度",
          "daily": "日额度", "tertiary": "其他额度"}
MESSAGES = {
    "auth_required": "登录状态不可用，请在 CodexBar 中检查该平台",
    "network": "连接平台失败，请在 CodexBar 中检查网络或刷新",
    "timeout": "查询超时，将自动重试",
    "permission": "CodexBar 无法访问所需登录信息",
    "provider_error": "CodexBar 暂未取得该平台额度",
    "invalid_data": "额度数据格式不正确，请检查 CodexBar 版本",
    "not_installed": "未找到 CodexBar 命令行工具",
    "command_failed": "CodexBar 查询未完成，将自动重试",
    "no_windows": "平台未提供可读取的额度窗口",
    "removed": "该平台本轮未返回，等待下次刷新",
}


def find_binary(name="codexbar"):
    found = shutil.which(name)
    if found or name != "codexbar":
        return found
    # Finder-launched command files can have a PATH without Homebrew directories.
    for path in (Path("/Applications/CodexBar.app/Contents/Helpers/CodexBarCLI"),
                 Path.home()/"Applications/CodexBar.app/Contents/Helpers/CodexBarCLI",
                 Path("/opt/homebrew/bin/codexbar"), Path("/usr/local/bin/codexbar")):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def parse_date(value):
    if not isinstance(value, str):
        raise ValueError("missing date")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("date must have timezone")
    return dt.astimezone(timezone.utc)


def error_code(error):
    # Classify in memory. Never retain or forward an upstream error string.
    message = str(error.get("message", "") if isinstance(error, dict) else error).lower()
    for code, terms in (
        ("permission", ("keychain", "permission", "denied")),
        ("auth_required", ("sign in", "login", "credential", "cookie", "authentication", "unauthorized", "401", "session key")),
        ("timeout", ("timed out", "timeout")),
        ("network", ("network", "offline", "connect", "resolve host")),
    ):
        if any(term in message for term in terms):
            return code
    return "provider_error"


def percentage(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("invalid percentage")
    return round(value, 2)


def normalize_snapshot(payload, now=None):
    """Accept schema-v1 top-level selected accounts only, once per provider."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict) or type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 1:
        raise ValueError("unsupported CodexBar schema")
    rows = payload.get("providers")
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError("invalid provider list")
    seen = set()
    providers = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid provider row")
        provider_id = row.get("id")
        if not isinstance(provider_id, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", provider_id) or provider_id in seen:
            raise ValueError("invalid or duplicate provider")
        seen.add(provider_id)
        if row.get("enabled") is False:
            continue
        provider = {"id": provider_id, "name": NAMES.get(provider_id, provider_id),
                    "measured_at": None, "source": "CodexBar", "windows": [], "error_code": None}
        if row.get("error") is not None:
            provider["error_code"] = error_code(row["error"])
            providers.append(provider)
            continue
        try:
            raw_windows = row.get("windows", [])
            if not isinstance(raw_windows, list) or len(raw_windows) > 32:
                raise ValueError("invalid windows")
            if raw_windows:
                measured = parse_date(row.get("updatedAt"))
                if measured > now + timedelta(seconds=60):
                    raise ValueError("measurement in future")
                provider["measured_at"] = measured.isoformat()
            window_ids = set()
            for index, window in enumerate(raw_windows):
                if not isinstance(window, dict):
                    raise ValueError("invalid window")
                kind = window.get("kind")
                if not isinstance(kind, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,96}", kind) or kind in window_ids:
                    raise ValueError("invalid window kind")
                window_ids.add(kind)
                used = window.get("usedPercent")
                remaining = window.get("remainingPercent")
                # Unknown measurements remain unknown. Never turn a missing value into 0%.
                if used is None and remaining is None:
                    continue
                if used is None:
                    remaining = percentage(remaining)
                    used = round(100 - remaining, 2)
                else:
                    used = percentage(used)
                    remaining = percentage(remaining) if remaining is not None else round(100 - used, 2)
                if abs(used + remaining - 100) > 1.1:
                    raise ValueError("inconsistent percentages")
                reset = window.get("resetAt")
                reset = parse_date(reset).isoformat() if reset is not None else None
                # Use known translated labels; upstream free text can contain personal data.
                label = LABELS.get(kind, f"其他额度 {index + 1}")
                provider["windows"].append({"kind": kind, "label": label, "used_percent": used,
                                             "remaining_percent": remaining, "reset_at": reset})
            if not provider["windows"]:
                provider["error_code"] = "no_windows"
        except (ValueError, TypeError, OverflowError):
            provider.update(windows=[], measured_at=None, error_code="invalid_data")
        providers.append(provider)
    return providers


def merge_snapshot(previous, current, attempted_at):
    """Keep last-good data on errors; an authoritative empty/disabled list removes it."""
    old = {p["id"]: p for p in previous.get("providers", [])}
    result = []
    for provider in current:
        p = dict(provider)
        prior = old.get(p["id"])
        if p["error_code"] and p["error_code"] != "no_windows" and prior:
            p["windows"] = prior.get("windows", [])
            p["measured_at"] = prior.get("measured_at")
        p["last_attempt"] = attempted_at
        result.append(p)
    return {"last_attempt": attempted_at, "bridge_error": None, "providers": result}


def quota_view(state, enabled=True, now=None, interval=120):
    """Freshness and reset expiry are computed on every read, even if polling stops."""
    now = now or datetime.now(timezone.utc)
    if not enabled:
        return {"state": "disabled", "providers": [], "last_attempt": None,
                "message": "CodexBar 接入已关闭", "interval": interval}
    state = state or {}
    rows = []
    for stored in state.get("providers", []):
        p = dict(stored)
        p["windows"] = [dict(w) for w in stored.get("windows", [])]
        measured = parse_date(p["measured_at"]) if p.get("measured_at") else None
        p["age_seconds"] = max(0, int((now - measured).total_seconds())) if measured else None
        p["stale"] = bool(p["windows"]) and (bool(p.get("error_code") or state.get("bridge_error"))
                         or p["age_seconds"] is None or p["age_seconds"] > 180)
        for window in p["windows"]:
            window["label"] = LABELS.get(window["kind"], window["label"])
            window["expired"] = bool(window["reset_at"] and parse_date(window["reset_at"]) <= now)
        if any(w["expired"] for w in p["windows"]):
            p["stale"] = True
        p["state"] = "stale" if p["stale"] else ("ok" if p["windows"] else "unavailable")
        p["message"] = MESSAGES.get(p.get("error_code") or state.get("bridge_error"), "")
        if p["stale"] and not p["message"]:
            p["message"] = "上次额度读数已过期，等待重新查询"
        rows.append(p)
    healthy = sum(p["state"] == "ok" for p in rows)
    status = "ok" if rows and healthy == len(rows) else ("partial" if healthy else ("stale" if any(p["windows"] for p in rows) else "unavailable"))
    if not state:
        status = "connecting"
    return {"state": status, "providers": rows, "last_attempt": state.get("last_attempt"),
            "message": MESSAGES.get(state.get("bridge_error"), "正在读取 CodexBar" if not state else ""),
            "interval": interval}


def fetch_json(binary, arguments, timeout, stop=None):
    """Run the installed CLI without a shell; bound output and kill our child tree."""
    import tempfile
    import time
    # File-backed pipes bound Python memory. Raw output exists only in private temp files.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen([binary, *arguments],
                                stdout=output, stderr=errors, start_new_session=os.name != "nt")
        deadline = time.monotonic() + timeout + 3
        try:
            while proc.poll() is None:
                if (stop and stop.is_set()) or time.monotonic() > deadline:
                    raise TimeoutError("CodexBar query timed out")
                if output.tell() > 4 * 1024 * 1024 or errors.tell() > 1024 * 1024:
                    raise ValueError("CodexBar output too large")
                if stop:
                    stop.wait(0.1)
                else:
                    time.sleep(0.1)
            if proc.returncode != 0:
                raise OSError("CodexBar command failed")
            output.seek(0)
            raw = output.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("CodexBar output too large")
            return json.loads(raw)
        finally:
            # The command can leave provider-owned children after exiting or timing out.
            if os.name != "nt":
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
                proc.wait(timeout=2)


def fetch_dashboard(binary, timeout, stop=None):
    return fetch_json(binary, ["dashboard", "--identity", "redacted", "--timeout", str(timeout)], timeout, stop)


def fetch_token_history(binary, timeout, stop=None):
    return fetch_json(binary, ["cost", "--provider", "codex", "--format", "json", "--summary-only", "--days", "30"], timeout, stop)


def normalize_history(payload):
    if not isinstance(payload, list) or len(payload) != 1:
        raise ValueError('invalid history response')
    row = payload[0]
    if row.get('provider') != 'codex' or type(row.get('schemaVersion')) is not int or row['schemaVersion'] != 1 or row.get('historyDays') != 30:
        raise ValueError('unsupported history scope')
    measured = parse_date(row['updatedAt'])
    if measured > datetime.now(timezone.utc)+timedelta(seconds=60):
        raise ValueError('future history')
    values = {}
    partial = row.get('historyCoverageIsEstablished') is not True
    for key in ('today','history'):
        bucket = row[key]
        value = bucket['totalTokens']
        if type(value) is not int or not 0 <= value <= 10**15:
            raise ValueError('invalid token count')
        values[key+'_tokens'] = value
        partial = partial or bool(bucket.get('incompleteRequestCount'))
        cost=bucket.get('costUSD')
        coverage=bucket.get('coverage') or {}
        complete=not bucket.get('incompleteRequestCount') and not coverage.get('unpriced') and not coverage.get('unmetered')
        values[key+'_cost_usd'] = (float(cost) if row.get('currencyCode')=='USD' and complete
            and type(cost) in (int,float) and math.isfinite(cost) and 0<=cost<=10**8 else None)
    zone = row.get('bucketTimeZone')
    if not isinstance(zone,str) or not re.fullmatch(r'[A-Za-z0-9_+/:-]{1,80}',zone):
        raise ValueError('missing history timezone')
    return {**values,'measured_at':measured.isoformat(),'period_days':30,'timezone':zone,
            'source':'CodexBar','scope':'local_codex','partial':partial,'cost_kind':'estimated','currency':'USD'}


def history_view(state, enabled=True):
    if not enabled:
        return {'state':'disabled','period_days':30,'history_tokens':None}
    state = state or {}
    snapshot = state.get('snapshot')
    if not snapshot:
        return {'state':'unavailable' if state else 'connecting','period_days':30,'history_tokens':None}
    age = (datetime.now(timezone.utc)-parse_date(snapshot['measured_at'])).total_seconds()
    stale = bool(state.get('error_code')) or age > 180
    return {**snapshot,'stale':stale,'state':'stale' if stale else ('partial' if snapshot['partial'] else 'ok')}


class TokenHistoryBridge:
    def __init__(self, store, get_config, fetcher=fetch_token_history):
        self.store,self.get_config,self.fetcher = store,get_config,fetcher

    def refresh(self, stop=None):
        config = {**DEFAULT_CONFIG, **self.get_config().get('codexbar',{})}
        if not config['enabled']:
            return config
        prior = self.store.load_token_history()
        try:
            binary = find_binary(config.get('binary','codexbar'))
            if not binary:
                raise FileNotFoundError('CodexBar missing')
            payload = self.fetcher(binary,max(3,min(60,int(config['timeout']))),stop)
            state = {'snapshot':normalize_history(payload),'error_code':None}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            state = {**prior,'error_code':'unavailable'}
        self.store.save_token_history(state)
        return config

    def run(self,stop):
        while not stop.is_set():
            config = self.refresh(stop)
            stop.wait(max(30,int(config.get('interval',120))))


class CodexBarBridge:
    def __init__(self, store, get_config, fetcher=fetch_dashboard):
        self.store = store
        self.get_config = get_config
        self.fetcher = fetcher

    def refresh(self, stop=None):
        config = {**DEFAULT_CONFIG, **self.get_config().get("codexbar", {})}
        if not config["enabled"]:
            return config
        prior = self.store.load_quota_state()
        now = utcnow()
        binary = find_binary(config.get("binary", "codexbar"))
        try:
            if not binary:
                raise FileNotFoundError("CodexBar missing")
            payload = self.fetcher(binary, max(3, min(60, int(config["timeout"]))), stop)
            current = normalize_snapshot(payload)
            state = merge_snapshot(prior, current, now)
        except (OSError, ValueError, TimeoutError) as exc:
            code = ("not_installed" if isinstance(exc, FileNotFoundError) else
                    "timeout" if isinstance(exc, TimeoutError) else
                    "invalid_data" if isinstance(exc, ValueError) else "command_failed")
            state = {**prior, "last_attempt": now, "bridge_error": code}
        self.store.save_quota_state(state)
        return config

    def run(self, stop):
        while not stop.is_set():
            config = self.refresh(stop)
            stop.wait(max(30, int(config.get("interval", 120))))
