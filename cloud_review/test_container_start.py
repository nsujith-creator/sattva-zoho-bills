import os
import stat
import unittest
from unittest.mock import patch, Mock, call
import container_start


class ContainerStartup(unittest.TestCase):
    def runtime(self):
        fake = Mock()
        fake.environ = {'DATA_DIR': '/data'}
        fake.O_RDONLY, fake.O_DIRECTORY, fake.O_NOFOLLOW = 0, 65536, 131072
        fake.geteuid.side_effect = [0, 65532]
        fake.getegid.return_value = 65532
        fake.open.return_value = 9
        fake.fstat.return_value.st_mode = stat.S_IFDIR | 0o755
        return fake

    def test_volume_is_private_and_no_app_can_start_before_privilege_drop(self):
        fake = self.runtime()
        with patch.object(container_start, 'os', fake), patch.object(container_start.runpy, 'run_path') as run:
            order = Mock(); order.attach_mock(fake, 'os'); order.attach_mock(run, 'app')
            container_start.main()
        fake.open.assert_called_once_with('/data', 196608)
        fake.fchown.assert_called_once_with(9, 65532, 65532)
        fake.fchmod.assert_called_once_with(9, 0o700)
        self.assertLess(order.mock_calls.index(call.os.close(9)), order.mock_calls.index(call.os.setgroups([])))
        self.assertLess(order.mock_calls.index(call.os.setgroups([])), order.mock_calls.index(call.os.setgid(65532)))
        self.assertLess(order.mock_calls.index(call.os.setgid(65532)), order.mock_calls.index(call.os.setuid(65532)))
        self.assertLess(order.mock_calls.index(call.os.setuid(65532)), order.mock_calls.index(call.app('/app/app.py', run_name='__main__')))

    def test_arbitrary_directory_refused_before_any_file_access(self):
        fake = self.runtime(); fake.environ = {'DATA_DIR': '/app'}
        with patch.object(container_start, 'os', fake), self.assertRaises(RuntimeError):
            container_start.prepare_and_drop()
        fake.open.assert_not_called()

    def test_symlink_or_mount_access_failure_prevents_web_app_start(self):
        fake = self.runtime(); fake.open.side_effect = OSError('symlink or missing mount')
        with patch.object(container_start, 'os', fake), patch.object(container_start.runpy, 'run_path') as run:
            with self.assertRaises(OSError): container_start.main()
        run.assert_not_called()

    def test_ownership_failure_closes_directory_and_never_starts_web_app(self):
        fake = self.runtime(); fake.fchown.side_effect = PermissionError('ownership denied')
        with patch.object(container_start, 'os', fake), patch.object(container_start.runpy, 'run_path') as run:
            with self.assertRaises(PermissionError): container_start.main()
        fake.close.assert_called_once_with(9); run.assert_not_called()

    def test_failed_or_ineffective_privilege_drop_prevents_web_app_start(self):
        for ineffective in (False, True):
            with self.subTest(ineffective=ineffective):
                fake = self.runtime()
                if ineffective: fake.geteuid.side_effect = [0, 0]
                else: fake.setuid.side_effect = PermissionError('drop denied')
                with patch.object(container_start, 'os', fake), patch.object(container_start.runpy, 'run_path') as run:
                    with self.assertRaises((PermissionError, RuntimeError)): container_start.main()
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
