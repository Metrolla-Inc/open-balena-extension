"""Background work: a thread pool in the service, run-inline in tests."""
import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger('fleet_api')


class ThreadJobs:
    def __init__(self, workers: int = 4):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='fleet-job')

    def submit(self, fn, *args, **kwargs):
        def run():
            try:
                fn(*args, **kwargs)
            except Exception:
                log.exception('background job %s failed', getattr(fn, '__name__', fn))
        return self._pool.submit(run)

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


class InlineJobs:
    """Runs each job when submitted (tests). With defer=True, jobs wait for run_pending()."""

    def __init__(self, defer: bool = False):
        self.defer = defer
        self.pending = []

    def submit(self, fn, *args, **kwargs):
        if self.defer:
            self.pending.append((fn, args, kwargs))
        else:
            fn(*args, **kwargs)

    def run_pending(self):
        while self.pending:
            fn, args, kwargs = self.pending.pop(0)
            fn(*args, **kwargs)

    def shutdown(self):
        pass
