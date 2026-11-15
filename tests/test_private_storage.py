import os
from pathlib import Path
import stat
import tempfile
import unittest

from dashboard.access import AccessStore
from dashboard.collector import Collector
from dashboard.storage import Store, prepare_private_database
from kindle_dashboard import write_json


@unittest.skipIf(os.name=='nt','POSIX file permissions')
class PrivateStorageTests(unittest.TestCase):
    def test_database_permissions_tighten_without_changing_existing_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            database=root/'existing.sqlite3'
            database.write_bytes(b'existing data')
            database.chmod(0o644)
            journal=root/'existing.sqlite3-journal'
            journal.write_bytes(b'private journal')
            journal.chmod(0o644)
            prepare_private_database(database)
            self.assertEqual(database.read_bytes(),b'existing data')
            self.assertEqual(journal.read_bytes(),b'private journal')
            for path in (database,journal):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)

    def test_all_storage_types_and_configuration_are_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            Store(root/'hub.sqlite3')
            AccessStore(root/'access.sqlite3')
            collector=Collector(root/'agent.sqlite3',{})
            collector.close()
            # An old temporary config must be tightened before replacement too.
            (root/'hub.json.tmp').write_text('old')
            (root/'hub.json.tmp').chmod(0o644)
            write_json(root/'hub.json',{'test':'value'})
            for name in ('hub.sqlite3','agent.sqlite3','access.sqlite3','hub.json'):
                self.assertEqual(stat.S_IMODE((root/name).stat().st_mode),0o600)


if __name__=='__main__':unittest.main()
