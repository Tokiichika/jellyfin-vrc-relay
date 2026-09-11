"""Structured, bounded, credential-free debug events. Never log raw requests/exceptions."""
import collections
import os
import threading
import time
import traceback


class Diagnostics:
    def __init__(self):
        self.lock = threading.Lock()
        self.enabled = False
        self.sequence = 0
        self.events = collections.deque(maxlen=500)

    def set_enabled(self, value):
        with self.lock:
            self.enabled = value
        self.emit('INFO', 'debug_enabled' if value else 'debug_disabled', force=True)

    def emit(self, level, event, item='', force=False, **fields):
        # Callers provide only explicitly selected non-sensitive fields; no arbitrary URL/header/body.
        if not self.enabled and not force and level != 'ERROR':
            return
        with self.lock:
            self.sequence += 1
            self.events.append({'seq': self.sequence, 'time': time.time(), 'level': level,
                                'event': event, 'item': item[:8], 'details': fields})

    def exception(self, event, error, item=''):
        frames = traceback.extract_tb(error.__traceback__)
        stack = [f'{os.path.basename(f.filename)}:{f.lineno} {f.name}' for f in frames[-10:]]
        # Exception text may include tokens, URLs, cookies or response bodies. Types and locations only.
        chain, seen, current = [], set(), error
        while current is not None and id(current) not in seen and len(chain) < 5:
            seen.add(id(current))
            chain.append(type(current).__name__)
            current = current.__cause__ or current.__context__
        self.emit('ERROR', event, item, exception_types=chain, stack=stack)

    def snapshot(self, after=0):
        with self.lock:
            return {'enabled': self.enabled, 'last_seq': self.sequence,
                    'events': [event for event in self.events if event['seq'] > after],
                    'capacity': 500}
