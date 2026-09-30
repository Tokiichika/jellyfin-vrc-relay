import concurrent.futures
import ipaddress
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from app import Cache, RelayError, Server
from hls import HLSError
from metrics import Metrics
from diagnostics import Diagnostics

ITEM = 'a' * 32
PACKET = bytes([0x47]) + bytes(187)
SEGMENT = PACKET * 1000


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body, mime='application/json', status=200):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get('X-Emby-Token') != 'test-only-secret':
            return self.reply(b'', status=401)
        p = urlsplit(self.path)
        query = parse_qs(p.query)
        self.server.calls.append((p.path, query))
        if p.path.endswith('PlaybackInfo'):
            return self.reply(json.dumps({'MediaSources': [{'Id': 'source1', 'Name': '示例剧集 S01E03', 'Size': 1234567,
                'RunTimeTicks': 120000000, 'DefaultAudioStreamIndex': 1,
                'MediaStreams': [{'Type': 'Video', 'Codec': 'hevc', 'Width': 1920, 'Height': 1080, 'Index': 0},
                                 {'Type': 'Audio', 'Codec': self.server.audio, 'Index': 1},
                                 {'Type': 'Subtitle', 'Codec': 'ass', 'Index': 2, 'IsExternal': True,
                                  'DisplayTitle': '简体中文', 'Language': 'chi', 'Path': '/private/subtitle.ass'}]}]}).encode())
        if p.path.endswith('main.m3u8'):
            self.server.parameters = query
            text = '#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n'
            for i in range(3):
                args = {k: v[0] for k, v in query.items()}
                args.update(runtimeTicks='120000000', actualSegmentLengthTicks='40000000', api_key='test-only-secret')
                text += '#EXTINF:4.000000,\nhls1/main/' + str(i) + '.ts?' + urlencode(args) + '\n'
            text += '#EXT-X-ENDLIST\n'
            return self.reply(text.encode(), 'application/vnd.apple.mpegurl')
        if '/hls1/main/' in p.path:
            self.server.segment_calls += 1
            if self.server.delay:
                self.server.entered.set()
                self.server.release.wait(3)
            mode = self.server.mode
            if mode == 'error':
                return self.reply(b'', status=503)
            if mode == 'html':
                return self.reply(b'<html>failure</html>', 'text/html')
            if mode == 'truncated':
                self.send_response(200)
                self.send_header('Content-Length', str(len(SEGMENT)))
                self.end_headers()
                self.wfile.write(SEGMENT[:188])
                return
            return self.reply(SEGMENT, 'video/mp2t')
        self.reply(b'', status=404)

    def do_DELETE(self):
        self.server.stops.append(parse_qs(urlsplit(self.path).query))
        self.reply(b'', status=204)


class HLSTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        self.origin.calls, self.origin.stops = [], []
        self.origin.audio, self.origin.mode = 'flac', 'ok'
        self.origin.segment_calls, self.origin.delay = 0, False
        self.origin.entered, self.origin.release = threading.Event(), threading.Event()
        threading.Thread(target=self.origin.serve_forever, daemon=True).start()
        self.cache = Cache(self.tmp.name, 2 * len(SEGMENT), 65536, ['127.0.0.1'], allow_http=True)
        self.url = f'http://127.0.0.1:{self.origin.server_port}/Items/{ITEM}/Download?api_key=test-only-secret'
        self.key = self.cache.hls.create(self.url, 'HLS movie')
        self.relay = Server(('127.0.0.1', 0), self.cache, 'x' * 32, 'https://video.example.com:8443')
        threading.Thread(target=self.relay.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.relay.server_port}'
        self.play = self.base + '/v/' + self.key

    def tearDown(self):
        self.origin.release.set()
        self.relay.shutdown()
        self.relay.server_close()
        self.origin.shutdown()
        self.origin.server_close()
        self.tmp.cleanup()

    def segment(self, index=0, headers=None):
        with urlopen(Request(self.play + '/segment/' + str(index) + '.ts', headers=headers or {}), timeout=5) as response:
            return response.read()

    def test_missing_title_uses_media_name(self):
        key = self.cache.hls.create(self.url, '', bitrate=4000000)
        self.assertEqual(self.cache.items[key]['title'], '示例剧集 S01E03')

    def test_default_profile_and_full_seekable_timeline(self):
        with urlopen(self.play + '/video.m3u8') as response:
            playlist = response.read().decode()
        self.assertIn('#EXT-X-ENDLIST', playlist)
        self.assertIn('#EXT-X-PLAYLIST-TYPE:VOD', playlist)
        self.assertEqual(playlist.count('#EXTINF'), 3)
        self.assertIn('segment/2.ts', playlist)
        self.assertNotIn('api_key', playlist)
        self.assertNotIn('test-only-secret', playlist)
        self.assertNotIn('127.0.0.1', playlist)
        p = self.origin.parameters
        self.assertEqual(p['VideoCodec'], ['h264'])
        self.assertEqual(p['AudioCodec'], ['aac'])
        self.assertEqual(p['VideoBitRate'], ['8000000'])
        self.assertEqual(p['MaxHeight'], ['1080'])
        self.assertEqual(self.segment(2), SEGMENT)  # arbitrary seek before segment zero
        self.assertEqual(self.origin.segment_calls, 1)

    def test_concurrent_clients_share_one_upstream_fetch(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            values = list(pool.map(lambda _: self.segment(0), range(12)))
        self.assertTrue(all(v == SEGMENT for v in values))
        self.assertEqual(self.origin.segment_calls, 1)
        self.assertEqual(self.cache.misses, 1)
        self.assertEqual(self.cache.hits, 11)
        self.assertEqual(self.cache.metrics.snapshot()['upload_bytes'], len(SEGMENT) * 12)

    def test_cache_budget_shared_with_files_and_restart(self):
        for i in (0, 1, 2):
            self.segment(i)
            self.assertLessEqual(self.cache.used, self.cache.budget)
        self.assertNotIn(self.cache.hls.filename(self.key, 0), self.cache.entries)
        other = Cache(self.tmp.name, self.cache.budget, 65536, ['127.0.0.1'], allow_http=True)
        with other.hls.segment(self.key, 2) as (file, size):
            self.assertEqual(file.read(), SEGMENT)
        self.assertEqual(self.origin.segment_calls, 3)
        # Direct-file cache reserves space in the same LRU budget.
        with self.cache.lock:
            self.cache.evict(65536)
        self.assertLessEqual(self.cache.used + 65536, self.cache.budget)

    def test_pinned_segment_not_evicted_or_deleted(self):
        with self.cache.hls.segment(self.key, 0) as (file, size):
            self.segment(1)
            self.segment(2)
            self.assertEqual(file.read(), SEGMENT)
            self.assertIn(self.cache.hls.filename(self.key, 0), self.cache.entries)
            with self.assertRaises(RelayError):
                self.cache.delete(self.key)
        self.cache.delete(self.key)
        self.assertEqual(self.cache.used, 0)

    def test_cached_clients_not_blocked_by_encoding(self):
        self.segment(0)
        self.origin.delay = True
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(self.segment, 1)
            self.assertTrue(self.origin.entered.wait(2))
            start = time.monotonic()
            self.assertEqual(self.segment(0), SEGMENT)
            self.assertLess(time.monotonic() - start, 1)
            self.origin.release.set()
            self.assertEqual(future.result(), SEGMENT)

    def test_bad_responses_not_cached_retry_succeeds(self):
        for mode in ('error', 'html', 'truncated'):
            self.origin.mode = mode
            with self.assertRaises(HTTPError) as cm:
                self.segment(0)
            self.assertEqual(cm.exception.code, 502)
            self.assertNotIn('test-only-secret', cm.exception.read().decode())
            cm.exception.close()
            self.assertEqual(self.cache.used, 0)
        self.origin.mode = 'ok'
        self.assertEqual(self.segment(0), SEGMENT)

    def test_idle_stop_targets_only_own_session_resume_reuses_cache(self):
        self.segment(0)
        self.cache.hls.active[self.key] = time.monotonic() - 200
        self.cache.hls.reap()
        stop = self.origin.stops[-1]
        self.assertEqual(stop['DeviceId'], [self.cache.items[self.key]['device_id']])
        self.assertEqual(stop['PlaySessionId'], [self.cache.items[self.key]['session_id']])
        self.assertNotIn(self.key, self.cache.hls.active)
        self.segment(0)
        self.assertEqual(self.origin.segment_calls, 1)
        self.segment(1)
        self.assertIn(self.key, self.cache.hls.active)

    def test_configurable_copy_and_custom_bitrates(self):
        key = self.cache.hls.create(self.url, 'Copy video', 720, 3500000, False, True, 128000)
        self.assertNotEqual(key, self.key)
        p = self.origin.parameters
        self.assertEqual(p['VideoCodec'], ['copy'])
        self.assertNotIn('VideoBitRate', p)
        self.assertNotIn('MaxHeight', p)
        self.assertEqual(p['AudioBitRate'], ['128000'])
        with self.assertRaises(HLSError):
            self.cache.hls.create(self.url, 'FLAC passthrough', transcode_audio=False)
        self.origin.audio = 'aac'
        self.cache.hls.create(self.url, 'Copy audio', transcode_audio=False)
        self.assertEqual(self.origin.parameters['AudioCodec'], ['copy'])
        self.assertNotIn('AudioBitRate', self.origin.parameters)
        with self.assertRaises(HLSError):
            self.cache.hls.create(self.url, '', bitrate=999999999)

    def test_hevc_reencoding_isolated_from_h264_and_persisted(self):
        key = self.cache.hls.create(self.url, 'HEVC output', video_codec='hevc')
        self.assertNotEqual(key, self.key)
        p = self.origin.parameters
        self.assertEqual(p['VideoCodec'], ['hevc'])
        self.assertEqual(p['AllowVideoStreamCopy'], ['false'])
        self.assertEqual(p['MaxVideoBitDepth'], ['8'])
        self.assertEqual(p['VideoBitRate'], ['8000000'])
        self.assertEqual(self.cache.items[key]['output_video'], 'hevc')
        self.assertEqual(self.cache.hls.create(self.url, '', video_codec='hevc'), key)
        restarted = Cache(self.tmp.name, self.cache.budget, 65536, ['127.0.0.1'], allow_http=True)
        self.assertEqual(restarted.items[key]['video_codec'], 'hevc')
        self.assertIn('VideoCodec=hevc', restarted.items[key]['segments'][0]['url'])
        with restarted.hls.segment(key, 0) as (file, size):
            self.assertEqual(file.read(), SEGMENT)

    def test_legacy_h264_links_reused_and_copy_ignores_target_codec(self):
        self.cache.items[self.key].pop('video_codec')
        self.assertEqual(self.cache.hls.create(self.url, ''), self.key)
        key = self.cache.hls.create(self.url, 'Copy', transcode_video=False, video_codec='hevc')
        self.assertEqual(self.origin.parameters['VideoCodec'], ['copy'])
        self.assertEqual(self.cache.items[key]['output_video'], 'hevc')
        self.assertNotIn('VideoBitRate', self.origin.parameters)
        self.assertEqual(self.cache.hls.create(self.url, '', transcode_video=False, video_codec='h264'), key)

    def test_invalid_video_codecs_rejected_before_origin_request(self):
        before = len(self.origin.calls), len(self.cache.items)
        for codec in ('av1', 'h265', '', None, True, ['hevc']):
            with self.subTest(codec=codec), self.assertRaises(HLSError):
                self.cache.hls.create(self.url, '', video_codec=codec)
        self.assertEqual(before, (len(self.origin.calls), len(self.cache.items)))

    def test_video_codec_api_create_variant_and_configured_default(self):
        headers = {'Authorization': 'Bearer ' + 'x' * 32, 'Content-Type': 'application/json'}
        def post(path, body):
            with urlopen(Request(self.base + path, headers=headers, data=json.dumps(body).encode())) as response:
                return json.load(response)
        key = post('/api/items', {'mode': 'hls', 'url': self.url, 'video_codec': 'hevc'})['id']
        entry = next(item for item in self.cache.status('https://example.com')['items'] if item['id'] == key)
        self.assertEqual((entry['video_codec'], entry['output_video']), ('hevc', 'hevc'))
        variant = post('/api/items/' + key + '/variant', {'mode': 'hls', 'video_codec': 'h264'})['id']
        self.assertEqual(variant, self.key)
        self.assertEqual(self.cache.items[key]['output_video'], 'hevc')
        self.cache.update_settings({'DEFAULT_VIDEO_CODEC': 'hevc'})
        self.assertEqual(post('/api/items', {'mode': 'hls', 'url': self.url})['id'], key)

    def test_unsafe_playlist_rejected(self):
        item = self.cache.items[self.key]
        prefix = '#EXTM3U\n#EXTINF:12,\n'
        suffix = '\n#EXT-X-ENDLIST\n'
        for target in ('https://evil.example/steal.ts', 'http://127.0.0.1/private', '//evil.example/key',
                       f'/Videos/{"b" * 32}/hls1/main/0.ts'):
            with self.assertRaises(HLSError):
                self.cache.hls.parse(item, item['playlist_url'], (prefix + target + suffix).encode())
        for tag in ('#EXT-X-KEY:METHOD=AES-128,URI="https://evil.example/key"', '#EXT-X-MAP:URI="init.mp4"', '#EXT-X-BYTERANGE:10'):
            with self.assertRaises(HLSError):
                self.cache.hls.parse(item, item['playlist_url'], ('#EXTM3U\n' + tag + suffix).encode())
        with self.assertRaises(HLSError):
            self.cache.hls.parse(item, item['playlist_url'], b'#EXTM3U\n')

    def test_partial_segment_and_no_secret_in_management(self):
        self.assertEqual(self.segment(0, {'Range': 'bytes=188-375'}), SEGMENT[188:376])
        status = self.cache.status('https://example.com:8443')
        text = json.dumps(status)
        self.assertNotIn('test-only-secret', text)
        self.assertNotIn('session_id', text)
        self.assertEqual(status['items'][0]['segments_cached'], 1)
        self.assertEqual(status['metrics']['active_clients'], 1)
        # Receiving the final body byte can race the server's finally/release block.
        deadline = time.monotonic() + 1
        while self.cache.metrics.snapshot()['active_requests'] and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(self.cache.metrics.snapshot()['active_requests'], 0)

    def test_variant_api_and_default_v1_compatibility(self):
        headers = {'Authorization': 'Bearer ' + 'x' * 32, 'Content-Type': 'application/json'}
        body = json.dumps({'mode': 'hls', 'title': 'New quality', 'bitrate': 4500000}).encode()
        with urlopen(Request(self.base + '/api/items/' + self.key + '/variant', headers=headers, data=body)) as response:
            key = json.load(response)['id']
        self.assertNotEqual(key, self.key)
        self.assertIn(self.key, self.cache.items)
        self.assertEqual(self.cache.items[key]['bitrate'], 4500000)

    def test_debug_api_auth_toggle_and_error_redaction(self):
        with self.assertRaises(HTTPError) as cm:
            urlopen(self.base + '/api/debug')
        self.assertEqual(cm.exception.code, 401)
        cm.exception.close()
        headers = {'Authorization': 'Bearer ' + 'x' * 32, 'Content-Type': 'application/json'}
        with urlopen(Request(self.base + '/api/debug', data=b'{"enabled":true}', headers=headers)) as response:
            self.assertTrue(json.load(response)['enabled'])
        self.segment(0)
        self.origin.mode = 'error'
        with self.assertRaises(HTTPError) as cm:
            self.segment(1)
        cm.exception.close()
        with urlopen(Request(self.base + '/api/debug', headers=headers)) as response:
            raw = response.read().decode()
        self.assertNotIn('test-only-secret', raw)
        self.assertNotIn('api_key=', raw)
        self.assertNotIn(self.key, raw)
        events = json.loads(raw)['events']
        self.assertIn('segment_cached', [e['event'] for e in events])
        self.assertIn('origin_http_error', [e['event'] for e in events])
        with urlopen(Request(self.base + '/api/debug', data=b'{"enabled":false}', headers=headers)) as response:
            self.assertFalse(json.load(response)['enabled'])


class MetricsTests(unittest.TestCase):
    def test_debug_bounded_and_exception_text_never_logged(self):
        d = Diagnostics()
        d.emit('DEBUG', 'hidden')
        self.assertEqual(d.snapshot()['events'], [])
        d.set_enabled(True)
        for n in range(600):
            d.emit('DEBUG', 'event', counter=n)
        self.assertEqual(len(d.snapshot()['events']), 500)
        cursor = d.snapshot()['last_seq']
        try:
            raise RuntimeError('https://private.example?api_key=never-log-this')
        except RuntimeError as error:
            d.exception('failed', error)
        result = d.snapshot(cursor)
        self.assertEqual(len(result['events']), 1)
        self.assertNotIn('never-log-this', json.dumps(result))
        self.assertIn('RuntimeError', json.dumps(result))

    def test_rates_expire_and_clients_are_estimates(self):
        clock = [100.0]
        m = Metrics(clock=lambda: clock[0])
        clock[0] = 110
        m.add('download', 1000)
        m.add('upload', 2000)
        m.begin('same-client', 'movie1')
        m.touch('same-client', 'movie2')
        m.touch('other-client', 'movie1')
        snapshot = m.snapshot()
        self.assertEqual(snapshot['download_bps'], 100)
        self.assertEqual(snapshot['upload_bps'], 200)
        self.assertEqual(snapshot['active_clients'], 2)
        self.assertEqual(snapshot['per_item']['movie1']['active_clients'], 2)
        m.end()
        clock[0] += 11
        self.assertEqual(m.snapshot()['upload_bps'], 0)
        clock[0] += 90
        self.assertEqual(m.snapshot()['active_clients'], 0)

    def test_trusted_proxy_headers_only(self):
        m = Metrics()
        trusted = [ipaddress.ip_network('127.0.0.0/8')]
        direct = m.identity('203.0.113.1', None, 'player', trusted)
        self.assertEqual(direct, m.identity('127.0.0.1', '203.0.113.1', 'player', trusted))
        self.assertEqual(direct, m.identity('203.0.113.1', '198.51.100.1', 'player', trusted))
        self.assertNotEqual(direct, m.identity('127.0.0.1', '203.0.113.2', 'player', trusted))


if __name__ == '__main__':
    unittest.main(verbosity=2)
