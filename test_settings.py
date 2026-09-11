import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from app import Cache
from settings import Settings, SettingsError, SCHEMA, encoded, read_env
from nas_config import FIELDS
import test_hls


class ConfigTests(unittest.TestCase):
    def test_local_assets_and_private_settings_auth(self):
        fixture = test_hls.HLSTests()
        fixture.setUp()
        try:
            for name, content_type in [('ui.css', 'text/css'), ('ui.js', 'text/javascript'), ('settings-app.js', 'text/javascript')]:
                with urlopen(fixture.base + '/' + name) as response:
                    self.assertEqual(response.headers.get_content_type(), content_type)
                    self.assertGreater(len(response.read()), 100)
            for path in ('/', '/settings'):
                with urlopen(fixture.base + path) as response:
                    page = response.read().decode()
                self.assertEqual(page.count('id="token"'), 1)
                self.assertEqual(page.count('id="connect"'), 1)
                self.assertNotIn('<iframe', page)
                self.assertIn('id="settings-view" hidden', page)
            for path in ('/settings.py', '/config/.env', '/api/settings'):
                with self.assertRaises(HTTPError) as error:
                    urlopen(fixture.base + path)
                self.assertIn(error.exception.code, (401, 404))
        finally:
            fixture.tearDown()

    def test_cache_limit_persist_shrink_and_protected_entries(self):
        cache = self.cache
        for name in ('old.bin', 'new.bin'):
            (cache.root / name).write_bytes(b'x' * 100000)
            cache.entries[name] = 100000
            cache.used += 100000
        cache.pins['new.bin'] = 1
        before = cache.settings.path.read_bytes()
        with self.assertRaises(SettingsError):
            cache.update_settings({'CACHE_MAX_BYTES': 80000})
        self.assertEqual(before, cache.settings.path.read_bytes())
        self.assertEqual(cache.used, 200000)
        cache.update_settings({'CACHE_MAX_BYTES': 150000})
        self.assertFalse((cache.root / 'old.bin').exists())
        self.assertTrue((cache.root / 'new.bin').exists())
        self.assertEqual(cache.budget, 150000)
        restarted = Cache(cache.root, 1000000, 65536, ['jellyfin.example.com'])
        self.assertEqual(restarted.budget, 150000)
        self.assertEqual(restarted.used, 100000)
        with self.assertRaises(SettingsError):
            cache.update_settings({'CACHE_MAX_BYTES': 64000})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(self.tmp.name, 1000000, 65536, ['jellyfin.example.com'])

    def tearDown(self):
        self.tmp.cleanup()

    def test_env_roundtrip_preserves_secret_and_unmanaged_values(self):
        path = Path(self.tmp.name) / 'separate.env'
        path.write_text('ADMIN_TOKEN=secret-original\n# user comment\nCHUNK_BYTES=65536\n', encoding='utf-8')
        store = Settings(path)
        notice = "个人's 页面 $HOME ${TOKEN} \\n literal\n第二行 <script>"
        values = store.prepare({'FOOTER_NOTICE': notice, 'NAS_API_KEY': 'private-nas-secret'})
        store.save(values)
        self.assertEqual(Settings(path).values['FOOTER_NOTICE'], notice)
        self.assertIn('ADMIN_TOKEN=secret-original\n# user comment\nCHUNK_BYTES=65536', path.read_text(encoding='utf-8'))
        self.assertNotIn('private-nas-secret', json.dumps(store.snapshot()))
        self.assertTrue(store.snapshot()['nas_key_configured'])
        self.assertEqual(store.prepare({'NAS_API_KEY': ''})['NAS_API_KEY'], 'private-nas-secret')

    def test_settings_restart_and_legacy_bandwidth_migration(self):
        root = Path(self.tmp.name) / 'old'
        root.mkdir()
        (root / 'bandwidth.json').write_text(json.dumps(dict(total_mbps=100, utilization=80, client_mbps=20)))
        cache = Cache(root, 1000000, 65536, ['jellyfin.example.com'])
        self.assertEqual(cache.bandwidth.snapshot()['effective_total_mbps'], 80)
        cache.update_settings({'REFRESH_SECONDS': 7, 'DEFAULT_HEIGHT': 720, 'HLS_IDLE_SECONDS': 180,
                               'DEBUG_ENABLED': True, 'BANDWIDTH_TOTAL_MBPS': 150})
        restarted = Cache(root, 1000000, 65536, ['jellyfin.example.com'])
        self.assertEqual(restarted.settings.values['REFRESH_SECONDS'], 7)
        self.assertEqual(restarted.hls.idle, 180)
        self.assertTrue(restarted.debug.enabled)
        self.assertEqual(restarted.bandwidth.snapshot()['effective_total_mbps'], 120)

    def test_atomic_write_failure_leaves_runtime_and_disk_unchanged(self):
        before = self.cache.settings.path.read_bytes()
        with patch('settings.os.replace', side_effect=OSError('disk failed')):
            with self.assertRaises(OSError):
                self.cache.update_settings({'BANDWIDTH_TOTAL_MBPS': 300})
        self.assertEqual(self.cache.settings.path.read_bytes(), before)
        self.assertEqual(self.cache.bandwidth.snapshot()['effective_total_mbps'], 160)
        self.assertEqual(self.cache.settings.values['BANDWIDTH_TOTAL_MBPS'], 200)

    def test_protected_keys_stale_revision_and_invalid_origin(self):
        for values in ({'ADMIN_TOKEN': 'new'}, {'NAS_ORIGIN': 'https://evil.example'},
                       {'BANDWIDTH_CLIENT_MBPS': 9}, {'REFRESH_SECONDS': 0}):
            with self.assertRaises(SettingsError):
                self.cache.update_settings(values)
        old = self.cache.settings.revision()
        self.cache.update_settings({'REFRESH_SECONDS': 8})
        with self.assertRaises(SettingsError):
            self.cache.update_settings({'REFRESH_SECONDS': 9}, old)

    def test_nas_apply_preserves_unmanaged_fields_and_verifies_readback(self):
        self.cache.update_settings({'NAS_ORIGIN': 'https://jellyfin.example.com', 'NAS_API_KEY': 'private',
                                    'NAS_ACCELERATION': 'qsv'})
        current = {field: self.cache.settings.values[key] for key, field in FIELDS.items()}
        current['HardwareDecodingCodecs'] = ['h264']
        current['UnrelatedSetting'] = 'preserved'
        current['HardwareAccelerationType'] = 'nvenc'
        calls = []
        def request(values, body=None):
            calls.append(body)
            if body is not None:
                current.update(body)
            return dict(current)
        self.cache.nas_config.request = request
        result = self.cache.nas_config.apply(self.cache.settings.values)
        self.assertEqual(result['NAS_ACCELERATION'], 'qsv')
        self.assertEqual(calls[1]['UnrelatedSetting'], 'preserved')
        self.assertEqual(len(calls), 3)
        current.pop('HardwareAccelerationType')
        with self.assertRaises(SettingsError):
            self.cache.nas_config.apply(self.cache.settings.values)

    def test_existing_admissions_block_unsafe_bandwidth_save(self):
        for i in range(10):
            self.cache.bandwidth.admit(str(i))
        before = self.cache.settings.path.read_bytes()
        with self.assertRaises(ValueError):
            self.cache.update_settings({'BANDWIDTH_TOTAL_MBPS': 50})
        self.assertEqual(self.cache.settings.path.read_bytes(), before)


class ConfigHTTPTests(unittest.TestCase):
    def test_auth_escaped_footer_and_settings_api(self):
        t = test_hls.HLSTests()
        t.setUp()
        try:
            for path in ('/api/settings', '/api/nas-config'):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(t.base + path)
                self.assertEqual(caught.exception.code, 401)
                caught.exception.close()
            headers = {'Authorization': 'Bearer ' + 'x' * 32}
            with urlopen(Request(t.base + '/api/settings', headers=headers)) as response:
                result = json.load(response)
            body = {'revision': result['revision'], 'values': {'FOOTER_NOTICE': '<script>alert(1)</script>',
                       'FOOTER_ICP': '测试备案号', 'DEFAULT_VIDEO_BITRATE': 6000000, 'NAS_API_KEY': 'secret-nas'}}
            with urlopen(Request(t.base + '/api/settings', data=json.dumps(body).encode(), headers=headers)) as response:
                self.assertNotIn('secret-nas', response.read().decode())
            for path in ('/', '/settings'):
                with urlopen(t.base + path) as response:
                    page = response.read().decode()
                self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', page)
                self.assertNotIn('secret-nas', page)
                self.assertIn('测试备案号', page)
        finally:
            t.tearDown()
