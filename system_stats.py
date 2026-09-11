"""Host-wide Linux CPU counters and MemAvailable; no Docker socket required."""
import os
from pathlib import Path
import threading
import time


class SystemStats:
    def __init__(self, proc=None):
        self.proc = Path(proc or os.environ.get('HOST_PROC_PATH', '/proc'))
        self.lock = threading.Lock()
        self.previous = None
        self.updated = 0
        self.result = {'available': False, 'cpu_percent': None, 'memory_percent': None}

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            if now - self.updated < 1:
                return dict(self.result)
            self.updated = now
            try:
                line = (self.proc / 'stat').read_text().splitlines()[0].split()
                if line[0] != 'cpu' or len(line) < 5:
                    raise ValueError('invalid cpu counters')
                counters = list(map(int, line[1:9]))  # guest time already included in user/nice
                total = sum(counters)
                idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
                cpu = None
                if self.previous and total > self.previous[0]:
                    cpu = max(0, min(100, 100 * (1 - (idle - self.previous[1]) / (total - self.previous[0]))))
                self.previous = total, idle
                mem = {}
                for line in (self.proc / 'meminfo').read_text().splitlines():
                    name, value = line.split(':', 1)
                    mem[name] = int(value.split()[0]) * 1024
                total_memory, available = mem['MemTotal'], mem['MemAvailable']
                if not 0 <= available <= total_memory or not total_memory:
                    raise ValueError('invalid memory counters')
                self.result = {'available': True, 'cpu_percent': round(cpu, 1) if cpu is not None else None,
                    'memory_percent': round((total_memory - available) / total_memory * 100, 1),
                    'memory_total': total_memory, 'memory_used': total_memory - available,
                    'memory_available': available, 'scope': 'host'}
            except (OSError, ValueError, KeyError, IndexError):
                self.result = {'available': False, 'cpu_percent': None, 'memory_percent': None,
                               'scope': 'host'}
            return dict(self.result)
