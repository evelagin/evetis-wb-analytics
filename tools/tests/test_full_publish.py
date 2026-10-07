"""Owner publication is exact, immutable and source-free; no implicit GO."""
from types import SimpleNamespace
import pytest
from tools.tenancy import full_publish as P, full_history as F, tenant_backfill as BF
from tools.tests.test_full_safety import authority
from tools.tests.test_full_controller import NOW
from tools.tests.test_durable_plan import Backend as Records


@pytest.fixture
def publisher(authority,monkeypatch):
    a=authority;r=Records();markers={};written=[]
    # Full release preflight exercised with actual authority tests. This
    # fixture isolates immutable owner publication/readback fault boundaries.
    monkeypatch.setattr(P,'preflight',lambda *args,**kw:a.c)
    def create(ds,name,labels,description):
        assert ds=='ref';written.append(name)
        if name in markers:return False
        markers[name]=(labels,description);return True
    b=SimpleNamespace(c=a.c,store=r.store,clock=lambda:NOW,
        tables=SimpleNamespace(get_table=lambda ds,name:markers.get(name),create_marker=create))
    return SimpleNamespace(m=a.m,b=b,markers=markers,written=written,results={g:'PASS' for g in F.GATES})


def test_go_cannot_precede_manifest_and_readback(publisher):
    p=publisher
    with pytest.raises(BF.B.EvidenceError):P.publish_go(p.b,p.m,p.results,expected_hash=p.m['hash'])
    assert not p.written
    out=P.publish_manifest(p.b,p.m,p.results,expected_hash=p.m['hash'])
    assert out['go']=='NOT_PUBLISHED' and out['source_dispatches']==0 and not p.written
    out=P.publish_go(p.b,p.m,p.results,expected_hash=p.m['hash'])
    assert out['go']=='PASS' and out['source_dispatches']==0
    P.publish_go(p.b,p.m,p.results,expected_hash=p.m['hash'])
    assert len(p.written)==1


def test_wrong_hash_and_conflicting_go_never_overwrite(publisher):
    p=publisher
    with pytest.raises(BF.B.EvidenceError):P.publish_manifest(p.b,p.m,p.results,expected_hash='0'*64)
    assert not p.b.store.history(p.m['hash'])
    P.publish_manifest(p.b,p.m,p.results,expected_hash=p.m['hash'])
    p.markers[F.go_marker(p.m)]=({'kind':'foreign'},'{}')
    with pytest.raises(BF.B.EvidenceError,match='conflict'):P.publish_go(p.b,p.m,p.results,expected_hash=p.m['hash'])
    assert not p.written


def test_authority_failure_blocks_all_publication(publisher,monkeypatch):
    p=publisher
    def deny(*a,**kw):raise BF.B.EvidenceError('binding / lifecycle / unqualified image')
    monkeypatch.setattr(P,'preflight',deny)
    with pytest.raises(BF.B.EvidenceError):P.publish_manifest(p.b,p.m,p.results,expected_hash=p.m['hash'])
    assert not p.b.store.history(p.m['hash']) and not p.written


def test_actual_owner_release_preflight(authority,monkeypatch):
    p=authority
    from pathlib import Path
    repo=BF.REPO;release=repo/'infra/tenant/releases/backfill'/f"{p.m['controller_source_sha']}.json"
    release.parent.mkdir(parents=True)
    release.write_text(__import__('json').dumps(p.release))
    monkeypatch.setattr(P.O,'verify_artifact_source',lambda r,root:None) # image hash drift tested in controller tests
    assert P.preflight(p.b,p.m,{g:'PASS' for g in F.GATES},require_backfilling=True)==p.c
    p.facts.hold=True
    with pytest.raises(BF.B.EvidenceError):P.preflight(p.b,p.m,{g:'PASS' for g in F.GATES})
