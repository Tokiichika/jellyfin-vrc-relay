"""Jellyfin finite HLS VOD adapter. No cloud-side encoding, no public origin URLs."""
import contextlib
import hashlib
import json
import math
import os
import re
import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request


class HLSError(Exception):
    pass


class HLS:
    def __init__(self, cache, timeout=90, idle=120):
        self.cache = cache
        self.timeout, self.idle = timeout, idle
        self.active = {}  # own DeviceId/PlaySessionId only; never stop other clients
        self.stopping = False

    def upstream(self, url, token, limit, method='GET'):
        path = urlsplit(url).path
        kind = 'metadata' if path.endswith('PlaybackInfo') else 'playlist' if path.endswith('.m3u8') else 'stop_encoding' if method == 'DELETE' else 'segment'
        started = time.monotonic()
        self.cache.debug.emit('DEBUG', 'origin_request', kind=kind, method=method, timeout=self.timeout)
        request = Request(url, method=method, headers={
            'X-Emby-Token': token, 'Accept-Encoding': 'identity',
            'User-Agent': 'Jellyfin-VRC-Relay/2.0'})
        try:
            with self.cache.opener.open(request, timeout=self.timeout) as response:
                if response.status not in (200, 204) or response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise HLSError('Jellyfin 返回了不支持的响应格式')
                expected = response.headers.get('Content-Length')
                if expected and int(expected) > limit:
                    raise HLSError('Jellyfin 响应超过大小限制，请降低码率')
                parts, size = [], 0
                while True:
                    part = response.read(min(65536, limit + 1 - size))
                    if not part:
                        break
                    self.cache.metrics.add('download', len(part))
                    parts.append(part)
                    size += len(part)
                    if size > limit:
                        raise HLSError('Jellyfin 响应超过大小限制，请降低码率')
                if expected and size != int(expected):
                    raise HLSError('Jellyfin 返回了不完整数据，请重试')
                self.cache.debug.emit('INFO', 'origin_response', kind=kind, status=response.status,
                                      bytes=size, seconds=round(time.monotonic() - started, 3))
                return b''.join(parts)
        except HTTPError as error:
            status = error.code
            error.close()
            self.cache.debug.emit('ERROR', 'origin_http_error', kind=kind, status=status,
                                  seconds=round(time.monotonic() - started, 3))
            raise HLSError(f'Jellyfin 回源 HTTP {status}；检查凭据、转码权限和 NAS 日志') from None
        except HLSError:
            raise
        except Exception as error:
            self.cache.debug.exception('origin_transport_error', error)
            raise HLSError('Jellyfin 回源失败或超时；检查 NAS 转码速度、证书和网络') from None

    def token(self, item):
        query = {k.lower(): v for k, v in parse_qsl(urlsplit(item['url']).query)}
        token = query.get('api_key') or query.get('apikey')
        if not token or len(token) > 512:
            raise HLSError('下载地址需要包含有效的 api_key')
        return token

    def resource_url(self, item, playlist_url, value):
        value = urljoin(playlist_url, value)
        origin, target = urlsplit(item['url']), urlsplit(value)
        if (target.scheme, target.netloc) != (origin.scheme, origin.netloc) or target.fragment:
            raise HLSError('拒绝 HLS 中跨源或带片段标识的资源地址')
        if not re.fullmatch(r'/Videos/[a-fA-F0-9-]+/hls1/[a-zA-Z0-9_-]+/\d+\.ts', target.path):
            raise HLSError('Jellyfin 返回了不支持的分片路径；本版本只接受动态 TS 点播分片')
        path_id = target.path.split('/')[2].replace('-', '').lower()
        if path_id != item['item_id'].lower():
            raise HLSError('HLS 分片不属于当前影片')
        query = [(k, v) for k, v in parse_qsl(target.query) if k.lower() not in ('api_key', 'apikey')]
        # Authentication stays in a server-side header, never in public playlists.
        result = urlunsplit((target.scheme, target.netloc, target.path, urlencode(query), ''))
        if len(result) > 8192:
            raise HLSError('HLS 分片地址过长')
        return result

    def parse(self, item, source_url, raw):
        try:
            source = raw.decode('utf-8-sig')
        except UnicodeError:
            raise HLSError('Jellyfin 播放列表不是 UTF-8') from None
        lines = source.splitlines()
        if not lines or lines[0].strip() != '#EXTM3U' or '#EXT-X-ENDLIST' not in source:
            raise HLSError('需要包含完整时间轴的 HLS 点播列表，不接受直播滑动窗口')
        output, resources, duration, pending = [], [], 0.0, None
        # Strict allowlist: do not accidentally expose credentials in unknown URI tags/comments.
        simple_tags = ('#EXT-X-VERSION:', '#EXT-X-TARGETDURATION:', '#EXT-X-MEDIA-SEQUENCE:',
                       '#EXT-X-DISCONTINUITY-SEQUENCE:')
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#EXTINF:'):
                if pending is not None:
                    raise HLSError('点播分片时长缺少对应资源')
                try:
                    pending = float(line.split(':', 1)[1].split(',', 1)[0])
                except ValueError:
                    raise HLSError('点播分片时长无效') from None
                if not math.isfinite(pending) or not 0 < pending <= 30:
                    raise HLSError('点播分片时长超出支持范围')
                output.append(f'#EXTINF:{pending:.6f},')
            elif line in ('#EXTM3U', '#EXT-X-ENDLIST', '#EXT-X-DISCONTINUITY', '#EXT-X-INDEPENDENT-SEGMENTS'):
                output.append(line)
            elif line.startswith('#EXT-X-PLAYLIST-TYPE:'):
                if line != '#EXT-X-PLAYLIST-TYPE:VOD':
                    raise HLSError('需要 VOD 点播列表')
                output.append(line)
            elif line.startswith(simple_tags):
                if not line.rsplit(':', 1)[1].isdigit():
                    raise HLSError('HLS 数值标签无效')
                output.append(line)
            elif line.startswith('#'):
                raise HLSError('Jellyfin 返回了不支持的 HLS 标签；不支持加密、字幕或字节范围 HLS')
            else:
                if pending is None:
                    raise HLSError('需要媒体点播列表，而非主播放列表')
                url = self.resource_url(item, source_url, line)
                resources.append({'url': url, 'duration': pending, 'start': duration})
                output.append('segment/' + str(len(resources) - 1) + '.ts')
                duration += pending
                pending = None
        if pending is not None or not resources or len(resources) > 15000:
            raise HLSError('点播分片数量无效或影片过长')
        if abs(duration - item['duration']) > max(10, item['duration'] * .01):
            raise HLSError('点播列表未覆盖影片完整时长，无法保证拖动进度')
        return '\n'.join(output) + '\n', resources

    @staticmethod
    def subtitle_tracks(media):
        return [{'index': s['Index'], 'title': str(s.get('DisplayTitle') or s.get('Title') or s.get('Language') or '字幕')[:200],
                 'language': str(s.get('Language') or '')[:32], 'codec': str(s.get('Codec') or '')[:32],
                 'external': bool(s.get('IsExternal'))}
                for s in media.get('MediaStreams', []) if s.get('Type') == 'Subtitle' and type(s.get('Index')) is int]

    def subtitles(self, url):
        self.cache.validate_url(url)
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, '', '', ''))
        with self.cache.fetch_lock:
            info = json.loads(self.upstream(origin + '/Items/' + parts.path.split('/')[2] + '/PlaybackInfo',
                                           self.token({'url': url}), 2 * 1024 * 1024))
        media = next((s for s in info.get('MediaSources', []) if s.get('RunTimeTicks', 0) > 0
                      and not s.get('IsInfiniteStream') and not s.get('RequiresOpening')), None)
        if not media:
            raise HLSError('没有可用媒体源')
        return self.subtitle_tracks(media)

    def create(self, url, title, height=1080, bitrate=8000000, transcode_video=True,
               transcode_audio=True, audio_bitrate=192000, subtitle_index=-1, video_codec='h264'):
        self.cache.validate_url(url)
        if type(subtitle_index) is not int or not -1 <= subtitle_index <= 10000:
            raise HLSError('字幕轨道无效')
        if (type(height) is not int or height not in (720, 1080, 2160)
                or type(bitrate) is not int or not 500000 <= bitrate <= 20000000
                or type(audio_bitrate) is not int or not 64000 <= audio_bitrate <= 512000
                or type(transcode_video) is not bool or type(transcode_audio) is not bool):
            raise HLSError('请选择受支持的分辨率和码率')
        if not isinstance(video_codec, str) or video_codec not in ('h264', 'hevc'):
            raise HLSError('视频重新编码仅支持 H.264 或 HEVC（H.265）')
        if subtitle_index >= 0 and not transcode_video:
            raise HLSError('烧录字幕需要重新编码视频；请勾选“是否重新编码”，或选择“不烧录字幕”')
        self.cache.debug.emit('INFO', 'hls_create_requested', height=height, bitrate=bitrate,
                              transcode_video=transcode_video, video_codec=video_codec,
                              transcode_audio=transcode_audio, audio_bitrate=audio_bitrate)
        options = dict(height=height, bitrate=bitrate, transcode_video=transcode_video,
                       transcode_audio=transcode_audio, audio_bitrate=audio_bitrate,
                       subtitle_index=subtitle_index, video_codec=video_codec)
        legacy_defaults = {'subtitle_index': -1, 'video_codec': 'h264'}
        with self.cache.fetch_lock:
            with self.cache.lock:
                for key, existing in self.cache.items.items():
                    if existing.get('mode') == 'hls' and existing['url'] == url and all(
                            k == 'video_codec' and not transcode_video
                            or existing.get(k, legacy_defaults.get(k)) == v for k, v in options.items()):
                        return key
                if len(self.cache.items) >= 100:
                    raise HLSError('最多保留 100 个链接，请删除旧链接')
            from secrets import token_urlsafe, token_hex
            key = token_urlsafe(24)
            parts = urlsplit(url)
            origin = urlunsplit((parts.scheme, parts.netloc, '', '', ''))
            item_id = parts.path.split('/')[2]
            item = {'mode': 'hls', 'url': url, 'item_id': item_id,
                    'origin': origin, 'device_id': 'vrc-relay-' + key,
                    'session_id': token_hex(16), **options,
                    'created': int(time.time()), 'title': title[:120] or '视频'}
            token = self.token(item)
            try:
                info = json.loads(self.upstream(origin + f'/Items/{item_id}/PlaybackInfo', token, 2 * 1024 * 1024))
                candidates = [s for s in info.get('MediaSources', []) if s.get('RunTimeTicks', 0) > 0 and not s.get('IsInfiniteStream') and not s.get('RequiresOpening')]
                if not candidates:
                    raise HLSError('没有可用的本地电影媒体源；不支持直播或需要开启的远程源')
                media = candidates[0]
                if not title.strip():
                    item['title'] = str(media.get('Name') or '视频').replace('\\', '/').rsplit('/', 1)[-1][:120]
                item['media_source_id'] = media['Id']
                item['duration'] = media['RunTimeTicks'] / 10000000
                item['size'] = media.get('Size') or 0
                streams = media.get('MediaStreams', [])
                item['subtitles'] = self.subtitle_tracks(media)
                subtitle = next((s for s in item['subtitles'] if s['index'] == subtitle_index), None)
                if subtitle_index >= 0 and subtitle is None:
                    raise HLSError('所选字幕已不存在，请重新读取字幕列表')
                item['subtitle_title'] = subtitle['title'] if subtitle else ''
                video = next((s for s in streams if s.get('Type') == 'Video'), {})
                audio = next((s for s in streams if s.get('Type') == 'Audio' and s.get('Index') == media.get('DefaultAudioStreamIndex')),
                             next((s for s in streams if s.get('Type') == 'Audio'), {}))
                if not video:
                    raise HLSError('媒体源没有视频轨道')
                item['source_video'] = video.get('Codec', '?')
                item['source_audio'] = audio.get('Codec', '无音轨')
                item['source_resolution'] = f"{video.get('Width', '?')}×{video.get('Height', '?')}"
                self.cache.debug.emit('INFO', 'source_metadata', key, seconds=item['duration'],
                    video=item['source_video'] if re.fullmatch('[a-zA-Z0-9_]{1,32}', item['source_video']) else 'unknown',
                    audio=item['source_audio'] if re.fullmatch('[a-zA-Z0-9_]{1,32}', item['source_audio']) else 'unknown')
                if not transcode_video and video.get('Codec') not in ('h264', 'hevc'):
                    raise HLSError('保留视频目前仅支持 H.264/HEVC；请启用视频重新编码')
                if audio and not transcode_audio and audio.get('Codec') not in ('aac', 'ac3', 'eac3', 'mp3', 'mp2'):
                    raise HLSError('该音频（如 FLAC）不支持当前 TS 分片直拷贝；请启用 AAC，或选择原文件模式')
                item['output_video'] = video_codec if transcode_video else video['Codec']
                item['output_audio'] = ('aac' if transcode_audio else audio['Codec']) if audio else '无音轨'
                params = {'MediaSourceId': item['media_source_id'], 'DeviceId': item['device_id'],
                          'PlaySessionId': item['session_id'], 'Static': 'false',
                          'VideoCodec': video_codec if transcode_video else 'copy',
                          'AudioCodec': 'aac' if transcode_audio else 'copy',
                          'AllowVideoStreamCopy': str(not transcode_video).lower(),
                          'AllowAudioStreamCopy': str(not transcode_audio).lower(), 'EnableAutoStreamCopy': 'false',
                          'SegmentContainer': 'ts', 'SegmentLength': 4, 'MinSegments': 1,
                          'BreakOnNonKeyFrames': 'false', 'SubtitleStreamIndex': subtitle_index,
                          'Context': 'Streaming'}
                if subtitle_index >= 0:
                    params['SubtitleMethod'] = 'Encode'
                if transcode_video:
                    params.update(VideoBitRate=bitrate, MaxWidth={720:1280,1080:1920,2160:3840}[height],
                                  MaxHeight=height, MaxVideoBitDepth=8)
                if transcode_audio:
                    params.update(AudioBitRate=audio_bitrate, MaxAudioChannels=2,
                                  TranscodingMaxAudioChannels=2, AudioSampleRate=48000)
                if audio.get('Index') is not None:
                    params['AudioStreamIndex'] = audio['Index']
                params['VideoStreamIndex'] = video['Index']
                source_url = origin + f'/Videos/{item_id}/main.m3u8?' + urlencode(params)
                raw = self.upstream(source_url, token, 2 * 1024 * 1024)
                playlist, resources = self.parse(item, source_url, raw)
                self.cache.debug.emit('INFO', 'vod_playlist_ready', key, segments=len(resources), duration=item['duration'])
                item.update(playlist=playlist, segments=resources, type='application/vnd.apple.mpegurl',
                            ext='.m3u8', validator='', playlist_url=source_url)
                with self.cache.lock:
                    if sum(len(i.get('segments', [])) for i in self.cache.items.values()) + len(resources) > 20000:
                        raise HLSError('播放列表总分片数达到上限，请删除旧 HLS 链接')
                    if len(json.dumps(self.cache.items)) + len(json.dumps(item)) > 24 * 1024 * 1024:
                        raise HLSError('播放列表元数据达到上限，请删除旧 HLS 链接')
                    self.cache.items[key] = item
                    try:
                        self.cache.save()
                    except Exception:
                        self.cache.items.pop(key, None)
                        raise
                return key
            except (KeyError, TypeError, ValueError):
                raise HLSError('Jellyfin 媒体信息格式不兼容，请检查服务器版本') from None

    def filename(self, key, index):
        return f'{key}.hls.{index}.bin'

    def ensure_segment(self, key, index, background=False):
        name = self.filename(key, index)
        with self.cache.fetch_lock.background() if background else self.cache.fetch_lock:
            with self.cache.lock:
                item = self.cache.items.get(key)
                if not item or item.get('mode') != 'hls' or not 0 <= index < len(item['segments']):
                    raise HLSError('播放链接或分片不存在')
                if name in self.cache.entries:
                    return False
                item = dict(item)
            start = time.monotonic()
            self.cache.debug.emit('INFO', 'segment_cache_miss', key, segment=index,
                                  position_seconds=item['segments'][index]['start'])
            self.active[key] = time.monotonic()
            self.cache.session_state[key] = '可能正在转码'
            data = self.upstream(item['segments'][index]['url'], self.token(item), 16 * 1024 * 1024)
            # A TS stream consists of 188-byte packets. Reject HTML/JSON errors before caching.
            if len(data) < 188 or len(data) % 188 or any(data[n] != 0x47 for n in range(0, min(len(data), 188 * 5), 188)):
                raise HLSError('Jellyfin 未返回有效的 MPEG-TS 视频分片')
            with self.cache.lock:
                self.cache.evict(len(data))
                path = self.cache.root / name
                temporary = path.with_suffix('.part')
                try:
                    temporary.write_bytes(data)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
                self.cache.entries[name] = len(data)
                self.cache.used += len(data)
                self.cache.misses += 1
                self.cache.upstream_bytes += len(data)
            self.cache.metrics.fetched(key, time.monotonic() - start)
            self.cache.debug.emit('INFO', 'segment_cached', key, segment=index, bytes=len(data),
                                  seconds=round(time.monotonic() - start, 3))
            self.active[key] = time.monotonic()
            return True

    @contextlib.contextmanager
    def segment(self, key, index, background=False):
        name = self.filename(key, index)
        downloaded = False
        # Cached viewers never wait behind a slow NAS encoder. Pin while streaming from disk.
        while True:
            with self.cache.lock:
                if key not in self.cache.items:
                    raise HLSError('播放链接不存在')
                if name in self.cache.entries:
                    file = (self.cache.root / name).open('rb')
                    length = self.cache.entries[name]
                    self.cache.pins[name] += 1
                    self.cache.entries.move_to_end(name)
                    os.utime(self.cache.root / name, None)
                    if not downloaded:
                        self.cache.hits += 1
                        self.cache.debug.emit('DEBUG', 'segment_cache_hit', key, segment=index, bytes=length)
                    break
            downloaded = self.ensure_segment(key, index, background=background) or downloaded
        try:
            yield file, length
        finally:
            file.close()
            with self.cache.lock:
                self.cache.pins[name] -= 1
                if not self.cache.pins[name]:
                    del self.cache.pins[name]

    def stop(self, key):
        item = self.cache.items.get(key)
        if not item or item.get('mode') != 'hls':
            return
        params = urlencode({'DeviceId': item['device_id'], 'PlaySessionId': item['session_id']})
        self.upstream(item['origin'] + '/Videos/ActiveEncodings?' + params, self.token(item), 65536, 'DELETE')
        self.active.pop(key, None)
        self.cache.session_state[key] = '已请求停止，缺片时恢复'
        self.cache.debug.emit('INFO', 'own_encoder_stop_requested', key)

    def reap(self):
        # Do not stop an encoder during an in-flight fetch. Stale restart sessions are also reaped.
        if not self.cache.fetch_lock.acquire(blocking=False):
            return
        try:
            for key, timestamp in list(self.active.items()):
                if time.monotonic() - timestamp > self.idle:
                    try:
                        self.stop(key)
                    except HLSError as error:
                        self.cache.metrics.error(key, str(error))
                        self.active[key] = time.monotonic()  # bounded retry interval
                        self.cache.session_state[key] = '停止请求失败，将重试'
        finally:
            self.cache.fetch_lock.release()
