import concurrent.futures
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from throttle import Bandwidth
import test_hls


class AdmissionTests(unittest.TestCase):
    def test_capacity_preserves_existing_across_segment_gaps(self):
        with tempfile.TemporaryDirectory() as root:
            bw = Bandwidth(Path(root))
            for i in range(16):
                self.assertTrue(bw.admit(str(i)))
                bw.release(str(i))
            self.assertFalse(bw.admit('late'))
            self.assertTrue(bw.admit('0'))
            self.assertTrue(bw.admit('0'))
            self.assertEqual(bw.snapshot()['admitted_ips'], 16)
            bw.release('0'); bw.release('0')
            with bw.cv:
                bw.leases['1']['last'] -= 91
            self.assertTrue(bw.admit('late'))
            self.assertFalse(bw.admit('1'))

    def test_atomic_arrivals_and_active_requests_do_not_expire(self):
        with tempfile.TemporaryDirectory() as root:
            bw = Bandwidth(Path(root), idle_seconds=0)
            with concurrent.futures.ThreadPoolExecutor(24) as pool:
                results = list(pool.map(bw.admit, map(str, range(24))))
            self.assertEqual(sum(results), 16)
            self.assertEqual(bw.snapshot()['admitted_ips'], 16)
            for i, accepted in enumerate(results):
                if accepted:
                    bw.release(str(i))
            self.assertEqual(bw.snapshot()['admitted_ips'], 0)

    def test_lowering_capacity_does_not_evict_existing(self):
        with tempfile.TemporaryDirectory() as root:
            bw = Bandwidth(Path(root))
            bw.admit('a'); bw.admit('b')
            for settings in [dict(total_mbps=10, utilization=100, client_mbps=32),
                             dict(total_mbps=200, utilization=80, client_mbps=9)]:
                with self.assertRaises(ValueError):
                    bw.configure(settings)
            self.assertEqual(bw.snapshot()['capacity'], 16)
            self.assertEqual(bw.snapshot()['admitted_ips'], 2)

    def test_http_rejects_before_origin_fetch_and_existing_still_plays(self):
        fixture = test_hls.HLSTests()
        fixture.setUp()
        try:
            bw = fixture.cache.bandwidth
            bw.configure(dict(total_mbps=10, utilization=100, client_mbps=10))
            with urlopen(Request(fixture.play + '/video.m3u8', headers={'X-Real-IP': '192.0.2.1'})) as r:
                self.assertEqual(r.status, 200)
            self.assertEqual(fixture.origin.segment_calls, 0)
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(fixture.play + '/segment/0.ts', headers={'X-Real-IP': '192.0.2.2'}))
            self.assertEqual(caught.exception.code, 503)
            self.assertIn('容量', json.load(caught.exception)['error'])
            self.assertEqual(fixture.origin.segment_calls, 0)
            with urlopen(Request(fixture.play + '/segment/0.ts', headers={'X-Real-IP': '192.0.2.1'})) as r:
                self.assertEqual(r.read(), test_hls.SEGMENT)
            self.assertEqual(bw.snapshot()['admitted_ips'], 1)
            deadline = time.monotonic() + 1
            while bw.snapshot()['active_senders'] and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(bw.snapshot()['active_senders'], 0)
        finally:
            fixture.tearDown()
