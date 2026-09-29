from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from upgrade import apply, FILES, deployment_version


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'update'
        self.target = Path(self.tmp.name) / 'existing'
        self.source.mkdir()
        self.target.mkdir()
        for name in FILES:
            (self.source / name).write_text('new', encoding='utf-8')
        (self.target / 'app.py').write_text("version='2.6.0'", encoding='utf-8')
        for name in ('settings.py', 'hls.py', 'nas_config.py', 'index.html', 'ui.js', 'settings-app.js'):
            (self.target / name).write_text('old', encoding='utf-8')
        (self.target / 'compose.yaml').write_text('custom compose', encoding='utf-8')
        (self.target / 'config').mkdir()
        (self.target / 'config/.env').write_text('private config', encoding='utf-8')
        (self.target / 'data').mkdir()
        (self.target / 'data/items.json').write_text('existing videos', encoding='utf-8')

    def test_preserves_config_cache_compose_and_backs_up_program(self):
        backup = apply(self.source, self.target)
        self.assertEqual((backup / 'app.py').read_text(), "version='2.6.0'")
        self.assertEqual((self.target / 'app.py').read_text(), 'new')
        self.assertEqual((self.target / 'compose.yaml').read_text(), 'custom compose')
        self.assertEqual((self.target / 'config/.env').read_text(), 'private config')
        self.assertEqual((self.target / 'data/items.json').read_text(), 'existing videos')

    def test_incomplete_update_rejected_before_any_change(self):
        (self.source / FILES[-1]).unlink()
        with self.assertRaises(ValueError):
            apply(self.source, self.target)
        self.assertEqual((self.target / 'app.py').read_text(), "version='2.6.0'")
        self.assertFalse((self.target / '.upgrade-backups').exists())

    def test_wrong_and_same_directory_rejected(self):
        with self.assertRaises(ValueError):
            apply(self.source, self.source)
        (self.target / 'config/.env').unlink()
        with self.assertRaises(ValueError):
            apply(self.source, self.target)

    def test_backup_failure_leaves_application_intact(self):
        with patch('upgrade.shutil.copy2', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                apply(self.source, self.target)
        self.assertEqual((self.target / 'app.py').read_text(), "version='2.6.0'")

    def test_version_detection_supports_quotes_and_private_release(self):
        for code, expected in [('data={"version": "2.5.1"}', '2.5.1'),
                               ('VERSION = "2.6.0"', '2.6.0'),
                               ("emit(version='2.7.0')", '2.7.0')]:
            with self.subTest(code=code):
                (self.target / 'app.py').write_text(code, encoding='utf-8')
                self.assertEqual(deployment_version(self.target), expected)

    def test_private_release_can_upgrade(self):
        (self.target / 'app.py').write_text('data={"version": "2.5.1"}', encoding='utf-8')
        backup = apply(self.source, self.target)
        self.assertIn('2.5.1', (backup / 'app.py').read_text())

    def test_unrecognized_conflicting_and_comment_only_versions_rejected(self):
        for code in ['# version="2.6.0"', 'VERSION="2.9.0"',
                     'VERSION="2.5.0"', 'VERSION="2.6.0"\ndata={"version":"2.5.1"}']:
            with self.subTest(code=code):
                (self.target / 'app.py').write_text(code, encoding='utf-8')
                with self.assertRaises(ValueError):
                    apply(self.source, self.target)
                self.assertFalse((self.target / '.upgrade-backups').exists())

    def test_incomplete_deployment_rejected(self):
        (self.target / 'settings.py').unlink()
        with self.assertRaisesRegex(ValueError, 'settings.py'):
            apply(self.source, self.target)
        self.assertFalse((self.target / '.upgrade-backups').exists())


if __name__ == '__main__':
    unittest.main()
