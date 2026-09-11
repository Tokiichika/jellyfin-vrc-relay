"""Shared Jellyfin HLS VOD / byte-range relay. Python standard library only."""
import collections
import hashlib
import html
import hmac
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from hls import HLS, HLSError
from metrics import Metrics
from diagnostics import Diagnostics
from throttle import Bandwidth
from preload import FetchGate, Preloader
from bili import Bili, BiliError
from system_stats import SystemStats
from settings import Settings, SettingsError, BW_KEYS
from nas_config import NASConfig


class RelayError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def byte_range(value, size):
    if not value:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not any(match.groups()):
        raise ValueError("unsupported range")
    left, right = match.groups()
    if not left:
        length = int(right)
        if length == 0:
            raise ValueError("empty suffix")
        return max(0, size - length), size - 1, True
    start = int(left)
    end = min(int(right), size - 1) if right else size - 1
    if start >= size or end < start:
        raise ValueError("unsatisfiable range")
    return start, end, True


class Cache:
    def __init__(self, root, budget, chunk, hosts, allow_http=False):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget, self.chunk = budget, chunk
        if not 64 * 1024 <= chunk <= 8 * 1024 * 1024 or budget < chunk:
            raise ValueError("Invalid cache/chunk limits")
        self.hosts, self.allow_http = set(hosts), allow_http
        self.lock = threading.RLock()
        self.pins = collections.Counter()
        self.metrics = Metrics()
        self.debug = Diagnostics()
        self.session_state = {}
        # One upstream download at a time: concurrent misses share the committed result.
        self.fetch_lock = FetchGate()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.items = {}
        self.entries = collections.OrderedDict()
        self.used = self.hits = self.misses = self.upstream_bytes = 0
        self.meta = self.root / "items.json"
        config_path = self.root / "cache-config.json"
        if config_path.exists():
            if json.loads(config_path.read_text())["chunk_bytes"] != chunk:
                raise ValueError("CHUNK_BYTES cannot change for an existing cache volume")
        else:
            config_path.write_text(json.dumps({"chunk_bytes": chunk}))
        if self.meta.exists():
            self.items = json.loads(self.meta.read_text(encoding="utf-8"))
        for path in self.root.glob("*.part"):
            path.unlink()
        for path in sorted(self.root.glob("*.bin"), key=lambda p: p.stat().st_mtime):
            self.entries[path.name] = path.stat().st_size
            self.used += self.entries[path.name]
        self.bandwidth = Bandwidth(self.root)
        self.settings = Settings(os.environ.get('SETTINGS_ENV_PATH', str(self.root / '.env')), self.bandwidth.config, budget)
        self.budget = self.settings.values['CACHE_MAX_BYTES']
        if self.budget < self.chunk:
            raise SettingsError('缓存上限不能小于缓存块大小')
        self.evict(0)
        self.hls = HLS(self, int(os.environ.get('HLS_TIMEOUT_SECONDS', '90')),
                       int(os.environ.get('HLS_IDLE_SECONDS', '120')))
        # After a restart stop any old own sessions on the next idle sweep.
        self.hls.active = {k: time.monotonic() for k, v in self.items.items() if v.get('mode') == 'hls'}
        self.preloader = Preloader(self)
        self.bili = Bili(self)
        self.system_stats = SystemStats()
        self.system_stats.snapshot()
        self.nas_config = NASConfig(self)
        self.update_settings({})  # migrate defaults/legacy bandwidth into .env once
        self.debug.emit('INFO', 'startup', force=True, version='2.6.0', cache_bytes=self.used,
                        cache_limit=self.budget, links=len(self.items), chunk_bytes=self.chunk)

    def validate_url(self, url):
        try:
            p = urlsplit(url)
            valid = (p.scheme == "https" or self.allow_http and p.scheme == "http")
            valid = valid and p.hostname in self.hosts and not p.username and not p.password
            valid = valid and not p.fragment and (self.allow_http or p.port in (None, 443))
            valid = valid and re.fullmatch(r"/Items/[a-fA-F0-9]{32}/Download", p.path)
            if not valid or len(url) > 8192:
                raise ValueError()
        except ValueError:
            raise RelayError("仅支持允许的 Jellyfin 主机上的 HTTPS /Items/ID/Download 地址") from None

    def update_settings(self, patch, revision=None):
        with self.settings.lock:
            values = self.settings.prepare(patch)
            self.nas_config.validate_origin(values)
            bw = {key: values[env] for key, env in BW_KEYS.items()}
            with self.lock:
                limit = values['CACHE_MAX_BYTES']
                if limit < self.chunk:
                    raise SettingsError('缓存上限不能小于缓存块大小')
                protected = sum(size for name, size in self.entries.items() if self.pins[name])
                if protected > limit:
                    raise SettingsError('正在读取或预载的缓存超过新上限，请稍后重试或先取消预载')
                self.bandwidth.configure(bw, persist=lambda: self.settings.save(values, revision))
                self.budget = limit
                self.evict(0)
            self.hls.timeout = int(values['HLS_TIMEOUT_SECONDS'])
            self.hls.idle = int(values['HLS_IDLE_SECONDS'])
            self.debug.set_enabled(values['DEBUG_ENABLED'])
            return self.settings.snapshot()

    def open_range(self, item, start, end, key=None):
        if item.get('source') == 'bilibili':
            return self.bili.open_range(key, item, start, end)
        self.validate_url(item["url"])
        headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity",
                   "User-Agent": "Jellyfin-VRC-Relay/1.0"}
        if item.get("validator"):
            headers["If-Range"] = item["validator"]
        try:
            response = self.opener.open(Request(item["url"], headers=headers), timeout=30)
        except Exception as error:
            self.debug.exception('file_origin_open_failed', error)
            raise RelayError("回源失败：请检查链接、凭据、网络或重定向配置") from None
        if response.status != 206 or response.headers.get("Content-Encoding", "identity") != "identity":
            response.close()
            raise RelayError("源站未返回原始字节范围，或文件已变更；请重新创建链接")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
        if not match or tuple(map(int, match.groups()[:2])) != (start, end):
            response.close()
            raise RelayError("源站 Content-Range 不符合请求")
        total = int(match.group(3))
        if total <= end or (item.get("size") and total != item["size"]):
            response.close()
            raise RelayError("源文件大小已变化，请重新创建链接")
        if item.get("validator_header"):
            if response.headers.get(item["validator_header"]) != item["validator"]:
                response.close()
                raise RelayError("源文件版本已变化，请重新创建链接")
        return response, total

    def save(self):
        path = self.root / "items.json.part"
        path.write_text(json.dumps(self.items, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)
        path.replace(self.meta)

    def create(self, url, title):
        self.validate_url(url)
        with self.fetch_lock:
            with self.lock:
                for key, item in self.items.items():
                    if item.get('mode', 'file') == 'file' and item["url"] == url:
                        return key
                if len(self.items) >= 100:
                    raise RelayError("最多保留 100 个链接，请先删除旧链接")
            with self.open_range({"url": url}, 0, 0)[0] as response:
                size = int(response.headers["Content-Range"].rsplit("/", 1)[1])
                if len(response.read(2)) != 1:
                    raise RelayError("源站范围响应长度错误")
                content_type = response.headers.get_content_type()
                if content_type in ("text/html", "application/json", "application/vnd.apple.mpegurl", "application/x-mpegURL"):
                    raise RelayError("源地址不是可缓存的视频文件")
                etag = response.headers.get("ETag", "")
                modified = response.headers.get("Last-Modified", "")
                validator = etag if etag and not etag.startswith("W/") else modified
                header = "ETag" if validator and validator == etag else "Last-Modified" if validator else ""
                disposition = response.headers.get("Content-Disposition", "")
                extension = re.search(r"\.(mp4|m4v|mkv|webm|avi|mov|ts)(?:[\"';\s]|$)", disposition, re.I)
                ext = "." + extension.group(1).lower() if extension else mimetypes.guess_extension(content_type) or ".bin"
            key = secrets.token_urlsafe(24)
            item = {"url": url, "title": title[:120] or "视频", "size": size,
                    "type": content_type, "ext": ext, "validator": validator,
                    "validator_header": header, "created": int(time.time())}
            with self.lock:
                self.items[key] = item
                self.upstream_bytes += 1
                self.metrics.add('download', 1)
                self.save()
            return key

    def evict(self, reserve):
        if reserve > self.budget:
            raise RelayError('单个分片超过缓存上限')
        for name in list(self.entries):
            if self.used + reserve <= self.budget:
                break
            if self.pins[name]:
                continue
            length = self.entries[name]
            (self.root / name).unlink(missing_ok=True)
            self.entries.pop(name)
            self.used -= length
            self.debug.emit('DEBUG', 'cache_evict', name.split('.')[0], bytes=length)
        if self.used + reserve > self.budget:
            raise RelayError('缓存正在使用中，暂时无法腾出足够空间，请稍后重试')

    def cached_chunk(self, key, name):
        with self.lock:
            if key not in self.items:
                raise RelayError('播放链接已删除')
            if name in self.entries:
                self.entries.move_to_end(name)
                self.hits += 1
                self.debug.emit('DEBUG', 'file_cache_hit', key, chunk=name.split('.')[-2])
                path = self.root / name
                os.utime(path, None)
                return path.read_bytes()
        return None

    def get_chunk(self, key, index):
        name = f"{key}.{index}.bin"
        cached = self.cached_chunk(key, name)
        if cached is not None:
            return cached
        with self.fetch_lock:
            cached = self.cached_chunk(key, name)
            if cached is not None:
                return cached
            with self.lock:
                if key not in self.items:
                    raise RelayError("播放链接已删除")
                item = dict(self.items[key])
                if name in self.entries:
                    self.entries.move_to_end(name)
                    self.hits += 1
                    path = self.root / name
                    os.utime(path, None)
                    return path.read_bytes()
            start = index * self.chunk
            end = min(start + self.chunk, item["size"]) - 1
            self.debug.emit('INFO', 'file_origin_fetch', key, start=start, end=end)
            with self.open_range(item, start, end, key=key)[0] as response:
                data = response.read(end - start + 2)
                self.metrics.add('download', len(data))
            if len(data) != end - start + 1:
                raise RelayError("回源数据不完整，请重试")
            with self.lock:
                self.evict(len(data))
                path = self.root / name
                temporary = path.with_suffix(".part")
                try:
                    temporary.write_bytes(data)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
                self.entries[name] = len(data)
                self.used += len(data)
                self.misses += 1
                self.upstream_bytes += len(data)
            return data

    def delete(self, key):
        with self.fetch_lock:
            with self.lock:
                if any(count for name, count in self.pins.items() if name.startswith(key + '.')):
                    raise RelayError('该视频正在传输分片，请停止播放后重试删除')
            if key in self.hls.active:
                self.hls.stop(key)
            self.clear_item(key)

    def clear_item(self, key):
        with self.lock:
            self.items.pop(key, None)
            for name in list(self.entries):
                if name.startswith(key + "."):
                    (self.root / name).unlink(missing_ok=True)
                    self.used -= self.entries.pop(name)
            self.metrics.forget(key)
            self.session_state.pop(key, None)
            self.save()
            self.debug.emit('INFO', 'item_deleted', key)

    def status(self, base):
        metrics = self.metrics.snapshot()
        system = self.system_stats.snapshot()
        defaults = {k: v for k, v in self.settings.snapshot()['values'].items() if k.startswith('DEFAULT_') or k == 'REFRESH_SECONDS'}
        with self.lock:
            items = []
            for key, item in self.items.items():
                mode = item.get('mode', 'file')
                names = {name: length for name, length in self.entries.items() if name.startswith(key + '.')}
                entry = {"id": key, "title": item['title'], "size": item['size'], 'mode': mode,
                         'type': item['type'], 'cache_bytes': sum(names.values()),
                         'version_checked': bool(item.get('validator')),
                         'play_url': f"{base}/v/{key}/video{item['ext']}",
                         **metrics['per_item'].get(key, {'active_clients': 0})}
                if item.get('source') == 'bilibili':
                    entry.update({k: item.get(k) for k in ('source', 'bvid', 'page', 'quality',
                        'duration', 'deadline', 'source_video', 'source_audio')})
                if mode == 'hls':
                    entry.update({k: item.get(k) for k in ('duration', 'source_video', 'source_audio',
                                 'source_resolution', 'output_video', 'output_audio', 'height', 'bitrate',
                                 'audio_bitrate', 'transcode_video', 'transcode_audio',
                                 'subtitle_index', 'subtitle_title', 'subtitles')})
                    entry['preload'] = self.preloader.snapshot(key)
                    entry.update(segments_total=len(item['segments']),
                                 segments_cached=sum(self.hls.filename(key, n) in names for n in range(len(item['segments']))),
                                 cached_seconds=sum(s['duration'] for n, s in enumerate(item['segments']) if self.hls.filename(key, n) in names),
                                 session_state=self.session_state.get(key, '按需生成'))
                items.append(entry)
            return {"version": '2.6.0', 'defaults': defaults, 'system': system, 'bandwidth': self.bandwidth.snapshot(), "cache_bytes": self.used, "cache_limit": self.budget,
                    "hits": self.hits, "misses": self.misses, "upstream_bytes": self.upstream_bytes,
                    'metrics': {k: v for k, v in metrics.items() if k != 'per_item'},
                    'disk_free_bytes': shutil.disk_usage(self.root).free, "items": items}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, cache, token, base, clients=24):
        self.cache, self.token, self.base = cache, token, base
        self.trusted = [ipaddress.ip_network(v.strip()) for v in os.environ.get(
            'TRUSTED_PROXY_CIDRS', '127.0.0.0/8,::1/128,172.16.0.0/12').split(',') if v.strip()]
        self.slots = threading.BoundedSemaphore(clients)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.cache.debug.emit('WARN', 'http_connection_limit')
            try:
                request.settimeout(2)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 5\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            request.settimeout(45)
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if error is not None:
            self.cache.debug.exception('http_worker_error', error)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VRCRelay/2.4"

    def send_response(self, code, message=None):
        self.response_status = code
        super().send_response(code, message)

    def log_message(self, *args):
        pass  # URLs and credentials must never reach access logs.

    def reply(self, status, body, content_type="application/json; charset=utf-8", media=False):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            if media:
                self.media_write(data)
            else:
                self.wfile.write(data)

    def authorized(self):
        provided = self.headers.get("Authorization", "")
        return hmac.compare_digest(provided.encode(), ("Bearer " + self.server.token).encode())

    def dispatch(self):
        self.close_connection = True
        path = urlsplit(self.path).path
        if path == "/health" and self.command in ("GET", "HEAD"):
            return self.reply(200, {"ok": True})
        if path in ('/ui.css', '/ui.js', '/settings-app.js') and self.command in ('GET', 'HEAD'):
            asset = Path(__file__).with_name(path[1:])
            content_type = 'text/css; charset=utf-8' if path.endswith('.css') else 'text/javascript; charset=utf-8'
            return self.reply(200, asset.read_bytes(), content_type)
        if path in ('/', '/settings') and self.command in ("GET", "HEAD"):
            name = 'index.html'
            page = Path(__file__).with_name(name).read_text(encoding='utf-8')
            values = self.server.cache.settings.snapshot()['values']
            page = page.replace('__FOOTER_ICP__', html.escape(values['FOOTER_ICP']))
            page = page.replace('__FOOTER_NOTICE__', html.escape(values['FOOTER_NOTICE']).replace('\n', '<br>'))
            return self.reply(200, page.encode(), "text/html; charset=utf-8")
        if path.startswith("/api/"):
            if not self.authorized():
                return self.reply(401, {"error": "管理密钥不正确"})
            if path in ('/api/settings', '/api/nas-config'):
                cache = self.server.cache
                if self.command == 'GET':
                    result = cache.settings.snapshot() if path == '/api/settings' else cache.nas_config.public(cache.nas_config.read(dict(cache.settings.values)))
                    return self.reply(200, result)
                if self.command == 'POST':
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 12000 or self.headers.get('Transfer-Encoding'):
                        return self.reply(400, {'error': '配置请求过大或无效'})
                    body = json.loads(self.rfile.read(length))
                    if path == '/api/settings':
                        if not isinstance(body.get('revision'), str):
                            raise SettingsError('请先读取设置后再保存')
                        return self.reply(200, cache.update_settings(body.get('values'), body['revision']))
                    with cache.nas_config.lock:
                        with cache.settings.lock:
                            if body.get('revision') != cache.settings.revision():
                                raise SettingsError('设置已改变，请重新加载后应用 NAS 配置')
                            values = dict(cache.settings.values)
                        return self.reply(200, cache.nas_config.apply(values))
            preload = re.fullmatch(r'/api/items/([A-Za-z0-9_-]{32})/preload', path)
            if self.command == 'POST' and (path in ('/api/bandwidth', '/api/subtitles') or preload):
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 12000 or self.headers.get('Transfer-Encoding'):
                    return self.reply(400, {'error': '请求大小无效'})
                body = json.loads(self.rfile.read(length))
                if path == '/api/bandwidth':
                    try:
                        if set(body) != set(BW_KEYS):
                            raise SettingsError('带宽配置格式错误')
                        self.server.cache.update_settings({BW_KEYS[k]: v for k, v in body.items()})
                        result = self.server.cache.bandwidth.snapshot()
                    except ValueError as error:
                        return self.reply(400, {'error': str(error)})
                    return self.reply(200, result)
                if path == '/api/subtitles':
                    if body.get('id'):
                        with self.server.cache.lock:
                            item = self.server.cache.items.get(body['id'])
                            if not item:
                                return self.reply(404, {'error': '播放链接不存在'})
                            url = item['url']
                    else:
                        url = body.get('url', '')
                    return self.reply(200, {'subtitles': self.server.cache.hls.subtitles(url)})
                key = preload.group(1)
                result = (self.server.cache.preloader.cancel(key) if body.get('cancel') is True
                          else self.server.cache.preloader.start(key, body.get('percent', self.server.cache.settings.values['DEFAULT_PRELOAD_PERCENT'])))
                return self.reply(200, result)
            if path == '/api/debug':
                if self.command == 'GET':
                    after = int(parse_qs(urlsplit(self.path).query).get('after', ['0'])[0])
                    return self.reply(200, self.server.cache.debug.snapshot(after))
                if self.command == 'POST':
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 256 or self.headers.get('Transfer-Encoding'):
                        return self.reply(400, {'error': '调试设置请求格式错误'})
                    enabled = json.loads(self.rfile.read(length)).get('enabled')
                    if type(enabled) is not bool:
                        return self.reply(400, {'error': 'enabled 必须是布尔值'})
                    self.server.cache.update_settings({'DEBUG_ENABLED': enabled})
                    return self.reply(200, self.server.cache.debug.snapshot())
            if path == "/api/items" and self.command == "GET":
                return self.reply(200, self.server.cache.status(self.server.base))
            variant = re.fullmatch(r'/api/items/([A-Za-z0-9_-]{32})/variant', path)
            if (path == "/api/items" or variant) and self.command == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 12000 or self.headers.get("Transfer-Encoding"):
                    return self.reply(400, {"error": "请求体大小不正确"})
                body = json.loads(self.rfile.read(length))
                url, title = body.get("url"), body.get("title", "")
                if variant:
                    with self.server.cache.lock:
                        source = self.server.cache.items.get(variant.group(1))
                        if not source:
                            return self.reply(404, {'error': '原播放链接不存在'})
                        url = source['url']
                if not isinstance(url, str) or not isinstance(title, str):
                    return self.reply(400, {"error": "URL 和名称必须是文本"})
                mode = body.get('mode', 'file')  # preserve v1 API behavior; UI defaults to HLS
                if mode == 'hls':
                    defaults = self.server.cache.settings.values
                    key = self.server.cache.hls.create(url.strip(), title.strip(),
                        body.get('height', defaults['DEFAULT_HEIGHT']), body.get('bitrate', defaults['DEFAULT_VIDEO_BITRATE']),
                        body.get('transcode_video', defaults['DEFAULT_TRANSCODE_VIDEO']), body.get('transcode_audio', defaults['DEFAULT_TRANSCODE_AUDIO']),
                        body.get('audio_bitrate', defaults['DEFAULT_AUDIO_BITRATE']), body.get('subtitle_index', -1))
                elif mode == 'file':
                    key = self.server.cache.create(url.strip(), title.strip())
                elif mode == 'bilibili':
                    key = self.server.cache.bili.create(url.strip(), title.strip())
                else:
                    return self.reply(400, {'error': '未知播放模式'})
                return self.reply(201, {"id": key})
            match = re.fullmatch(r'/api/items/([A-Za-z0-9_-]{32})/stop', path)
            if match and self.command == 'POST':
                with self.server.cache.fetch_lock:
                    self.server.cache.hls.stop(match.group(1))
                return self.reply(200, {'ok': True})
            match = re.fullmatch(r"/api/items/([A-Za-z0-9_-]{32})", path)
            if match and self.command == "DELETE":
                self.server.cache.preloader.cancel(match.group(1))
                self.server.cache.delete(match.group(1))
                with self.server.cache.lock:
                    self.server.cache.preloader.jobs.pop(match.group(1), None)
                return self.reply(200, {"ok": True})
            return self.reply(404, {"error": "接口不存在"})
        match = re.fullmatch(r'/v/([A-Za-z0-9_-]{32})/(video\.[a-zA-Z0-9]+|segment/(\d+)\.ts)', path)
        if match and self.command in ("GET", "HEAD"):
            key = match.group(1)
            with self.server.cache.lock:
                item = self.server.cache.items.get(key)
                if not item:
                    return self.reply(404, {'error': '播放链接不存在'})
                is_hls = item.get('mode') == 'hls'
            if bool(match.group(3)) and not is_hls:
                return self.reply(404, {'error': '分片不存在'})
            self.media_key = key
            self.limit_identity = self.server.cache.metrics.identity(self.client_address[0],
                self.headers.get('X-Real-IP'), '', self.server.trusted)
            if not self.server.cache.bandwidth.admit(self.limit_identity):
                self.server.cache.debug.emit('WARN', 'viewer_capacity_rejected', key)
                return self.reply(503, {'error': '当前观看容量已满，请稍后重试；已有观众优先'})
            self.server.cache.debug.emit('DEBUG', 'media_request', key, method=self.command,
                kind='hls_segment' if match.group(3) is not None else 'playlist' if is_hls else 'file',
                segment=int(match.group(3)) if match.group(3) is not None else None,
                range_requested=bool(self.headers.get('Range')),
                byte_range=self.headers.get('Range') if re.fullmatch(r'bytes=[0-9-]{1,60}', self.headers.get('Range', '')) else None)
            self.viewer = self.server.cache.metrics.identity(self.client_address[0],
                          self.headers.get('X-Real-IP'), self.headers.get('User-Agent', ''), self.server.trusted)
            if self.command == 'GET':
                self.server.cache.metrics.begin(self.viewer, key)
            try:
                if is_hls:
                    return self.hls_video(key, int(match.group(3)) if match.group(3) is not None else None)
                return self.video(key)
            finally:
                self.server.cache.bandwidth.release(self.limit_identity)
                if getattr(self, 'throttled', False):
                    self.server.cache.bandwidth.end(self.limit_identity)
                    self.throttled = False
                if self.command == 'GET':
                    self.server.cache.metrics.end()
        return self.reply(404, {"error": "地址不存在"})

    def media_write(self, data):
        if not getattr(self, 'throttled', False):
            self.limit_identity = self.server.cache.metrics.identity(self.client_address[0],
                self.headers.get('X-Real-IP'), '', self.server.trusted)
            self.server.cache.bandwidth.begin(self.limit_identity)
            self.throttled = True
        for offset in range(0, len(data), 65536):
            part = data[offset:offset + 65536]
            self.server.cache.bandwidth.acquire(self.limit_identity, len(part))
            self.wfile.write(part)
            self.wfile.flush()
            self.media_bytes += len(part)
            if self.first_byte is None:
                self.first_byte = round(time.monotonic() - self.request_started, 3)
            self.server.cache.metrics.add('upload', len(part))
            self.server.cache.metrics.touch(self.viewer, self.media_key)

    def hls_video(self, key, index):
        cache = self.server.cache
        if index is None:
            with cache.lock:
                body = cache.items[key]['playlist'].encode()
            self.reply(200, body, 'application/vnd.apple.mpegurl', media=True)
            return
        with cache.lock:
            if index >= len(cache.items[key]['segments']):
                return self.reply(404, {'error': '分片不存在'})
        with cache.hls.segment(key, index) as (file, size):
            try:
                start, end, partial = byte_range(self.headers.get('Range') if self.command == 'GET' else None, size)
            except ValueError:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.send_header('Connection', 'close')
                self.end_headers()
                return
            self.send_response(206 if partial else 200)
            self.send_header('Content-Type', 'video/mp2t')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Cache-Control', 'private, no-store')
            self.send_header('Connection', 'close')
            if partial:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            if self.command == 'HEAD':
                return
            file.seek(start)
            try:
                while start <= end:
                    data = file.read(min(65536, end - start + 1))
                    if not data:
                        break
                    self.media_write(data)
                    start += len(data)
            except OSError:
                self.media_aborted = True
                self.close_connection = True

    def video(self, key):
        cache = self.server.cache
        with cache.lock:
            item = dict(cache.items.get(key, {}))
        if not item:
            return self.reply(404, {"error": "播放链接不存在"})
        size = item["size"]
        tag = '"' + hashlib.sha256((key + str(size)).encode()).hexdigest() + '"'
        value = self.headers.get("Range") if self.command == "GET" else None
        if self.headers.get("If-Range") not in (None, tag):
            value = None
        try:
            start, end, partial = byte_range(value, size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        # Fetch before committing headers, so initial upstream failures are proper 502s.
        data = cache.get_chunk(key, start // cache.chunk) if self.command == "GET" else None
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", item["type"])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", tag)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Connection", "close")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            while start <= end:
                index, offset = divmod(start, cache.chunk)
                if data is None:
                    data = cache.get_chunk(key, index)
                length = min(len(data) - offset, end - start + 1)
                self.media_write(data[offset:offset + length])
                start += length
                data = None
        except (RelayError, BiliError) as error:
            self.media_aborted = True
            self.server.cache.metrics.error(key, str(error))
            self.close_connection = True
        except OSError:
            self.media_aborted = True
            # Never append JSON or a second HTTP response to a partial video body.
            self.close_connection = True

    def run_request(self):
        self.media_key = ''
        self.request_started = time.monotonic()
        self.media_bytes, self.first_byte, self.media_aborted = 0, None, False
        self.response_status = None
        try:
            self.dispatch()
        except SettingsError as error:
            self.reply(400, {'error': str(error)})
        except (RelayError, HLSError, BiliError) as exc:
            self.server.cache.metrics.error(getattr(self, 'media_key', ''), str(exc))
            self.server.cache.debug.exception('request_failed', exc, self.media_key)
            self.server.cache.debug.emit('ERROR', 'request_error_detail', self.media_key, message=str(exc))
            self.reply(502, {"error": str(exc)})
        except (ValueError, TypeError, AttributeError) as error:
            self.server.cache.debug.exception('invalid_request', error, self.media_key)
            self.reply(400, {"error": "请求格式错误"})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.media_aborted = True
            self.server.cache.debug.emit('DEBUG', 'client_disconnected_or_timeout', self.media_key)
            self.close_connection = True
        except OSError as error:
            self.server.cache.debug.exception('storage_or_network_error', error, self.media_key)
            self.reply(503, {"error": "存储或网络暂不可用"})
        except Exception as error:
            self.server.cache.debug.exception('unexpected_error', error, self.media_key)
            self.server.cache.metrics.error(self.media_key, '服务内部异常，请查看调试日志中的类型和代码位置')
            self.reply(500, {'error': '服务内部异常，请查看调试日志中的类型和代码位置'})
        finally:
            if self.media_key:
                self.server.cache.debug.emit('INFO', 'media_response_finished', self.media_key,
                    status=self.response_status, bytes=self.media_bytes, first_byte_seconds=self.first_byte,
                    seconds=round(time.monotonic() - self.request_started, 3), aborted=self.media_aborted)

    do_GET = do_HEAD = do_POST = do_DELETE = run_request


if __name__ == "__main__":
    os.umask(0o077)
    token = os.environ.get("ADMIN_TOKEN", "")
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if len(token) < 32 or token.startswith("replace-"):
        raise SystemExit("Set ADMIN_TOKEN to a random secret of at least 32 characters")
    public = urlsplit(base)
    if public.scheme != "https" or not public.netloc or public.path or public.query or public.fragment or public.username:
        raise SystemExit("Set PUBLIC_BASE_URL to your public HTTPS origin")
    cache = Cache(os.environ.get("DATA_DIR", "./data"),
                  int(os.environ.get("CACHE_MAX_BYTES", "9000000000")),
                  int(os.environ.get("CHUNK_BYTES", "2097152")),
                  [h.strip() for h in os.environ.get("UPSTREAM_HOSTS", "jellyfin.example.com").split(",")])
    print("Relay ready; access logging disabled", flush=True)
    server = Server(("0.0.0.0", int(os.environ.get("PORT", "8080"))), cache, token, base,
                    int(os.environ.get("MAX_CLIENTS", "24")))
    def maintenance():
        while True:
            time.sleep(15)
            cache.hls.reap()
    threading.Thread(target=maintenance, daemon=True).start()
    server.serve_forever()
