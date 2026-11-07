"""Retained Supplies FULL leaf (index 465) — dispatch authority and environment.

Regression for Heldi STOP e2b59cec (2026-10-17): the committed DISPATCH_INTENT
561d5b50 (leaf 465, sequence 68) was rejected by full_resume.scope inside
cloud_access.dispatch, and the expected Run environment also lacked
BACKFILL_FULL_ROOT_HASH that tenant_backfill.start adds. No network is used.
"""
import json
import socket
import urllib.request
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.tenancy import cloud_access as A, cloud_controller as C, durable_plan as D
from tools.tenancy import full_history as F, tenant_backfill as BF
from pipelines.ozon.runtime import full_resume as FR

FIXTURE = Path(__file__).resolve().parents[1] / 'tenancy/fixtures/heldi_leaf465_seq68_intent.json'
TODAY = __import__('datetime').date(2026, 10, 17)
INTENT_HASH = '561d5b50877b4075b27c7d898ac88fe8fe33e5b8ad5bbfe062037af0dc6321be'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError('network is blocked in this test')
    monkeypatch.setattr(urllib.request, 'urlopen', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


def real():
    ev = json.loads(FIXTURE.read_text())
    doc = ev['payload']['plan']
    prep = ev['payload']['preparation']
    env = {e['name']: e['value'] for e in prep['overrides']['overrides']['containerOverrides'][0]['env']}
    auth = json.loads(env['BACKFILL_FULL_AUTHORITY'])
    return ev, doc, prep, auth


def access(calls, tokens=None):
    class Tokens:
        def token(self, kind):
            (tokens if tokens is not None else []).append(kind)
            return 'SYNTHETIC_NON_CREDENTIAL'
    return A.CloudAccess(BF.target('client_001'), Tokens(), lambda *a: calls.append(a) or {'name': 'projects/mpa-t-client-001/locations/europe-west1/operations/x'})


def job(prep):
    return prep['job'].rsplit('/', 1)[-1]


def test_fixture_is_the_committed_intent_of_the_stop():
    ev, doc, prep, auth = real()
    assert D.digest(ev) == INTENT_HASH and ev['kind'] == 'DISPATCH_INTENT' and ev['sequence'] == 68
    m = FR.manifest()
    assert doc == m['retained_supplies'] and BF.QF.matches(doc) and not FR.matches(doc)
    assert auth == {'root': m['hash'], 'index': 465, 'shard': ev['root_hash'], 'receipt_sequence': 68,
                    'plan_id': doc['runtime_plan']['plan_id']}
    assert F.shard_root(m, 465, doc) == ev['root_hash'] and m['programs'][465]['kind'] == 'RETAINED_INVENTORY'


def test_old_scope_derivation_rejects_the_retained_document():
    _, doc, _, _ = real()
    with pytest.raises(BF.B.EvidenceError, match='namespace differs'):
        FR.scope(doc['runtime_plan'], FR.manifest()['hash'])


def test_exact_committed_intent_reaches_the_send_boundary_once():
    ev, doc, prep, auth = real()
    calls, tokens = [], []
    access(calls, tokens).dispatch(doc, job(prep), prep['overrides'], full_authority=auth, full_intent=ev)
    assert len(calls) == 1 and tokens == ['reader']
    method, url, body, _ = calls[0]
    assert method == 'POST' and url.endswith('/jobs/ozon-runtime-daily:run') and body == prep['overrides']


def test_real_backend_single_dispatch_per_wake():
    ev, doc, prep, auth = real()
    calls = []
    b = C.Backend(access(calls), None)
    b.full_dispatch_authority, b.full_dispatch_intent = auth, ev
    b.dispatch(doc, job(prep), prep['overrides'])
    with pytest.raises(BF.B.EvidenceError, match='one source dispatch'):
        b.dispatch(doc, job(prep), prep['overrides'])
    assert len(calls) == 1


def test_start_overrides_equal_dispatch_expectation_end_to_end(monkeypatch):
    """tenant_backfill.start → on_prepared → CloudAccess.dispatch → send, every cloud edge faked."""
    ev, doc, prep, auth = real()
    calls, posts, appended = [], [], []
    acc = access(calls)
    cid = BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE', acc.c['project_id']])[:16]
    tables = SimpleNamespace(
        list_tables=lambda ds: [(f'L_{cid}_0284', {'owner': 'bf-old', 'until': '1'}, 1)],
        get_table=lambda ds, name: None,
        append=lambda ds, table, rows: appended.extend(rows))
    monkeypatch.setattr(BF, 'preflight', lambda c, d, now, backend=None, **k: (tables, []))
    intents = []

    class Backend:
        full_dispatch_authority = auth

        def append_request(self, method, url, body):
            posts.append((method, url, body))
            return {}

        def dispatch(self, d, name, body):
            return acc.dispatch(d, name, body, full_authority=auth, full_intent=intents[0])

    def before(preparation):
        intents.append({'version': D.VERSION, 'root_hash': ev['root_hash'], 'kind': 'DISPATCH_INTENT',
                        'sequence': 68, 'payload': {'plan': doc, 'preparation': preparation}})

    receipt = BF.start(doc, doc['ack_hash'], backend=Backend(), on_prepared=before)
    assert len(calls) == 1 and receipt['lease_generation'] == 285
    env = {e['name']: e['value'] for e in calls[0][2]['overrides']['containerOverrides'][0]['env']}
    assert env['BACKFILL_FULL_ROOT_HASH'] == FR.manifest()['hash'] and env['BACKFILL_RESUME_PLAN_ID'] == doc['runtime_plan']['plan_id']
    assert set(env) == set(e['name'] for e in prep['overrides']['overrides']['containerOverrides'][0]['env'])
    assert len(posts) == 1 and posts[0][2]['tableReference']['tableId'] == f'L_{cid}_0285'


def _drop(env_list, name):
    return [e for e in env_list if e['name'] != name]


@pytest.mark.parametrize('fault', [
    'other_qf_document', 'index_464', 'index_466', 'bool_index', 'shard', 'plan_id', 'root',
    'receipt_sequence', 'bool_sequence', 'intent_sequence', 'intent_root', 'intent_plan', 'intent_kind',
    'other_run', 'lease_generation_zero', 'other_job_in_intent', 'no_root_hash_env', 'wrong_root_hash_env',
    'extra_env', 'no_resume_plan_env', 'no_authority', 'no_intent', 'ack_hash',
])
def test_any_deviation_never_obtains_token_or_sends(fault):
    ev, doc, prep, auth = real()
    ev, doc, prep, auth = deepcopy(ev), deepcopy(doc), deepcopy(prep), deepcopy(auth)
    body = deepcopy(prep['overrides'])
    env = body['overrides']['containerOverrides'][0]['env']
    intent = deepcopy(ev)
    intent['payload']['preparation']['overrides'] = body
    if fault == 'other_qf_document':
        doc = BF.QF.accepted_doc(BF.QF.SKU)
        intent['payload']['plan'] = doc
    if fault in ('index_464', 'index_466'):
        auth['index'] = int(fault[-3:])
    if fault == 'bool_index':
        auth['index'] = True
    if fault == 'shard':
        auth['shard'] = 'f' * 64
    if fault == 'plan_id':
        auth['plan_id'] = 'f' * 64
    if fault == 'root':
        auth['root'] = 'f' * 64
    if fault == 'receipt_sequence':
        auth['receipt_sequence'] = 67
    if fault == 'bool_sequence':
        auth['receipt_sequence'] = True
    if fault == 'intent_sequence':
        intent['sequence'] = 69
    if fault == 'intent_root':
        intent['root_hash'] = 'f' * 64
    if fault == 'intent_plan':
        intent['payload']['plan'] = dict(doc, max_units=19)
    if fault == 'intent_kind':
        intent['kind'] = 'DISPATCH_RECEIPT'
    if fault == 'other_run':
        intent['payload']['preparation']['run_id'] = 'bf-11111111-1111-1111-1111-111111111111'
    if fault == 'lease_generation_zero':
        intent['payload']['preparation']['lease_generation'] = 0
    if fault == 'other_job_in_intent':
        intent['payload']['preparation']['job'] = intent['payload']['preparation']['job'].replace('daily', 'fast')
    if fault == 'no_root_hash_env':
        env[:] = _drop(env, 'BACKFILL_FULL_ROOT_HASH')
    if fault == 'wrong_root_hash_env':
        next(e for e in env if e['name'] == 'BACKFILL_FULL_ROOT_HASH')['value'] = 'f' * 64
    if fault == 'extra_env':
        env.append({'name': 'BACKFILL_WINDOW_DAYS', 'value': '2'})
    if fault == 'no_resume_plan_env':
        env[:] = _drop(env, 'BACKFILL_RESUME_PLAN_ID')
    if fault == 'ack_hash':
        intent['payload']['preparation']['ack_hash'] = 'f' * 64
    if fault in ('no_root_hash_env', 'wrong_root_hash_env', 'extra_env', 'no_resume_plan_env'):
        intent['payload']['preparation']['overrides'] = body
    full_authority = None if fault == 'no_authority' else auth
    full_intent = None if fault in ('no_intent', 'no_authority') else intent
    calls, tokens = [], []
    with pytest.raises((A.TT.TableError, BF.B.EvidenceError)):
        access(calls, tokens).dispatch(doc, job(prep), body, full_authority=full_authority, full_intent=full_intent)
    assert not calls and not tokens


@pytest.mark.parametrize('mutation', ['two_supplies_programs', 'kind', 'accepted_plan', 'foreign_tenant', 'other_retained_doc'])
def test_manifest_shape_is_required_exactly(monkeypatch, mutation):
    ev, doc, prep, auth = real()
    m = deepcopy(FR.manifest())
    if mutation == 'two_supplies_programs':
        m['programs'][464] = dict(m['programs'][465])
    if mutation == 'kind':
        m['programs'][465]['kind'] = 'CURRENT_SNAPSHOT'
    if mutation == 'accepted_plan':
        m['programs'][465]['accepted_qualification_plan'] = 'x'
    if mutation == 'foreign_tenant':
        m['tenant'] = 'client_002'
    if mutation == 'other_retained_doc':
        m['retained_supplies'] = dict(m['retained_supplies'], max_units=19)
    with pytest.raises(A.TT.TableError):
        A.retained_supplies_index(m, doc, auth, BF.target('client_001'), TODAY)


def test_retained_index_accepts_exact_case_only():
    _, doc, _, auth = real()
    assert A.retained_supplies_index(FR.manifest(), doc, auth, BF.target('client_001'), TODAY) == 465


def test_frozen_full_leaf_path_still_uses_scope(monkeypatch):
    """Standard FULL validation is unchanged: frozen documents never reach the retained branch."""
    seen = []
    monkeypatch.setattr(A, 'retained_supplies_index', lambda *a: seen.append(a) or 0)
    from tools.tenancy import full_pretransport_image_check as CHECK
    _, doc, job_name, body, auth, intent = CHECK.fixture()
    assert FR.matches(doc)
    calls = []
    access(calls).dispatch(doc, job_name, body, full_authority=auth, full_intent=intent)
    assert len(calls) == 1 and not seen
