"""Synthetic integration/fault tests. No real codes, endpoints, credentials or crypto."""
import ast
from contextlib import redirect_stdout
import io
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.chz.v2.domain import Crash, Prepared, SafetyError, canonical, digest
from tools.chz.v2.store import Store, RUNTIME_ROOT
from tools.chz.v2.fakes import FakeNK, FakeSigner, FakeSUZ, FakeTrueAPI
from tools.chz.v2.coordinator import Coordinator
from tools.chz.v2.queue import Queue

# Installed before any core execution. Stops even accidental indirect network/auth helpers.
DENIED = []
PROTECTED_ACCESSES = []
AUDIT_ACTIVE = False
def offline_audit(event, args):
    if not AUDIT_ACTIVE:
        return
    if event.startswith(('socket.', 'subprocess.', 'os.exec', 'os.spawn')) or event in {'os.system', 'ctypes.dlopen'}:
        DENIED.append(event)
        raise RuntimeError('OFFLINE_BOUNDARY')
    if event in {'open', 'sqlite3.connect'} and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.fsdecode(args[0])
        if any(x in path for x in ('/.config/evetis-chz', '/.config/gcloud', '/.ssh/', '/cprocsp/', '/Keychains/')):
            PROTECTED_ACCESSES.append(event)
            raise RuntimeError('PROTECTED_BOUNDARY')
sys.addaudithook(offline_audit)

PRODUCT = 'TEST-GTIN-001'
PARTICIPANT = 'SYN:participant'


def child_issue(root):
    s = Store(root, 'SANDBOX', PARTICIPANT)
    try:
        Queue(s).reserve(PRODUCT, 2, 20)
    except SafetyError:
        pass
    finally:
        s.close()


def synchronized_issue(root, pipe, first):
    s = Store(root, 'SANDBOX', PARTICIPANT)
    try:
        pipe.send('ready')
        pipe.recv()
        if first:
            with s.lock():
                pipe.send('locked')
                pipe.recv()
                issue = Queue(s).reserve(PRODUCT, 2, 20)
            pipe.send(issue)
        else:
            try:
                Queue(s).reserve(PRODUCT, 2, 20)
            except SafetyError:
                pipe.send('blocked')
            else:
                pipe.send('unexpected-success')
            pipe.recv()
            pipe.send(Queue(s).reserve(PRODUCT, 2, 21))
    finally:
        s.close(); pipe.close()


class PipeLink:
    """Two anonymous OS pipes, no socketpair and no network capability."""
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
    def send(self, value): self.writer.send(value)
    def recv(self): return self.reader.recv()
    def poll(self, timeout): return self.reader.poll(timeout)
    def close(self): self.reader.close(); self.writer.close()


def pipe_links(ctx):
    a_read, b_write = ctx.Pipe(duplex=False)
    b_read, a_write = ctx.Pipe(duplex=False)
    return PipeLink(a_read, a_write), PipeLink(b_read, b_write)


class CoreTests(unittest.TestCase):
    def setUp(self):
        global AUDIT_ACTIVE
        AUDIT_ACTIVE = True
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=RUNTIME_ROOT)
        self.root = Path(self.tmp.name)
        self.s = Store(self.root, 'SANDBOX', PARTICIPANT)
        self.api = FakeSUZ('SANDBOX', PARTICIPANT)
        self.c = Coordinator(self.s, self.api)

    def tearDown(self):
        global AUDIT_ACTIVE
        try:
            self.s.close()
            self.tmp.cleanup()
            self.assertEqual(DENIED, [])
            self.assertEqual(PROTECTED_ACCESSES, [])
        finally:
            AUDIT_ACTIVE = False

    def prepared(self, demand='SYN:demand', kind='ORDER', payload=None):
        return Prepared.create('SANDBOX', PARTICIPANT, kind, demand,
                               payload or {'gtin': PRODUCT, 'quantity': 2})

    def ready(self, demand='SYN:demand', kind='ORDER', payload=None, reviewed=()):
        p = self.prepared(demand, kind, payload)
        self.s.prepare(p, 1)
        approval = self.s.approve(p.operation_id, p.payload_hash, p.environment, 'SYN:owner', 2, 100, reviewed)
        return p, approval

    def codes(self, n=5, status='INTRODUCED'):
        for i in range(n):
            self.s.add_code('SYN:code-' + str(i), PRODUCT, i+1, status, 1, 'SYN:source')

    def reopen(self):
        self.s.close()
        self.s = Store(self.root, 'SANDBOX', PARTICIPANT)
        self.c = Coordinator(self.s, self.api)

    def test_identity_and_preview_immutable_no_code_leak(self):
        p = self.prepared(kind='UTILISATION', payload={'gtin': PRODUCT, 'codes': ['SYN:b', 'SYN:a']})
        q = self.prepared(kind='UTILISATION', payload={'codes': ['SYN:a', 'SYN:b'], 'gtin': PRODUCT})
        self.assertEqual(p.operation_id, q.operation_id)
        self.assertNotIn('SYN:a', json.dumps(p.preview()))
        self.assertNotIn('SYN:a', repr(p))
        self.s.prepare(p, 1)
        self.s.prepare(q, 2)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('UPDATE operations SET payload=?', (b'{}',))

    def test_new_demand_duplicate_review_not_identity(self):
        p, a = self.ready()
        q = self.prepared('SYN:new-demand')
        self.assertNotEqual(p.operation_id, q.operation_id)
        self.assertEqual(p.fingerprint, q.fingerprint)
        self.s.prepare(q, 1)
        with self.assertRaises(SafetyError):
            self.s.approve(q.operation_id, q.payload_hash, 'SANDBOX', 'SYN:owner', 2, 100)
        self.s.approve(q.operation_id, q.payload_hash, 'SANDBOX', 'SYN:owner', 2, 100, [p.operation_id])

    def test_expired_approval_and_changed_hash(self):
        p, a = self.ready()
        with self.assertRaises(SafetyError):
            self.c.execute(p.operation_id, a, 100)
        with self.assertRaises(SafetyError):
            self.s.approve(p.operation_id, '0'*64, 'SANDBOX', 'SYN:owner', 3, 100)
        self.assertEqual(self.api.submission_count, 0)

    def test_environment_is_hard_boundary(self):
        p = Prepared.create('PROD', PARTICIPANT, 'ORDER', 'SYN:d', {'gtin': PRODUCT, 'quantity': 1})
        with self.assertRaises(SafetyError): self.s.prepare(p, 1)
        with self.assertRaises(SafetyError): Coordinator(self.s, FakeSUZ('PROD', PARTICIPANT))
        with self.assertRaises(SafetyError): Store(self.root, 'PROD', PARTICIPANT)
        with self.assertRaises(SafetyError): Prepared.create('', PARTICIPANT, 'ORDER', 'SYN:d', {})

    def test_attempt_separate_one_approval_one_submission(self):
        p, a = self.ready()
        self.c.execute(p.operation_id, a, 3)
        with self.assertRaises(SafetyError): self.c.execute(p.operation_id, a, 4)
        self.assertEqual(self.api.submission_count, 1)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)
        self.assertEqual(self.c.reconcile(p.operation_id, 5), 'SUCCEEDED')

    def test_rejection_requires_fresh_explicit_approval(self):
        p, a = self.ready()
        self.api.mode = 'reject'
        self.c.execute(p.operation_id, a, 3)
        with self.assertRaises(SafetyError): self.c.execute(p.operation_id, a, 4)
        b = self.s.approve(p.operation_id, p.payload_hash, 'SANDBOX', 'SYN:owner', 5, 100)
        self.api.mode = 'success'
        self.c.execute(p.operation_id, b, 6)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 2)
        self.assertEqual(self.api.submission_count, 2)
        self.assertEqual(self.c.reconcile(p.operation_id, 7), 'SUCCEEDED')

    def test_timeout_after_acceptance_never_auto_retry(self):
        p, a = self.ready()
        self.api.mode = 'timeout'
        self.c.execute(p.operation_id, a, 3)
        before = dict(self.api.records)
        self.assertEqual(len(before), 1)
        remote = next(iter(before.values()))
        self.assertEqual((remote.outcome, remote.operation_id, remote.payload_hash), ('SUCCEEDED', p.operation_id, p.payload_hash))
        wire_hash = self.s.db.execute('SELECT wire_hash FROM attempts').fetchone()[0]
        self.reopen()
        self.c.restart(4)
        self.assertEqual(self.s.get(p.operation_id)['state'], 'UNKNOWN')
        self.assertEqual(self.s.db.execute('SELECT consumed FROM approvals WHERE id=?', (a,)).fetchone()[0], 1)
        self.assertEqual(self.s.db.execute('SELECT wire_hash FROM attempts').fetchone()[0], wire_hash)
        with self.assertRaises(SafetyError): self.c.execute(p.operation_id, a, 5)
        self.assertEqual(self.c.reconcile(p.operation_id, 6), 'SUCCEEDED')
        self.assertEqual(self.api.submission_count, 1)
        self.assertEqual(self.api.records, before)

    def test_crash_before_submit_is_conservatively_unknown(self):
        p, a = self.ready()
        with self.assertRaises(Crash): self.c.execute(p.operation_id, a, 3, 'before_submit')
        self.reopen(); self.c.restart(4)
        self.assertEqual(self.api.submission_count, 0)
        self.assertEqual(self.c.reconcile(p.operation_id, 5), 'UNKNOWN')
        with self.assertRaises(SafetyError): self.c.execute(p.operation_id, a, 6)

    def test_crash_after_http_before_journal(self):
        p, a = self.ready()
        with self.assertRaises(Crash): self.c.execute(p.operation_id, a, 3, 'after_http')
        self.reopen(); self.c.restart(4)
        self.assertEqual(self.c.reconcile(p.operation_id, 5), 'SUCCEEDED')
        self.assertEqual(self.api.submission_count, 1)

    def test_restart_during_reconciling_and_delayed_readback(self):
        p, a = self.ready(); self.c.execute(p.operation_id, a, 3)
        with self.assertRaises(Crash): self.c.reconcile(p.operation_id, 4, 'during_reconcile')
        self.reopen(); self.c.restart(5)
        self.api.visible = False
        self.assertEqual(self.c.reconcile(p.operation_id, 6), 'UNKNOWN')
        self.api.visible = True
        self.assertEqual(self.c.reconcile(p.operation_id, 7), 'SUCCEEDED')

    def test_three_hashes_and_exact_signed_bytes(self):
        p, a = self.ready()
        packet = FakeSigner.packet(self.s.get(p.operation_id))
        self.assertNotEqual(digest(packet.transport), p.payload_hash)
        self.assertNotEqual(digest(packet.wire), digest(packet.signed))
        FakeSigner.verify(packet, p.payload)
        from dataclasses import replace
        with self.assertRaises(SafetyError): FakeSigner.verify(replace(packet, signed=b'SYN:changed'), p.payload)
        self.c.execute(p.operation_id, a, 3)
        r = self.s.db.execute('SELECT * FROM attempts').fetchone()
        packet = FakeSigner.packet(self.s.get(p.operation_id), r['id'])
        self.assertEqual(r['business_hash'], p.payload_hash)
        self.assertEqual(r['signed_hash'], digest(packet.signed))
        self.assertEqual(r['wire_hash'], digest(packet.wire))

    def test_duplicate_codes_and_extra_forbidden(self):
        for payload in [
            {'gtin': PRODUCT, 'codes': ['SYN:a', 'SYN:a']},
            {'gtin': PRODUCT, 'sets': [{'parent':'SYN:p1','children':['SYN:a']}, {'parent':'SYN:p2','children':['SYN:a']}]},
            {'gtin': PRODUCT, 'sets': [{'parent':'SYN:p1','children':['SYN:p1']}]},
            {'gtin': PRODUCT, 'extra':'SYN:a'}]:
            with self.subTest(case=digest(canonical(payload))[:8]):
                with self.assertRaises(SafetyError): self.prepared(kind='SET', payload=payload)

    def test_ledger_blocks_same_code_two_operations(self):
        self.codes(status='APPLIED')
        payload={'gtin': PRODUCT,'codes':['SYN:code-0']}
        p,a=self.ready(kind='INTRODUCTION',payload=payload)
        c=Coordinator(self.s, FakeTrueAPI('SANDBOX',PARTICIPANT))
        c.execute(p.operation_id,a,3)
        q,b=self.ready('SYN:demand2','INTRODUCTION',payload,[p.operation_id])
        with self.assertRaises(SafetyError): c.execute(q.operation_id,b,4)
        self.assertEqual(c.adapter.submission_count,1)

    def test_all_seven_operation_types_terminal_evidence(self):
        cases=[('ORDER',{'gtin':PRODUCT,'quantity':2},FakeSUZ),
               ('RETRIEVE',{'gtin':PRODUCT,'quantity':2,'order':'SYN:order'},FakeSUZ),
               ('UTILISATION',{'gtin':PRODUCT,'codes':['SYN:code-0']},FakeSUZ),
               ('INTRODUCTION',{'gtin':PRODUCT,'codes':['SYN:code-1']},FakeTrueAPI),
               ('SET',{'gtin':PRODUCT,'sets':[{'parent':'SYN:code-2','children':['SYN:code-3']}]},FakeTrueAPI),
               ('NK_FEED',{'gtin':PRODUCT,'version':'SYN:v1','value':'SYN:v2'},FakeNK),
               ('NK_SIGN',{'gtin':PRODUCT,'version':'SYN:v2','value':'SYN:xml'},FakeNK)]
        self.codes()
        self.s.db.execute("UPDATE codes SET external_status='EMITTED',allocation='QUARANTINED' WHERE id='SYN:code-0'")
        self.s.db.execute("UPDATE codes SET external_status='APPLIED',allocation='QUARANTINED' WHERE id='SYN:code-1'")
        for kind,payload,cls in cases:
            with self.subTest(kind=kind):
                p,a=self.ready('SYN:'+kind,kind,payload)
                c=Coordinator(self.s,cls('SANDBOX',PARTICIPANT))
                c.execute(p.operation_id,a,3)
                self.assertEqual(c.reconcile(p.operation_id,4),'SUCCEEDED')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM memberships').fetchone()[0],1)

    def test_retrieval_recovery_no_new_codes(self):
        p,a=self.ready(kind='RETRIEVE',payload={'gtin':PRODUCT,'quantity':2,'order':'SYN:order'})
        self.api.mode='timeout'; self.c.execute(p.operation_id,a,3)
        original = next(iter(self.api.records.values()))
        self.reopen(); self.c.restart(4)
        self.assertEqual(self.c.reconcile(p.operation_id,5),'SUCCEEDED')
        self.assertEqual(self.api.submission_count,1)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM codes').fetchone()[0],2)
        self.assertEqual(tuple(x[0] for x in self.s.db.execute('SELECT id FROM codes ORDER BY position')), original.codes)
        self.assertEqual(self.s.db.execute('SELECT external_id FROM attempts').fetchone()[0], original.external_id)
        with self.assertRaises(SafetyError): self.c.reconcile(p.operation_id,6)

    def test_retrieval_closed_order_remains_unknown(self):
        p,a=self.ready(kind='RETRIEVE',payload={'gtin':PRODUCT,'quantity':2,'order':'SYN:order'})
        self.api.mode='timeout'; self.c.execute(p.operation_id,a,3); self.api.closed=True
        self.assertEqual(self.c.reconcile(p.operation_id,4),'UNKNOWN')
        self.assertEqual(self.api.submission_count,1)

    def test_partial_is_not_success_or_retry(self):
        p,a=self.ready(); self.api.mode='partial'; self.c.execute(p.operation_id,a,3)
        self.assertEqual(self.c.reconcile(p.operation_id,4),'PARTIAL')
        with self.assertRaises(SafetyError): self.c.execute(p.operation_id,a,5)

    def test_crash_after_queue_reservation(self):
        self.codes()
        with self.assertRaises(Crash): Queue(self.s).reserve(PRODUCT,2,2,'after_reservation')
        self.reopen()
        issue=self.s.db.execute('SELECT id FROM issues').fetchone()[0]
        self.assertEqual(Queue(self.s).recovery_state(issue),'NON_DELIVERABLE_RESERVED')
        self.assertEqual(Queue(self.s).pointer(PRODUCT),3)

    def test_artifact_crash_matrix_and_non_deliverable_orphan(self):
        self.codes(10)
        for fault in ['during_artifacts','before_rename','after_rename']:
            with self.subTest(fault=fault):
                q=Queue(self.s); issue=q.reserve(PRODUCT,2,2)
                with self.assertRaises(Crash): q.build(issue,fault)
                self.reopen(); q=Queue(self.s)
                with self.assertRaises(SafetyError): q.receipt(issue)
                if fault=='after_rename': self.assertEqual(q.recovery_state(issue),'NON_DELIVERABLE_PENDING_RECOVERY')
                q.build(issue); q.commit(issue,4)
                self.assertEqual(q.receipt(issue)['state'],'COMMITTED')

    def test_issue_commit_crash_and_idempotent_receipt(self):
        self.codes()
        q=Queue(self.s); issue=q.reserve(PRODUCT,2,2); q.build(issue)
        with self.assertRaises(Crash): q.commit(issue,3,'during_issue_commit')
        self.reopen(); q=Queue(self.s)
        with self.assertRaises(SafetyError): q.receipt(issue)
        with self.assertRaises(Crash): q.commit(issue,4,'after_issue_commit')
        self.reopen(); q=Queue(self.s)
        r=q.receipt(issue); q.build(issue); q.commit(issue,5)
        self.assertEqual(r,q.receipt(issue))
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM issues').fetchone()[0],1)

    def test_corrupted_artifact_and_pointer(self):
        self.codes(); q=Queue(self.s); issue=q.reserve(PRODUCT,2,2); q.build(issue); q.commit(issue,3)
        (self.root/'pointer.txt').write_text('not-a-pointer')
        self.assertEqual(q.pointer(PRODUCT),3)
        (self.root/('bundle-'+issue)/'items.json').write_text('{}')
        with self.assertRaises(SafetyError): q.receipt(issue)

    def test_duplicate_process_stale_lock_and_concurrent_issue(self):
        self.codes()
        other=Store(self.root,'SANDBOX',PARTICIPANT)
        try:
            with self.s.lock():
                with self.assertRaises(SafetyError): Queue(other).reserve(PRODUCT,2,2)
        finally: other.close()
        (self.root/'executor.lock').write_text('stale pid')
        ctx=multiprocessing.get_context('fork')
        pipes = [pipe_links(ctx) for _ in range(2)]
        jobs = [ctx.Process(target=synchronized_issue, args=(self.root, pipes[i][1], i == 0)) for i in range(2)]
        for i, p in enumerate(jobs):
            p.start(); self.assertTrue(pipes[i][0].poll(10)); self.assertEqual(pipes[i][0].recv(), 'ready')
        pipes[0][0].send('go'); self.assertTrue(pipes[0][0].poll(10)); self.assertEqual(pipes[0][0].recv(), 'locked')
        pipes[1][0].send('contend'); self.assertTrue(pipes[1][0].poll(10)); self.assertEqual(pipes[1][0].recv(), 'blocked')
        pipes[0][0].send('reserve'); self.assertTrue(pipes[0][0].poll(10)); first = pipes[0][0].recv()
        jobs[0].join(10); self.assertEqual(jobs[0].exitcode, 0)
        pipes[1][0].send('retry'); self.assertTrue(pipes[1][0].poll(10)); second = pipes[1][0].recv()
        jobs[1].join(10); self.assertEqual(jobs[1].exitcode, 0)
        for pair in pipes:
            for connection in pair: connection.close()
        self.assertNotEqual(first, second)
        rows=self.s.db.execute('SELECT code_id FROM issue_items').fetchall()
        self.assertEqual(len(rows),len({r[0] for r in rows}))
        self.assertEqual(len(rows),4)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM issues').fetchone()[0], 2)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0], 4)

    def test_real_process_crash_releases_lock_keeps_reservation(self):
        self.codes()
        pid=os.fork()
        if pid==0:
            s=Store(self.root,'SANDBOX',PARTICIPANT)
            Queue(s).reserve(PRODUCT,2,2)
            with s.lock(): os._exit(73)
        _,status=os.waitpid(pid,0)
        self.assertEqual(os.waitstatus_to_exitcode(status),73)
        self.reopen()
        with self.s.lock(): pass
        self.assertEqual(Queue(self.s).pointer(PRODUCT),3)

    def test_journal_append_only_chain_and_redaction(self):
        self.codes(1, status='EMITTED')
        p,a=self.ready(kind='UTILISATION', payload={'gtin':PRODUCT,'codes':['SYN:code-0']})
        self.c.execute(p.operation_id,a,3); self.c.reconcile(p.operation_id,4)
        self.s.verify()
        with self.assertRaises(sqlite3.IntegrityError): self.s.db.execute('DELETE FROM events')
        text=json.dumps([tuple(r) for r in self.s.db.execute('SELECT * FROM events')])
        self.assertNotIn('SYN:code-0',text)
        with self.assertRaises(SafetyError):
            with self.s.tx():self.s.event(p.operation_id,'bad',5,authorization='secret')

    def test_journal_corruption_blocks_execute(self):
        p,a=self.ready()
        self.s.db.execute('DROP TRIGGER immutable_event_update')
        self.s.db.execute('UPDATE events SET event_hash=? WHERE seq=1', ('0' * 64,))
        with self.assertRaises(SafetyError):self.c.execute(p.operation_id,a,3)
        self.assertEqual(self.api.submission_count,0)

    def test_no_real_inputs_or_adapters_or_home_paths(self):
        for payload in [{'gtin':'04619689656079','quantity':1},
                        {'gtin':PRODUCT,'quantity':1,'token':'not-allowed'}]:
            with self.assertRaises(SafetyError):self.prepared(payload=payload)
        with self.assertRaises(SafetyError):Store('/private/tmp/not-authorized','SANDBOX',PARTICIPANT)
        with self.assertRaises(SafetyError):Coordinator(self.s,object())
        banned={'socket','urllib','http','requests','subprocess','ctypes','chz'}
        root=Path(__file__).resolve().parents[2]/'chz/v2'
        for p in root.glob('*.py'):
            tree=ast.parse(p.read_text())
            for node in ast.walk(tree):
                if isinstance(node,ast.Import):self.assertFalse({a.name.split('.')[0] for a in node.names}&banned)
                if isinstance(node,ast.ImportFrom):self.assertNotIn((node.module or '').split('.')[0],banned)
            self.assertNotIn('https://',p.read_text())
            self.assertNotIn('/opt/cprocsp',p.read_text())
        self.assertEqual(DENIED,[])

    def test_core_stdout_empty(self):
        stream=io.StringIO()
        with redirect_stdout(stream):
            p,a=self.ready(); self.c.execute(p.operation_id,a,3); self.c.reconcile(p.operation_id,4)
        self.assertEqual(stream.getvalue(),'')

    def test_restart_preserves_each_non_inflight_state(self):
        states = ('PREPARED', 'APPROVED', 'ACCEPTED', 'UNKNOWN', 'SUCCEEDED', 'REJECTED', 'PARTIAL', 'INVALIDATED', 'CANCELLED')
        for quantity, state in enumerate(states, 1):
            with self.subTest(state=state):
                p = self.prepared('SYN:state-' + state, payload={'gtin': PRODUCT, 'quantity': quantity})
                self.s.prepare(p, 1)
                if state == 'CANCELLED':
                    with self.s.tx(): self.s.transition(p.operation_id, 'CANCELLED', 2)
                elif state != 'PREPARED':
                    a = self.s.approve(p.operation_id, p.payload_hash, 'SANDBOX', 'SYN:owner', 2, 100)
                    if state == 'INVALIDATED':
                        with self.s.tx(): self.s.transition(p.operation_id, 'INVALIDATED', 3)
                    elif state != 'APPROVED':
                        self.api.mode = {'UNKNOWN': 'timeout', 'REJECTED': 'reject', 'PARTIAL': 'partial'}.get(state, 'success')
                        self.c.execute(p.operation_id, a, 3)
                        if state in {'SUCCEEDED', 'PARTIAL'}: self.c.reconcile(p.operation_id, 4)
                before = self.api.submission_count
                self.reopen(); self.c.restart(5)
                self.assertEqual(self.s.get(p.operation_id)['state'], state)
                self.assertEqual(self.api.submission_count, before)

    def test_filesystem_failures_leave_issue_non_deliverable(self):
        self.codes()
        q = Queue(self.s); issue = q.reserve(PRODUCT, 2, 2)
        for target in ('tools.chz.v2.queue.os.fsync', 'tools.chz.v2.queue.os.rename'):
            with self.subTest(target=target):
                with patch(target, side_effect=OSError('synthetic failure')):
                    with self.assertRaises(OSError): q.build(issue)
                with self.assertRaises(SafetyError): q.receipt(issue)
        q.build(issue)
        with self.assertRaises(SafetyError): q.receipt(issue)
        q.commit(issue, 3)
        self.assertEqual(q.receipt(issue)['ff_receipt'], 'UNCONFIRMED')

    def test_ambiguous_or_mismatched_retrieval_evidence(self):
        from dataclasses import replace
        p, a = self.ready(kind='RETRIEVE', payload={'gtin': PRODUCT, 'quantity': 2, 'order': 'SYN:order'})
        self.api.mode = 'timeout'; self.c.execute(p.operation_id, a, 3)
        original = next(iter(self.api.records.values()))
        self.api.records['SYN:external-2'] = replace(original, external_id='SYN:external-2')
        self.assertEqual(self.c.reconcile(p.operation_id, 4), 'UNKNOWN')
        del self.api.records['SYN:external-2']
        for bad in (replace(original, payload_hash='0'*64), replace(original, complete=False),
                    replace(original, codes=('SYN:duplicate', 'SYN:duplicate'))):
            self.api.records[original.external_id] = bad
            self.assertEqual(self.c.reconcile(p.operation_id, 5), 'UNKNOWN')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM codes').fetchone()[0], 0)
        self.assertEqual(self.api.submission_count, 1)

    def test_lifecycle_and_operation_participation_history(self):
        self.codes(1, status='EMITTED')
        for kind, cls in [('UTILISATION', FakeSUZ), ('INTRODUCTION', FakeTrueAPI)]:
            p, a = self.ready('SYN:' + kind, kind, {'gtin': PRODUCT, 'codes': ['SYN:code-0']})
            c = Coordinator(self.s, cls('SANDBOX', PARTICIPANT))
            c.execute(p.operation_id, a, 3)
            self.assertEqual(c.reconcile(p.operation_id, 4), 'SUCCEEDED')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM operation_codes').fetchone()[0], 2)
        self.assertEqual(Queue(self.s).pointer(PRODUCT), 1)
        issue = Queue(self.s).reserve(PRODUCT, 1, 5)
        with self.assertRaises(SafetyError): Queue(self.s).reserve(PRODUCT, 1, 6)
        self.assertEqual(Queue(self.s).recovery_state(issue), 'NON_DELIVERABLE_RESERVED')

    def test_attempt_bytes_immutable_and_corruption_detected(self):
        p, a = self.ready(); self.c.execute(p.operation_id, a, 3)
        with self.assertRaises(sqlite3.IntegrityError): self.s.db.execute("UPDATE attempts SET wire=X'00'")
        self.s.db.execute('DROP TRIGGER immutable_attempt')
        wire = json.loads(self.s.db.execute('SELECT wire FROM attempts').fetchone()[0])
        wire['fake_signature'] = '0' * 64
        self.s.db.execute('UPDATE attempts SET wire=?', (canonical(wire),))
        with self.assertRaises(SafetyError): self.c.reconcile(p.operation_id, 4)

    def test_journal_rejects_secret_in_allowed_field(self):
        p, _ = self.ready()
        with self.assertRaises(SafetyError):
            with self.s.tx(): self.s.event(p.operation_id, 'TEST', 3, outcome='SYN:code-0')
        self.assertEqual(PROTECTED_ACCESSES, [])

    def test_symlink_artifact_and_path_traversal_rejected(self):
        self.codes(); q = Queue(self.s); issue = q.reserve(PRODUCT, 2, 2)
        stage = self.root / ('staging-' + issue)
        stage.mkdir(); target = self.root / 'synthetic-target'; target.write_text('unchanged')
        (stage / 'items.json').symlink_to(target)
        with self.assertRaises(OSError): q.build(issue)
        self.assertEqual(target.read_text(), 'unchanged')
        with self.assertRaises(SafetyError): q.build('../../outside')

    def test_orphan_without_sqlite_issue_never_committed(self):
        orphan = 'a' * 32
        (self.root / ('bundle-' + orphan)).mkdir()
        q = Queue(self.s)
        self.assertEqual(q.recover_bundles()[orphan], 'NON_DELIVERABLE_ORPHAN')
        with self.assertRaises(SafetyError): q.commit(orphan, 4)
        with self.assertRaises(SafetyError): q.receipt(orphan)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM issues').fetchone()[0], 0)

    def test_forged_prepared_identity_and_late_duplicate_blocked(self):
        from dataclasses import replace
        p, a = self.ready()
        with self.assertRaises(SafetyError): self.s.prepare(replace(p, payload_hash='0' * 64), 2)
        self.s.prepare(self.prepared('SYN:late-demand'), 3)
        with self.assertRaises(SafetyError): self.c.execute(p.operation_id, a, 4)
        self.assertEqual(self.api.submission_count, 0)


if __name__=='__main__':
    unittest.main(verbosity=2)
