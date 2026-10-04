"""R1-01..06 adversarial regression. All data/state is synthetic and private."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import multiprocessing
import os
import sqlite3
import unittest
from unittest.mock import patch

from . import test_core as boundary
from tools.chz.v2.domain import SafetyError, canonical, digest
from tools.chz.v2.fakes import Evidence, FakeSigner, FakeTrueAPI
from tools.chz.v2.coordinator import Coordinator
from tools.chz.v2.queue import Queue
from tools.chz.v2.store import Store

PRODUCT, PARTICIPANT = boundary.PRODUCT, boundary.PARTICIPANT


def reservation_worker(root, op, link, first):
    s = Store(root, 'SANDBOX', PARTICIPANT)
    try:
        link.send('ready'); link.recv()
        if first:
            with s.lock():
                link.send('locked'); link.recv(); s.reserve_operation(op)
            link.send('reserved')
        else:
            for _ in range(2):
                try: s.reserve_operation(op)
                except SafetyError: link.send('blocked')
                else: link.send('unexpected-success')
                if _ == 0: link.recv()
    finally:
        s.close(); link.close()


class R2Tests(unittest.TestCase):
    setUp = boundary.CoreTests.setUp
    tearDown = boundary.CoreTests.tearDown
    prepared = boundary.CoreTests.prepared
    ready = boundary.CoreTests.ready
    codes = boundary.CoreTests.codes
    reopen = boundary.CoreTests.reopen

    def attempt(self):
        p, a = self.ready()
        aid = self.c.execute(p.operation_id, a, 3)
        return p, a, aid, next(iter(self.api.records.values()))

    def committed(self):
        self.codes(); q = Queue(self.s); issue = q.reserve(PRODUCT, 2, 2)
        q.build(issue); q.commit(issue, 3)
        return q, issue

    def test_r1_03_unknown_reservation_cannot_be_released_by_sql(self):
        self.codes(2)
        p, approval = self.ready(kind='SET', payload={'gtin': PRODUCT,
            'sets': [{'parent': 'SYN:code-0', 'children': ['SYN:code-1']}]})
        api = FakeTrueAPI('SANDBOX', PARTICIPANT)
        coordinator = Coordinator(self.s, api)
        api.mode = 'timeout'
        attempt = coordinator.execute(p.operation_id, approval, 3)
        self.assertEqual(api.submission_count, 1)
        self.assertEqual(next(iter(api.records.values())).outcome, 'SUCCEEDED')
        for code in ('SYN:code-0', 'SYN:code-1'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.s.db.execute('DELETE FROM reservations WHERE code_id=?', (code,))
        self.reopen(); coordinator = Coordinator(self.s, api)
        coordinator.restart(4)
        self.assertEqual(self.s.get(p.operation_id)['state'], 'UNKNOWN')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM reservations WHERE owner_id=?', (p.operation_id,)).fetchone()[0], 2)
        self.assertEqual(self.s.db.execute('SELECT state FROM attempts WHERE id=?', (attempt,)).fetchone()[0], 'UNKNOWN')
        self.assertEqual(coordinator.reconcile(p.operation_id, 5), 'SUCCEEDED')
        self.assertEqual(self.s.db.execute('SELECT operation_id FROM memberships').fetchone()[0], p.operation_id)
        self.assertEqual(api.submission_count, 1)

    def test_r1_01_generic_public_terminal_and_approval_bypass_rejected(self):
        p, approval = self.ready(); self.api.mode = 'timeout'
        attempt = self.c.execute(p.operation_id, approval, 3)
        before = self.s.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        for target in ('RECONCILING', 'REJECTED', 'SUCCEEDED', 'APPROVED', 'SUBMITTING'):
            with self.subTest(target=target):
                with self.assertRaises(SafetyError): self.s.transition(p.operation_id, target, 4, attempt)
        with self.assertRaises(SafetyError): self.s.approve(p.operation_id, p.payload_hash, 'SANDBOX', 'SYN:owner', 5, 100)
        self.assertEqual(self.s.get(p.operation_id)['state'], 'UNKNOWN')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], before)
        self.assertEqual(self.api.submission_count, 1)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)

    def test_r1_01_previous_attempt_evidence_cannot_resolve_current_attempt(self):
        p, approval = self.ready()
        self.api.mode = 'reject'
        first = self.c.execute(p.operation_id, approval, 3)
        old = next(iter(self.api.records.values()))
        fresh = self.s.approve(p.operation_id, p.payload_hash, 'SANDBOX', 'SYN:owner', 4, 100)
        self.api.mode = 'timeout'
        second = self.c.execute(p.operation_id, fresh, 5)
        self.assertNotEqual(first, second)
        self.assertEqual(old.attempt_id, first)
        self.s.begin_reconciliation(p.operation_id, 6)
        self.assertEqual(self.s.finish_reconciliation(p.operation_id, second, old, 7), 'UNKNOWN')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM validated_evidence WHERE attempt_id=?', (second,)).fetchone()[0], 0)
        self.assertIsNone(self.s.db.execute('SELECT external_id FROM attempts WHERE id=?', (second,)).fetchone()[0])
        self.reopen()
        self.c.restart(8)
        with self.assertRaises(SafetyError): self.c.execute(p.operation_id, fresh, 9)
        # Deliberately present a valid current reply: fake ambiguity is not silently resolved.
        current = [v for v in self.api.records.values() if v.attempt_id == second][0]
        self.s.begin_reconciliation(p.operation_id, 10)
        self.assertEqual(self.s.finish_reconciliation(p.operation_id, second, current, 11), 'SUCCEEDED')
        self.assertEqual(self.api.submission_count, 2)

    def test_r1_01_wrong_operation_attempt_cannot_resolve_or_mark_unknown(self):
        p, _, aid, result = self.attempt()
        q, approval = self.ready('SYN:other', payload={'gtin': PRODUCT, 'quantity': 3})
        other = self.c.execute(q.operation_id, approval, 4)
        self.s.begin_reconciliation(p.operation_id, 5)
        with self.assertRaises(SafetyError): self.s.finish_reconciliation(p.operation_id, other, result, 6)
        with self.assertRaises(SafetyError): self.s.mark_unknown(p.operation_id, other, 6)
        self.assertEqual(self.s.get(p.operation_id)['state'], 'RECONCILING')
        self.assertEqual(self.s.get(q.operation_id)['state'], 'ACCEPTED')
        self.assertEqual(self.s.finish_reconciliation(p.operation_id, aid, result, 7), 'SUCCEEDED')
        self.assertEqual(self.api.submission_count, 2)

    def test_r1_01_incomplete_contradictory_wrong_type_evidence_fail_closed(self):
        p, _, aid, original = self.attempt()
        bad = [None, {}]
        for mode in ('readback_incomplete', 'readback_wrong_hash', 'readback_wrong_env', 'readback_wrong_kind',
                     'readback_wrong_participant', 'readback_contradiction', 'readback_wrong_status'):
            self.api.mode = mode
            bad.append(self.api.readback(p.operation_id, original.external_id))
        for i, result in enumerate(bad):
            with self.subTest(case=i):
                self.s.begin_reconciliation(p.operation_id, 5+i)
                self.assertEqual(self.s.finish_reconciliation(p.operation_id, aid, result, 6+i), 'UNKNOWN')
                with self.assertRaises(SafetyError): self.c.execute(p.operation_id, self.s.db.execute('SELECT id FROM approvals').fetchone()[0], 7+i)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM validated_evidence').fetchone()[0], 1)
        self.assertEqual(self.api.submission_count, 1)

    def test_r1_02_direct_reservation_late_failure_rolls_back_all_items(self):
        self.codes(1, status='EMITTED')
        p, _ = self.ready(kind='UTILISATION', payload={'gtin': PRODUCT, 'codes': ['SYN:code-0', 'SYN:zmissing']})
        with self.assertRaises(SafetyError): self.s.reserve_operation(p.operation_id)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0], 0)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM reservation_owners').fetchone()[0], 0)
        self.assertIsNone(self.s.db.execute('SELECT holder FROM codes').fetchone()[0])
        self.assertFalse(self.s.db.in_transaction)

    def test_r1_02_public_control_and_journal_atomic_on_event_failure(self):
        p, _ = self.ready()
        before = self.s.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        with patch.object(self.s, 'event', side_effect=SafetyError('synthetic journal failure')):
            with self.assertRaises(SafetyError): self.s.transition(p.operation_id, 'INVALIDATED', 3)
        self.assertEqual(self.s.get(p.operation_id)['state'], 'APPROVED')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], before)
        self.s.transition(p.operation_id, 'INVALIDATED', 4)
        self.assertEqual(self.s.get(p.operation_id)['state'], 'INVALIDATED')

    def test_r1_02_nested_public_failure_savepoint_does_not_leak_reservation(self):
        self.codes(1, status='EMITTED')
        p, _ = self.ready(kind='UTILISATION', payload={'gtin': PRODUCT, 'codes': ['SYN:code-0', 'SYN:zmissing']})
        with self.s.tx():
            with self.assertRaises(SafetyError): self.s.reserve_operation(p.operation_id)
            self.s.add_code('SYN:new', PRODUCT, 2, 'EMITTED', 4, 'SYN:source')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0], 0)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM codes').fetchone()[0], 2)

    def test_r1_02_synchronized_public_reservations_one_child_two_sets(self):
        self.codes(3)
        first = self.prepared('SYN:first', 'SET', {'gtin': PRODUCT, 'sets': [{'parent': 'SYN:code-0', 'children': ['SYN:code-2']}]})
        second = self.prepared('SYN:second', 'SET', {'gtin': PRODUCT, 'sets': [{'parent': 'SYN:code-1', 'children': ['SYN:code-2']}]})
        for p in (first, second):
            self.s.prepare(p, 1); self.s.approve(p.operation_id, p.payload_hash, 'SANDBOX', 'SYN:owner', 2, 100)
        ctx = multiprocessing.get_context('fork'); links = [boundary.pipe_links(ctx) for _ in range(2)]
        jobs = [ctx.Process(target=reservation_worker, args=(self.root, p.operation_id, links[i][1], i==0)) for i,p in enumerate((first,second))]
        for i, proc in enumerate(jobs):
            proc.start(); self.assertTrue(links[i][0].poll(10)); self.assertEqual(links[i][0].recv(), 'ready')
        links[0][0].send('go'); self.assertTrue(links[0][0].poll(10)); self.assertEqual(links[0][0].recv(), 'locked')
        links[1][0].send('contend'); self.assertTrue(links[1][0].poll(10)); self.assertEqual(links[1][0].recv(), 'blocked')
        links[0][0].send('reserve'); self.assertTrue(links[0][0].poll(10)); self.assertEqual(links[0][0].recv(), 'reserved')
        jobs[0].join(10); self.assertEqual(jobs[0].exitcode, 0)
        links[1][0].send('retry'); self.assertTrue(links[1][0].poll(10)); self.assertEqual(links[1][0].recv(), 'blocked')
        jobs[1].join(10); self.assertEqual(jobs[1].exitcode, 0)
        for pair in links:
            for connection in pair: connection.close()
        self.assertEqual(self.s.db.execute('SELECT holder FROM codes WHERE id=?', ('SYN:code-2',)).fetchone()[0], first.operation_id)
        self.assertIsNone(self.s.db.execute('SELECT holder FROM codes WHERE id=?', ('SYN:code-1',)).fetchone()[0])
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0], 2)
        self.assertEqual(self.api.submission_count, 0)

    def test_r1_03_direct_sql_enums_dangling_owner_and_singleton_rejected(self):
        self.codes(1); p, _ = self.ready()
        statements = ["UPDATE operations SET state='BAD'", "UPDATE codes SET external_status='BAD'",
                      "UPDATE codes SET allocation='BAD'", "UPDATE codes SET allocation='RESERVED'",
                      "UPDATE codes SET allocation='RESERVED',holder='missing'",
                      "INSERT INTO metadata(environment,participant,schema_version) VALUES('SANDBOX','SYN:other',2)"]
        for sql in statements:
            with self.subTest(statement=statements.index(sql)):
                with self.assertRaises(sqlite3.IntegrityError): self.s.db.execute(sql)
        self.assertEqual(self.s.get(p.operation_id)['state'], 'APPROVED')
        self.s.verify()

    def test_r1_03_direct_sql_attempt_requires_consumption_and_matching_approval(self):
        p, approval = self.ready()
        q, other = self.ready('SYN:q', payload={'gtin': PRODUCT, 'quantity': 3})
        packet = FakeSigner.packet(self.s.get(p.operation_id))
        def insert(a):
            self.s.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             ('a'*32, p.operation_id, a, 'SUBMITTING', 3, None, p.payload_hash,
                              digest(packet.transport), digest(packet.signed), digest(packet.wire), packet.wire, '[]', None, 'SANDBOX', PARTICIPANT))
        with self.assertRaises(sqlite3.IntegrityError): insert(approval)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.s.tx():
                self.s.db.execute('UPDATE approvals SET consumed=1 WHERE id=?', (other,)); insert(other)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
        self.assertEqual(self.s.db.execute('SELECT consumed FROM approvals WHERE id=?', (other,)).fetchone()[0], 0)

    def test_r1_03_sql_two_active_attempts_for_one_operation_rejected(self):
        p, approval = self.ready()
        original = tuple(self.s.db.execute('SELECT * FROM approvals WHERE id=?', (approval,)).fetchone())
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'UNIQUE constraint failed: attempts.operation_id'):
            with self.s.tx():
                clone = list(original); clone[0] = 'b' * 32
                self.s.db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?)', clone)
                self.s.db.execute('UPDATE approvals SET consumed=1 WHERE operation_id=?', (p.operation_id,))
                for attempt, owner_approval in (('c' * 32, approval), ('d' * 32, clone[0])):
                    packet = FakeSigner.packet(self.s.get(p.operation_id), attempt)
                    self.s.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (attempt, p.operation_id, owner_approval, 'SUBMITTING', 3, None, p.payload_hash,
                         digest(packet.transport), digest(packet.signed), digest(packet.wire), packet.wire,
                         '[]', None, 'SANDBOX', PARTICIPANT))
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
        self.assertEqual(self.s.db.execute('SELECT consumed FROM approvals WHERE id=?', (approval,)).fetchone()[0], 0)

    def test_r1_03_direct_sql_terminal_without_evidence_rejected(self):
        p, approval = self.ready(); self.api.mode='timeout'; aid=self.c.execute(p.operation_id, approval, 3)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.s.tx():
                self.s.db.execute("UPDATE operations SET state='RECONCILING' WHERE id=?", (p.operation_id,))
                self.s.db.execute("UPDATE attempts SET state='RECONCILING' WHERE id=?", (aid,))
                self.s.db.execute("UPDATE operations SET state='REJECTED' WHERE id=?", (p.operation_id,))
        self.assertEqual(self.s.get(p.operation_id)['state'], 'UNKNOWN')
        self.assertEqual(self.api.submission_count, 1)

    def test_r1_03_sql_membership_unique_and_ownership_enforced(self):
        self.codes(3)
        p, approval = self.ready(kind='SET', payload={'gtin': PRODUCT, 'sets': [{'parent':'SYN:code-0','children':['SYN:code-2']}]})
        api=FakeTrueAPI('SANDBOX',PARTICIPANT); c=Coordinator(self.s, api)
        c.execute(p.operation_id,approval,3); self.assertEqual(c.reconcile(p.operation_id,4),'SUCCEEDED')
        with self.assertRaises(sqlite3.IntegrityError): self.s.db.execute('INSERT INTO memberships VALUES(?,?,?)', ('SYN:code-2','SYN:code-1',p.operation_id))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.s.tx():
                self.s.db.execute('DROP TRIGGER membership_acquire')
                self.s.db.execute('INSERT INTO memberships VALUES(?,?,?)', ('SYN:code-2','SYN:code-1',p.operation_id))
        with self.assertRaises(sqlite3.IntegrityError): self.s.db.execute('DELETE FROM reservations WHERE code_id=?', ('SYN:code-2',))
        self.assertEqual(api.submission_count, 1)

    def test_r1_03_sql_code_cannot_participate_in_two_issues(self):
        q, issue = self.committed()
        other = q.reserve(PRODUCT, 1, 5)
        with self.assertRaises(sqlite3.IntegrityError): self.s.db.execute('INSERT INTO issue_items VALUES(?,?)', (other, 'SYN:code-0'))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.s.tx():
                self.s.db.execute('DROP TRIGGER issue_item_insert')
                self.s.db.execute('INSERT INTO issue_items VALUES(?,?)', (other, 'SYN:code-0'))
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM issue_items WHERE code_id=?', ('SYN:code-0',)).fetchone()[0], 1)
        self.assertEqual(q.receipt(issue)['state'], 'COMMITTED')

    def test_r1_04_committed_composition_and_business_commit_immutable(self):
        q, issue = self.committed(); before=q.receipt(issue)
        changes = [('DELETE FROM issue_items WHERE issue_id=?', (issue,)),
                   ('UPDATE issue_items SET code_id=? WHERE issue_id=?', ('SYN:code-4',issue)),
                   ('INSERT INTO issue_items VALUES(?,?)', (issue,'SYN:code-4')),
                   ('UPDATE issues SET manifest=? WHERE id=?', ('0'*64,issue)),
                   ('DELETE FROM issues WHERE id=?', (issue,)),
                   ('DELETE FROM reservations WHERE owner_id=?', (issue,))]
        for i,(sql,args) in enumerate(changes):
            with self.subTest(change=i):
                with self.assertRaises(sqlite3.IntegrityError): self.s.db.execute(sql,args)
        self.assertEqual(q.receipt(issue),before)

    def test_r1_04_correlated_artifact_and_row_corruption_cannot_change_receipt(self):
        q,issue=self.committed(); old=self.s.db.execute('SELECT manifest FROM issues WHERE id=?',(issue,)).fetchone()[0]
        # Simulate corruption beyond normal DB permissions, without removing commit protection.
        self.s.db.execute('DROP TRIGGER immutable_issue_item_update')
        self.s.db.execute('UPDATE issue_items SET code_id=? WHERE issue_id=? AND code_id=?',('SYN:code-4',issue,'SYN:code-0'))
        data=q._manifest(issue); bundle=self.root/('bundle-'+issue)
        (bundle/'items.json').write_bytes(data); (bundle/'manifest.sha256').write_text(digest(data))
        with self.assertRaises(SafetyError): q.receipt(issue)
        self.assertEqual(self.s.db.execute('SELECT manifest FROM issues WHERE id=?',(issue,)).fetchone()[0],old)

    def test_r1_04_missing_and_partial_reservation_ownership_fail_commit(self):
        self.codes(6); q=Queue(self.s)
        for loss in (1,2):
            issue=q.reserve(PRODUCT,2,2); q.build(issue)
            ids=[r[0] for r in self.s.db.execute('SELECT code_id FROM issue_items WHERE issue_id=? ORDER BY code_id',(issue,))]
            self.s.db.executemany('DELETE FROM reservations WHERE code_id=?',[(c,) for c in ids[:loss]])
            before=self.s.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            with self.assertRaises(SafetyError): q.commit(issue,3)
            row=self.s.db.execute('SELECT state,manifest FROM issues WHERE id=?',(issue,)).fetchone()
            self.assertEqual(tuple(row),('RESERVED',None))
            self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],before)
            with self.assertRaises(SafetyError): q.receipt(issue)

    def test_r1_04_rowcount_mismatch_rolls_back_commit(self):
        self.codes(); q=Queue(self.s); issue=q.reserve(PRODUCT,2,2); q.build(issue)
        self.s.db.execute("CREATE TRIGGER synthetic_ignore BEFORE UPDATE OF allocation ON codes WHEN NEW.allocation='ISSUED_TO_FF' AND NEW.id='SYN:code-1' BEGIN SELECT RAISE(IGNORE); END")
        before=self.s.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        with self.assertRaises(SafetyError): q.commit(issue,3)
        self.assertEqual(self.s.db.execute('SELECT state FROM issues WHERE id=?',(issue,)).fetchone()[0],'RESERVED')
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM codes WHERE allocation='ISSUED_TO_FF'").fetchone()[0],0)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],before)
        self.s.db.execute('DROP TRIGGER synthetic_ignore'); q.commit(issue,4)
        self.assertEqual(q.receipt(issue)['state'],'COMMITTED')

    def test_r1_04_sql_null_missing_manifest_fields_fail_closed(self):
        self.codes(); q=Queue(self.s); issue=q.reserve(PRODUCT,2,2); q.build(issue)
        for body in (b'{}', canonical({'simulation':True,'issue':issue,'environment':'SANDBOX','items':[]})):
            with self.assertRaises(sqlite3.IntegrityError):
                self.s.db.execute("UPDATE issues SET state='COMMITTED',manifest=?,committed_payload=? WHERE id=?",('0'*64,body,issue))
        self.assertEqual(q.recovery_state(issue),'NON_DELIVERABLE_PENDING_RECOVERY')

    def test_r1_04_os_process_loss_inside_sqlite_commit_recovers(self):
        self.codes(); q=Queue(self.s); issue=q.reserve(PRODUCT,2,2); q.build(issue)
        before=self.s.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        fingerprint=digest((self.root/('bundle-'+issue)/'items.json').read_bytes())
        pid=os.fork()
        if pid==0:
            s=Store(self.root,'SANDBOX',PARTICIPANT)
            def crash(_fault,point):
                if point=='during_issue_commit': os._exit(74)
            with patch('tools.chz.v2.queue.hit',side_effect=crash): Queue(s).commit(issue,3)
            os._exit(75)
        _,status=os.waitpid(pid,0); self.assertEqual(os.waitstatus_to_exitcode(status),74)
        self.reopen(); q=Queue(self.s)
        self.assertEqual(q.recovery_state(issue),'NON_DELIVERABLE_PENDING_RECOVERY')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],before)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM codes WHERE allocation='ISSUED_TO_FF'").fetchone()[0],0)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],2)
        q.commit(issue,4); self.assertEqual(q.receipt(issue)['bundle_hash'],fingerprint)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM issues').fetchone()[0],1)

    def test_r1_05_bad_evidence_preserves_trusted_external_id_and_history(self):
        p,_,aid,original=self.attempt(); trusted=original.external_id
        for mode in ('readback_wrong_id','readback_incomplete','readback_wrong_hash'):
            self.api.mode=mode; result=self.api.readback(p.operation_id,trusted)
            self.s.begin_reconciliation(p.operation_id,4)
            self.assertEqual(self.s.finish_reconciliation(p.operation_id,aid,result,5),'UNKNOWN')
            self.assertEqual(self.s.db.execute('SELECT external_id FROM attempts WHERE id=?',(aid,)).fetchone()[0],trusted)
            self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM validated_evidence').fetchone()[0],1)
        self.s.begin_reconciliation(p.operation_id,6)
        self.assertEqual(self.s.finish_reconciliation(p.operation_id,aid,original,7),'SUCCEEDED')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM validated_evidence').fetchone()[0],2)
        with self.assertRaises(sqlite3.IntegrityError): self.s.db.execute('UPDATE attempts SET external_id=? WHERE id=?',('SYN:external-999',aid))
        self.assertEqual(self.api.submission_count,1)

    def test_r1_05_unknown_correlation_set_only_by_valid_recovery(self):
        p,approval=self.ready(); self.api.mode='timeout'; aid=self.c.execute(p.operation_id,approval,3)
        original=next(iter(self.api.records.values()))
        self.s.begin_reconciliation(p.operation_id,4)
        self.assertEqual(self.s.finish_reconciliation(p.operation_id,aid,replace(original,complete=False),5),'UNKNOWN')
        self.assertIsNone(self.s.db.execute('SELECT external_id FROM attempts').fetchone()[0])
        self.assertEqual(self.c.reconcile(p.operation_id,6),'SUCCEEDED')
        self.assertEqual(self.s.db.execute('SELECT external_id FROM attempts').fetchone()[0],original.external_id)
        self.assertEqual(self.api.submission_count,1)

    def test_r1_06_all_public_journal_string_fields_reject_sensitive_values(self):
        self.codes(1,status='EMITTED')
        p,a=self.ready(kind='UTILISATION',payload={'gtin':PRODUCT,'codes':['SYN:code-0']})
        aid=self.c.execute(p.operation_id,a,3); self.c.reconcile(p.operation_id,4)
        before=self.s.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        sensitive=['SYN:code-0','SYN:TOKEN_SECRET','SYN:PIN_SECRET','SYN:AUTHORIZATION_SECRET']
        output=io.StringIO()
        with redirect_stdout(output),redirect_stderr(output):
            for value in sensitive:
                calls=[lambda:self.s.event(value,'PREPARED',5),
                       lambda:self.s.event(p.operation_id,value,5),
                       lambda:self.s.event(p.operation_id,'PREPARED',5,value),
                       lambda:self.s.event(p.operation_id,'PREPARED',5,external_id=value),
                       lambda:self.s.event(p.operation_id,'PREPARED',5,outcome=value),
                       lambda:self.s.event(p.operation_id,'PREPARED',5,payload_hash=value)]
                for call in calls:
                    with self.assertRaises(SafetyError) as error: call()
                    self.assertNotIn(value,str(error.exception))
        text=json.dumps([tuple(r) for r in self.s.db.execute('SELECT * FROM events')])+output.getvalue()
        for value in sensitive:self.assertNotIn(value,text)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],before)

    def test_r1_06_event_attempt_reference_must_belong_to_operation(self):
        p,_,aid,_=self.attempt()
        q,approval=self.ready('SYN:q',payload={'gtin':PRODUCT,'quantity':3}); self.c.execute(q.operation_id,approval,3)
        with self.assertRaises(SafetyError):self.s.event(q.operation_id,'SUBMITTING->ACCEPTED',4,aid)
        self.s.verify()

    def test_r1_01_manufactured_complete_rejection_has_no_fake_provenance(self):
        p,approval=self.ready(); self.api.mode='timeout'; aid=self.c.execute(p.operation_id,approval,3)
        forged=Evidence('REJECTED',p.operation_id,p.payload_hash,None,400,True,
                        environment='SANDBOX',participant=PARTICIPANT,kind='ORDER',attempt_id=aid)

        self.s.begin_reconciliation(p.operation_id,4)
        self.assertEqual(self.s.finish_reconciliation(p.operation_id,aid,forged,5),'UNKNOWN')
        with self.assertRaises(SafetyError):self.s.approve(p.operation_id,p.payload_hash,'SANDBOX','SYN:owner',6,100)
        self.assertEqual(self.c.reconcile(p.operation_id,7),'SUCCEEDED')
        self.assertEqual(self.api.submission_count,1)

    def test_r1_03_sql_replace_cannot_rewrite_immutable_committed_rows(self):
        q,issue=self.committed(); before=q.receipt(issue)
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute("INSERT OR REPLACE INTO issues(id,state,created_at,item_count) VALUES(?,'RESERVED',3,2)",(issue,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('INSERT OR REPLACE INTO issue_items VALUES(?,?)',(issue,'SYN:code-0'))
        p,approval=self.ready()
        values=tuple(self.s.get(p.operation_id))
        with self.assertRaises(sqlite3.IntegrityError):self.s.db.execute('INSERT OR REPLACE INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)',values)
        self.assertEqual(q.receipt(issue),before)

    def test_r1_03_sql_initial_terminal_state_rejected(self):
        p=self.prepared()
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (p.operation_id,p.fingerprint,p.payload_hash,p.environment,p.participant,p.kind,p.demand,p.payload,'SUCCEEDED',1))
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)

    def test_r1_06_sql_unsafe_event_values_and_replacement_rejected(self):
        p,_=self.ready(); last=self.s.db.execute('SELECT * FROM events ORDER BY seq DESC LIMIT 1').fetchone()
        for details in ('{"outcome":"SYN:TOKEN_SECRET"}','{"external_id":"SYN:code-0"}','{"payload_hash":"SYN:PIN_SECRET"}'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.s.db.execute('INSERT INTO events(operation_id,attempt_id,environment,timestamp,transition,details,previous_hash,event_hash) VALUES(?,NULL,?,?,?,?,?,?)',
                                  (p.operation_id,'SANDBOX',3,'PREPARED',details,last['event_hash'],'0'*64))
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?,?)',tuple(last))
        self.s.verify()

    def test_r1_03_old_schema_refused_without_migration(self):
        root=self.root/'old'; root.mkdir(); db=sqlite3.connect(root/'state.sqlite')
        db.execute('CREATE TABLE metadata(environment TEXT,participant TEXT,schema_version INTEGER)')
        db.execute('INSERT INTO metadata VALUES(?,?,1)',('SANDBOX',PARTICIPANT)); db.commit(); db.close()
        before=(root/'state.sqlite').read_bytes()
        with self.assertRaises(SafetyError):Store(root,'SANDBOX',PARTICIPANT)
        self.assertEqual((root/'state.sqlite').read_bytes(),before)

    def test_r1_05_coordinator_wrong_returned_id_remains_unknown(self):
        p,_,aid,original=self.attempt(); self.api.mode='readback_wrong_id'
        self.assertEqual(self.c.reconcile(p.operation_id,4),'UNKNOWN')
        self.assertEqual(self.s.db.execute('SELECT external_id FROM attempts WHERE id=?',(aid,)).fetchone()[0],original.external_id)
        self.api.mode='success'; self.reopen(); self.c.restart(5)
        self.assertEqual(self.c.reconcile(p.operation_id,6),'SUCCEEDED')
        self.assertEqual(self.api.submission_count,1)


if __name__=='__main__': unittest.main(verbosity=2)
