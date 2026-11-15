import json
import tempfile
import unittest
from pathlib import Path

from dashboard.collector import Collector
from dashboard.repair import rebuild_local_usage
from dashboard.storage import Store, utcnow
from kindle_dashboard import write_json


class RepairTests(unittest.TestCase):
    def test_rebuild_corrects_only_local_codex_and_is_safe_to_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);logs=root/'logs';logs.mkdir()
            data=root/'data';data.mkdir()
            device={'id':'mac','name':'Mac','platform':'Darwin','codex_dir':str(logs)}
            write_json(data/'hub.json',{'local_device':device,'devices':[]})
            row={'timestamp':utcnow(),'type':'event_msg','payload':{'type':'token_count','info':{
                'total_token_usage':{'input_tokens':10100,'cached_input_tokens':5050,'output_tokens':1010},
                'last_token_usage':{'input_tokens':100,'cached_input_tokens':50,'output_tokens':10}}}}
            (logs/'session.jsonl').write_text(json.dumps(row)+'\n')
            collector=Collector(data/'local-agent.sqlite3',{'codex_dir':str(logs)})
            collector.scan();event=collector.batch()[0];collector.acknowledge([event]);collector.close()
            store=Store(data/'hub.sqlite3')
            wrong={**event,'input_tokens':10100,'cached_input_tokens':5050,'output_tokens':1010}
            generic={**event,'id':'generic','source':'other','input_tokens':200,'cached_input_tokens':0,'output_tokens':20}
            remote={**device,'id':'windows'}
            store.ingest(device,[wrong,generic],{})
            store.ingest(remote,[event],{})
            result=rebuild_local_usage(data)
            self.assertEqual(result['removed_inherited_tokens'],11000)
            self.assertEqual(store.summary()['totals']['tokens'],440)
            self.assertEqual(len(list((data/'backups').glob('*/hub.sqlite3'))),1)
            collector=Collector(data/'local-agent.sqlite3',{'codex_dir':str(logs)})
            self.assertEqual(collector.scan()['pending'],0)
            collector.close()
            self.assertEqual(rebuild_local_usage(data)['removed_inherited_tokens'],0)


if __name__=='__main__':unittest.main()
