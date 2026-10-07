"""Offline adversarial full-program gates; no cloud credentials or source calls."""
from datetime import date
import copy
import pytest
from tools.tenancy import full_history as F, tenant_backfill as BF


def starts():
    return {e: date(2023, 3, 24) for e in BF.B.DATED}


def chunks():
    return [c for entity in sorted(BF.B.DATED)
            for c in BF.CK.plan(entity, date(2023, 3, 24), date(2026, 10, 6))]


def test_full_program_covers_every_domain_and_every_t5_day_exactly_once():
    cs = chunks(); ps = F.programs(cs, date(2026, 10, 6), starts())
    assert {p['entity'] for p in ps} == BF.B.DOMAINS
    assert len(ps) < 1000
    for c in cs:
        leaves = [p for p in ps if p['t5_chunk_id'] == c.chunk_id]
        assert leaves[0]['from'] == str(c.start) and leaves[-1]['to'] == str(c.end)
        assert sum((date.fromisoformat(p['to']) - date.fromisoformat(p['from'])).days + 1 for p in leaves) == (c.end - c.start).days + 1
        assert all((date.fromisoformat(p['to']) - date.fromisoformat(p['from'])).days < 31 for p in leaves)
    for e in ('prices', 'stocks'):
        p = next(p for p in ps if p['entity'] == e)
        assert p['kind'] == 'CURRENT_SNAPSHOT' and 'from' not in p


@pytest.mark.parametrize('change', [lambda cs: cs.pop(0), lambda cs: cs.append(cs[0]),
    lambda cs: cs.__setitem__(0, BF.CK.Chunk(cs[0].domain, cs[0].start, date(2027, 1, 1))),
    lambda cs: cs.__delitem__(slice(0, 5))])
def test_plan_corruption_cannot_produce_complete_coverage(change):
    cs = chunks(); change(cs)
    with pytest.raises(BF.B.EvidenceError): F.programs(cs, date(2026, 10, 6), starts())


def test_missing_whole_domain_fails_closed():
    with pytest.raises(BF.B.EvidenceError):
        F.programs([c for c in chunks() if c.domain != 'ads_sku_daily'], date(2026, 10, 6), starts())


def test_no_unproven_gate_or_source_existence_is_go():
    results = {k: 'PASS' for k in F.GATES}; evidence = {k: '1' * 64 for k in F.GATES}
    assert F.verify_gate_results(results, evidence)['verdict'] == 'PASS'
    for gate in F.GATES:
        for verdict in ('UNPROVEN', 'FAIL', 'BLOCKED', True):
            bad = dict(results, **{gate: verdict})
            with pytest.raises(BF.B.EvidenceError): F.verify_gate_results(bad, evidence)
    with pytest.raises(BF.B.EvidenceError): F.verify_gate_results(results, {})


def test_finance_source_completion_never_asserts_economic_finality_or_taxonomy():
    assert F.FINANCE['economic_finality'] == 'PROVISIONAL'
    assert F.FINANCE['rolling_refresh_days'] == 30
    assert F.FINANCE['type_84_pnl_mapping'] == 'UNKNOWN'
    assert F.FINANCE['older_corrections'] == 'EXPLICIT_OWNER_REOPEN'


def manifest():
    supply = next(d for d in BF.QF.manifest()['plans'] if d['runtime_plan']['entity'] == 'supplies')
    return F.make_manifest(tenant='client_001', created_at='2026-10-07T13:00:00Z',
        cutover='2026-10-06', chunks=chunks(), runtime_source='1'*40,
        runtime_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:'+'2'*64,
        controller_source='3'*40,
        controller_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/tenant-backfill-controller@sha256:'+'4'*64,
        controller_implementation='5'*64, boundary_evidence={e:'6'*64 for e in BF.B.DATED},
        starts=starts(), qualification_evidence={e:'7'*64 for e in F.GATES}, retained_supplies=supply)


def test_full_manifest_round_trip_size_and_owner_only_go_linkage():
    m = manifest(); assert F.validate_manifest(m)['tenant_id'] == 'client_001'
    assert len(F.BF.B.digest(m)) == 64
    assert len(__import__('json').dumps(m).encode()) < 900000
    labels, value = F.go_value(m, {e:'PASS' for e in F.GATES})
    assert labels['kind'] == 'full_history_go'
    assert __import__('json').loads(value)['t5_plan_hash'] == BF.CK.plan_hash(chunks())
    assert F.go_marker(m) == 'BFGO_' + m['hash']
    # A structural PASS test is not a live qualification claim.


@pytest.mark.parametrize('change', [lambda m:m['programs'].pop(0),
    lambda m:m['finance'].update(economic_finality='FINAL'),
    lambda m:m['continuation'].update(performance_rolling_guard=90),
    lambda m:m.update(project='foreign-project'),
    lambda m:m['programs'][0].update(t5_chunk_id='0'*16)])
def test_modified_manifest_cannot_be_activated_even_with_recomputed_hash(change):
    m=manifest();change(m);m['hash']=BF.B.digest({k:v for k,v in m.items() if k!='hash'})
    with pytest.raises(BF.B.EvidenceError): F.validate_manifest(m)


def test_shards_preserve_root_index_and_exact_leaf_identity():
    m=manifest();d={'ack_hash':'8'*64}
    assert F.shard_root(m,0,d)!=F.shard_root(m,1,d)
    assert F.shard_root(m,0,d)!=F.shard_root(m,0,{'ack_hash':'9'*64})
    with pytest.raises(BF.B.EvidenceError): F.shard_root(m,True,d)


def test_sku90_accepted_day_is_exactly_one_reused_leaf_without_coverage_gap():
    m=manifest();accepted=[p for p in m['programs'] if p.get('accepted_qualification_plan')]
    assert len(accepted)==1 and accepted[0]['from']==accepted[0]['to']=='2026-09-17'
    i=m['programs'].index(accepted[0])
    assert F.render_leaf(m,i,date(2026,10,7))==BF.QF.accepted_doc(BF.QF.SKU)
    assert F.canonical_chunks(m)==chunks()
