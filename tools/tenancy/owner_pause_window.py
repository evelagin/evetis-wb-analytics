"""Owner-side: when may an owner pause tenant-backfill-tick without a wake observing the pause mid-way?

Heldi STOP bf7d1370 (2026-10-28): the release freeze paused the tick while wake vwfcr ran. A release that INSTALLS the
typed owner-pause fix is itself frozen under the OLD code, so every owner pause (freeze, emergency) must wait for a
safe window. Pure decision, no I/O: the package executor supplies the clock and the controller execution listing.
SAFE only when: >= 60 s since the last */3 fire (a just-fired wake is listed), >= 25 s to the next fire, no active
controller execution, and every execution created since the last fire (5 s tolerance) completed.
"""
from datetime import datetime, timedelta

PERIOD = 180          # */3 * * * * in Europe/Moscow: MSK is UTC+3, so minute boundaries coincide with UTC
MIN_SINCE_FIRE = 60
MIN_TO_NEXT_FIRE = 25


def _at(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def decide(now, executions):
    """('SAFE', facts) or ('WAIT', reason). `now` aware UTC; `executions` Cloud Run v2 execution resources."""
    if now.utcoffset() is None:
        raise ValueError('aware UTC clock required')
    since = (now.minute % 3) * 60 + now.second
    to_next = PERIOD - since
    if since < MIN_SINCE_FIRE:
        return 'WAIT', 'just after a tick fire'
    if to_next < MIN_TO_NEXT_FIRE:
        return 'WAIT', 'just before a tick fire'
    boundary = (now - timedelta(seconds=since)).replace(microsecond=0)
    if any(not e.get('completionTime') for e in executions):
        return 'WAIT', 'active controller execution'
    if any(_at(e['createTime']) >= boundary - timedelta(seconds=5) and not e.get('completionTime') for e in executions):
        return 'WAIT', 'execution of the last fire not completed'
    return 'SAFE', dict(seconds_since_fire=since, seconds_to_next_fire=to_next)
