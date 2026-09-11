"""Server-side public Bilibili MP4 resolution and strict CDN range retrieval."""
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit, urljoin
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener
from mp4 import inspect, MP4Error


class BiliError(Exception):
    pass


def public_connection(address, timeout=30, source_address=None):
    answers = socket.getaddrinfo(*address, type=socket.SOCK_STREAM)
    if not answers or any(not ipaddress.ip_address(a[4][0]).is_global for a in answers):
        raise BiliError('拒绝访问非公网 CDN 地址')
    # Connect to the addresses just checked; do not resolve the hostname again.
    deadline = time.monotonic() + timeout
    for family, kind, protocol, _, sockaddr in answers[:8]:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(max(.1, deadline - time.monotonic()))
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError:
            sock.close()
            if time.monotonic() >= deadline:
                break
    raise BiliError('无法连接 B 站 CDN，请检查服务器网络')


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = public_connection


class PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(PublicHTTPSConnection, request, context=ssl.create_default_context())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Bili:
    def __init__(self, cache):
        self.cache = cache
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), PublicHTTPSHandler())

    @staticmethod
    def video(value):
        if not isinstance(value, str) or len(value) > 12000:
            raise BiliError('B 站分享内容过长或格式无效')
        value = value.strip()
        links = []
        for candidate in re.findall(r'https://[^\s<>"\'【】（）。，！？、]+', value):
            candidate = candidate.rstrip(').,;!?]')
            try:
                parsed = urlsplit(candidate)
                if parsed.hostname in ('www.bilibili.com', 'bilibili.com', 'm.bilibili.com') and parsed.path.startswith('/video/'):
                    links.append(candidate)
            except ValueError:
                continue
        if len(links) > 1:
            raise BiliError('分享内容中包含多个 B 站视频链接，请一次只粘贴一个')
        if links:
            value = links[0]
        if re.fullmatch(r'BV[0-9A-Za-z]{10}', value):
            return value, 1
        p = urlsplit(value)
        if p.scheme != 'https' or p.hostname not in ('www.bilibili.com', 'bilibili.com', 'm.bilibili.com') or p.username or p.password or p.port not in (None, 443):
            raise BiliError('请输入 BV 号或 https://www.bilibili.com/video/BV… 链接')
        match = re.fullmatch(r'/video/(BV[0-9A-Za-z]{10})/?', p.path)
        if not match:
            raise BiliError('需要包含 BV 号的视频链接，不支持直播或短链接')
        page = parse_qs(p.query).get('p', ['1'])[0]
        if not page.isdigit() or not 1 <= int(page) <= 1000:
            raise BiliError('分 P 编号无效')
        return match.group(1), int(page)

    @staticmethod
    def cdn(value):
        p = urlsplit(value)
        if (len(value) > 8192 or p.scheme != 'https' or p.username or p.password or p.fragment
                or p.port not in (None, 443) or not p.hostname
                or not any(p.hostname.endswith('.' + d) for d in ('bilivideo.com', 'bilivideo.cn'))
                or not p.path.lower().endswith('.mp4')):
            raise BiliError('仅接受 bilivideo.com / bilivideo.cn 的 HTTPS MP4 直链')
        return value

    @staticmethod
    def deadline(url):
        value = parse_qs(urlsplit(url).query).get('deadline', [''])[0]
        return int(value) if value.isdigit() and len(value) <= 12 else None

    def request(self, url, headers=None, api=False):
        for _ in range(4):
            if api:
                p = urlsplit(url)
                if p.scheme != 'https' or p.netloc != 'api.bilibili.com' or p.path not in ('/x/player/pagelist', '/x/player/playurl'):
                    raise BiliError('解析接口地址无效')
            else:
                self.cdn(url)
            try:
                return self.opener.open(Request(url, headers={'User-Agent': 'Mozilla/5.0',
                    'Referer': 'https://www.bilibili.com/', 'Accept-Encoding': 'identity', **(headers or {})}), timeout=20)
            except HTTPError as error:
                if not api and error.code in (301, 302, 303, 307, 308):
                    target = error.headers.get('Location', '')
                    error.close()
                    if not target:
                        raise BiliError('CDN 重定向缺少目标') from None
                    url = urljoin(url, target)
                    continue
                raise
        raise BiliError('CDN 重定向次数过多')

    def json(self, path, params):
        with self.request('https://api.bilibili.com' + path + '?' + urlencode(params), api=True) as response:
            data = response.read(2 * 1024 * 1024 + 1)
            self.cache.metrics.add('download', len(data))
            if len(data) > 2 * 1024 * 1024:
                raise BiliError('B 站元数据过大')
            value = json.loads(data)
        if value.get('code') != 0:
            code = value.get('code')
            safe_code = code if type(code) is int else 'unknown'
            raise BiliError(f'B 站解析失败（代码 {safe_code}）；可能需要登录、视频不可用或服务器请求受限')
        return value.get('data')

    def resolve(self, bvid, page, quality=116):
        pages = self.json('/x/player/pagelist', {'bvid': bvid})
        if not isinstance(pages, list) or not 1 <= page <= len(pages):
            raise BiliError('分 P 不存在或视频不可用')
        entry = pages[page - 1]
        data = self.json('/x/player/playurl', {'bvid': bvid, 'cid': entry['cid'], 'qn': quality,
            'type': '', 'otype': 'json', 'platform': 'html5', 'high_quality': 1})
        urls = data.get('durl') or []
        if len(urls) != 1:
            raise BiliError('接口未返回单文件 MP4；不支持 DASH 音视频分离、多段或受限视频')
        url = urls[0]['url']
        if url.startswith('http://'):
            url = 'https://' + url[7:]
        self.cdn(url)
        return {'url': url, 'bvid': bvid, 'page': page, 'cid': entry['cid'],
                'quality': data.get('quality'), 'duration': entry.get('duration'),
                'title': str(entry.get('part') or bvid)[:120]}

    def raw_range(self, item, start, end):
        headers = {'Range': f'bytes={start}-{end}'}
        if item.get('validator'):
            headers['If-Range'] = item['validator']
        response = self.request(item['url'], headers)
        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
        if (response.status != 206 or response.headers.get('Content-Encoding', 'identity') != 'identity'
                or not match or tuple(map(int, match.groups()[:2])) != (start, end)):
            response.close()
            raise BiliError('B 站 CDN 未返回正确的字节范围，或源文件已改变')
        total = int(match.group(3))
        if total <= end or (item.get('size') and total != item['size']):
            response.close()
            raise BiliError('B 站源文件大小已改变，请生成新链接')
        if item.get('validator_header') and response.headers.get(item['validator_header']) != item['validator']:
            response.close()
            raise BiliError('B 站源文件版本已改变，请生成新链接')
        return response, total

    def read_range(self, item, start, end):
        with self.raw_range(item, start, end)[0] as response:
            data = response.read(end - start + 2)
        self.cache.metrics.add('download', len(data))
        if len(data) != end - start + 1:
            raise BiliError('B 站 CDN 返回数据不完整')
        return data

    def probe(self, item):
        with self.raw_range(item, 0, 0)[0] as response:
            size = int(response.headers['Content-Range'].rsplit('/', 1)[1])
            if len(response.read(2)) != 1:
                raise BiliError('B 站探测响应不完整')
            etag = response.headers.get('ETag', '')
            validator = etag if etag and not etag.startswith('W/') else ''
        self.cache.metrics.add('download', 1)
        candidate = {**item, 'size': size, 'validator': validator, 'validator_header': 'ETag' if validator else ''}
        candidate.update(inspect(lambda a, b: self.read_range(candidate, a, b), size))
        candidate['deadline'] = self.deadline(item['url'])
        return candidate

    def create(self, value, title):
        try:
            return self._create(value, title)
        except HTTPError as error:
            code = error.code
            error.close()
            self.cache.debug.emit('ERROR', 'bili_create_http_error', status=code)
            raise BiliError(f'B 站接口或 CDN 返回 HTTP {code}；检查直链有效期、访问权限和服务器网络') from None
        except BiliError:
            raise
        except MP4Error as error:
            raise BiliError('MP4 格式检查失败：' + str(error)) from None
        except Exception as error:
            self.cache.debug.exception('bili_create_failed', error)
            raise BiliError('B 站解析或 CDN 探测失败；检查直链有效期、服务器网络和调试日志') from None

    def _create(self, value, title):
        direct = urlsplit(value).hostname and any(urlsplit(value).hostname.endswith('.' + d) for d in ('bilivideo.com', 'bilivideo.cn'))
        bvid, page = (None, None) if direct else self.video(value)
        if direct:
            self.cdn(value)
        with self.cache.fetch_lock:
            with self.cache.lock:
                for key, item in self.cache.items.items():
                    if item.get('source') == 'bilibili' and ((bvid and item.get('bvid') == bvid and item.get('page') == page)
                            or (direct and item['url'] == value)):
                        return key
                if len(self.cache.items) >= 100:
                    raise BiliError('最多保留 100 个链接，请删除旧链接')
            source = {'url': value, 'title': 'B 站视频直链'} if direct else self.resolve(bvid, page)
            item = self.probe(source)
            item.update(source='bilibili', mode='file', title=title[:120] or source['title'],
                        type='video/mp4', ext='.mp4', created=int(time.time()))
            key = secrets.token_urlsafe(24)
            with self.cache.lock:
                self.cache.items[key] = item
                try:
                    self.cache.save()
                except Exception:
                    self.cache.items.pop(key, None)
                    raise
            self.cache.debug.emit('INFO', 'bili_ready', key, quality=item.get('quality'),
                                  video=item['source_video'], audio=item['source_audio'])
            return key

    def refresh(self, key, item):
        if not item.get('bvid'):
            raise BiliError('B 站直链已失效或被拒绝；请重新解析后添加，建议使用 BV 链接以便自动续期')
        with self.cache.lock:
            current = self.cache.items[key]
            if time.time() - current.get('refresh_attempt', 0) < 30:
                raise BiliError('B 站续期暂不可用，请至少 30 秒后重试')
            current['refresh_attempt'] = time.time()
        candidate = self.resolve(item['bvid'], item['page'], item.get('quality') or 116)
        candidate = self.probe(candidate)
        if (not item.get('validator') or candidate['validator'] != item['validator']
                or candidate['size'] != item['size'] or candidate['moov_hash'] != item['moov_hash']
                or candidate['cid'] != item['cid'] or candidate['quality'] != item['quality']):
            raise BiliError('续期后的媒体版本无法确认一致；为避免混用缓存，请删除旧链接并重新添加')
        with self.cache.lock:
            updated = {**self.cache.items[key], **{k: candidate[k] for k in ('url', 'deadline')}}
            self.cache.items[key] = updated
            self.cache.save()
        self.cache.debug.emit('INFO', 'bili_url_refreshed', key)
        return updated

    def open_range(self, key, item, start, end):
        try:
            refreshed = False
            deadline = item.get('deadline')
            if deadline and deadline <= time.time() + 30:
                item = self.refresh(key, item)
                refreshed = True
            try:
                return self.raw_range(item, start, end)
            except HTTPError as error:
                code = error.code
                error.close()
                self.cache.debug.emit('WARN', 'bili_cdn_http_error', key, status=code)
                if code not in (401, 403, 410) or refreshed:
                    raise BiliError(f'B 站 CDN 返回 HTTP {code}，请稍后重试') from None
                item = self.refresh(key, item)
                return self.raw_range(item, start, end)
        except BiliError:
            raise
        except Exception as error:
            self.cache.debug.exception('bili_fetch_failed', error, key)
            raise BiliError('B 站回源或续期失败，请检查服务器网络和调试日志') from None
