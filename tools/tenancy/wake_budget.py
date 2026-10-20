"""Internal time budget of one controller wake (Cloud Run task timeout is a hard kill).

A wake killed by the platform leaves its control claim without the canonical
closure, and every later wake must then STOP (incident 2026-10-19: the holder
timed out 28.9 min into a 30 min task). Work therefore starts only when the
wake can still finish it AND close: a new leaf, a reconciliation, a completion,
a source dispatch and the read-only progress report each need their worst case
plus the closure reserve. Nothing here interrupts a started dispatch, releases a lease
or fence, or closes a claim; a declined step is handed to the next tick.
"""
import time

PLATFORM_SECONDS = 1800            # controller_timeout_seconds of the qualified release
STARTUP_SECONDS = 60               # container start before main() is measured
CLOSE_RESERVE_SECONDS = 360        # ensure + closure CAS + readback + result + exit: 6 requests at the 60 s timeout
LEAF_SECONDS = 180                 # one more leaf/entity evaluation (preflight, history, quota, decision)
RECONCILE_SECONDS = 420            # terminal receipt → checkpoint reconcile → persisted readback → RECONCILED
COMPLETE_SECONDS = 300             # complete leaf → coverage proof → CHUNK_COMPLETE / CURRENT_CERT
DISPATCH_SECONDS = 600             # intent → lease → Run POST → receipt (observed ~2 s; 10 requests at 60 s)
REPORT_SECONDS = 420               # read-only progress report after the closure


class Budget:
    def __init__(self, clock=time.monotonic, platform=PLATFORM_SECONDS):
        self.clock, self.platform, self.started = clock, platform, clock()

    def elapsed(self):
        return self.clock() - self.started + STARTUP_SECONDS

    def remaining(self):
        return self.platform - self.elapsed()

    def allows(self, seconds):
        return self.remaining() - CLOSE_RESERVE_SECONDS >= seconds

    def report_allowed(self):
        # Reporting runs after the closure: only process exit must still fit.
        return self.remaining() - 60 >= REPORT_SECONDS


def of(backend):
    """The wake's budget; a backend without one (tests, owner tools) is unlimited."""
    return getattr(backend, 'wake_budget', None) or getattr(getattr(backend, 'base', None), 'wake_budget', None)


def exhausted(result):
    """A budget handoff ends the wake: no further leaf/entity is planned or prepared."""
    return str(result.get('basis', '')).startswith('WAKE_BUDGET')


def handoff(kind, **extra):
    """A declined step: no source call, nothing durable written, the next tick continues."""
    return dict({'status': 'WAITING', 'source_dispatches': 0, 'basis': 'WAKE_BUDGET_' + kind}, **extra)
