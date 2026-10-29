"""Process-local activity ownership and analysis lifecycle."""

import itertools
import secrets
import threading
import time

from . import contracts as c

_SERIAL = itertools.count(1)


class Gate:
    def __init__(self, emit, *, fault=None, closing=None):
        self._lock = threading.Lock()
        self._emit = emit
        self._fault = fault or (lambda: None)
        self._closing = closing or (lambda: None)
        self._state = c.State.IDLE
        self._gen = self._seq = 0
        self._owner = None
        self._jobs = {}
        self._grants = {}
        self._release_resources = lambda: True
        self._finish_epoch = 0
        self.close_pending = False
        self.worker_done = False
        self._emit([c.EVENT_STATE, self._payload()])

    def _payload(self):
        return {"state": self._state.value, "gen": self._gen, "seq": self._seq}

    def _transition(self, target):
        if target not in c.TRANSITIONS[self._state]:
            return False
        was_busy = self._state in c.BUSY_STATES
        self._state = target
        self._seq += 1
        if target in (c.State.DRAINING, c.State.FAULT, c.State.CLOSING):
            self._grants.clear()
            for job in self._jobs.values():
                job.cancel.set()
        self._emit([c.EVENT_STATE, self._payload()])
        busy = target in c.BUSY_STATES
        if busy != was_busy:
            self._emit([c.EVENT_BUSY, busy])
        if target == c.State.FAULT:
            self._fault()
        return True

    def state(self):
        with self._lock:
            return self._state, self._gen

    def payload(self):
        with self._lock:
            return self._payload()

    def reserve(self, kind):
        if kind != "power":
            raise ValueError("only power can be reserved")
        with self._lock:
            if self._state != c.State.IDLE:
                return None
            self._owner = c.Token(kind, self._gen, next(_SERIAL))
            self.worker_done = False
            self._transition(c.State.POWER)
            return self._owner

    def release(self, token):
        with self._lock:
            if token != self._owner or token is None:
                return False
            self.worker_done = True
            if self._state != c.State.POWER:
                return False
            self._owner = None
            return self._transition(c.State.IDLE)

    @staticmethod
    def _timer(seconds, callback):
        timer = threading.Timer(seconds, callback)
        timer.daemon = True
        timer.start()

    def finishing(self, token):
        with self._lock:
            if self._state != c.State.POWER or token != self._owner:
                return
            self._finish_epoch += 1
            epoch = self._finish_epoch
        def expired():
            with self._lock:
                if (self._state == c.State.POWER and self._owner == token
                        and self._finish_epoch == epoch):
                    self._transition(c.State.FAULT)
        self._timer(c.POWER_FINISH_TIMEOUT_S, expired)

    def untimed(self, token) -> bool:
        """Atomically admit the owner to untimed work and invalidate its timer."""
        with self._lock:
            if self._state != c.State.POWER or token != self._owner:
                return False
            self._finish_epoch += 1
            return True

    def enter(self, prepare):
        """Prepare the server under the same lock as admission; failure leaves idle."""
        with self._lock:
            if self._state == c.State.ANALYSIS:
                return self._gen, None
            if self._state != c.State.IDLE:
                return None, self._state.value
            prepare()
            self._gen += 1
            self._transition(c.State.ANALYSIS)
            return self._gen, None

    def set_release_resources(self, callback):
        with self._lock:
            self._release_resources = callback

    def leave(self, gen):
        with self._lock:
            if type(gen) is not int or gen != self._gen:
                return False
            if self._state == c.State.DRAINING:
                return True
            if self._state != c.State.ANALYSIS:
                return False
            self._transition(c.State.DRAINING)
        self._timer(c.DRAINING_TIMEOUT_S, lambda: self._drain_expired(gen))
        threading.Thread(target=self._drain, args=(gen,), daemon=True).start()
        return True

    def _drain_expired(self, gen):
        close = False
        with self._lock:
            if self._state == c.State.DRAINING and self._gen == gen:
                self._transition(c.State.FAULT)
                if self.close_pending:
                    close = self._transition(c.State.CLOSING)
        if close:
            self._closing()

    def _drain(self, gen):
        while True:
            with self._lock:
                if self._state != c.State.DRAINING or self._gen != gen:
                    return
                ready = not self._jobs
                release = self._release_resources
            if ready:
                try:
                    ready = release()
                except Exception:
                    ready = False
                if ready:
                    with self._lock:
                        if self._state != c.State.DRAINING or self._gen != gen:
                            return
                        close = self.close_pending
                        self._transition(c.State.CLOSING if close else c.State.IDLE)
                    if close:
                        self._closing()
                    return
            time.sleep(0.01)

    def on_closing(self):
        with self._lock:
            if self._state == c.State.POWER:
                return "power"
            if self._state == c.State.CLOSING:
                return "closing"
            if self._state == c.State.DRAINING:
                self.close_pending = True
                return "draining"
            self._transition(c.State.CLOSING)
            return "start"

    def job_begin(self, gen, req=None):
        with self._lock:
            if type(gen) is not int or self._state != c.State.ANALYSIS or gen != self._gen:
                return None
            job = c.JobToken(gen, next(_SERIAL), req)
            self._jobs[job.serial] = job
            return job

    def job_end(self, job):
        with self._lock:
            if isinstance(job, c.JobToken) and self._jobs.get(job.serial) is job:
                del self._jobs[job.serial]

    def job_count(self):
        with self._lock:
            return len(self._jobs)

    def cancel_req(self, gen, req):
        with self._lock:
            found = False
            for job in self._jobs.values():
                if job.gen == gen and job.req == req:
                    job.cancel.set()
                    found = True
            return found

    def issue_grant(self, gen, mode, purpose, path):
        with self._lock:
            if type(gen) is not int or self._state != c.State.ANALYSIS or gen != self._gen:
                return None
            grant = c.Grant(secrets.token_urlsafe(16), gen, mode, purpose, str(path),
                            time.monotonic() + c.GRANT_TTL_S)
            self._grants[grant.id] = grant
            while len(self._grants) > c.MAX_GRANTS_PER_GEN:
                del self._grants[next(iter(self._grants))]
            return grant

    def consume_grant(self, grant, gen, mode, purpose):
        with self._lock:
            item = self._grants.pop(grant, None)
            if (item is not None and type(gen) is int and self._state == c.State.ANALYSIS
                    and item.gen == gen == self._gen and item.mode == mode
                    and item.purpose == purpose and time.monotonic() < item.expires):
                return item.path
            return None
