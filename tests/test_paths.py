import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dashboard.paths import default_data_dir
from dashboard.control import Controller


class RuntimePathsTests(unittest.TestCase):
    def test_checkout_keeps_existing_data_default(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(default_data_dir(Path('/example/project')),Path('/example/project/data'))
            self.assertEqual(Controller(root='/example/project').data,Path('/example/project/data'))

    def test_installed_data_location_is_explicit_and_overridable(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ,{'KINDLE_DASHBOARD_DATA_DIR':directory}):
                self.assertEqual(default_data_dir(),Path(directory).resolve())
                self.assertEqual(Controller(root='/example/backend').data,Path(directory).resolve())
                self.assertEqual(Controller(root='/example/backend',data_dir=Path(directory)/'explicit').data,
                                 Path(directory,'explicit').resolve())


if __name__=='__main__':unittest.main()
