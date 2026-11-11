"""Heldi (client_001) cloud watch, layer 2: read-only health of tenant-backfill-controller.

Replaces the owner-laptop observe loop (its gcloud token expired on 25.10 at 14:56Z and the real
STOP aaf68ede of 20:10Z went unnoticed for 8 h 12 min). Runs as Cloud Run Job `heldi-health-eval`
every 30 min under its own read-only service account; the metadata token never expires.

Checks are the `observe` stage of the S4.1a package executor, without its journal and without
any pause: the watcher only reports. Evaluation reuses the controller's own modules (import only):
  * unresolved STOP          -- tools.tenancy.stop_acceptance (the one acceptance set of the gate);
  * CURRENT of today         -- tools.tenancy.current_controller (CURRENT_CERT of the independent
                                entities by CURRENT_DEADLINE_HOUR_MSK);
  * Scheduler                -- tick ENABLED, ordinary ozon-* PAUSED;
  * controller executions    -- duration > WAKE_LIMIT_SECONDS, failed without a bounded wait;
  * FULL progress            -- no DISPATCHED/RECONCILED in PROGRESS_WINDOW while chunks remain.

Read boundary: tools.tenancy.cloud_access.CloudAccess (reader authority only: GET of the four tenant
datasets, Run and Scheduler metadata, one bounded SELECT envelope) plus one guarded Logging
entries:list. Append/dispatch authority does not exist here: the token provider refuses it and the
service account has no write permission (infra/tenant-monitoring/watch.tf).

Output (stdout, Cloud Logging jsonPayload):
  * HEARTBEAT every completed evaluation (watchdog metric);
  * HEALTH only when the verdict fingerprint changes, or as a reminder of a non-OK verdict after
    REMINDER; alert policies match HEALTH lines (FAIL: critical, DEGRADED/OK: change notice).
Exit 0 after any completed evaluation (FAIL included); exit 3 when the watcher itself cannot run.

Owner dry-run (read-only, same guard):  python -m tools.heldi_watch.health --owner-token-command "gcloud auth print-access-token"
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

TENANT = 'client_001'
WATCH_JOB = 'heldi-health-eval'
WATCH_ACCOUNT_ID = 'sa-heldi-watch'
CONTROLLER = 'tenant-backfill-controller'
TICK = 'tenant-backfill-tick'
ORDINARY = ('ozon-daily', 'ozon-fast', 'ozon-weekly')
LOGS = 'https://logging.googleapis.com/v2/entries:list'
MSK = timezone(timedelta(hours=3))

WAKE_LIMIT_SECONDS = 1500           # package_s41a: >= 300 s margin to the 1800 s platform kill
CURRENT_DEADLINE_HOUR_MSK = 9       # CURRENT_CERT of today expected by 09:00 MSK (observed 00:14-00:50 MSK)
LOOKBACK = timedelta(hours=2)       # controller executions judged by each evaluation
PROGRESS_WINDOW = timedelta(hours=6)
LOG_INDEX_GRACE = timedelta(minutes=10)   # a just-finished execution may not be indexed yet
READ_RETRY_DEGRADED = 3             # bounded read waits in LOOKBACK before a DEGRADED notice
REMINDER = timedelta(hours=12)
SELF_HISTORY = timedelta(hours=26)
WAIT_STATUSES = frozenset({'WAITING_READ_RETRY', 'WAITING_LEASE'})
PROGRESS_STATUSES = frozenset({'DISPATCHED', 'RECONCILED', 'RECONCILED_BOUNDARY'})

OK, DEGRADED, FAIL = 'OK', 'DEGRADED', 'FAIL'
RANK = {OK: 0, DEGRADED: 1, FAIL: 2}


class WatchUnavailable(Exception):
    """The watcher itself cannot evaluate (identity, transport to metadata): exit 3, no heartbeat."""


def at(value):
    out = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if out.utcoffset() is None:
        raise ValueError('timestamp without timezone')
    return out.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


# ------------------------------------------------------------------ read-only transport
class Tokens:
    """Reader token only. In the cloud: metadata identity must be the watch SA (never the controller)."""

    def __init__(self, project, send, *, command=None, clock=time.time):
        self.project, self.send, self.command, self.clock = project, send, command, clock
        self.expected = f'{WATCH_ACCOUNT_ID}@{project}.iam.gserviceaccount.com'
        self.cached = None

    def token(self, kind):
        from tools.tenancy import tenant_tables as TT
        if kind != 'reader':
            raise TT.TableError('watch has no write authority')
        if self.cached and self.cached[1] > self.clock() + 60:
            return self.cached[0]
        if self.command:
            value = subprocess.check_output(shlex.split(self.command), timeout=60).decode().strip()
            self.cached = (value, self.clock() + 240)
            return value
        from tools.tenancy.cloud_access import METADATA
        identity = self.send('GET', METADATA + 'email', headers={'Metadata-Flavor': 'Google'})
        if identity != self.expected:
            raise TT.TableError('unexpected watch identity')
        result = self.send('GET', METADATA + 'token', headers={'Metadata-Flavor': 'Google'})
        token, expiry = result.get('access_token'), result.get('expires_in')
        if not isinstance(token, str) or not token or type(expiry) is not int or not 60 < expiry <= 3600:
            raise TT.TableError('invalid metadata credential envelope')
        self.cached = (token, self.clock() + expiry)
        return token


def denied(*_args, **_kwargs):
    from tools.tenancy import tenant_tables as TT
    raise TT.TableError('watch has no write authority')


class Reader:
    """CloudAccess reader boundary + one guarded Logging read. No other network path."""

    def __init__(self, contract, tokens, send):
        from tools.tenancy.cloud_access import CloudAccess
        self.c, self.tokens, self.send = contract, tokens, send
        self.access = CloudAccess(contract, tokens, send)

    def request(self, method, url, body=None):
        return self.access.request(method, url, body)

    def logs(self, flt, *, page_size=1000, max_pages=5, newest_first=True):
        from tools.tenancy import tenant_tables as TT
        if not flt.startswith('resource.type="cloud_run_job" AND ') or not 1 <= page_size <= 1000:
            raise TT.TableError('logging read outside the watch boundary')
        out, token = [], None
        for _ in range(max_pages):
            body = {'resourceNames': ['projects/' + self.c['project_id']], 'filter': flt,
                    'orderBy': 'timestamp desc' if newest_first else 'timestamp asc', 'pageSize': page_size}
            if token:
                body['pageToken'] = token
            x = self.send('POST', LOGS, body, {'Authorization': 'Bearer ' + self.tokens.token('reader'),
                                               'Content-Type': 'application/json'})
            out += x.get('entries', [])
            token = x.get('nextPageToken')
            if not token:
                return out
        raise TT.TableError('logging read exceeds the watch budget')


def run_url(c, job, suffix=''):
    return f"https://run.googleapis.com/v2/projects/{c['project_id']}/locations/{c['region']}/jobs/{job}{suffix}"


# ------------------------------------------------------------------ controller context
def controller_context(reader, clock):
    """Owner-side backend of the live controller (as package_s41a.owner_backend), reads only.

    The release comes from the controller's own immutable deployment descriptor (as its bootstrap).
    The watch image is built FROM the controller image, so the implementation hashes must match; a
    later controller release without a rebuilt watcher is reported, never evaluated with other code.
    """
    from tools.tenancy import tenant_backfill as BF, durable_plan as D, tenant_tables as TT
    from tools.tenancy import orchestration_contract as O, cloud_controller as C
    from tools.tenancy.validation import parse_tenant_json
    from pipelines.ozon.runtime import full_resume as FR
    c = reader.c
    job = reader.request('GET', run_url(c, CONTROLLER))
    containers = job['template']['template']['containers']
    env = {e['name']: e.get('value') for e in containers[0].get('env', [])}
    source, root, state = env.get('CONTROLLER_SOURCE_SHA'), env.get('BACKFILL_ROOT_HASH'), env.get('HISTORICAL_SCHEDULER_STATE')
    tables = TT.Tables(c['project_id'], request=reader.request, write_request=denied)
    marker = tables.get_table(c['datasets']['tenant_locks'], C.descriptor_name(source, root, state))
    facts = dict(generation=job.get('generation'), source=source, root=root, scheduler_state=state)
    if marker is None:
        return None, facts, [finding('CONTROLLER_DESCRIPTOR_MISSING', FAIL, 'deployment descriptor of the live controller is absent')]
    descriptor = parse_tenant_json(marker[1] or '{}')
    if set(descriptor) != {'settings', 'release'} or descriptor['settings'].get('release') != source or descriptor['settings'].get('root_hash') != root:
        return None, facts, [finding('CONTROLLER_DESCRIPTOR_DIFFERS', FAIL, 'descriptor scope/provenance differs from the live job')]
    release = descriptor['release']
    try:
        O.verify_artifact_source(release, BF.REPO)
    except BF.B.EvidenceError:
        return None, facts, [finding('WATCH_IMAGE_STALE', DEGRADED, 'watch image is not built from the live controller release; rebuild heldi-health-eval FROM ' + str(release.get('image')))]
    block = O.block(c, descriptor['settings'], BF.REPO, release)
    out = []
    if any(env.get(k) != v for k, v in block['job']['env'].items()) or containers[0].get('image') != release['image']:
        out.append(finding('CONTROLLER_ENV_DRIFT', FAIL, 'live controller env/image differs from its descriptor'))
    manifest = FR.manifest()
    if manifest['hash'] != root:
        return None, facts, out + [finding('WATCH_IMAGE_STALE', DEGRADED, 'FULL root of the live controller differs from the packaged manifest')]
    c = dict(c, orchestration=block)
    reader.access.c = c
    b = object.__new__(C.Backend)
    b.c, b.clock, b.current_execution, b.active = c, clock, None, False
    b.access = SimpleNamespace(c=c, tables=tables, request=reader.request, append_request=denied, dispatch=denied)
    b.wake_activity = {'dispatch_attempts': 0}
    b.manifest = manifest
    b.store = D.DurableRecords(c, tables, lambda cc, sql, p: BF.select(cc, sql, p, request=reader.request), denied)
    return b, facts, out


# ------------------------------------------------------------------ checks
def finding(code, level, detail, subject=None):
    return dict(code=code, level=level, detail=detail, subject=subject)


def transport(error):
    from tools.tenancy import tenant_tables as TT
    return isinstance(error, (TT.TableError, OSError))


def check_stop(b):
    """Unresolved STOPPED records of the FULL root by the shared acceptance set (gate = monitor = this)."""
    from tools.tenancy import stop_acceptance as SA, durable_plan as D
    root = b.c['orchestration']['job']['env']['BACKFILL_ROOT_HASH']
    try:
        records = b.store.history(root)
        unresolved = sorted(D.digest(r) for r in SA.unresolved(b, b.manifest, records))
    except Exception as e:     # noqa: BLE001 - classified below, never swallowed silently
        if transport(e):
            return [finding('STOP_GATE_UNREADABLE', DEGRADED, 'STOP acceptance read failed: ' + type(e).__name__)], {}
        return [finding('STOP_EVIDENCE_FAILURE', FAIL, 'STOP acceptance evidence failure: ' + type(e).__name__)], {}
    facts = dict(root_records=len(records), stop_records=sum(r['kind'] == 'STOPPED' for r in records), unresolved=[h[:8] for h in unresolved])
    if unresolved:
        return [finding('UNRESOLVED_STOP', FAIL, 'unresolved STOP ' + ', '.join(h[:8] for h in unresolved), subject=unresolved)], facts
    return [], facts


def check_current(b, now):
    """CURRENT_CERT of today's Moscow cycle for every independent entity by the deadline."""
    from tools.tenancy import current_controller as CUR, full_controller as H
    if b.c['orchestration']['job']['env'].get('CURRENT_DATA_MODE') is None:
        return [], dict(mode=None)
    local = now.astimezone(MSK)
    today = local.date()
    hb = H.Backend(b, b.manifest)
    pol = CUR.policy(hb)
    facts = dict(day=today.isoformat(), deadline_passed=local.hour >= CURRENT_DEADLINE_HOUR_MSK)
    if pol is None:
        return ([finding('CURRENT_POLICY_ABSENT', FAIL, 'CURRENT mode is set but no qualified policy is readable')]
                if facts['deadline_passed'] else []), facts
    certified, partial = set(), set()
    if today.isoformat() in CUR.cycles(hb, pol):
        records, _items = CUR.plans(hb, pol, today)
        certified = {r['payload']['fact']['entity'] for r in records if r['kind'] == 'CURRENT_CERT'}
        partial = {r['payload']['entity'] for r in records if r['kind'] == 'CURRENT_PARTIAL'}
        facts['cycle'] = True
    else:
        facts['cycle'] = False
    facts.update(certified=sorted(certified), partial=sorted(partial))
    if not facts['deadline_passed']:
        return [], facts
    if not facts['cycle']:
        return [finding('CURRENT_CYCLE_NOT_STARTED', FAIL, 'CURRENT cycle %s not started by %02d:00 MSK' % (today, CURRENT_DEADLINE_HOUR_MSK), subject=today.isoformat())], facts
    missing = sorted(set(CUR.INDEPENDENT_ENTITIES) - certified)
    if missing:
        return [finding('CURRENT_NOT_CERTIFIED', FAIL, 'CURRENT %s not certified by %02d:00 MSK: %s' % (today, CURRENT_DEADLINE_HOUR_MSK, ', '.join(missing)), subject=[today.isoformat()] + missing)], facts
    return [], facts


def check_schedulers(reader):
    c = reader.c
    x = reader.request('GET', f"https://cloudscheduler.googleapis.com/v1/projects/{c['project_id']}/locations/{c['region']}/jobs")
    if x.get('nextPageToken'):
        raise ValueError('scheduler listing exceeds one page')
    states = {j['name'].rsplit('/', 1)[-1]: j.get('state') for j in x.get('jobs', [])}
    out = []
    if states.get(TICK) is None:
        out.append(finding('TICK_MISSING', FAIL, 'Scheduler %s is absent' % TICK))
    elif states[TICK] != 'ENABLED':
        out.append(finding('TICK_NOT_ENABLED', DEGRADED, 'Scheduler %s is %s: controller is not woken' % (TICK, states[TICK]), subject=states[TICK]))
    enabled = sorted(n for n in ORDINARY if states.get(n) not in (None, 'PAUSED'))
    if enabled:
        out.append(finding('ORDINARY_SCHEDULER_NOT_PAUSED', FAIL, 'ordinary Scheduler not PAUSED: ' + ', '.join(enabled), subject=enabled))
    return out, dict(states={n: states.get(n) for n in (TICK,) + ORDINARY})


def recent_executions(reader, since):
    """Controller executions created since `since` (Run lists newest first; order is verified)."""
    out, token = [], ''
    for _ in range(10):
        x = reader.request('GET', run_url(reader.c, CONTROLLER, '/executions?pageSize=200' + ('&pageToken=' + urllib.parse.quote(token, safe='') if token else '')))
        page = x.get('executions', [])
        created = [at(e['createTime']) for e in page]
        out += [e for e, t in zip(page, created) if t >= since]
        token = x.get('nextPageToken') or ''
        if not token or (created == sorted(created, reverse=True) and created and created[-1] < since):
            return out
    raise ValueError('execution listing exceeds the watch budget')


def controller_facts(reader, since):
    """Status lines of the controller since `since`: [(timestamp, execution, status, progress)]."""
    c = reader.c
    flt = ('resource.type="cloud_run_job" AND resource.labels.job_name="%s" AND logName="projects/%s/logs/run.googleapis.com%%2Fstdout"'
           ' AND jsonPayload.status:* AND timestamp>="%s"') % (CONTROLLER, c['project_id'], since.isoformat().replace('+00:00', 'Z'))
    out = []
    for e in reader.logs(flt):
        p = e.get('jsonPayload') or {}
        progress = p.get('progress') if isinstance(p.get('progress'), dict) else {}
        out.append(dict(at=at(e['timestamp']), execution=(e.get('labels') or {}).get('run.googleapis.com/execution_name'),
                        status=p.get('status'), completed=progress.get('completed_chunks'), total=progress.get('total_chunks')))
    return sorted(out, key=lambda f: f['at'])


def check_executions(executions, facts, now):
    statuses = {}
    for f in facts:
        statuses.setdefault(f['execution'], []).append(f['status'])
    out, waits = [], []
    for e in sorted(executions, key=lambda e: e['createTime']):
        name = e['name'].rsplit('/', 1)[-1]
        start = at(e.get('startTime') or e['createTime'])
        done = at(e['completionTime']) if e.get('completionTime') else None
        seconds = ((done or now) - start).total_seconds()
        if seconds > WAKE_LIMIT_SECONDS:
            out.append(finding('EXECUTION_OVERRUN', FAIL, '%s %s %d s (> %d s, no margin to the 1800 s kill)' % (name, 'ran' if done else 'is running', seconds, WAKE_LIMIT_SECONDS), subject=name))
        if not done or not (e.get('failedCount') or e.get('cancelledCount')):
            continue
        seen = statuses.get(name, [])
        if 'STOPPED' in seen:
            out.append(finding('EXECUTION_STOPPED', FAIL, '%s STOPPED' % name, subject=name))
        elif WAIT_STATUSES & set(seen):
            waits.append(name)
        elif not seen and now - done < LOG_INDEX_GRACE:
            continue                    # logs not indexed yet: judged on the next evaluation
        else:
            out.append(finding('EXECUTION_FAILED_WITHOUT_WAIT', FAIL, '%s failed without a bounded wait (statuses: %s)' % (name, ','.join(s for s in seen if s) or 'none'), subject=name))
    if len(waits) >= READ_RETRY_DEGRADED:
        out.append(finding('REPEATED_READ_WAIT', DEGRADED, '%d bounded read waits in %d h' % (len(waits), LOOKBACK.total_seconds() // 3600), subject=len(waits)))
    return out, dict(executions=len(executions), running=sum(not e.get('completionTime') for e in executions), bounded_waits=len(waits))


def check_progress(facts, now, tick_enabled):
    """FULL progress: while chunks remain, at least one source step within PROGRESS_WINDOW."""
    progress = [f for f in facts if isinstance(f['total'], int) and isinstance(f['completed'], int)]
    info = dict(window_hours=PROGRESS_WINDOW.total_seconds() // 3600, steps=sum(f['status'] in PROGRESS_STATUSES for f in facts))
    if progress:
        info.update(completed_chunks=progress[-1]['completed'], total_chunks=progress[-1]['total'])
    covered = facts and facts[0]['at'] <= now - PROGRESS_WINDOW + timedelta(minutes=30)
    remaining = progress and progress[-1]['completed'] < progress[-1]['total']
    if tick_enabled and covered and remaining and not info['steps']:
        return [finding('NO_FULL_PROGRESS', DEGRADED, 'no DISPATCHED/RECONCILED in %d h, %d/%d chunks' % (info['window_hours'], info['completed_chunks'], info['total_chunks']))], info
    return [], info


# ------------------------------------------------------------------ evaluation
def evaluate(reader, clock, synthetic=None):
    """One read-only evaluation: (verdict record). Every check is isolated; a read failure is DEGRADED.

    `synthetic` (acceptance only) adds one SYNTHETIC_FAIL finding to an otherwise real evaluation, so the
    owner proves the whole path job -> log -> policy -> e-mail without a production STOP.
    """
    now = clock()
    findings, facts = [], {}
    if synthetic is not None:
        findings.append(finding('SYNTHETIC_FAIL', FAIL, 'SYNTHETIC FAIL %s: проверка доставки, не авария' % synthetic, subject=synthetic))

    timings = facts.setdefault('seconds', {})

    def guarded(name, fn):
        started = time.monotonic()
        try:
            got, info = fn()
            findings.extend(got)
            facts[name] = info
            return info
        except Exception as e:  # noqa: BLE001 - a broken read is reported, never a silent OK
            findings.append(finding('READ_FAILED', DEGRADED, '%s: %s' % (name, type(e).__name__), subject=name))
            return None
        finally:
            timings[name] = round(time.monotonic() - started, 1)

    def context():
        b, info, got = controller_context(reader, lambda: now)
        context.backend = b
        return got, info
    context.backend = None
    guarded('controller', context)
    b = context.backend
    if b is not None:
        guarded('stop', lambda: check_stop(b))
        guarded('current', lambda: check_current(b, now))
    schedulers = guarded('schedulers', lambda: check_schedulers(reader))
    log_facts = []

    def lines():
        log_facts.extend(controller_facts(reader, now - PROGRESS_WINDOW))
        return [], dict(lines=len(log_facts))
    guarded('logs', lines)
    if facts.get('logs') is not None:
        guarded('executions', lambda: check_executions(recent_executions(reader, now - LOOKBACK), log_facts, now))
        tick = (schedulers or {}).get('states', {}).get(TICK) == 'ENABLED'
        guarded('progress', lambda: check_progress(log_facts, now, tick))
    return verdict(findings, facts, now)


def verdict(findings, facts, now):
    level = max((f['level'] for f in findings), key=RANK.__getitem__, default=OK)
    key = sorted([f['code'], json.dumps(f['subject'], sort_keys=True)] for f in findings)
    worst = [f for f in findings if f['level'] == level]
    summary = 'OK' if level == OK else '; '.join(f['detail'] for f in worst)[:400]
    return dict(health=level, fingerprint=digest([level, key])[:16], reasons=sorted({f['code'] for f in findings}),
                summary=summary, findings=findings, facts=facts, evaluated_at=now.isoformat())


def previous(reader, now):
    """The watcher's own last HEALTH line (read-only). None when absent or unreadable."""
    flt = ('resource.type="cloud_run_job" AND resource.labels.job_name="%s" AND jsonPayload.heldi_watch="HEALTH"'
           ' AND timestamp>="%s"') % (WATCH_JOB, (now - SELF_HISTORY).isoformat().replace('+00:00', 'Z'))
    try:
        entries = reader.logs(flt, page_size=1, max_pages=1)
    except Exception:  # noqa: BLE001 - unknown previous state means: emit
        return None
    if not entries:
        return None
    p = entries[0].get('jsonPayload') or {}
    return dict(fingerprint=p.get('fingerprint'), health=p.get('health'), at=at(entries[0]['timestamp']))


def emit_health(v, prev, now):
    if prev is None or prev['fingerprint'] != v['fingerprint']:
        return True
    return v['health'] != OK and now - prev['at'] >= REMINDER


SEVERITY = {OK: 'INFO', DEGRADED: 'WARNING', FAIL: 'ERROR'}


def lines(v, prev, emit):
    """stdout records; `message` is the human summary Cloud Logging shows."""
    out = []
    if emit:
        out.append(dict(heldi_watch='HEALTH', tenant=TENANT, severity=SEVERITY[v['health']], health=v['health'],
                        previous=prev['health'] if prev else 'NONE', fingerprint=v['fingerprint'], reasons=v['reasons'],
                        reason_codes=','.join(v['reasons']) or 'NONE',
                        summary=v['summary'], message='Heldi %s: %s' % (v['health'], v['summary']),
                        findings=v['findings'], facts=v['facts'], evaluated_at=v['evaluated_at']))
    out.append(dict(heldi_watch='HEARTBEAT', tenant=TENANT, severity='INFO', health=v['health'], fingerprint=v['fingerprint'],
                    message='Heldi watch heartbeat: %s' % v['health'], evaluated_at=v['evaluated_at']))
    return out


def main(argv=None, *, send=None, clock=None, out=None):
    import argparse
    from tools.tenancy import tenant_backfill as BF
    from tools.tenancy import cloud_access as A
    parser = argparse.ArgumentParser(description='Heldi cloud watch (read-only)')
    parser.add_argument('--owner-token-command', help='owner dry-run: reader token command (no identity check, no change detection)')
    parser.add_argument('--synthetic-fail', metavar='TEST_ID', help='acceptance: add one SYNTHETIC_FAIL finding to a real evaluation (alert path check)')
    args = parser.parse_args(argv)
    if args.synthetic_fail is not None and not re.fullmatch(r'[A-Za-z0-9-]{4,40}', args.synthetic_fail):
        parser.error('TEST_ID: 4-40 of [A-Za-z0-9-]')
    send = send or A.wire
    clock = clock or (lambda: datetime.now(timezone.utc))
    out = out or sys.stdout
    try:
        c = deepcopy(BF.target(TENANT))
        reader = Reader(c, Tokens(c['project_id'], send, command=args.owner_token_command), send)
        reader.tokens.token('reader')               # identity first: a foreign identity never evaluates
    except Exception as e:  # noqa: BLE001
        print(json.dumps(dict(heldi_watch='UNAVAILABLE', tenant=TENANT, severity='ERROR', message='Heldi watch cannot run: ' + type(e).__name__)), file=out)
        return 3
    v = evaluate(reader, clock, synthetic=args.synthetic_fail)
    now = at(v['evaluated_at'])
    prev = None if args.owner_token_command else previous(reader, now)
    for line in lines(v, prev, args.owner_token_command is not None or emit_health(v, prev, now)):
        print(json.dumps(line, sort_keys=True, ensure_ascii=False, default=str), file=out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
