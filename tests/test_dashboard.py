import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

from dashboard.collector import Collector, run_agent
from dashboard.render import render_dashboard
from dashboard.storage import Store, utcnow
from dashboard.web import DashboardServer
from kindle_dashboard import write_json


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.device={"id":"win-device","name":"Windows 工作站","platform":"Windows","token":"pair-secret"}
        self.local={"id":"mac-device","name":"MacBook","platform":"Darwin"}
        self.store=Store(self.root/"hub.sqlite3")
        self.config=self.root/"hub.json"
        write_json(self.config,{"local_device":self.local,"devices":[self.device],"timezone_offset_minutes":480})

    def tearDown(self):
        self.temp.cleanup()

    def event(self,ident="a",**kwargs):
        return {"id":ident,"source":"custom","model":"test-model","timestamp":utcnow(),
                "input_tokens":100,"cached_input_tokens":20,"output_tokens":30,"cost_usd":None,**kwargs}

    def start_server(self):
        server=DashboardServer(("127.0.0.1",0),self.store,self.config)
        code=server.access.create_pairing()['pairing']['code']
        token=server.access.pair(code,'Test browser','127.0.0.1','test')
        prior=urllib.request._opener
        opener=urllib.request.build_opener()
        opener.addheaders.append(('Cookie','kindle_access='+token))
        urllib.request.install_opener(opener)
        self.addCleanup(urllib.request.install_opener,prior)
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def post(self,url,payload,token="pair-secret"):
        request=urllib.request.Request(url+"/api/ingest",json.dumps(payload).encode(),
            headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
        return urllib.request.urlopen(request,timeout=3)

    def test_idempotency_atomic_validation_and_unknown_cost(self):
        self.assertEqual(self.store.ingest(self.device,[self.event()],{}),1)
        self.assertEqual(self.store.ingest(self.device,[self.event()],{}),0)
        with self.assertRaises(ValueError):
            self.store.ingest(self.device,[self.event("b"),self.event("bad",input_tokens=-1)],{})
        data=self.store.summary()
        self.assertEqual(data["totals"]["tokens"],130)
        self.assertEqual(data["totals"]["records"],1)
        self.assertIsNone(data["totals"]["cost_usd"])
        self.assertIsNone(data["quota"])

    def test_two_devices_and_partial_cost_coverage(self):
        self.store.ingest(self.device,[self.event()],{})
        self.store.ingest(self.local,[self.event(cost_usd=0.2)],{})
        data=self.store.summary()
        self.assertEqual(data["totals"]["tokens"],260)
        self.assertEqual(data["totals"]["cost_records"],1)
        self.assertAlmostEqual(data["totals"]["cost_usd"],0.2)
        self.assertEqual(len(data["devices"]),2)

    def test_shanghai_day_boundary_and_trend(self):
        now=datetime.now(timezone(timedelta(hours=8)))
        start=now.replace(hour=0,minute=0,second=0,microsecond=0)
        self.store.ingest(self.device,[self.event("yesterday",timestamp=(start-timedelta(seconds=1)).isoformat()),
                         self.event("today",timestamp=start.isoformat())],{})
        data=self.store.summary()
        self.assertEqual(data["totals"]["tokens"],130)
        self.assertEqual(data["totals"]["all_tokens"],260)
        self.assertEqual(data["trend"][-2]["tokens"],130)
        self.assertEqual(data["trend"][-1]["tokens"],130)

    def test_offline_and_expected_devices(self):
        self.store.ingest(self.local,[],{})
        with self.store.connect() as db:
            db.execute("UPDATE devices SET seen=?",((datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat(),))
        data=self.store.summary(expected=[self.local,self.device])
        self.assertFalse(any(d["online"] for d in data["devices"]))
        self.assertIsNone(next(d for d in data["devices"] if d["id"]==self.device["id"])["seen"])

    def test_codex_cumulative_restart_and_repeated_snapshots(self):
        logs=self.root/"sessions"
        logs.mkdir()
        f=logs/"rollout.jsonl"
        def count(n):
            return {"timestamp":utcnow(),"type":"event_msg","payload":{"type":"token_count","info":{
                "total_token_usage":{"input_tokens":n,"cached_input_tokens":n//2,"output_tokens":n//10},
                "last_token_usage":{"input_tokens":100,"cached_input_tokens":50,"output_tokens":10}}}}
        rows=[{"type":"session_meta","payload":{"id":"session-one"}},
              {"type":"turn_context","payload":{"model":"test-codex"}},count(100),count(100),count(200)]
        f.write_text("".join(json.dumps(r)+"\n" for r in rows))
        c=Collector(self.root/"agent.sqlite3",{"codex_dir":str(logs)})
        self.assertEqual(c.scan()["pending"],2)
        events=c.batch()
        self.assertEqual(sum(e["input_tokens"]+e["output_tokens"] for e in events),220)
        self.assertTrue(all(e["cost_usd"] is None for e in events))
        c.acknowledge(events)
        c.close()
        c=Collector(self.root/"agent.sqlite3",{"codex_dir":str(logs)})
        self.addCleanup(c.close)
        self.assertEqual(c.scan()["pending"],0)
        with f.open("a") as stream:
            stream.write(json.dumps(count(300))+"\n")
        self.assertEqual(c.scan()["pending"],1)
        self.assertEqual(c.batch()[0]["input_tokens"],100)

    def test_resumed_log_excludes_inherited_cumulative_baseline(self):
        c=Collector(self.root/"agent.sqlite3",{})
        self.addCleanup(c.close)
        context={"session":"continued-session"}
        def snapshot(total,last):
            return {"timestamp":utcnow(),"type":"event_msg","payload":{"type":"token_count","info":{
                "total_token_usage":{"input_tokens":total,"cached_input_tokens":total//2,"output_tokens":total//10},
                "last_token_usage":{"input_tokens":last,"cached_input_tokens":last//2,"output_tokens":last//10}}}}
        first=c._record(snapshot(10100,100),context,'codex')
        self.assertEqual(first['input_tokens'],100)
        self.assertEqual(first['output_tokens'],10)
        self.assertIsNone(c._record(snapshot(10100,100),context,'codex'))
        later=c._record(snapshot(10600,500),context,'codex')
        self.assertEqual(later['input_tokens'],500)

    def test_first_snapshot_without_increment_is_baseline_only(self):
        c=Collector(self.root/"agent.sqlite3",{})
        self.addCleanup(c.close)
        context={"session":"unknown-baseline"}
        row={"timestamp":utcnow(),"type":"event_msg","payload":{"type":"token_count","info":{
            "total_token_usage":{"input_tokens":10000,"cached_input_tokens":5000,"output_tokens":1000}}}}
        self.assertIsNone(c._record(row,context,'codex'))

    def test_invalid_codex_snapshot_does_not_change_accounting_baseline(self):
        c=Collector(self.root/"agent.sqlite3",{})
        self.addCleanup(c.close)
        def snapshot(n, **overrides):
            counts={"input_tokens":n,"cached_input_tokens":n//2,"output_tokens":n//10,**overrides}
            return {"timestamp":utcnow(),"type":"event_msg","payload":{"type":"token_count","info":{
                "total_token_usage":counts,
                "last_token_usage":{"input_tokens":100,"cached_input_tokens":50,"output_tokens":10}}}}
        for invalid in (-1,True,100.5,"100"):
            with self.subTest(invalid=invalid):
                context={"session":"bad-count"}
                c._record(snapshot(100),context,'codex')
                with self.assertRaises(ValueError):
                    c._record(snapshot(200,input_tokens=invalid),context,'codex')
                event=c._record(snapshot(200),context,'codex')
                self.assertEqual(event['input_tokens'],100)
                self.assertEqual(event['output_tokens'],10)
        context={"session":"bad-time"}
        c._record(snapshot(100),context,'codex')
        bad=snapshot(200);bad['timestamp']='invalid'
        with self.assertRaises(ValueError):c._record(bad,context,'codex')
        event=c._record(snapshot(200),context,'codex')
        self.assertIsNotNone(event)
        self.assertEqual(event['input_tokens'],100)

    def test_compaction_usage_is_counted_once_without_changing_normal_counters(self):
        logs=self.root/'sessions';logs.mkdir()
        usage={"input_tokens":40,"cached_input_tokens":30,"output_tokens":4}
        compacted={"timestamp":utcnow(),"type":"compacted","payload":{
            "compaction_response_id":"response-compaction",
            "latest_token_usage_record":{"thread_id":"session-one","response_id":"response-compaction","usage":usage}}}
        def count(n):
            return {"timestamp":utcnow(),"type":"event_msg","payload":{"type":"token_count","info":{
                "total_token_usage":{"input_tokens":n,"cached_input_tokens":n//2,"output_tokens":n//10},
                "last_token_usage":{"input_tokens":100,"cached_input_tokens":50,"output_tokens":10}}}}
        rows=[{"type":"session_meta","payload":{"id":"session-one"}},count(100),
            {"type":"token_usage_record","payload":compacted['payload']['latest_token_usage_record']},
            compacted,compacted,count(100),count(200)]
        (logs/'rollout.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        c=Collector(self.root/'agent.sqlite3',{'codex_dir':str(logs)});self.addCleanup(c.close)
        self.assertEqual(c.scan()['errors'],0)
        events=c.batch()
        self.assertEqual(sum(e['input_tokens']+e['output_tokens'] for e in events),264)
        self.assertEqual(len(events),3)
        # An inherited compacted snapshot belongs to its original thread.
        self.assertIsNone(c._record(compacted,{'session':'fork-child'},'codex'))
        compacted['payload']['compaction_response_id']='different-response'
        self.assertIsNone(c._record(compacted,{'session':'session-one'},'codex'))

    def test_partial_line_malformed_record_and_replay(self):
        f=self.root/"events.jsonl"
        event=json.dumps(self.event())
        f.write_text('bad-json\n'+event[:15])
        c=Collector(self.root/"agent.sqlite3",{"jsonl_paths":[str(f)]})
        self.addCleanup(c.close)
        self.assertEqual(c.scan()["errors"],1)
        self.assertEqual(c.batch(),[])
        with f.open("a") as stream:
            stream.write(event[15:]+"\n")
        self.assertEqual(c.scan()["pending"],1)
        c.acknowledge(c.batch())
        f.write_text(event+"\n")
        c.scan()
        self.assertEqual(c.batch(),[])

    def test_http_auth_and_device_identity(self):
        url=self.start_server()
        payload={"device":self.device,"events":[self.event()],"status":{}}
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.post(url,payload,token="wrong")
        self.assertEqual(exc.exception.code,401)
        with self.post(url,payload) as response:
            self.assertEqual(json.load(response)["inserted"],1)
        with self.post(url,payload) as response:
            self.assertEqual(json.load(response)["inserted"],0)
        payload["device"]=self.local
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.post(url,payload)
        self.assertEqual(exc.exception.code,403)

    def test_excessively_nested_json_is_rejected_without_partial_write(self):
        base=self.start_server()
        request=urllib.request.Request(base+'/api/ingest',b'['*1500+b']'*1500,
            headers={'Authorization':'Bearer pair-secret','Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request,timeout=3)
        self.assertEqual(exc.exception.code,400)
        self.assertEqual(self.store.summary()['totals']['records'],0)
        with urllib.request.urlopen(base+'/health') as response:
            self.assertEqual(response.status,200)

    def test_browser_request_boundary_and_frame_protection(self):
        url=self.start_server()
        for headers in ({'Host':'attacker.example'}, {'Sec-Fetch-Site':'cross-site'},
                        {'Host':'localhost:invalid'}, {'Host':'user@localhost'}):
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(urllib.request.Request(url+'/api/summary',headers=headers))
            self.assertEqual(exc.exception.code,403)
        with urllib.request.urlopen(url+'/kindle') as response:
            self.assertEqual(response.headers['X-Frame-Options'],'DENY')
            self.assertEqual(response.headers['Content-Security-Policy'],"frame-ancestors 'none'")

    def test_malformed_status_does_not_partially_write(self):
        url=self.start_server()
        payload={'device':self.device,'events':[self.event()], 'status':{'files':float('inf')}}
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.post(url,payload)
        self.assertEqual(exc.exception.code,400)
        self.assertEqual(self.store.summary()['totals']['records'],0)

    def test_disconnect_queue_recovery_over_http(self):
        f=self.root/"events.jsonl"
        f.write_text(json.dumps(self.event())+"\n")
        config={**self.device,"jsonl_paths":[str(f)],"server_url":"http://127.0.0.1:1"}
        agent_db=self.root/"agent.sqlite3"
        self.assertFalse(run_agent(config,agent_db,once=True))
        config["server_url"]=self.start_server()
        self.assertTrue(run_agent(config,agent_db,once=True))
        self.assertTrue(run_agent(config,agent_db,once=True))
        self.assertEqual(self.store.summary()["totals"]["records"],1)
        self.assertEqual(self.store.summary()["devices"][0]["status"]["pending"],0)

    def test_lost_acknowledgment_is_safe_to_retry(self):
        f=self.root/"events.jsonl"
        f.write_text(json.dumps(self.event())+"\n")
        config={**self.device,"jsonl_paths":[str(f)]}
        path=self.root/"agent.sqlite3"
        def lose_ack(device,events,status):
            self.store.ingest(device,events,status)
            raise OSError("response lost")
        self.assertFalse(run_agent(config,path,sink=lose_ack,once=True))
        self.assertTrue(run_agent(config,path,sink=self.store.ingest,once=True))
        self.assertEqual(self.store.summary()["totals"]["records"],1)

    def test_pages_pngs_and_validation(self):
        url=self.start_server()
        for path in ("/","/kindle","/kindle?view=devices","/api/summary","/health"):
            with urllib.request.urlopen(url+path) as response:
                self.assertEqual(response.status,200)
                self.assertNotIn(b"pair-secret",response.read())
        with urllib.request.urlopen(url+"/dashboard.png") as response:
            image=Image.open(BytesIO(response.read()))
            self.assertEqual(image.size,(1072,1448))
            self.assertEqual(image.mode,"L")
            self.assertLessEqual(len(image.getcolors(maxcolors=256)),16)
        with urllib.request.urlopen(url+"/dashboard.png?width=800&height=600") as response:
            image=Image.open(BytesIO(response.read()))
            self.assertEqual(image.size,(800,600))
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url+"/dashboard.png?width=999999")
        self.assertEqual(exc.exception.code,400)

    def test_render_long_names_all_resolutions(self):
        self.store.ingest(self.device,[self.event(model="a-very-long-model-name-"*4)],{})
        for width,height in ((600,800),(800,600),(758,1024),(1072,1448)):
            for view in ("usage","devices"):
                image=render_dashboard(width,height,self.store.summary(),view)
                self.assertEqual(image.size,(width,height))


if __name__=="__main__":
    unittest.main()
