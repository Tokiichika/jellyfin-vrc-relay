from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

from deploy import prepare, public_origin, upstream_hosts, DeployError, command, wait_healthy
from settings import read_env


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / '.env.example').write_bytes(Path(__file__).with_name('.env.example').read_bytes())

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_install_random_secret_and_normalized_inputs(self):
        path, values, created = prepare(self.root, 'https://relay.example.com/', 'JELLYFIN.example.com', False)
        self.assertTrue(created)
        self.assertGreaterEqual(len(values['ADMIN_TOKEN']), 48)
        self.assertEqual(values['PUBLIC_BASE_URL'], 'https://relay.example.com')
        self.assertEqual(values['UPSTREAM_HOSTS'], 'jellyfin.example.com')
        self.assertEqual(values['FOOTER_ICP'], '')
        self.assertEqual(read_env(path.read_text(encoding='utf-8')), values)
        self.assertFalse((self.root / '.env').exists())

    def test_repeat_preserves_config_key_and_ignores_new_initial_defaults(self):
        path, values, _ = prepare(self.root, 'https://relay.example.com', '', False)
        original = path.read_bytes()
        _, reused, created = prepare(self.root, 'https://other.example.com', 'other.example.com', False)
        self.assertFalse(created)
        self.assertEqual(reused, values)
        self.assertEqual(path.read_bytes(), original)

    def test_legacy_config_is_migrated_verbatim(self):
        legacy = 'ADMIN_TOKEN=' + 'x' * 40 + '\nPUBLIC_BASE_URL=https://old.example.com:8443\nUPSTREAM_HOSTS=nas.example.com\n# custom\nCACHE_MAX_BYTES=3000000000\n'
        (self.root / '.env').write_text(legacy, encoding='utf-8', newline='\n')
        path, values, created = prepare(self.root, interactive=False)
        self.assertEqual(path.read_text(encoding='utf-8'), legacy)
        self.assertFalse(created)
        self.assertEqual(values['ADMIN_TOKEN'], 'x' * 40)

    def test_existing_config_has_priority_over_legacy(self):
        path, values, _ = prepare(self.root, 'https://relay.example.com', '', False)
        (self.root / '.env').write_text('ADMIN_TOKEN=invalid', encoding='utf-8')
        self.assertEqual(prepare(self.root, interactive=False)[1], values)

    def test_invalid_legacy_is_not_silently_rekeyed(self):
        (self.root / '.env').write_text('ADMIN_TOKEN=replace-with-secret', encoding='utf-8')
        with self.assertRaises(DeployError):
            prepare(self.root, interactive=False)
        self.assertFalse((self.root / 'config').exists())

    def test_missing_input_and_invalid_url_create_no_config(self):
        for url in (None, 'http://relay.example.com', 'https://user:secret@example.com', 'https://relay.example.com/path', 'https://relay.example.com:99999'):
            with self.assertRaises(DeployError):
                prepare(self.root, url, '', False)
        self.assertFalse((self.root / 'config').exists())

    def test_interactive_only_bilibili(self):
        answers = iter(['https://relay.example.com', ''])
        _, values, _ = prepare(self.root, ask=lambda _: next(answers))
        self.assertEqual(values['UPSTREAM_HOSTS'], '')

    def test_host_validation_dedup_and_no_shell_or_url_input(self):
        self.assertEqual(upstream_hosts('NAS.example.com, nas.example.com,127.0.0.1'), 'nas.example.com,127.0.0.1')
        for value in ('https://nas.example.com', 'nas.example.com:443', 'nas.example.com\nEVIL=value', '$(id)', '../etc'):
            with self.assertRaises(DeployError):
                upstream_hosts(value)

    def test_write_failure_leaves_no_partial_configuration(self):
        with patch('deploy.os.link', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                prepare(self.root, 'https://relay.example.com', '', False)
        self.assertFalse((self.root / 'config/.env').exists())
        self.assertEqual(list((self.root / 'config').iterdir()), [])

    def test_missing_docker_fails_with_actionable_message(self):
        with patch('deploy.subprocess.run', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(DeployError, 'Docker'):
                command(['docker', 'version'], self.root)

    def test_health_check_waits_for_actual_container(self):
        values = [Mock(stdout='container-id'), Mock(stdout='starting'), Mock(stdout='healthy')]
        with patch('deploy.command', side_effect=values) as run, patch('deploy.time.sleep'):
            wait_healthy(self.root)
        self.assertEqual(run.call_args.args[0][-1], 'container-id')

    def test_unhealthy_container_is_not_reported_as_success(self):
        with patch('deploy.command', side_effect=[Mock(stdout='container-id'), Mock(stdout='unhealthy')]):
            with self.assertRaisesRegex(DeployError, '健康检查'):
                wait_healthy(self.root)


if __name__ == '__main__':
    unittest.main()
