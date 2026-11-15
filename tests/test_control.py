import json
import socket
import tempfile
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
from dashboard.control import Controller


@unittest.skipUnless(sys.platform=='darwin','Mac control application')
class ControlTests(unittest.TestCase):
    def test_pid_from_other_instance_is_never_adopted_or_stopped(self):
        with tempfile.TemporaryDirectory() as temp:
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
            owner=Controller(data_dir=Path(temp)/'one',port=port)
            self.addCleanup(owner.stop)
            started=owner.start(['--no-collect','--no-codexbar'])
            other=Controller(data_dir=Path(temp)/'two',port=port)
            other.data.mkdir()
            other.pid_file.write_text(str(started['pid']))
            self.assertFalse(other.status()['running'])
            self.assertFalse(other.status()['can_stop'])
            other.stop()
            self.assertTrue(owner.status()['running'])

    def test_control_lock_has_a_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            controller=Controller(data_dir=temp)
            with patch('dashboard.control.fcntl.flock',side_effect=BlockingIOError), patch(
                    'dashboard.control.time.monotonic',side_effect=[0,6]):
                with self.assertRaisesRegex(RuntimeError,'控制操作'):
                    with controller.lock():pass

    def test_lifecycle_and_duplicate_start(self):
        with tempfile.TemporaryDirectory() as temp:
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
            controller=Controller(data_dir=Path(temp),port=port)
            self.addCleanup(controller.stop)
            self.assertEqual(controller.status()['state'],'stopped')
            started=controller.start(['--no-collect','--no-codexbar'])
            self.assertTrue(started['running'])
            self.assertTrue(started['can_stop'])
            self.assertEqual(controller.start()['pid'],started['pid'])
            self.assertEqual(controller.stop()['state'],'stopped')
            self.assertEqual(controller.stop()['state'],'stopped')
            self.assertTrue(controller.start(['--no-collect','--no-codexbar'])['running'])
            self.assertEqual(controller.stop()['state'],'stopped')

    def test_busy_port_and_stale_pid_never_stop_another_program(self):
        with tempfile.TemporaryDirectory() as temp, socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen()
            controller=Controller(data_dir=temp,port=listener.getsockname()[1])
            Path(temp,'server.pid').write_text('1\n')
            status=controller.start()
            self.assertEqual(status['state'],'conflict')
            self.assertFalse(status['can_stop'])
            self.assertEqual(controller.stop()['state'],'conflict')
            self.assertEqual(listener.getsockname()[1],controller.port)


if __name__=='__main__':unittest.main()
