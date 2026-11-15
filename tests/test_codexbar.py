import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard.codexbar import (CodexBarBridge, fetch_dashboard, merge_snapshot,
                                normalize_snapshot, quota_view)
from dashboard.storage import Store
from dashboard.render import render_dashboard
from dashboard.web import DashboardServer
from kindle_dashboard import write_json


class CodexBarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "hub.sqlite3")
        self.now = datetime.now(timezone.utc)
        self.snapshot = {
            "schemaVersion": 1, "generatedAt": self.now.isoformat(),
            "providers": [{
                "id": "codex", "name": "private-name@example.test", "enabled": True,
                "updatedAt": self.now.isoformat(), "source": "oauth", "error": None,
                "identity": {"accountEmail": "private@example.test", "plan": "Private Plan"},
                "credits": {"remaining": 99}, "cost": {"todayUSD": 123},
                "windows": [
                    {"kind": "session", "label": "private@example.test", "usedPercent": 57,
                     "remainingPercent": 43, "resetAt": (self.now + timedelta(hours=2)).isoformat()},
                    {"kind": "weekly", "usedPercent": 53, "remainingPercent": 47,
                     "resetAt": (self.now + timedelta(days=5)).isoformat()},
                ],
                "accounts": [{"windows": [{"usedPercent": 99}]}],
            }, {
                "id": "claude", "enabled": True, "updatedAt": self.now.isoformat(),
                "windows": [], "error": {"message": "network failed at /private/path?secret=HIDDEN"},
            }],
        }

    def state(self):
        return merge_snapshot({}, normalize_snapshot(self.snapshot, self.now), self.now.isoformat())

    def test_real_schema_privacy_and_no_cost_or_account_aggregation(self):
        rows = normalize_snapshot(self.snapshot, self.now)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]["windows"]), 2)
        self.assertEqual(rows[0]["windows"][0]["used_percent"], 57)
        raw = json.dumps(rows)
        for secret in ("private", "HIDDEN", "identity", "credits", "cost", "accounts"):
            self.assertNotIn(secret, raw)
        self.assertEqual(rows[1]["error_code"], "network")

    def test_partial_failure_preserves_healthy_provider(self):
        view = quota_view(self.state(), now=self.now)
        self.assertEqual(view["state"], "partial")
        self.assertEqual([r["state"] for r in view["providers"]], ["ok", "unavailable"])

    def test_known_zero_not_confused_with_unknown(self):
        w = self.snapshot["providers"][0]["windows"][0]
        w.update(usedPercent=0, remainingPercent=100)
        self.assertEqual(normalize_snapshot(self.snapshot, self.now)[0]["windows"][0]["used_percent"], 0)
        w.update(usedPercent=None, remainingPercent=None)
        rows = normalize_snapshot(self.snapshot, self.now)
        self.assertEqual(len(rows[0]["windows"]), 1)
        self.assertEqual(rows[0]["windows"][0]["kind"], "weekly")

    def test_remaining_only_and_no_reset(self):
        self.snapshot["providers"][0]["windows"] = [{"kind": "session", "remainingPercent": 63}]
        w = normalize_snapshot(self.snapshot, self.now)[0]["windows"][0]
        self.assertEqual(w["used_percent"], 37)
        self.assertIsNone(w["reset_at"])

    def test_invalid_percentage_is_isolated(self):
        for value in (-1, 101, float("nan"), float("inf"), True, "57"):
            self.snapshot["providers"][0]["windows"][0]["usedPercent"] = value
            rows = normalize_snapshot(self.snapshot, self.now)
            self.assertEqual(rows[0]["error_code"], "invalid_data")
            self.assertEqual(rows[0]["windows"], [])
            self.assertEqual(rows[1]["error_code"], "network")

    def test_future_or_missing_measurement_rejected(self):
        for value in (None, "bad date", (self.now + timedelta(hours=1)).isoformat()):
            self.snapshot["providers"][0]["updatedAt"] = value
            self.assertEqual(normalize_snapshot(self.snapshot, self.now)[0]["error_code"], "invalid_data")

    def test_schema_and_duplicate_provider_fail_closed(self):
        for version in (None, 2, True, "1"):
            self.snapshot["schemaVersion"] = version
            with self.assertRaises(ValueError):
                normalize_snapshot(self.snapshot, self.now)
        self.snapshot["schemaVersion"] = 1
        self.snapshot["providers"].append(copy.deepcopy(self.snapshot["providers"][0]))
        with self.assertRaises(ValueError):
            normalize_snapshot(self.snapshot, self.now)

    def test_last_good_survives_error_without_becoming_fresh(self):
        prior = self.state()
        self.snapshot["providers"][0]["error"] = {"message": "No login credential"}
        new = merge_snapshot(prior, normalize_snapshot(self.snapshot, self.now), self.now.isoformat())
        p = quota_view(new, now=self.now)["providers"][0]
        self.assertEqual(p["state"], "stale")
        self.assertEqual(p["measured_at"], prior["providers"][0]["measured_at"])
        self.assertEqual(p["windows"][0]["used_percent"], 57)
        self.assertEqual(p["error_code"], "auth_required")

    def test_empty_and_disabled_provider_clear_cached_windows(self):
        prior = self.state()
        self.snapshot["providers"][0]["windows"] = []
        new = merge_snapshot(prior, normalize_snapshot(self.snapshot, self.now), self.now.isoformat())
        self.assertEqual(new["providers"][0]["windows"], [])
        self.snapshot["providers"][0]["enabled"] = False
        new = merge_snapshot(prior, normalize_snapshot(self.snapshot, self.now), self.now.isoformat())
        self.assertEqual([r["id"] for r in new["providers"]], ["claude"])
        self.assertEqual(merge_snapshot(prior, [], self.now.isoformat())["providers"], [])

    def test_stale_by_measurement_time_and_reset_expiry(self):
        state = self.state()
        self.assertTrue(quota_view(state, now=self.now+timedelta(seconds=181))["providers"][0]["stale"])
        state["providers"][0]["windows"][0]["reset_at"] = (self.now-timedelta(seconds=1)).isoformat()
        p = quota_view(state, now=self.now)["providers"][0]
        self.assertTrue(p["stale"])
        self.assertTrue(p["windows"][0]["expired"])
        self.assertEqual(p["windows"][0]["remaining_percent"], 43)  # Never invent a 100% reset.

    def test_command_error_and_disabled_views(self):
        state = {**self.state(), "bridge_error": "timeout"}
        self.assertTrue(quota_view(state, now=self.now)["providers"][0]["stale"])
        self.assertEqual(quota_view(state, enabled=False)["providers"], [])
        self.assertEqual(quota_view({})["state"], "connecting")

    def test_bridge_persists_only_sanitized_data_and_recovers(self):
        config = {"codexbar": {"binary": sys.executable}}
        bridge = CodexBarBridge(self.store, lambda: config, fetcher=lambda *args: self.snapshot)
        bridge.refresh()
        state = self.store.load_quota_state()
        self.assertEqual(len(state["providers"]), 2)
        self.assertNotIn("private", json.dumps(state))
        def fail(*args):
            raise TimeoutError("private secret must not escape")
        bridge.fetcher = fail
        bridge.refresh()
        self.assertEqual(self.store.load_quota_state()["bridge_error"], "timeout")
        bridge.fetcher = lambda *args: self.snapshot
        bridge.refresh()
        self.assertIsNone(self.store.load_quota_state()["bridge_error"])
        self.assertEqual(quota_view(self.store.load_quota_state())["providers"][0]["state"], "ok")

    def test_missing_cli_does_not_break_store_or_overwrite_usage(self):
        config = {"codexbar": {"binary": "definitely-not-installed-kindle-cli"}}
        CodexBarBridge(self.store, lambda: config).refresh()
        self.assertEqual(self.store.load_quota_state()["bridge_error"], "not_installed")
        self.assertEqual(self.store.summary()["totals"]["tokens"], 0)

    def test_render_quota_and_compact_overview_with_long_errors(self):
        data = self.store.summary()
        data["quotas"] = quota_view(self.state(), now=self.now)
        for dims in ((600,800), (800,600), (758,1024), (1072,1448)):
            for view in ("usage", "quota", "devices"):
                self.assertEqual(render_dashboard(*dims, data, view).size, dims)

    @unittest.skipIf(os.name == "nt", "POSIX test executable")
    def test_actual_cli_transport_and_cancellation(self):
        cli = self.root / "fake-codexbar"
        cli.write_text(f"#!{sys.executable}\nimport json\nprint({json.dumps(self.snapshot)!r})\n")
        cli.chmod(0o700)
        self.assertEqual(fetch_dashboard(str(cli), 3)["schemaVersion"], 1)
        cli.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        stop = threading.Event()
        stop.set()
        before = time.monotonic()
        with self.assertRaises(TimeoutError):
            fetch_dashboard(str(cli), 3, stop)
        self.assertLess(time.monotonic() - before, 3)

    def test_http_summary_is_nonblocking_and_does_not_expose_identity(self):
        import urllib.request
        self.store.save_quota_state(self.state())
        config = self.root / "hub.json"
        write_json(config, {"local_device": {"id":"local", "name":"Mac", "platform":"Darwin"}, "devices":[]})
        server = DashboardServer(("127.0.0.1", 0), self.store, config)
        code = server.access.create_pairing()['pairing']['code']
        token = server.access.pair(code, 'Test browser', '127.0.0.1', 'test')
        opener = urllib.request.build_opener()
        opener.addheaders.append(('Cookie', 'kindle_access='+token))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        start = time.monotonic()
        with opener.open(url+"/api/summary", timeout=2) as response:
            raw = response.read()
            data = json.loads(raw)
        self.assertLess(time.monotonic()-start, 2)
        self.assertEqual(data["quotas"]["state"], "partial")
        self.assertNotIn(b"private", raw)
        self.assertNotIn(b"HIDDEN", raw)
        with opener.open(url+"/kindle?view=quota") as response:
            html = response.read().decode()
        self.assertEqual(html.count("aria-label="), 3)
        server.codexbar_enabled = False
        with opener.open(url+"/api/summary") as response:
            self.assertEqual(json.load(response)["quotas"]["state"], "disabled")


if __name__ == "__main__":
    unittest.main()
