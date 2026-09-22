import io
import tarfile
from types import SimpleNamespace
from unittest import TestCase, mock

from brt6.runtime.offline_dependencies import ensure_cached_dependencies


class OfflineDependencyTests(TestCase):
    def test_existing_dependency_is_not_reinstalled(self):
        c=mock.Mock();c.exec_run.return_value=SimpleNamespace(exit_code=0,output=b'')
        self.assertEqual(ensure_cached_dependencies(c,repo='sphinx-doc/sphinx')['status'],'ALREADY_AVAILABLE')
        c.exec_run.assert_called_once();c.put_archive.assert_not_called()

    def test_missing_dependency_is_installed_offline_and_checked(self):
        c=mock.Mock();c.exec_run.side_effect=[SimpleNamespace(exit_code=n,output=b'') for n in [1,0,0]]
        c.put_archive.return_value=True
        result=ensure_cached_dependencies(c,repo='sphinx-doc/sphinx')
        self.assertEqual(result['status'],'REPAIRED')
        args=c.exec_run.call_args_list[1].args[0]
        self.assertIn('--no-index',args);self.assertIn('--no-deps',args)
        payload=c.put_archive.call_args.args
        self.assertEqual(payload[0],'/root')
        with tarfile.open(fileobj=io.BytesIO(payload[1])) as archive:
            self.assertEqual(archive.getnames(),['roman-3.3-py2.py3-none-any.whl'])

    def test_install_failure_stops_before_candidate_execution(self):
        c=mock.Mock();c.exec_run.side_effect=[SimpleNamespace(exit_code=1,output=b'missing'),SimpleNamespace(exit_code=1,output=b'failed')]
        c.put_archive.return_value=True
        with self.assertRaisesRegex(RuntimeError,'installation failed'):
            ensure_cached_dependencies(c,repo='sphinx-doc/sphinx')
        self.assertEqual(c.exec_run.call_count,2)

    def test_unrelated_repository_is_untouched(self):
        c=mock.Mock()
        self.assertEqual(ensure_cached_dependencies(c,repo='django/django')['status'],'NOT_REQUIRED')
        c.exec_run.assert_not_called();c.put_archive.assert_not_called()
