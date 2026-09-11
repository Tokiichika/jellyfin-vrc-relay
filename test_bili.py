import io
import json
from email.message import Message
from pathlib import Path
import struct
import tempfile
import time
import threading
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app import Cache, Server
from bili import Bili, BiliError, public_connection
from mp4 import inspect, MP4Error
from system_stats import SystemStats


def box(kind, body):
    return struct.pack('>I4s', len(body) + 8, kind) + body


def movie(video=b'avc1', audio=True):
    # Minimal metadata tree with real MPEG-4 descriptor nesting and mdat at the front.
    stsd = box(b'stsd', bytes(4) + struct.pack('>I', 1) + box(video, bytes(78)))
    track = box(b'trak', box(b'mdia', box(b'minf', box(b'stbl', stsd))))
    if audio:
        esds = box(b'esds', bytes(4) + bytes([3, 6, 0, 1, 0, 4, 1, 0x40]))
        stsd = box(b'stsd', bytes(4) + struct.pack('>I', 1) + box(b'mp4a', bytes(28) + esds))
        track += box(b'trak', box(b'mdia', box(b'minf', box(b'stbl', stsd))))
    return box(b'ftyp', b'isom' + bytes(12)) + box(b'mdat', bytes(200000)) + box(b'moov', track)


class Response(io.BytesIO):
    def __init__(self, body, headers=None, status=200):
        super().__init__(body)
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = str(value)
        self.status = status


class BiliTests(unittest.TestCase):
    def test_share_text_extracts_video_and_preserves_page(self):
        text = '【示例视频标题】 https://www.bilibili.com/video/BV1TEST00001/?share_source=copy_web&vd_source=abc'
        self.assertEqual(Bili.video(text), ('BV1TEST00001', 1))
        self.assertEqual(Bili.video('【标题】\nhttps://www.bilibili.com/video/BV1TEST00001/?p=2。'), ('BV1TEST00001', 2))
        key = self.cache.bili.create(text, '')
        self.assertEqual(self.cache.items[key]['bvid'], 'BV1TEST00001')
        with self.assertRaises(BiliError):
            Bili.video(text + '\nhttps://www.bilibili.com/video/BV1TEST00001/')
        with self.assertRaises(BiliError):
            Bili.video('【标题】https://www.bilibili.com.evil.test/video/BV1TEST00001/')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Cache(self.temp.name, 1000000, 65536, ['jellyfin.example.com'])
        self.data = movie()
        self.etag = '"stable-media-version"'
        self.calls = []
        self.multi = False
        self.fail = False
        self.cache.bili.request = self.request

    def tearDown(self):
        self.temp.cleanup()

    def request(self, url, headers=None, api=False):
        self.calls.append((urlsplit(url).path, headers))
        if api:
            if urlsplit(url).path.endswith('pagelist'):
                data = [{'cid': 123, 'part': 'test', 'duration': 60}]
            else:
                data = {'quality': 64, 'durl': [{'url': 'https://test.bilivideo.com/file.mp4?deadline=9999999999'}] * (2 if self.multi else 1)}
            return Response(json.dumps({'code': 0, 'data': data}).encode())
        if self.fail:
            raise HTTPError(url, 403, 'denied', {}, None)
        a, b = map(int, headers['Range'][6:].split('-'))
        return Response(self.data[a:b + 1], {'Content-Range': f'bytes {a}-{b}/{len(self.data)}',
                        'Content-Length': b - a + 1, 'Content-Type': 'video/mp4', 'ETag': self.etag}, 206)

    def test_bv_and_direct_share_existing_chunk_pipeline(self):
        key = self.cache.bili.create('https://www.bilibili.com/video/BV1TEST00001/?share_source=test', '')
        self.assertEqual(self.cache.bili.create('BV1TEST00001', ''), key)
        self.assertEqual(self.cache.get_chunk(key, 0), self.data[:65536])
        count = len(self.calls)
        self.assertEqual(self.cache.get_chunk(key, 0), self.data[:65536])
        self.assertEqual(len(self.calls), count)
        item = self.cache.status('https://example.com')['items'][0]
        self.assertEqual((item['source_video'], item['source_audio']), ('h264', 'aac'))
        self.assertNotIn('deadline=', json.dumps(item))
        direct = self.cache.bili.create('https://test.bilivideo.com/file.mp4?secret=test', 'direct')
        self.assertNotIn('bvid', self.cache.items[direct])

    def test_expiry_refresh_preserves_verified_cache(self):
        key = self.cache.bili.create('BV1TEST00001', '')
        self.cache.get_chunk(key, 0)
        self.cache.items[key]['deadline'] = 1
        self.assertEqual(self.cache.get_chunk(key, 1), self.data[65536:131072])
        self.assertGreater(self.cache.items[key]['deadline'], time.time())
        self.assertIn(f'{key}.0.bin', self.cache.entries)

    def test_changed_version_refuses_cache_mix(self):
        key = self.cache.bili.create('BV1TEST00001', '')
        self.cache.get_chunk(key, 0)
        self.cache.items[key]['deadline'] = 1
        self.etag = '"changed"'
        with self.assertRaises(BiliError):
            self.cache.get_chunk(key, 1)
        self.assertNotIn(f'{key}.1.bin', self.cache.entries)

    def test_expired_direct_cached_bytes_still_serve(self):
        key = self.cache.bili.create('https://test.bilivideo.com/file.mp4', '')
        self.cache.get_chunk(key, 0)
        self.cache.items[key]['deadline'] = 1
        self.assertEqual(self.cache.get_chunk(key, 0), self.data[:65536])
        with self.assertRaisesRegex(BiliError, '直链'):
            self.cache.get_chunk(key, 1)

    def test_unsupported_format_and_multifile_rejected(self):
        self.multi = True
        with self.assertRaises(BiliError):
            self.cache.bili.create('BV1TEST00001', '')
        self.multi = False
        self.data = movie(video=b'hvc1')
        with self.assertRaisesRegex(BiliError, '格式'):
            self.cache.bili.create('BV1TEST00001', '')
        self.assertFalse(self.cache.items)

    def test_strict_host_and_public_address_validation(self):
        for value in ['https://bilivideo.com.evil.com/a.mp4', 'https://127.0.0.1/a.mp4',
                      'http://test.bilivideo.com/a.mp4', 'https://test.bilivideo.com:8080/a.mp4',
                      'https://test.bilivideo.com/a.m4s', 'https://x@test.bilivideo.com/a.mp4']:
            with self.assertRaises(BiliError):
                Bili.cdn(value)
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(BiliError):
                public_connection(('test.bilivideo.com', 443))
        self.assertEqual(Bili.video('https://www.bilibili.com/video/BV1TEST00001?p=2'), ('BV1TEST00001', 2))

    def test_redirect_cannot_escape_cdn_and_http_error_is_sanitized(self):
        adapter = Bili(self.cache)
        headers = Message()
        headers['Location'] = 'https://evil.example/file.mp4?secret=private'
        adapter.opener = Mock()
        adapter.opener.open.side_effect = HTTPError('https://test.bilivideo.com/file.mp4', 302, 'redirect', headers, None)
        with self.assertRaises(BiliError):
            adapter.request('https://test.bilivideo.com/file.mp4')
        self.assertEqual(adapter.opener.open.call_count, 1)
        self.fail = True
        with self.assertRaises(BiliError) as caught:
            self.cache.bili.create('https://test.bilivideo.com/file.mp4?secret=private', '')
        self.assertIn('403', str(caught.exception))
        self.assertNotIn('private', str(caught.exception))

    def test_management_creation_and_http_range_playback(self):
        server = Server(('127.0.0.1', 0), self.cache, 'x' * 32, 'https://example.com')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(Request(base + '/api/items', data=json.dumps({'mode': 'bilibili',
                         'url': 'BV1TEST00001', 'title': 'test'}).encode(),
                         headers={'Authorization': 'Bearer ' + 'x' * 32})) as response:
                key = json.load(response)['id']
            with urlopen(Request(base + '/v/' + key + '/video.mp4', headers={'Range': 'bytes=65530-65550'})) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.headers['Content-Type'], 'video/mp4')
                self.assertEqual(response.read(), self.data[65530:65551])
        finally:
            server.shutdown()
            server.server_close()


class SystemTests(unittest.TestCase):
    def test_cpu_delta_and_mem_available_are_host_totals(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / 'stat').write_text('cpu 100 0 100 800 0 0 0 0 50 0\n')
            (root / 'meminfo').write_text('MemTotal: 2048000 kB\nMemAvailable: 1024000 kB\n')
            stats = SystemStats(root)
            self.assertIsNone(stats.snapshot()['cpu_percent'])
            (root / 'stat').write_text('cpu 130 0 120 850 0 0 0 0 60 0\n')
            stats.updated = 0
            data = stats.snapshot()
            self.assertEqual(data['cpu_percent'], 50)
            self.assertEqual(data['memory_percent'], 50)
            self.assertEqual(data['memory_total'], 2048000 * 1024)

    def test_missing_proc_is_unavailable_not_zero(self):
        with tempfile.TemporaryDirectory() as root:
            data = SystemStats(root).snapshot()
            self.assertFalse(data['available'])
            self.assertIsNone(data['cpu_percent'])
