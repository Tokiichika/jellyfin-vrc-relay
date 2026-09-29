"""Manual finite HLS prefix preloading with foreground fetch priority."""
import contextlib
import math
import threading


class FetchGate:
    def __init__(self):
        self.cv = threading.Condition()
        self.busy = False
        self.waiters = 0

    def acquire(self, blocking=True, background=False):
        with self.cv:
            if not blocking and (self.busy or self.waiters):
                return False
            if not background:
                self.waiters += 1
            try:
                while self.busy or (background and self.waiters):
                    self.cv.wait()
                self.busy = True
                return True
            finally:
                if not background:
                    self.waiters -= 1

    def release(self):
        with self.cv:
            self.busy = False
            self.cv.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()

    @contextlib.contextmanager
    def background(self):
        self.acquire(background=True)
        try:
            yield
        finally:
            self.release()


class Preloader:
    def __init__(self, cache):
        self.cache = cache
        # Use the cache lock for all job state, avoiding lock-order inversion.
        self.jobs = {}
        self.worker = None

    def start(self, key, percent=5):
        if type(percent) not in (int, float) or not math.isfinite(percent) or not 1 <= percent <= 100:
            raise ValueError('preload percent must be 1..100')
        with self.cache.lock:
            if self.cache.maintenance.is_set():
                raise ValueError('正在清理数据，请稍后再预载')
            item = self.cache.items.get(key)
            if not item or item.get('mode') != 'hls':
                raise ValueError('only HLS supports preload')
            if self.jobs.get(key, {}).get('state') in ('queued', 'running', 'cancelling'):
                return self.snapshot(key)
            total = sum(s['start'] < item['duration'] * percent / 100 for s in item['segments'])
            self.jobs[key] = dict(state='queued', percent=percent, total=total, done=0, bytes=0,
                                  target_seconds=sum(s['duration'] for s in item['segments'][:total]),
                                  cancel=False, error='')
            if self.worker is None:
                self.worker = threading.Thread(target=self.run, daemon=True)
                self.worker.start()
            return self.snapshot(key)

    def cancel(self, key):
        with self.cache.lock:
            job = self.jobs.get(key)
            if job and job['state'] in ('queued', 'running', 'cancelling'):
                job['cancel'] = True
                job['state'] = 'cancelled' if job['state'] == 'queued' else 'cancelling'
            return self.snapshot(key)

    def snapshot(self, key):
        with self.cache.lock:
            job = self.jobs.get(key)
            if not job:
                return None
            return {k: v for k, v in job.items() if k != 'cancel'} | {
                'resident': sum(self.cache.hls.filename(key, n) in self.cache.entries for n in range(job['total']))}

    def run(self):
        while True:
            with self.cache.lock:
                pending = next(((k, j) for k, j in self.jobs.items() if j['state'] == 'queued'), None)
                if pending is None:
                    self.worker = None
                    return
                key, job = pending
                job['state'] = 'running'
            pinned = []
            self.cache.debug.emit('INFO', 'preload_started', key, percent=job['percent'], segments=job['total'])
            try:
                for index in range(job['total']):
                    with self.cache.lock:
                        if job['cancel']:
                            break
                    with self.cache.hls.segment(key, index, background=True) as (_, size):
                        name = self.cache.hls.filename(key, index)
                        with self.cache.lock:
                            self.cache.pins[name] += 1
                            pinned.append(name)
                            job['done'] += 1
                            job['bytes'] += size
                with self.cache.lock:
                    job['state'] = 'cancelled' if job['cancel'] else 'completed'
            except Exception as error:
                self.cache.debug.exception('preload_failed', error, key)
                with self.cache.lock:
                    job['state'] = 'failed'
                    job['error'] = '预载失败：请检查缓存可用空间、NAS 转码与调试日志；可降低预载比例后重试。'
            finally:
                with self.cache.lock:
                    for name in pinned:
                        self.cache.pins[name] -= 1
                        if not self.cache.pins[name]:
                            del self.cache.pins[name]
                self.cache.debug.emit('INFO', 'preload_finished', key, state=job['state'], segments=job['done'])
