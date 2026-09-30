import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import test_hls
from throttle import Bandwidth
from preload import FetchGate
from hls import HLSError


class Features(unittest.TestCase):
    def setUp(self):
        self.fixture = test_hls.HLSTests()
        self.fixture.setUp()
        self.cache = self.fixture.cache

    def tearDown(self):
        self.fixture.origin.release.set()
        worker = self.cache.preloader.worker
        if worker:
            worker.join(5)
        self.fixture.tearDown()

    def api(self, path, body, token='x' * 32):
        with urlopen(Request(self.fixture.base + path, data=json.dumps(body).encode(),
                             headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})) as r:
            return json.load(r)

    def wait_job(self):
        worker = self.cache.preloader.worker
        if worker:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        return self.cache.preloader.snapshot(self.fixture.key)

    def test_preload_prefix_reuses_playback_cache(self):
        self.api('/api/items/' + self.fixture.key + '/preload', {'percent': 5})
        job = self.wait_job()
        self.assertEqual((job['state'], job['done'], job['resident']), ('completed', 1, 1))
        self.assertEqual(job['target_seconds'], 4)
        self.fixture.segment()
        self.assertEqual(self.fixture.origin.segment_calls, 1)

    def test_preload_budget_failure_unpins(self):
        self.cache.preloader.start(self.fixture.key, 100)
        job = self.wait_job()
        self.assertEqual(job['state'], 'failed')
        self.assertLessEqual(self.cache.used, self.cache.budget)
        self.assertFalse(self.cache.pins)

    def test_cancel_inflight_finishes_only_current_segment(self):
        self.fixture.origin.delay = True
        self.cache.preloader.start(self.fixture.key, 100)
        self.assertTrue(self.fixture.origin.entered.wait(2))
        self.cache.preloader.cancel(self.fixture.key)
        self.fixture.origin.release.set()
        job = self.wait_job()
        self.assertEqual((job['state'], job['done']), ('cancelled', 1))
        self.assertFalse(self.cache.pins)

    def test_subtitle_burn_isolated_link_and_secret_redaction(self):
        tracks = self.api('/api/subtitles', {'id': self.fixture.key})['subtitles']
        self.assertEqual(tracks[0]['index'], 2)
        self.assertTrue(tracks[0]['external'])
        self.assertNotIn('Path', tracks[0])
        key = self.cache.hls.create(self.fixture.url, 'subbed', subtitle_index=2)
        self.assertNotEqual(key, self.fixture.key)
        params = self.fixture.origin.parameters
        self.assertEqual(params['SubtitleMethod'], ['Encode'])
        self.assertEqual(params['SubtitleStreamIndex'], ['2'])
        self.assertEqual(params['VideoCodec'], ['h264'])
        self.assertEqual(params['AllowVideoStreamCopy'], ['false'])
        self.assertIn('SubtitleMethod=Encode', self.cache.items[key]['segments'][0]['url'])
        self.assertNotIn('test-only-secret', json.dumps(self.cache.status('https://example.com')))
        with self.assertRaises(HLSError):
            self.cache.hls.create(self.fixture.url, 'bad', subtitle_index=99)

    def test_subtitle_requires_explicit_reencoding_and_keeps_selected_codec(self):
        before = len(self.fixture.origin.calls), len(self.cache.items)
        for transcode in (False, None, 'false'):
            with self.subTest(transcode=transcode), self.assertRaises(HLSError):
                self.cache.hls.create(self.fixture.url, 'subbed', transcode_video=transcode,
                                      subtitle_index=2, video_codec='hevc')
        self.assertEqual(before, (len(self.fixture.origin.calls), len(self.cache.items)))
        key = self.api('/api/items', {'mode': 'hls', 'url': self.fixture.url,
                       'transcode_video': True, 'subtitle_index': 2, 'video_codec': 'hevc'})['id']
        params = self.fixture.origin.parameters
        self.assertEqual(params['VideoCodec'], ['hevc'])
        self.assertEqual(params['SubtitleMethod'], ['Encode'])
        self.assertEqual(params['AllowVideoStreamCopy'], ['false'])
        self.assertEqual(self.cache.items[key]['output_video'], 'hevc')
        with self.assertRaises(HTTPError) as caught:
            self.api('/api/items', {'mode': 'hls', 'url': self.fixture.url,
                     'transcode_video': False, 'subtitle_index': 2, 'video_codec': 'hevc'})
        self.assertIn('烧录字幕需要重新编码', json.load(caught.exception)['error'])
        caught.exception.close()

    def test_api_auth_and_bandwidth_persistence(self):
        for path, body in [('/api/bandwidth', {}), ('/api/subtitles', {'id': self.fixture.key}),
                           ('/api/items/' + self.fixture.key + '/preload', {})]:
            with self.assertRaises(HTTPError) as caught:
                self.api(path, body, token='bad')
            self.assertEqual(caught.exception.code, 401)
        self.api('/api/bandwidth', dict(total_mbps=100, utilization=80, client_mbps=20))
        from settings import Settings
        saved = Settings(self.cache.root / '.env').values
        self.assertEqual(saved['BANDWIDTH_TOTAL_MBPS'] * saved['BANDWIDTH_UTILIZATION'] / 100, 80)
        self.fixture.segment()
        deadline = time.monotonic() + 1
        while self.cache.bandwidth.snapshot()['active_senders'] and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(self.cache.bandwidth.snapshot()['active_senders'], 0)


class Concurrency(unittest.TestCase):
    def test_foreground_fetch_has_priority(self):
        gate, order = FetchGate(), []
        gate.acquire()
        def run(background):
            with gate.background() if background else gate:
                order.append(background)
        bg = threading.Thread(target=run, args=(True,))
        fg = threading.Thread(target=run, args=(False,))
        bg.start()
        fg.start()
        deadline = time.monotonic() + 2
        while gate.waiters == 0 and time.monotonic() < deadline:
            time.sleep(.005)
        gate.release()
        bg.join(2)
        fg.join(2)
        self.assertEqual(order, [False, True])

    def test_same_ip_connections_share_limit_and_global_budget(self):
        with tempfile.TemporaryDirectory() as root:
            bw = Bandwidth(Path(root), minimum_mbps=.01)
            bw.configure(dict(total_mbps=2, utilization=100, client_mbps=1))
            bw.begin('a'); bw.begin('a')
            self.assertEqual(bw.snapshot()['active_senders'], 1)
            def transfer(identity):
                for _ in range(8):
                    bw.acquire(identity, 16384)
            started = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                list(pool.map(transfer, ['a', 'a']))
            self.assertGreaterEqual(time.monotonic() - started, 1.8)
            bw.begin('b'); bw.begin('c')
            self.assertAlmostEqual(bw.snapshot()['effective_client_mbps'], 2 / 3)
            bw.end('a'); bw.end('a'); bw.end('b'); bw.end('c')
            self.assertEqual(bw.snapshot()['active_senders'], 0)
            bw.configure(dict(total_mbps=1, utilization=100, client_mbps=0))
            bw.begin('a'); bw.begin('b')
            started = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                list(pool.map(transfer, ['a', 'b']))
            self.assertGreaterEqual(time.monotonic() - started, 1.8)
            bw.end('a'); bw.end('b')

    def test_invalid_settings_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            bw = Bandwidth(Path(root), minimum_mbps=.01)
            for value in (0, float('nan'), True, -1):
                with self.assertRaises(ValueError):
                    bw.configure(dict(total_mbps=value, utilization=80, client_mbps=32))


if __name__ == '__main__':
    unittest.main()
