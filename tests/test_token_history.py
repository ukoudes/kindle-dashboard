import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from dashboard.codexbar import TokenHistoryBridge,normalize_history,history_view
from dashboard.storage import Store,utcnow
from dashboard.render import render_dashboard,history_metric


class TokenHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'hub.db')
        self.payload=[{'schemaVersion':1,'provider':'codex','historyDays':30,'bucketTimeZone':'Asia/Shanghai',
            'updatedAt':utcnow(),'historyCoverageIsEstablished':True,'currencyCode':'USD',
            'today':{'totalTokens':62000000,'incompleteRequestCount':0,'costUSD':123},
            'history':{'totalTokens':1260000000,'incompleteRequestCount':0,'costUSD':999},
            'accountEmail':'private@example.test'}]

    def test_history_has_explicit_scope_estimated_costs_and_no_identity(self):
        value=normalize_history(self.payload)
        self.assertEqual(value['history_tokens'],1260000000)
        self.assertEqual(value['period_days'],30)
        self.assertEqual(value['scope'],'local_codex')
        self.assertNotIn('private',json.dumps(value))
        self.assertEqual(value['today_cost_usd'],123)
        self.assertEqual(value['cost_kind'],'estimated')

    def test_invalid_or_unknown_totals_never_become_zero(self):
        for count in (None,-1,True,1.2,'123'):
            payload=copy.deepcopy(self.payload);payload[0]['history']['totalTokens']=count
            with self.assertRaises(ValueError):normalize_history(payload)
        payload=copy.deepcopy(self.payload);payload[0]['historyDays']=90
        with self.assertRaises(ValueError):normalize_history(payload)

    def test_bridge_retains_last_good_and_never_adds_snapshot_to_events(self):
        bridge=TokenHistoryBridge(self.store,lambda:{'codexbar':{'binary':sys.executable}},lambda *a:self.payload)
        bridge.refresh();bridge.refresh()
        self.assertEqual(self.store.summary()['totals']['all_tokens'],0)
        self.assertEqual(history_view(self.store.load_token_history())['state'],'ok')
        def fail(*a):raise TimeoutError('no response')
        bridge.fetcher=fail;bridge.refresh()
        view=history_view(self.store.load_token_history())
        self.assertEqual(view['history_tokens'],1260000000);self.assertEqual(view['state'],'stale')

    def test_partial_and_expired_history_stays_marked(self):
        self.payload[0]['history']['incompleteRequestCount']=2
        value=normalize_history(self.payload)
        self.assertEqual(history_view({'snapshot':value})['state'],'partial')
        value['measured_at']=(datetime.now(timezone.utc)-timedelta(minutes=4)).isoformat()
        self.assertEqual(history_view({'snapshot':value})['state'],'stale')
        self.assertIsNone(history_view({})['history_tokens'])
        self.assertEqual(history_view({'snapshot':value},enabled=False)['state'],'disabled')

    def test_headline_does_not_silently_use_partial_import_total(self):
        data=self.store.summary();data['totals']['all_tokens']=666000000
        data['token_history']=history_view({'snapshot':normalize_history(self.payload)})
        self.assertEqual(history_metric(data),('本机近30天 Tokens','1.26B'))
        data['token_history']=history_view({})
        self.assertEqual(history_metric(data)[1],'—')

    def test_progress_bar_fills_remaining_not_used(self):
        data=self.store.summary()
        data['quotas']={'providers':[{'id':'codex','name':'Codex','state':'ok','stale':False,
            'measured_at':utcnow(),'windows':[{'kind':'session','label':'时段额度','used_percent':75,
            'remaining_percent':25,'reset_at':None}]}]}
        image=render_dashboard(600,800,data,'usage')
        # Main Coding Plan page: only the first quarter of the remaining bar fills.
        self.assertEqual(image.getpixel((100,480)),0)
        self.assertEqual(image.getpixel((300,480)),255)


if __name__=='__main__':unittest.main()
