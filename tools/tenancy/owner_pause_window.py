"""Owner-side: when may an owner pause tenant-backfill-tick without a wake observing the pause mid-way?

Heldi STOP bf7d1370 (2026-10-28): the release freeze paused the tick while wake vwfcr ran. A release that INSTALLS the
typed owner-pause fix is itself frozen under the OLD code, so every owner pause (freeze, emergency) must wait for a
safe window. Pure decision, no I/O: the package executor supplies the clock and the controller execution listing.
SAFE only when: the live tick schedule is a known cadence (*/3 -> 180 s, */10 -> 600 s; anything else REFUSE), the
controller execution listing is complete (no nextPageToken; the caller pages it fully), >= 60 s since the last fire
(a just-fired wake is listed by then), >= 25 s to the next fire, and no controller execution is active (this includes
the execution of the last fire).
"""
from datetime import datetime

PERIODS = {'*/3 * * * *': 180, '*/10 * * * *': 600}   # Europe/Moscow is UTC+3: minute boundaries coincide with UTC
MIN_SINCE_FIRE = 60
MIN_TO_NEXT_FIRE = 25


def decide(now, listing, schedule):
    """('SAFE', facts), ('WAIT', reason) or ('REFUSE', reason). `now` aware UTC; `listing` the COMPLETE Cloud Run v2
    execution listing of the controller ({'executions': [...]} without nextPageToken); `schedule` the live tick cron."""
    if now.utcoffset() is None:
        raise ValueError('aware UTC clock required')
    period = PERIODS.get(schedule)
    if period is None:
        return 'REFUSE', 'unknown tick schedule %r: no safe window can be computed' % (schedule,)
    if not isinstance(listing, dict) or listing.get('nextPageToken') or not isinstance(listing.get('executions', []), list):
        return 'REFUSE', 'incomplete controller execution listing'
    since = (now.minute % (period // 60)) * 60 + now.second
    to_next = period - since
    if since < MIN_SINCE_FIRE:
        return 'WAIT', 'just after a tick fire'
    if to_next < MIN_TO_NEXT_FIRE:
        return 'WAIT', 'just before a tick fire'
    if any(not e.get('completionTime') for e in listing.get('executions', [])):
        return 'WAIT', 'active controller execution'
    return 'SAFE', dict(period=period, seconds_since_fire=since, seconds_to_next_fire=to_next)
