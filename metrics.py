"""Bounded application-payload metrics, not host NIC accounting."""
import collections
import hashlib
import ipaddress
import secrets
import threading
import time


class Metrics:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.lock = threading.RLock()
        self.buckets = collections.deque()
        self.download = self.upload = self.errors = self.requests = 0
        self.clients = {}
        self.item_stats = {}
        self.salt = secrets.token_bytes(32)

    def _prune(self, now):
        while self.buckets and self.buckets[0][0] <= now - 10:
            self.buckets.popleft()
        self.clients = {k: v for k, v in self.clients.items() if v[0] > now - 90}

    def add(self, direction, size):
        with self.lock:
            now = self.clock()
            self._prune(now)
            second = int(now)
            if not self.buckets or self.buckets[-1][0] != second:
                self.buckets.append([second, 0, 0])
            if direction == 'download':
                self.download += size
                self.buckets[-1][1] += size
            else:
                self.upload += size
                self.buckets[-1][2] += size

    def identity(self, peer, real_ip, agent, trusted):
        address = ipaddress.ip_address(peer)
        if real_ip and any(address in net for net in trusted):
            try:
                address = ipaddress.ip_address(real_ip.strip())
            except ValueError:
                pass
        return hashlib.sha256(self.salt + (str(address) + '\0' + agent[:256]).encode()).hexdigest()[:20]

    def touch(self, identity, item):
        with self.lock:
            now = self.clock()
            self._prune(now)
            if len(self.clients) >= 4096:
                self.clients.pop(next(iter(self.clients)))
            self.clients[(identity, item)] = (now, item)
            self.item_stats.setdefault(item, {})['last_access'] = now

    def begin(self, identity, item):
        with self.lock:
            self.touch(identity, item)
            self.requests += 1

    def end(self):
        with self.lock:
            self.requests -= 1

    def error(self, item, message):
        with self.lock:
            self.errors += 1
            self.item_stats.setdefault(item, {}).update(last_error=message, error_at=time.time())

    def fetched(self, item, seconds):
        with self.lock:
            self.item_stats.setdefault(item, {}).update(last_fetch_seconds=round(seconds, 2), last_error='')

    def forget(self, item):
        with self.lock:
            self.item_stats.pop(item, None)
            self.clients = {k: v for k, v in self.clients.items() if v[1] != item}

    def snapshot(self):
        with self.lock:
            now = self.clock()
            self._prune(now)
            window = max(1, min(10, now - self.started))
            per_item = {}
            for key, stats in self.item_stats.items():
                per_item[key] = {**stats, 'last_access_ago': round(now - stats.get('last_access', now)),
                                 'active_clients': sum(1 for _, item in self.clients if item == key)}
                per_item[key].pop('last_access', None)
            return {'download_bps': round(sum(b[1] for b in self.buckets) / window),
                    'upload_bps': round(sum(b[2] for b in self.buckets) / window),
                    'download_bytes': self.download, 'upload_bytes': self.upload,
                    'active_clients': len({identity for identity, _ in self.clients}),
                    'active_requests': self.requests, 'errors': self.errors,
                    'uptime_seconds': round(now - self.started), 'per_item': per_item}
