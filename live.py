"""Live room control and MediaMTX authentication. No media is stored on disk."""
import base64
import copy
import hmac
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, build_opener, ProxyHandler
from throttle import Bandwidth

LIVE_BW = dict(total_mbps='LIVE_TOTAL_MBPS', utilization='LIVE_UTILIZATION', client_mbps='LIVE_CLIENT_MBPS')


class Live:
    def __init__(self, cache):
        self.cache = cache
        self.lock = threading.RLock()
        self.path = cache.root / 'live.json'
        self.data = json.loads(self.path.read_text('utf-8')) if self.path.exists() else {'paused': False, 'rooms': {}}
        self.bandwidth = Bandwidth(cache.root, idle_seconds=0)
        # Live settings are persisted in .env, never in the VOD bandwidth file.
        self.bandwidth.config = {k: cache.settings.values[v] for k, v in LIVE_BW.items()}
        self.connections = set()
        self.enabled = os.environ.get('LIVE_ENABLED') == 'true'
        self.secret = os.environ.get('LIVE_INTERNAL_TOKEN', '')
        self.api_url = os.environ.get('LIVE_MTX_API', 'http://mediamtx:9997')
        self.opener = build_opener(ProxyHandler({}))
        self.engine = {'online': False, 'error': '直播引擎尚未连接', 'paths': {}, 'input_mbps': 0}
        self.sent = 0
        self.output_mbps = 0
        self.gateway = None

    def save(self):
        temp = self.path.with_suffix('.part')
        temp.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')
        temp.replace(self.path)

    def engine_api(self, path, method='GET'):
        token = base64.b64encode(('relay-api:' + self.secret).encode()).decode()
        req = Request(self.api_url + path, method=method, headers={'Authorization': 'Basic ' + token})
        with self.opener.open(req, timeout=3) as response:
            body = response.read(1024 * 1024)
            return json.loads(body) if body else {}

    def authenticate(self, body):
        if not self.enabled or len(self.secret) < 32 or not isinstance(body, dict):
            return False
        if body.get('action') == 'api':
            return body.get('user') == 'relay-api' and hmac.compare_digest(str(body.get('password', '')).encode(), self.secret.encode())
        match = re.fullmatch(r'live/([A-Za-z0-9_-]{32})', str(body.get('path', '')))
        if not match:
            return False
        with self.lock:
            room = self.data['rooms'].get(match[1])
            if not room or not room['enabled']:
                return False
            if body.get('action') == 'read':
                return body.get('protocol') == 'rtsp' and not self.data['paused'] and not room['paused']
            if body.get('action') == 'publish' and body.get('protocol') == 'rtmp':
                query = parse_qs(str(body.get('query', '')))
                password = body.get('password') or query.get('pass', [''])[0]
                return hmac.compare_digest(str(password).encode(), room['publish_key'].encode())
        return False

    def allowed(self, key):
        room = self.data['rooms'].get(key)
        return self.enabled and not self.data['paused'] and room and room['enabled'] and not room['paused']

    def bind(self, connection, key):
        with self.lock:
            if not self.allowed(key) or connection.room not in (None, key):
                raise PermissionError('直播间不可观看')
            connection.room = key

    def close_viewers(self, key=None):
        with self.lock:
            victims = [c for c in self.connections if key is None or c.room == key]
            for conn in victims:
                conn.close()
            return len(victims)

    def stop_publisher(self, key):
        try:
            result = self.engine_api('/v3/rtmp/conns/list?itemsPerPage=1000')
            for conn in result.get('items', []):
                if conn.get('path') == 'live/' + key:
                    self.engine_api('/v3/rtmp/conns/kick/' + conn['id'], 'POST')
        except Exception as error:
            self.cache.debug.exception('live_publisher_kick_failed', error)
            return '新推流已禁止，但未能确认旧推流已断开；请停止 OBS，检查直播引擎后重试。'
        return ''

    def control(self, body):
        if not isinstance(body, dict):
            raise ValueError('直播操作无效')
        operation, key = body.get('operation'), body.get('id')
        if not isinstance(operation, str) or key is not None and not isinstance(key, str):
            raise ValueError('直播操作格式无效')
        warning = ''
        with self.lock:
            previous = copy.deepcopy(self.data)
            if operation == 'create':
                title = body.get('title', '')
                if not isinstance(title, str) or not title.strip() or len(title) > 120:
                    raise ValueError('请填写不超过 120 字的直播间名称')
                if len(self.data['rooms']) >= 20:
                    raise ValueError('最多创建 20 个直播间')
                key = secrets.token_urlsafe(24)
                self.data['rooms'][key] = dict(title=title.strip(), publish_key=secrets.token_urlsafe(32), enabled=True, paused=False, created=time.time())
            elif operation in ('pause_all', 'resume_all'):
                self.data['paused'] = operation == 'pause_all'
            else:
                if key not in self.data['rooms']:
                    raise ValueError('直播间不存在')
                room = self.data['rooms'][key]
                if operation in ('pause', 'resume'):
                    room['paused'] = operation == 'pause'
                elif operation in ('disable', 'enable'):
                    room['enabled'] = operation == 'enable'
                elif operation == 'rotate':
                    room['publish_key'] = secrets.token_urlsafe(32)
                elif operation == 'delete':
                    del self.data['rooms'][key]
                else:
                    raise ValueError('未知直播操作')
            try:
                self.save()
            except OSError:
                self.data = previous
                raise
            if operation in ('pause_all', 'pause', 'disable', 'delete'):
                self.close_viewers(None if operation == 'pause_all' else key)
        if operation in ('disable', 'delete', 'rotate'):
            warning = self.stop_publisher(key)
        self.cache.debug.emit('INFO', 'live_control', operation=operation)
        return {'ok': True, 'warning': warning}

    def snapshot(self, base):
        host = os.environ.get('LIVE_PUBLIC_HOST') or urlsplit(base).hostname or 'localhost'
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'
        with self.lock:
            result = copy.deepcopy(self.engine)
            rooms = []
            for key, room in self.data['rooms'].items():
                conns = [c for c in self.connections if c.room == key and not c.stop.is_set()]
                engine = result['paths'].get('live/' + key, {})
                rooms.append({**room, 'id': key, 'ready': bool(engine.get('ready')), 'tracks': engine.get('tracks', []),
                    'clients': len({c.identity for c in conns}), 'connections': len(conns),
                    'play_url': f'rtspt://{host}:' + os.environ.get('LIVE_RTSP_PORT', '8554') + '/live/' + key,
                    'obs_server': f'rtmp://{host}:' + os.environ.get('LIVE_RTMP_PORT', '1935') + '/live',
                    'obs_key': key + '?user=publisher&pass=' + room['publish_key']})
            result.pop('paths', None)
            return {**result, 'enabled': self.enabled, 'gateway': self.gateway is not None,
                    'paused': self.data['paused'], 'rooms': rooms, 'output_mbps': self.output_mbps,
                    'sent_bytes': self.sent, 'bandwidth': self.bandwidth.snapshot()}

    def monitor(self):
        previous, previous_sent, last = {}, 0, time.monotonic()
        while True:
            try:
                paths = self.engine_api('/v3/paths/list?itemsPerPage=1000').get('items', [])
                publishers = self.engine_api('/v3/rtmp/conns/list?itemsPerPage=1000').get('items', [])
                # Close a publisher that raced with disable/delete/key rotation.
                for publisher in publishers:
                    if publisher.get('state') != 'publish':
                        continue
                    request = {'action': 'publish', 'protocol': 'rtmp', 'path': publisher.get('path'), 'query': publisher.get('query', '')}
                    if not self.authenticate(request):
                        self.engine_api('/v3/rtmp/conns/kick/' + publisher['id'], 'POST')
                now = time.monotonic()
                current = {c['id']: c.get('inboundBytes', c.get('bytesReceived', 0)) for c in publishers}
                received = sum(max(0, n - previous.get(k, n)) for k, n in current.items())
                with self.lock:
                    self.engine = {'online': True, 'error': '', 'paths': {p['name']: p for p in paths}, 'input_mbps': received * 8 / max(.01, now - last) / 1e6}
                    self.output_mbps = max(0, self.sent - previous_sent) * 8 / max(.01, now - last) / 1e6
                    previous_sent = self.sent
                previous, last = current, now
            except Exception as error:
                with self.lock:
                    self.engine = {'online': False, 'error': '无法连接直播引擎，请检查 mediamtx 容器及内部鉴权配置', 'paths': {}, 'input_mbps': 0}
                    now = time.monotonic()
                    self.output_mbps = max(0, self.sent - previous_sent) * 8 / max(.01, now - last) / 1e6
                    previous_sent, last = self.sent, now
                    previous = {}
                self.cache.debug.exception('live_engine_unavailable', error)
            time.sleep(3)

    def start(self):
        if not self.enabled:
            return
        if len(self.secret) < 32:
            raise ValueError('直播内部密钥缺失，请重新运行 install.sh')
        from live_gateway import Gateway
        self.gateway = Gateway(('0.0.0.0', 8554), self)
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()
        threading.Thread(target=self.monitor, daemon=True).start()
