import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from app import Cache, RelayError, Server, byte_range

DATA = bytes(range(256)) * 2048
CHUNK = 65536


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests += 1
        mode = self.server.mode
        if mode == 'redirect':
            self.send_response(302)
            self.send_header('Location', 'http://127.0.0.1:1/private')
            self.end_headers()
            return
        start, end, _ = byte_range(self.headers.get('Range'), len(DATA))
        if mode == 'ignore' or (self.headers.get('If-Range') and self.headers['If-Range'] != self.server.etag):
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(206)
        self.send_header('Content-Range', f'bytes {start}-{end}/{len(DATA)}')
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('ETag', self.server.etag)
        if getattr(self.server, 'disposition', ''):
            self.send_header('Content-Disposition', self.server.disposition)
        body = DATA[start:end + 1]
        if mode == 'truncate':
            body = body[:10]
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        self.origin.requests, self.origin.mode, self.origin.etag = 0, 'ok', '"v1"'
        self.thread = threading.Thread(target=self.origin.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.origin.server_port}/Items/{"a" * 32}/Download?api_key=test-secret'
        self.cache = Cache(self.tmp.name, 2 * CHUNK, CHUNK, ['127.0.0.1'], allow_http=True)
        self.key = self.cache.create(self.url, 'Test movie')
        self.relay = Server(('127.0.0.1', 0), self.cache, 'x' * 32, 'https://video.example.com')
        self.worker = threading.Thread(target=self.relay.serve_forever, daemon=True)
        self.worker.start()
        self.base = f'http://127.0.0.1:{self.relay.server_port}'
        self.play = self.base + f'/v/{self.key}/video.mp4'

    def tearDown(self):
        self.relay.shutdown()
        self.relay.server_close()
        self.origin.shutdown()
        self.origin.server_close()
        self.worker.join()
        self.thread.join()
        self.tmp.cleanup()

    def get(self, range_value, method='GET', **headers):
        if range_value:
            headers['Range'] = range_value
        return urlopen(Request(self.play, headers=headers, method=method), timeout=5)

    def admin(self, path, body=None, token='x' * 32):
        data = json.dumps(body).encode() if body is not None else None
        return urlopen(Request(self.base + path, data=data,
                              headers={'Authorization': 'Bearer ' + token,
                                       'Content-Type': 'application/json'}), timeout=5)

    def test_activity_auth_restart_and_repeated_creation(self):
        endpoint = '/api/items/' + self.key + '/copied'
        with self.assertRaises(HTTPError) as denied:
            self.admin(endpoint, {}, token='wrong')
        self.assertEqual(denied.exception.code, 401)
        denied.exception.close()
        self.assertNotIn('last_copied', self.cache.items[self.key])
        with self.admin(endpoint, {}) as response:
            copied = json.load(response)
        with self.admin('/api/items', {'url': self.url, 'title': '第三集', 'mode': 'file'}) as response:
            created = json.load(response)
        self.assertEqual(created['id'], self.key)
        self.assertEqual(self.origin.requests, 1)  # reused link, no extra origin request
        restored = Cache(self.tmp.name, 2 * CHUNK, CHUNK, ['127.0.0.1'], allow_http=True)
        item = restored.status('https://example.test')['items'][0]
        self.assertEqual(item['title'], '第三集')
        self.assertEqual(item['last_copied'], copied['last_copied'])
        self.assertEqual(item['last_created'], created['last_created'])
        self.assertNotIn('url', item)

    def test_activity_order_legacy_and_delete(self):
        second = self.cache.create(self.url.replace('a' * 32, 'b' * 32), '第四集')
        legacy = self.cache.status('https://example.test')['items']
        self.assertTrue(all(i['last_created'] == i['created'] and not i['last_copied'] for i in legacy))
        self.cache.remember_activity(second, 'created')
        self.cache.remember_activity(self.key, 'created')
        self.assertEqual(self.cache.status('https://example.test')['items'][0]['id'], self.key)
        first = self.cache.remember_activity(self.key, 'copied')
        last = self.cache.remember_activity(second, 'copied')
        self.assertGreater(last['last_copied'], first['last_copied'])
        self.cache.delete(second)
        with self.assertRaises(RelayError):
            self.cache.remember_activity(second, 'copied')

    def test_failed_activity_save_does_not_change_memory_or_disk(self):
        original = self.cache.meta.read_bytes()
        with patch.object(self.cache, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.cache.remember_activity(self.key, 'created', 'should not survive')
        self.assertEqual(self.cache.items[self.key]['title'], 'Test movie')
        self.assertNotIn('last_created', self.cache.items[self.key])
        self.assertEqual(self.cache.meta.read_bytes(), original)

    def test_source_filename_title(self):
        self.origin.disposition = "attachment; filename*=UTF-8''%E7%AC%AC%E4%B8%89%E9%9B%86.mp4"
        key = self.cache.create(self.url.replace('a' * 32, 'c' * 32), '')
        self.assertEqual(self.cache.items[key]['title'], '第三集.mp4')

    def test_concurrent_viewers_only_download_one_chunk(self):
        def viewer(_):
            with self.get('bytes=100-999') as response:
                self.assertEqual(response.status, 206)
                return response.read()
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(viewer, range(12)))
        self.assertTrue(all(data == DATA[100:1000] for data in results))
        self.assertEqual(self.origin.requests, 2)  # one probe, one shared chunk

    def test_seek_across_boundary_and_suffix(self):
        with self.get('bytes=65530-65545') as response:
            self.assertEqual(response.read(), DATA[65530:65546])
        with self.get('bytes=-31') as response:
            self.assertEqual(response.read(), DATA[-31:])
        self.assertLessEqual(self.cache.used, 2 * CHUNK)

    def test_eviction_and_revisit(self):
        for index in [0, 1, 2, 0]:
            self.assertEqual(self.cache.get_chunk(self.key, index), DATA[index * CHUNK:(index + 1) * CHUNK])
            disk = sum(p.stat().st_size for p in Path(self.tmp.name).glob('*.bin'))
            self.assertLessEqual(disk, 2 * CHUNK)
        self.assertEqual(self.cache.misses, 4)

    def test_head_and_unsatisfiable(self):
        with self.get(None, method='HEAD') as response:
            self.assertEqual(int(response.headers['Content-Length']), len(DATA))
            self.assertEqual(response.read(), b'')
        self.assertEqual(self.origin.requests, 1)
        for value in ['bytes=999999-', 'bytes=5-2', 'bytes=-0', 'bytes=0-1,4-5']:
            with self.assertRaises(HTTPError) as cm:
                self.get(value)
            self.assertEqual(cm.exception.code, 416)
            self.assertEqual(cm.exception.headers['Content-Range'], f'bytes */{len(DATA)}')
            cm.exception.close()

    def test_full_response_and_if_range(self):
        with self.get(None) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), DATA)
            tag = response.headers['ETag']
        with self.get('bytes=0-10', **{'If-Range': tag}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), DATA[:11])
        with self.get('bytes=0-10', **{'If-Range': '"old"'}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), DATA)

    def test_restart_reuses_cache(self):
        self.cache.get_chunk(self.key, 0)
        other = Cache(self.tmp.name, 2 * CHUNK, CHUNK, ['127.0.0.1'], allow_http=True)
        self.assertEqual(other.get_chunk(self.key, 0), DATA[:CHUNK])
        self.assertEqual(self.origin.requests, 2)

    def test_chunk_size_change_rejected_and_budget_shrink(self):
        self.cache.get_chunk(self.key, 0)
        self.cache.get_chunk(self.key, 1)
        with self.assertRaises(ValueError):
            Cache(self.tmp.name, 4 * CHUNK, 2 * CHUNK, ['127.0.0.1'], allow_http=True)
        # A manually edited .env is authoritative on restart, just like the settings page.
        values = self.cache.settings.prepare({'CACHE_MAX_BYTES': CHUNK})
        self.cache.settings.save(values)
        smaller = Cache(self.tmp.name, CHUNK, CHUNK, ['127.0.0.1'], allow_http=True)
        self.assertEqual(smaller.used, CHUNK)

    def test_management_create_delete_over_http(self):
        headers = {'Authorization': 'Bearer ' + 'x' * 32, 'Content-Type': 'application/json'}
        body = json.dumps({'url': self.url + '&test=second', 'title': '<test>'}).encode()
        with urlopen(Request(self.base + '/api/items', data=body, headers=headers)) as response:
            self.assertEqual(response.status, 201)
            key = json.load(response)['id']
        with urlopen(Request(self.base + '/api/items/' + key, headers=headers, method='DELETE')) as response:
            self.assertEqual(response.status, 200)
        self.assertNotIn(key, self.cache.items)

    def test_upstream_errors_do_not_poison_cache(self):
        for mode in ['ignore', 'redirect', 'truncate']:
            self.origin.mode = mode
            with self.assertRaises(RelayError):
                self.cache.get_chunk(self.key, 0)
            self.assertEqual(self.cache.used, 0)
        self.origin.mode = 'ok'
        self.origin.etag = '"changed"'
        with self.assertRaises(RelayError):
            self.cache.get_chunk(self.key, 0)

    def test_auth_and_no_secret_in_api(self):
        with self.assertRaises(HTTPError) as cm:
            urlopen(self.base + '/api/items')
        self.assertEqual(cm.exception.code, 401)
        cm.exception.close()
        req = Request(self.base + '/api/items', headers={'Authorization': 'Bearer ' + 'x' * 32})
        with urlopen(req) as response:
            body = response.read().decode()
            self.assertNotIn('test-secret', body)
            self.assertNotIn('api_key', body)
            self.assertEqual(json.loads(body)['items'][0]['title'], 'Test movie')

    def test_allowlist_and_dedup_and_delete(self):
        for url in ['https://example.com/Items/' + 'a' * 32 + '/Download', 'http://127.0.0.1/private', 'http://user:pw@127.0.0.1/Items/' + 'a' * 32 + '/Download']:
            with self.assertRaises(RelayError):
                self.cache.create(url, '')
        self.assertEqual(self.cache.create(self.url, 'Again'), self.key)
        self.assertEqual(self.origin.requests, 1)
        self.cache.get_chunk(self.key, 0)
        self.cache.delete(self.key)
        self.assertEqual(self.cache.used, 0)
        with self.assertRaises(HTTPError) as cm:
            self.get('bytes=0-1')
        self.assertEqual(cm.exception.code, 404)
        cm.exception.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
