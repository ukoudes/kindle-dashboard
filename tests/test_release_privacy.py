import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.privacy_audit import audit
from scripts.prepare_release import archive_files


class ReleasePrivacyTests(unittest.TestCase):
    def test_nested_private_content_fails_without_printing_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            payload='SENSITIVE_FIXTURE_VALUE'
            inner=root/'inner.zip'
            with zipfile.ZipFile(inner,'w') as archive:archive.writestr('data/private.txt',payload)
            outer=root/'outer.zip'
            with zipfile.ZipFile(outer,'w') as archive:archive.writestr('inner.zip',inner.read_bytes())
            result=audit([outer],[payload])
            self.assertFalse(result['passed'])
            self.assertNotIn(payload,json.dumps(result))
            self.assertTrue(any(f['reason']=='known private value' for f in result['findings']))

    def test_archive_has_normalized_times_permissions_and_no_extra_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            item=root/'example.txt';item.write_text('public example')
            item.chmod(0o600)
            archive_files(root/'release.zip',[(item,'project/example.txt')])
            with zipfile.ZipFile(root/'release.zip') as archive:
                info=archive.infolist()[0]
                self.assertEqual(info.date_time,(2020,1,1,0,0,0))
                self.assertEqual(info.extra,b'')
                self.assertEqual(info.comment,b'')
                self.assertEqual((info.external_attr>>16)&0o777,0o644)
            self.assertTrue(audit([root/'release.zip'])['passed'])

    def test_embedded_home_paths_and_network_literals_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'leak.txt'
            path.write_bytes(b'/'+b'Users'+b'/example-person/private')
            self.assertFalse(audit([path])['passed'])
            path.write_text('http://'+'10.'+'20.30.40')
            self.assertFalse(audit([path])['passed'])


if __name__=='__main__':unittest.main()
