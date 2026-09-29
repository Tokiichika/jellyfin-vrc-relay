"""Shared payload token buckets; connections from the same IP share a quota."""
import json
import math
import threading
import time


class Bandwidth:
    def __init__(self, root, minimum_mbps=10, idle_seconds=90):
        self.path = root / 'bandwidth.json'
        self.cv = threading.Condition()
        self.clients = {}
        self.leases = {}
        self.minimum_mbps = minimum_mbps
        self.idle_seconds = idle_seconds
        self.rejected = 0
        self.tokens = 0
        self.last = time.monotonic()
        self.config = dict(total_mbps=200, utilization=80, client_mbps=32)
        if self.path.exists():
            self.config = self.validate(json.loads(self.path.read_text()))

    @staticmethod
    def validate(values):
        limits = {'total_mbps': (1, 10000), 'utilization': (1, 100), 'client_mbps': (0, 10000)}
        if set(values) != set(limits):
            raise ValueError('invalid bandwidth settings')
        for key, (low, high) in limits.items():
            value = values[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('invalid bandwidth settings')
        return dict(values)

    def configure(self, values, persist=None):
        values = self.validate(values)
        with self.cv:
            self.expire()
            capacity = math.floor(values['total_mbps'] * values['utilization'] / 100 / self.minimum_mbps)
            if capacity < max(1, len(self.leases)):
                raise ValueError('新带宽不足以为已有出口 IP 保留 10 Mbps，请等待观众离开后再降低总带宽')
            if 0 < values['client_mbps'] < self.minimum_mbps:
                raise ValueError('单 IP 上限不能低于 10 Mbps；填 0 表示仅动态均分')
            if persist is not None:
                persist()
            else:
                temporary = self.path.with_suffix('.part')
                temporary.write_text(json.dumps(values))
                temporary.replace(self.path)
            self.refill()
            self.config = values
            self.tokens = 0
            for client in self.clients.values():
                client['tokens'] = 0
            self.cv.notify_all()
        return self.snapshot()

    def expire(self):
        now = time.monotonic()
        for identity, lease in list(self.leases.items()):
            if lease['refs'] == 0 and now - lease['last'] >= self.idle_seconds:
                del self.leases[identity]

    def admit(self, identity):
        """Reserve across HLS gaps, before headers or any origin work."""
        with self.cv:
            self.expire()
            if identity not in self.leases:
                total = self.config['total_mbps'] * self.config['utilization'] / 100
                cap = self.config['client_mbps']
                if len(self.leases) >= math.floor(total / self.minimum_mbps) or 0 < cap < self.minimum_mbps:
                    self.rejected += 1
                    return False
                self.leases[identity] = {'refs': 0, 'last': time.monotonic()}
            lease = self.leases[identity]
            lease['refs'] += 1
            lease['last'] = time.monotonic()
            return True

    def release(self, identity):
        with self.cv:
            lease = self.leases[identity]
            lease['refs'] -= 1
            lease['last'] = time.monotonic()

    def rates(self):
        total = self.config['total_mbps'] * self.config['utilization'] / 100 * 125000
        per = total / max(1, len(self.clients))
        if self.config['client_mbps']:
            per = min(per, self.config['client_mbps'] * 125000)
        return total, per

    def refill(self):
        now = time.monotonic()
        elapsed, self.last = now - self.last, now
        total, per = self.rates()
        self.tokens = min(65536, self.tokens + elapsed * total)
        for client in self.clients.values():
            client['tokens'] = min(65536, client['tokens'] + elapsed * per)

    def begin(self, identity):
        with self.cv:
            self.refill()
            client = self.clients.setdefault(identity, {'refs': 0, 'tokens': 0})
            client['refs'] += 1
            self.cv.notify_all()

    def end(self, identity):
        with self.cv:
            self.refill()
            client = self.clients[identity]
            client['refs'] -= 1
            if not client['refs']:
                del self.clients[identity]
            self.cv.notify_all()

    def acquire(self, identity, size, cancelled=None):
        if not 0 <= size <= 65536:
            raise ValueError('write exceeds bucket size')
        with self.cv:
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise ConnectionAbortedError('播放请求已由管理员清退')
                self.refill()
                client = self.clients[identity]
                if min(self.tokens, client['tokens']) >= size:
                    self.tokens -= size
                    client['tokens'] -= size
                    return
                total, per = self.rates()
                self.cv.wait(min(.25, max(.001, (size - self.tokens) / total, (size - client['tokens']) / per)))

    def snapshot(self):
        with self.cv:
            self.expire()
            total, per = self.rates()
            return {**self.config, 'active_senders': len(self.clients),
                    'admitted_ips': len(self.leases), 'capacity': math.floor(total / 125000 / self.minimum_mbps),
                    'minimum_mbps': self.minimum_mbps, 'idle_seconds': self.idle_seconds, 'rejected_requests': self.rejected,
                    'effective_total_mbps': total / 125000, 'effective_client_mbps': per / 125000}
