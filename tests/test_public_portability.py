"""Public portability boundaries; no model, Docker, server, home directory or credentials."""
import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hub import paths
from scripts.check_public_repository import check


class PathBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        os.environ.pop('SMH_ASSET_ROOT', None)

    def tearDown(self):
        self.environment.stop()

    def test_project_write_does_not_depend_on_drive_or_username(self):
        target = ROOT / 'runtime' / 'synthetic-portable' / 'data.db'
        self.assertEqual(paths.checked_path(target), target)

    def test_parent_traversal_and_sibling_write_are_rejected(self):
        for target in (ROOT / '..' / 'outside.db', ROOT.parent / (ROOT.name + '-other') / 'data.db'):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'path_outside'):
                paths.checked_path(target)

    def test_external_asset_requires_explicit_root(self):
        external = ROOT.parent / 'synthetic-shared-assets'
        with self.assertRaisesRegex(ValueError, 'asset_outside'):
            paths.checked_asset(external / 'model.bin')
        os.environ['SMH_ASSET_ROOT'] = str(external)
        self.assertEqual(paths.checked_asset(external / 'model.bin'), external / 'model.bin')
        self.assertEqual(paths.checked_asset(ROOT / 'models' / 'model.bin'), ROOT / 'models' / 'model.bin')

    def test_asset_root_does_not_grant_external_writes(self):
        external = ROOT.parent / 'synthetic-shared-assets'
        os.environ['SMH_ASSET_ROOT'] = str(external)
        with self.assertRaisesRegex(ValueError, 'path_outside'):
            paths.checked_path(external / 'write.db')

    def test_relative_and_whole_filesystem_asset_roots_are_rejected(self):
        for configured in ('relative-assets', ROOT.anchor):
            with self.subTest(root=configured):
                os.environ['SMH_ASSET_ROOT'] = configured
                with self.assertRaisesRegex(ValueError, 'invalid_asset_root'):
                    paths.checked_asset(ROOT / 'models' / 'model.bin')

    def test_linked_path_is_rejected_before_use(self):
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaisesRegex(ValueError, 'reparse_point_forbidden'):
                paths.checked_path(ROOT / 'runtime' / 'linked.db')
            with self.assertRaisesRegex(ValueError, 'reparse_point_forbidden'):
                paths.checked_asset(ROOT / 'models' / 'linked.bin')

    @unittest.skipUnless(hasattr(Path, 'is_junction'), 'junction detection unavailable on this Python')
    def test_junction_is_rejected(self):
        with patch.object(Path, 'is_junction', return_value=True):
            with self.assertRaisesRegex(ValueError, 'reparse_point_forbidden'):
                paths.checked_path(ROOT / 'runtime' / 'linked.db')

    def test_public_source_and_current_document_links(self):
        report = check()
        self.assertEqual(report['findings'], [], report)


class SessionSnapshotBoundaryTests(unittest.TestCase):
    def test_explicit_project_snapshot_can_resolve_registered_workspace(self):
        from hub.db import MemoryDatabase, DEFAULT_SESSIONS_DIR, norm_path_str
        registered = norm_path_str(ROOT)
        database = SimpleNamespace(get_meta=lambda *args: {registered: 'synthetic-project'})
        snapshot = DEFAULT_SESSIONS_DIR / 'synthetic-session' / 'context.json'
        with patch.object(Path, 'is_file', return_value=True), patch.object(
            Path, 'read_text', autospec=True,
            return_value=json.dumps({'workspace_paths': [str(ROOT)]}),
        ) as read:
            result = MemoryDatabase.resolve_bound_project(database, session_id_header='synthetic-session')
        self.assertEqual(result, ('synthetic-project', registered))
        read.assert_called_once_with(snapshot, encoding='utf-8')

    def test_session_path_traversal_never_reads_a_file(self):
        from hub.db import MemoryDatabase
        database = SimpleNamespace(get_meta=lambda *args: {})
        with patch.object(Path, 'read_text') as read:
            for session in ('../outside', 'nested/session', 'nested' + chr(92) + 'session'):
                self.assertEqual(MemoryDatabase.resolve_bound_project(database, session_id_header=session), (None, None))
            read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
