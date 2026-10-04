"""Synthetic-only schema-2 store; public mutations own their lock and transaction."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .domain import Prepared, SafetyError, TRANSITIONS, canonical, digest, gtin, synthetic
from .fakes import Evidence, FakeSigner, Packet, verified_fake_evidence

REPO = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = REPO / 'tools/tests/chz_v2/.runtime'
HASH = re.compile(r'[0-9a-f]{64}')
ID = re.compile(r'[0-9a-f]{32}')
EXTERNAL = re.compile(r'SYN:external-[0-9]+')
EDGES = {a + '->' + b for a, targets in TRANSITIONS.items() for b in targets}
EDGES.discard('RECONCILING->ACCEPTED')  # No new receipt may replace reconciliation evidence.


def timestamp(value):
    if type(value) is not int or value < 0:
        raise SafetyError('Некорректное время')


def reference(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise SafetyError('Некорректная ссылка')


class Store:
    def __init__(self, root, environment, participant):
        root = Path(root)
        if environment not in {'PROD', 'SANDBOX'}:
            raise SafetyError('Среда обязательна')
        synthetic(participant)
        if RUNTIME_ROOT.is_symlink() or not root.resolve().is_relative_to(RUNTIME_ROOT) or root.is_symlink():
            raise SafetyError('State разрешён только в synthetic test directory')
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        if any(p.is_symlink() for p in self.root.rglob('*')):
            raise SafetyError('Ссылки в state запрещены')
        self.environment, self.participant = environment, participant
        self._lock_depth = 0
        self.db = sqlite3.connect(self.root / 'state.sqlite', isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.root / 'state.sqlite', 0o600)
        try:
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA recursive_triggers=ON')
            # Refuse old state BEFORE DDL. R2 supplies no automatic migration.
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                rows = self.db.execute('SELECT environment,participant,schema_version FROM metadata').fetchall()
                if len(rows) != 1 or tuple(rows[0]) != (environment, participant, 2):
                    raise SafetyError('Несовместимая версия или identity state; migration запрещена')
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=EXTRA')
            self.db.execute('PRAGMA fullfsync=ON')
            with self.lock():
                self.db.executescript(Path(__file__).with_name('schema.sql').read_text())
                with self.tx():
                    if not self.db.execute('SELECT 1 FROM metadata').fetchone():
                        self.db.execute('INSERT INTO metadata(environment,participant,schema_version) VALUES(?,?,2)', (environment, participant))
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def lock(self):
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        fd = os.open(self.root / 'executor.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SafetyError('Исполнитель занят') from None
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
        finally:
            os.close(fd)

    @contextmanager
    def tx(self):
        with self.lock():
            nested = self.db.in_transaction
            name = 's' + uuid.uuid4().hex
            self.db.execute('SAVEPOINT ' + name if nested else 'BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('RELEASE ' + name if nested else 'COMMIT')
            except BaseException:
                if nested:
                    self.db.execute('ROLLBACK TO ' + name)
                    self.db.execute('RELEASE ' + name)
                else:
                    self.db.execute('ROLLBACK')
                raise

    def _event_fields(self, op, transition, now, attempt, details):
        timestamp(now)
        is_operation = isinstance(op, str) and HASH.fullmatch(op)
        reference(op, HASH if is_operation else ID)
        row = self.db.execute('SELECT 1 FROM ' + ('operations' if is_operation else 'issues') + ' WHERE id=?', (op,)).fetchone()
        if not row or transition not in (EDGES | {'PREPARED'} if is_operation else {'QUEUE_RESERVED', 'QUEUE_COMMITTED'}):
            raise SafetyError('Недопустимое journal reference или transition')
        if attempt is not None:
            self._attempt(op, attempt)
        if not is_operation and attempt is not None:
            raise SafetyError('Attempt несовместим с выдачей')
        allowed = {'payload_hash', 'external_id', 'http_status', 'response_hash', 'outcome', 'count'}
        if set(details) - allowed:
            raise SafetyError('Недопустимое поле audit event')
        for key, value in details.items():
            valid = ((key.endswith('_hash') and isinstance(value, str) and HASH.fullmatch(value))
                     or (key == 'external_id' and (value is None or isinstance(value, str) and EXTERNAL.fullmatch(value)))
                     or (key == 'http_status' and type(value) is int and 100 <= value <= 599)
                     or (key == 'count' and type(value) is int and value >= 0)
                     or (key == 'outcome' and isinstance(value, str) and value in {'ACCEPTED', 'SUCCEEDED', 'REJECTED', 'PARTIAL', 'UNCERTAIN', 'RESTART', 'INSUFFICIENT'}))
            if not valid:
                raise SafetyError('Недопустимое значение audit event')

    def event(self, operation_id, transition, now, attempt_id=None, **details):
        with self.tx():
            self._event_fields(operation_id, transition, now, attempt_id, details)
            prev = self.db.execute('SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1').fetchone()
            prev = prev[0] if prev else ''
            body = [operation_id, attempt_id, self.environment, now, transition, details, prev]
            self.db.execute('INSERT INTO events(operation_id,attempt_id,environment,timestamp,transition,details,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?)',
                            (operation_id, attempt_id, self.environment, now, transition,
                             canonical(details).decode(), prev, digest(canonical(body))))

    def verify(self):
        meta = self.db.execute('SELECT environment,participant,schema_version FROM metadata').fetchall()
        if len(meta) != 1 or tuple(meta[0]) != (self.environment, self.participant, 2):
            raise SafetyError('Нарушена identity state')
        if self.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or self.db.execute('PRAGMA foreign_key_check').fetchone():
            raise SafetyError('Нарушена целостность базы')
        prev = ''
        for r in self.db.execute('SELECT * FROM events ORDER BY seq'):
            details = json.loads(r['details'])
            self._event_fields(r['operation_id'], r['transition'], r['timestamp'], r['attempt_id'], details)
            body = [r['operation_id'], r['attempt_id'], r['environment'], r['timestamp'], r['transition'], details, prev]
            if r['environment'] != self.environment or r['previous_hash'] != prev or r['event_hash'] != digest(canonical(body)):
                raise SafetyError('Нарушена целостность журнала')
            prev = r['event_hash']
        for a in self.db.execute('SELECT * FROM attempts'):
            packet = FakeSigner.packet(self.get(a['operation_id']), a['id'])
            if (a['business_hash'], a['transport_hash'], a['signed_hash'], a['wire_hash'], a['wire']) != (
                    packet.business_hash, digest(packet.transport), digest(packet.signed), digest(packet.wire), packet.wire):
                raise SafetyError('Нарушена целостность execution bytes')
        for e in self.db.execute('SELECT * FROM validated_evidence'):
            if digest(canonical(json.loads(e['body']))) != e['response_hash']:
                raise SafetyError('Нарушена целостность evidence')
        if self.db.execute("SELECT 1 FROM codes c LEFT JOIN reservations r ON r.code_id=c.id WHERE c.holder IS NOT r.owner_id LIMIT 1").fetchone():
            raise SafetyError('Нарушена целостность reservation')

    def get(self, op):
        reference(op, HASH)
        r = self.db.execute('SELECT * FROM operations WHERE id=?', (op,)).fetchone()
        if not r:
            raise SafetyError('Операция не найдена')
        p = Prepared.create(r['environment'], r['participant'], r['kind'], r['demand'], json.loads(r['payload']))
        if (p.operation_id, p.payload_hash, p.fingerprint, p.environment, p.participant) != (r['id'], r['payload_hash'], r['fingerprint'], self.environment, self.participant):
            raise SafetyError('Нарушена identity операции')
        return r

    def _attempt(self, op, attempt):
        reference(op, HASH); reference(attempt, ID)
        a = self.db.execute('SELECT * FROM attempts WHERE id=? AND operation_id=?', (attempt, op)).fetchone()
        if not a:
            raise SafetyError('Attempt не принадлежит operation')
        return a

    def _transition(self, op, target, now, attempt=None, **details):
        if not self.db.in_transaction or not self._lock_depth:
            raise SafetyError('Внутренний переход требует transaction')
        timestamp(now)
        old = self.get(op)['state']
        if old + '->' + target not in EDGES:
            raise SafetyError('Недопустимый переход')
        if attempt is not None:
            self._attempt(op, attempt)
        self.db.execute('UPDATE operations SET state=? WHERE id=?', (target, op))
        if attempt is not None:
            self.db.execute('UPDATE attempts SET state=? WHERE id=? AND operation_id=?', (target, attempt, op))
        self.event(op, old + '->' + target, now, attempt, **details)

    def transition(self, op, target, now, attempt=None, **details):
        # Public generic API supports only pre-submission controls, never authority/evidence states.
        if target not in {'CANCELLED', 'INVALIDATED'} or attempt is not None or details:
            raise SafetyError('Требуется специализированный evidence-bound API')
        with self.tx():
            self._transition(op, target, now)
            self.db.execute('DELETE FROM reservations WHERE owner_id=?', (op,))

    def prepare(self, prepared, now):
        timestamp(now)
        verified = Prepared.create(prepared.environment, prepared.participant, prepared.kind, prepared.demand, json.loads(prepared.payload))
        if verified != prepared or (prepared.environment, prepared.participant) != (self.environment, self.participant):
            raise SafetyError('Prepared identity не соответствует state')
        with self.tx():
            if not self.db.execute('SELECT 1 FROM operations WHERE id=?', (prepared.operation_id,)).fetchone():
                self.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)',
                                (prepared.operation_id, prepared.fingerprint, prepared.payload_hash, prepared.environment,
                                 prepared.participant, prepared.kind, prepared.demand, prepared.payload, 'PREPARED', now))
                p = json.loads(prepared.payload)
                bindings = [(c, 'DOCUMENT') for c in p['codes']]
                for unit in p.get('sets', []):
                    bindings += [(unit['parent'], 'PARENT'), *((c, 'CHILD') for c in unit['children'])]
                self.db.executemany('INSERT INTO operation_codes VALUES(?,?,?)', [(prepared.operation_id, c, role) for c, role in bindings])
                self.event(prepared.operation_id, 'PREPARED', now, payload_hash=prepared.payload_hash)
        return prepared.operation_id

    def approve(self, op, exact_hash, environment, owner, now, expires_at, reviewed_duplicates=()):
        synthetic(owner); timestamp(now); timestamp(expires_at); reference(exact_hash, HASH)
        if expires_at <= now:
            raise SafetyError('Approval уже истёк')
        with self.tx():
            r = self.get(op)
            if r['payload_hash'] != exact_hash or r['environment'] != environment:
                raise SafetyError('Approval не соответствует preview')
            duplicates = {x[0] for x in self.db.execute('SELECT id FROM operations WHERE fingerprint=? AND id<>?', (r['fingerprint'], op))}
            if duplicates != set(reviewed_duplicates):
                raise SafetyError('Владелец должен рассмотреть совпадающие demand')
            aid = uuid.uuid4().hex
            self.db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,0)', (aid, op, exact_hash, environment, self.participant, owner, now, expires_at, canonical(sorted(duplicates)).decode()))
            self._transition(op, 'APPROVED', now)
            return aid

    def add_code(self, code, product, position, status, now, source):
        synthetic(code); synthetic(source); gtin(product); timestamp(now)
        if status not in {'EMITTED', 'APPLIED', 'INTRODUCED', 'RETIRED', 'UNKNOWN'} or type(position) is not int or position < 1:
            raise SafetyError('Некорректный ledger input')
        with self.tx():
            self.db.execute('INSERT INTO codes VALUES(?,?,?,?,?,?,?,NULL)', (code, product, position, status, now, source, 'AVAILABLE' if status == 'INTRODUCED' else 'QUARANTINED'))

    def reserve_operation(self, op):
        with self.tx():
            operation = self.get(op)
            if operation['state'] != 'APPROVED':
                raise SafetyError('Reservation требует APPROVED operation')
            self.db.execute('INSERT OR IGNORE INTO reservation_owners VALUES(?,?,NULL)', (op, op))
            p = json.loads(operation['payload'])
            ids = p['codes'] + [c for u in p.get('sets', []) for c in [u['parent'], *u['children']]]
            if len(ids) != len(set(ids)):
                raise SafetyError('Повтор КИ')
            for code in ids:
                r = self.db.execute('SELECT * FROM codes WHERE id=?', (code,)).fetchone()
                allowed = {'UTILISATION': {'EMITTED'}, 'INTRODUCTION': {'APPLIED'}, 'SET': {'INTRODUCED'}}
                if (not r or r['external_status'] not in allowed.get(operation['kind'], set())
                        or r['allocation'] == 'ISSUED_TO_FF' or r['holder'] not in (None, op)
                        or operation['kind'] != 'SET' and r['gtin'] != p['gtin']
                        or self.db.execute('SELECT 1 FROM memberships WHERE child=? OR parent=?', (code, code)).fetchone()):
                    raise SafetyError('КИ недоступен для операции')
                if r['holder'] is None:
                    acquired = self.db.execute('INSERT INTO reservations SELECT id,? FROM codes WHERE id=? AND holder IS NULL AND allocation IN (\'AVAILABLE\',\'QUARANTINED\')', (op, code)).rowcount
                    if acquired != 1:
                        raise SafetyError('Reservation не захвачена')

    def start_attempt(self, op, approval, now, packet, baseline=()):
        timestamp(now); reference(approval, ID)
        with self.tx():
            self.verify(); r = self.get(op)
            if type(packet) is not Packet or (packet.operation_id, packet.kind, packet.environment, packet.participant) != (op, r['kind'], self.environment, self.participant):
                raise SafetyError('Неверный packet binding')
            FakeSigner.verify(packet, r['payload'])
            if packet.attempt_id:
                raise SafetyError('Attempt identity назначает Store')
            for ext in baseline: reference(ext, EXTERNAL)
            a = self.db.execute('SELECT * FROM approvals WHERE id=?', (approval,)).fetchone()
            if (r['state'] != 'APPROVED' or not a or a['operation_id'] != op or a['consumed'] or not a['created_at'] <= now < a['expires_at']
                    or (a['payload_hash'], a['environment'], a['participant']) != (r['payload_hash'], self.environment, self.participant)):
                raise SafetyError('Нет действующего exact approval')
            duplicates = sorted(x[0] for x in self.db.execute('SELECT id FROM operations WHERE fingerprint=? AND id<>?', (r['fingerprint'], op)))
            if duplicates != json.loads(a['duplicate_review']):
                raise SafetyError('После approval появился новый совпадающий demand')
            self.reserve_operation(op)
            self.db.execute('UPDATE approvals SET consumed=1 WHERE id=?', (approval,))
            attempt = uuid.uuid4().hex
            packet = FakeSigner.packet(r, attempt)
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (attempt, op, approval, 'SUBMITTING', now, None, r['payload_hash'], digest(packet.transport), digest(packet.signed), digest(packet.wire),
                             packet.wire, canonical(sorted(baseline)).decode(), None, self.environment, self.participant))
            self._transition(op, 'SUBMITTING', now, attempt)
            return attempt

    def mark_unknown(self, op, attempt, now, outcome='UNCERTAIN'):
        if outcome not in {'UNCERTAIN', 'RESTART', 'INSUFFICIENT'}:
            raise SafetyError('Недопустимый recovery outcome')
        with self.tx():
            a = self._attempt(op, attempt)
            r = self.get(op)
            latest = self.db.execute('SELECT id FROM attempts WHERE operation_id=? ORDER BY rowid DESC LIMIT 1', (op,)).fetchone()[0]
            if latest != attempt or a['state'] != r['state'] or r['state'] not in {'SUBMITTING', 'RECONCILING'}:
                raise SafetyError('Нет активного inflight attempt')
            self._transition(op, 'UNKNOWN', now, attempt, outcome=outcome)

    def begin_reconciliation(self, op, now):
        with self.tx():
            self.verify(); r = self.get(op)
            a = self.db.execute('SELECT * FROM attempts WHERE operation_id=? ORDER BY rowid DESC LIMIT 1', (op,)).fetchone()
            if r['state'] not in {'UNKNOWN', 'ACCEPTED'} or not a or a['state'] != r['state']:
                raise SafetyError('Нет операции для сверки')
            self._transition(op, 'RECONCILING', now, a['id'])
            return a['id']

    def _valid_evidence(self, r, a, result, phase, expected_external=None):
        if not verified_fake_evidence(result):
            return False
        if result.attempt_id != a['id']:
            return False
        if (result.operation_id, result.payload_hash, result.environment, result.participant, result.kind) != (r['id'], r['payload_hash'], self.environment, self.participant, r['kind']):
            return False
        if type(result.complete) is not bool or type(result.http_status) is not int or type(result.codes) is not tuple:
            return False
        expected = a['external_id'] or expected_external
        if expected is not None and result.external_id != expected:
            return False
        outcomes = {'ACCEPTED', 'REJECTED'} if phase == 'SUBMISSION' else {'SUCCEEDED', 'REJECTED', 'PARTIAL'}
        if result.outcome not in outcomes or result.complete != (result.outcome != 'ACCEPTED'):
            return False
        if result.outcome == 'REJECTED':
            if not 400 <= result.http_status <= 599 or result.codes:
                return False
        elif not 200 <= result.http_status <= 299 or not isinstance(result.external_id, str) or not EXTERNAL.fullmatch(result.external_id):
            return False
        if result.external_id is not None and (not isinstance(result.external_id, str) or not EXTERNAL.fullmatch(result.external_id)):
            return False
        try:
            for code in result.codes: synthetic(code)
        except SafetyError:
            return False
        if len(set(result.codes)) != len(result.codes) or r['kind'] != 'RETRIEVE' and result.codes:
            return False
        if r['kind'] == 'RETRIEVE' and result.outcome == 'SUCCEEDED':
            if len(result.codes) != json.loads(r['payload'])['quantity']:
                return False
            if any(self.db.execute('SELECT 1 FROM codes WHERE id=?', (c,)).fetchone() for c in result.codes):
                return False
        if result.outcome == 'SUCCEEDED' and r['kind'] in {'UTILISATION', 'INTRODUCTION', 'SET'}:
            p = json.loads(r['payload'])
            items = p['codes'] + [c for u in p.get('sets', []) for c in [u['parent'], *u['children']]]
            for c in items:
                if not self.db.execute("SELECT 1 FROM codes c JOIN reservations x ON x.code_id=c.id WHERE c.id=? AND c.holder=? AND x.owner_id=? AND c.allocation='RESERVED'", (c, r['id'], r['id'])).fetchone():
                    return False
        return True

    def _save_evidence(self, op, attempt, result, phase, now):
        self.db.execute('INSERT INTO validated_evidence VALUES(?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex, op, attempt, phase, result.outcome, canonical(result.body()).decode(), result.safe()['response_hash'], now))
        self.db.execute('UPDATE attempts SET evidence=?,external_id=COALESCE(external_id,?) WHERE id=?', (canonical(result.safe()).decode(), result.external_id, attempt))

    def _release(self, op):
        self.db.execute('DELETE FROM reservations WHERE owner_id=?', (op,))

    def record_submission(self, op, attempt, result, now):
        with self.tx():
            self.verify(); r = self.get(op); a = self._attempt(op, attempt)
            latest = self.db.execute('SELECT id FROM attempts WHERE operation_id=? ORDER BY rowid DESC LIMIT 1', (op,)).fetchone()[0]
            if r['state'] != 'SUBMITTING' or a['state'] != 'SUBMITTING' or latest != attempt:
                raise SafetyError('Нет SUBMITTING attempt')
            if not self._valid_evidence(r, a, result, 'SUBMISSION'):
                self._transition(op, 'UNKNOWN', now, attempt, outcome='INSUFFICIENT')
                return 'UNKNOWN'
            self._save_evidence(op, attempt, result, 'SUBMISSION', now)
            self._transition(op, result.outcome, now, attempt, **result.safe())
            if result.outcome == 'REJECTED': self._release(op)
            return result.outcome

    def finish_reconciliation(self, op, attempt, result, now, expected_external=None):
        with self.tx():
            self.verify(); r = self.get(op); a = self._attempt(op, attempt)
            latest = self.db.execute('SELECT id FROM attempts WHERE operation_id=? ORDER BY rowid DESC LIMIT 1', (op,)).fetchone()[0]
            if r['state'] != 'RECONCILING' or a['state'] != 'RECONCILING' or latest != attempt:
                raise SafetyError('Нет актуального RECONCILING attempt')
            if expected_external is not None: reference(expected_external, EXTERNAL)
            if not self._valid_evidence(r, a, result, 'RECONCILIATION', expected_external):
                self._transition(op, 'UNKNOWN', now, attempt, outcome='INSUFFICIENT')
                return 'UNKNOWN'
            self._save_evidence(op, attempt, result, 'RECONCILIATION', now)
            p = json.loads(r['payload'])
            if result.outcome == 'SUCCEEDED':
                if r['kind'] == 'RETRIEVE':
                    start = self.db.execute('SELECT COALESCE(MAX(position),0) FROM codes').fetchone()[0]
                    for i, code in enumerate(result.codes):
                        self.add_code(code, p['gtin'], start+i+1, 'EMITTED', now, result.external_id)
                elif r['kind'] == 'SET':
                    for u in p['sets']:
                        self.db.executemany('INSERT INTO memberships VALUES(?,?,?)', [(c, u['parent'], op) for c in u['children']])
                elif r['kind'] in {'UTILISATION', 'INTRODUCTION'}:
                    status = 'APPLIED' if r['kind'] == 'UTILISATION' else 'INTRODUCED'
                    self.db.executemany('UPDATE codes SET external_status=?,observed_at=?,source=? WHERE id=? AND holder=?', [(status, now, result.external_id, c, op) for c in p['codes']])
                    self._release(op)
                    if status == 'INTRODUCED':
                        self.db.executemany("UPDATE codes SET allocation='AVAILABLE' WHERE id=?", [(c,) for c in p['codes']])
            if result.outcome == 'REJECTED': self._release(op)
            self._transition(op, result.outcome, now, attempt, **result.safe())
            return result.outcome

    def restart_attempts(self, now):
        with self.tx():
            self.verify()
            for a in self.db.execute("SELECT * FROM attempts WHERE state IN ('SUBMITTING','RECONCILING')").fetchall():
                self.mark_unknown(a['operation_id'], a['id'], now, 'RESTART')
