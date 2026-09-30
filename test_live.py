import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from app import Cache, Server
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from live_gateway import Gateway, Viewer
from live_setup import prepare_live, compose_args


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'SETTINGS_ENV_PATH': str(self.root / '.env'), 'LIVE_ENABLED': 'true', 'LIVE_INTERNAL_TOKEN': 'internal-test-key-' * 3})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cache = Cache(self.root, 9000000000, 65536, [])
        self.live = self.cache.live
        self.live.control({'operation': 'create', 'title': '<unsafe> 我的直播'})
        self.key, self.room = next(iter(self.live.data['rooms'].items()))

    def test_publish_auth_separate_from_read_and_management(self):
        request = dict(action='publish', protocol='rtmp', path='live/' + self.key, password='wrong')
        self.assertFalse(self.live.authenticate(request))
        request['password'] = self.room['publish_key']
        self.assertTrue(self.live.authenticate(request))
        self.assertFalse(self.live.authenticate({**request, 'protocol': 'rtsp'}))
        self.assertFalse(self.live.authenticate({**request, 'action': 'api'}))
        self.assertTrue(self.live.authenticate(dict(action='read', protocol='rtsp', path='live/' + self.key)))
        self.assertFalse(self.live.authenticate(dict(action='read', protocol='rtmp', path='live/' + self.key)))
        self.live.control({'operation': 'pause_all'})
        self.assertFalse(self.live.authenticate(dict(action='read', protocol='rtsp', path='live/' + self.key)))
        self.assertTrue(self.live.authenticate(request))

    def test_control_persisted_and_does_not_touch_vod(self):
        self.live.control({'operation': 'pause', 'id': self.key})
        self.assertTrue(json.loads(self.live.path.read_text('utf-8'))['rooms'][self.key]['paused'])
        self.assertFalse(self.live.allowed(self.key))
        self.assertFalse((self.root / 'playback.json').exists())
        self.live.control({'operation': 'resume', 'id': self.key})
        self.assertTrue(self.live.allowed(self.key))

    def test_live_capacity_and_runtime_config_are_separate(self):
        self.cache.update_settings({'LIVE_TOTAL_MBPS': 25, 'LIVE_UTILIZATION': 80})
        self.assertTrue(self.live.bandwidth.admit('a'))
        self.assertTrue(self.live.bandwidth.admit('b'))
        self.assertFalse(self.live.bandwidth.admit('c'))
        old = self.cache.settings.path.read_bytes()
        with self.assertRaises(ValueError):
            self.cache.update_settings({'LIVE_TOTAL_MBPS': 15})
        self.assertEqual(old, self.cache.settings.path.read_bytes())
        self.assertEqual(self.cache.bandwidth.snapshot()['total_mbps'], 200)
        self.live.bandwidth.release('a')
        self.assertTrue(self.live.bandwidth.admit('c'))

    def test_rotation_invalidates_publish_not_play_link(self):
        before = self.live.snapshot('https://relay.example.test')['rooms'][0]
        with patch.object(self.live, 'stop_publisher', return_value=''):
            self.live.control({'operation': 'rotate', 'id': self.key})
        after = self.live.snapshot('https://relay.example.test')['rooms'][0]
        self.assertEqual(before['play_url'], after['play_url'])
        self.assertNotEqual(before['obs_key'], after['obs_key'])

    def test_gateway_quota_and_pause_close_socket(self):
        engine = socket.socket()
        engine.bind(('127.0.0.1', 0))
        engine.listen()
        engine.settimeout(3)
        self.addCleanup(engine.close)
        def origin():
            try:
                conn, _ = engine.accept()
                with conn:
                    conn.recv(8192)
                    conn.sendall(b'RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n')
                    time.sleep(.2)
            except OSError:
                pass
        threading.Thread(target=origin, daemon=True).start()
        with patch.dict(os.environ, {'LIVE_MTX_RTSP_HOST': '127.0.0.1', 'LIVE_MTX_RTSP_PORT': str(engine.getsockname()[1])}):
            gateway = Gateway(('127.0.0.1', 0), self.live)
            threading.Thread(target=gateway.serve_forever, daemon=True).start()
            try:
                with socket.create_connection(gateway.server_address, timeout=3) as client:
                    client.sendall(('DESCRIBE rtsp://localhost/live/' + self.key + ' RTSP/1.0\r\nCSeq: 1\r\n\r\n').encode())
                    self.assertIn(b'200 OK', client.recv(4096))
                    self.assertEqual(self.live.bandwidth.snapshot()['admitted_ips'], 1)
                    self.live.control({'operation': 'pause_all'})
                    self.assertEqual(client.recv(4096), b'')
            finally:
                gateway.shutdown()
                gateway.server_close()

    def test_short_frames_rejected(self):
        with self.assertRaises(ConnectionError):
            Viewer.exact(io.BytesIO(b'x'), 4)

    def test_live_bucket_limits_bytes_and_kick_interrupts_wait(self):
        self.cache.update_settings({'LIVE_TOTAL_MBPS': 10, 'LIVE_UTILIZATION': 100, 'LIVE_CLIENT_MBPS': 10})
        bandwidth = self.live.bandwidth
        bandwidth.admit('viewer')
        bandwidth.begin('viewer')
        start = time.monotonic()
        for _ in range(24):
            bandwidth.acquire('viewer', 65536)
        self.assertGreater(time.monotonic() - start, 1.10)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(ConnectionAbortedError):
            bandwidth.acquire('viewer', 65536, cancelled)
        bandwidth.end('viewer')
        bandwidth.release('viewer')

    def test_failed_save_rolls_back_control_and_does_not_kick(self):
        with patch.object(self.live, 'save', side_effect=OSError('disk full')), patch.object(self.live, 'close_viewers') as kick:
            with self.assertRaises(OSError):
                self.live.control({'operation': 'pause_all'})
            self.assertFalse(self.live.data['paused'])
            kick.assert_not_called()

    def test_connection_cannot_switch_rooms_or_join_paused_room(self):
        conn = Mock(room=None)
        self.live.bind(conn, self.key)
        self.live.control({'operation': 'create', 'title': 'Another room'})
        another = next(k for k in self.live.data['rooms'] if k != self.key)
        with self.assertRaises(PermissionError):
            self.live.bind(conn, another)
        self.live.control({'operation': 'pause_all'})
        with self.assertRaises(PermissionError):
            self.live.bind(Mock(room=None), self.key)

    def test_live_api_auth_and_management_capacity(self):
        server = Server(('127.0.0.1', 0), self.cache, 'local-management-test-key', 'https://relay.example.test', clients=1)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = build_opener(ProxyHandler({}))
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with self.assertRaises(HTTPError) as denied:
                opener.open(base + '/api/live', timeout=2)
            self.assertEqual(denied.exception.code, 401)
            denied.exception.close()
            server.slots.acquire()  # One occupied VOD connection must not starve auth/API.
            try:
                req = Request(base + '/api/live', headers={'Authorization': 'Bearer local-management-test-key'})
                with opener.open(req, timeout=2) as response:
                    self.assertEqual(len(json.load(response)['rooms']), 1)
                body = json.dumps({'action': 'publish', 'protocol': 'rtmp', 'path': 'live/' + self.key, 'password': 'wrong'}).encode()
                req = Request(base + '/internal/live/' + self.live.secret, data=body)
                with self.assertRaises(HTTPError) as denied:
                    opener.open(req, timeout=2)
                self.assertEqual(denied.exception.code, 403)
                denied.exception.close()
            finally:
                server.slots.release()
        finally:
            server.shutdown()
            server.server_close()


class LiveSetupTests(unittest.TestCase):
    def test_setup_idempotent_keeps_user_settings_and_hides_api_ports(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'config').mkdir()
            env = root / 'config/.env'
            env.write_text('ADMIN_TOKEN=existing-private-test-key\nLIVE_RTMP_PORT=21935\n')
            prepare_live(root)
            first = env.read_bytes()
            prepare_live(root)
            self.assertEqual(first, env.read_bytes())
            self.assertIn(b'LIVE_RTMP_PORT=21935', first)
            cfg = (root / 'config/mediamtx.yml').read_text()
            self.assertIn('rtspTransports: [tcp]', cfg)
            self.assertIn('record: false', cfg)
            self.assertIn('authHTTPExclude: []', cfg)
            self.assertIn('compose.live.yaml', compose_args(['docker', 'compose', 'up', '-d'], root))

    def test_invalid_ports_and_custom_engine_config_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'config').mkdir()
            (root / 'config/.env').write_text('LIVE_RTMP_PORT=1935\nLIVE_RTSP_PORT=1935\n')
            with self.assertRaises(ValueError):
                prepare_live(root)
            (root / 'config/.env').write_text('ADMIN_TOKEN=test\n')
            (root / 'config/mediamtx.yml').write_text('custom')
            with self.assertRaises(ValueError):
                prepare_live(root)
            self.assertEqual((root / 'config/mediamtx.yml').read_text(), 'custom')
            self.assertEqual((root / 'config/.env').read_text(), 'ADMIN_TOKEN=test\n')


if __name__ == '__main__':
    unittest.main()
