"""Execution/recovery orchestrator: all state mutations use guarded Store APIs."""
import json
from .domain import SafetyError, hit
from .fakes import FakeNK, FakeSigner, FakeSUZ, FakeTrueAPI


class Coordinator:
    def __init__(self, store, adapter):
        if type(adapter) not in (FakeSUZ, FakeTrueAPI, FakeNK):
            raise SafetyError('Разрешены только встроенные fake adapters')
        if (adapter.environment, adapter.participant) != (store.environment, store.participant):
            raise SafetyError('Неверная среда или identity адаптера')
        self.s, self.adapter = store, adapter

    def execute(self, op, approval, now, fault=None):
        with self.s.lock():
            r = self.s.get(op)
            if r['kind'] not in self.adapter.kinds:
                raise SafetyError('Неверный тип адаптера')
            packet = FakeSigner.packet(r)
            baseline = self.adapter.blocks() if r['kind'] == 'RETRIEVE' else ()
            attempt = self.s.start_attempt(op, approval, now, packet, baseline)
            packet = FakeSigner.packet(r, attempt)
            hit(fault, 'before_submit')
            try:
                result = self.adapter.submit(packet)
            except Exception:
                self.s.mark_unknown(op, attempt, now)
                return attempt
            hit(fault, 'after_http')
            self.s.record_submission(op, attempt, result, now)
            return attempt

    def restart(self, now):
        self.s.restart_attempts(now)

    def reconcile(self, op, now, fault=None):
        with self.s.lock():
            r = self.s.get(op)
            if r['kind'] not in self.adapter.kinds:
                raise SafetyError('Неверный тип адаптера')
            attempt = self.s.begin_reconciliation(op, now)
            a = self.s.db.execute('SELECT * FROM attempts WHERE id=?', (attempt,)).fetchone()
            hit(fault, 'during_reconcile')
            expected = a['external_id']
            try:
                if r['kind'] == 'RETRIEVE':
                    candidates = [expected] if expected else list(set(self.adapter.blocks()) - set(json.loads(a['baseline'])))
                    expected = candidates[0] if len(candidates) == 1 else None
                    result = self.adapter.retry_block(expected) if expected else None
                else:
                    result = self.adapter.readback(op, expected)
            except Exception:
                self.s.mark_unknown(op, attempt, now)
                return 'UNKNOWN'
            return self.s.finish_reconciliation(op, attempt, result, now, expected)
