import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from dashboard.storage import Store,utcnow,validate_event
from dashboard.collector import Collector
from dashboard.panels import build_panels,select_panel
from dashboard.codexbar import normalize_history


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'hub.db')
        self.mac={'id':'mac','name':'MacBook','platform':'Darwin'}
        self.win={'id':'win','name':'Windows','platform':'Windows'}

    def event(self,ident='one',**values):
        return {'id':ident,'source':'Codex','model':'test','timestamp':utcnow(),
                'input_tokens':100,'cached_input_tokens':80,'output_tokens':10,
                'session_id':'thread-one','title':'看板开发',**values}

    def test_classification_and_session_aggregation_survive_retries(self):
        for device in (self.mac,self.win):
            self.store.ingest(device,[self.event(),self.event('two')],{})
            self.store.ingest(device,[self.event()],{})
        self.store.ingest(self.mac,[self.event('api',billing_type='api',cost_usd=.1)],{})
        self.store.ingest(self.mac,[self.event('unknown',source='other',session_id=None)],{})
        plan=self.store.channel_summary('plan',device_id='mac')
        self.assertEqual(plan['today']['tokens'],220)
        self.assertEqual(plan['conversations'][0]['tokens'],220)
        self.assertEqual(len(plan['conversations']),1)
        self.assertEqual(len(self.store.channel_summary('plan')['conversations']),2)
        api=self.store.channel_summary('api')
        self.assertEqual(api['today']['tokens'],110)
        self.assertAlmostEqual(api['today']['cost_usd'],.1)
        self.assertEqual(api['conversations'][0]['title'],'看板开发')

    def test_unknown_cost_is_not_zero_or_partial_bill(self):
        self.store.ingest(self.mac,[self.event(billing_type='api',cost_usd=0)],{})
        self.assertEqual(self.store.channel_summary('api')['today']['cost_usd'],0)
        self.store.ingest(self.mac,[self.event('unpriced',billing_type='api')],{})
        self.assertIsNone(self.store.channel_summary('api')['today']['cost_usd'])
        with self.assertRaises(ValueError):validate_event(self.event(cost_usd=.1))
        with self.assertRaises(ValueError):validate_event(self.event(billing_type='anything'))
        self.assertEqual(validate_event(self.event(cost_usd=.1,cost_kind='estimated'))['cost_kind'],'estimated')

    def test_background_usage_stays_in_totals_without_filling_recent_conversations(self):
        self.store.ingest(self.mac,[self.event(),self.event('background',session_id='review',
            title='Background review',conversation_kind='background')],{})
        panel=self.store.channel_summary('plan')
        self.assertEqual(panel['today']['tokens'],220)
        self.assertEqual(len(panel['conversations']),1)
        self.assertEqual(panel['conversations'][0]['tokens'],110)

    def test_headline_uses_one_device_and_one_snapshot_for_both_periods(self):
        self.store.ingest(self.mac,[self.event()],{})
        self.store.ingest(self.win,[self.event(input_tokens=9000)],{})
        data=self.store.summary(expected=[self.mac,self.win])
        data['token_history']={'today_tokens':555,'history_tokens':999,'today_cost_usd':1.2,
            'history_cost_usd':7.8,'timezone':'Asia/Shanghai','measured_at':utcnow(),'state':'ok'}
        build_panels(self.store,data,{'local_device':self.mac})
        selected=select_panel(data)
        self.assertEqual(selected['today'],{'tokens':555,'cost_usd':1.2})
        self.assertEqual(selected['history'],{'tokens':999,'cost_usd':7.8})
        self.assertEqual(len(selected['conversations']),1)
        self.assertEqual(select_panel(data,device='win')['today']['tokens'],9010)
        self.assertIsNone(select_panel(data,'api')['today']['tokens'])
        data['token_history']['measured_at']=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        build_panels(self.store,data,{'local_device':self.mac})
        self.assertIsNone(select_panel(data)['today']['tokens'])
        self.assertIsNone(select_panel(data)['today']['cost_usd'])

    def test_mixed_native_api_disables_unscoped_plan_snapshot(self):
        self.store.ingest(self.mac,[self.event(),self.event('api',billing_type='api')],{})
        data=self.store.summary(expected=[self.mac]);data['token_history']={
            'today_tokens':9999,'history_tokens':88888,'today_cost_usd':9,'history_cost_usd':99,
            'timezone':'Asia/Shanghai','measured_at':utcnow(),'state':'ok'}
        build_panels(self.store,data,{'local_device':self.mac})
        panel=select_panel(data)
        self.assertEqual(panel['today']['tokens'],110)
        self.assertIsNone(panel['today']['cost_usd'])
        self.assertEqual(panel['state'],'partial')

    def test_only_read_display_title_and_retain_metadata_in_hub(self):
        logs=self.root/'sessions';logs.mkdir()
        with sqlite3.connect(self.root/'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads(id,title,name,first_user_message)')
            db.execute('INSERT INTO threads VALUES (?,?,?,?)',('thread-one','旧标题','自定义标题','PRIVATE BODY'))
        row={'timestamp':utcnow(),'type':'event_msg','payload':{'type':'token_count','info':{
            'total_token_usage':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':10},
            'last_token_usage':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':10}}}}
        (logs/'log.jsonl').write_text(json.dumps({'type':'session_meta','payload':{'id':'thread-one'}})+'\n'+json.dumps(row)+'\n')
        c=Collector(self.root/'agent.db',{'codex_dir':str(logs)});self.addCleanup(c.close)
        self.assertEqual(c.scan()['errors'],0)
        event=c.batch()[0]
        self.assertEqual(event['title'],'自定义标题')
        self.assertNotIn('PRIVATE BODY',json.dumps(event))
        self.store.ingest(self.mac,[event],{})
        conversation=self.store.channel_summary('plan')['conversations'][0]
        self.assertEqual(conversation['title'],'自定义标题')
        self.assertEqual(conversation['tokens'],110)

    def test_estimates_require_usd_and_complete_price_coverage(self):
        row={'schemaVersion':1,'provider':'codex','historyDays':30,'updatedAt':utcnow(),
             'historyCoverageIsEstablished':True,'bucketTimeZone':'Asia/Shanghai','currencyCode':'USD',
             'today':{'totalTokens':100,'costUSD':.1},'history':{'totalTokens':1000,'costUSD':2}}
        self.assertEqual(normalize_history([row])['today_cost_usd'],.1)
        row['today']['coverage']={'unpriced':1}
        self.assertIsNone(normalize_history([row])['today_cost_usd'])
        row['currencyCode']='CNY'
        self.assertIsNone(normalize_history([row])['history_cost_usd'])


if __name__=='__main__':unittest.main()
