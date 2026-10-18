from copy import deepcopy
import pytest
from tools.tenancy import full_fbo_diagnostic_closure as Z,tenant_backfill as BF,durable_plan as D
from tools.tenancy.fbo_diagnostic_image_check import fixture,check

def test_image_contract():assert set(check().values())=={'PASS'}

def test_isolation_retains_incomplete_FBO_and_selects_independent_domains():
    from tools.tenancy.full_controller import isolated_indices,completed
    m,p,b,root,sh,objects,rows,effects=fixture()
    indices=isolated_indices(m,[p]);assert 269 in indices
    assert indices=={i for i,v in enumerate(m['programs']) if v['entity']=='fbo_postings'}
    assert all(m['programs'][i]['entity']!='finance_accrual' for i in indices)
    assert completed(root,m)=={} and not effects
    p['isolation_scope']='LEAF';assert isolated_indices(m,[p])=={269}

@pytest.mark.parametrize('name',sorted(Z.PREDICATES))
@pytest.mark.parametrize('value',[False,None,1,'UNKNOWN'])
def test_every_unknown_predicate_prevents_closure(name,value):
    m,p,b,root,sh,objects,rows,effects=fixture();p['predicates'][name]=value;p=Z.sealed({k:v for k,v in p.items() if k!='hash'})
    with pytest.raises(BF.B.EvidenceError):Z.publish(b,p,m)
    assert not effects

@pytest.mark.parametrize('fault',['root','manifest','RAW_effect','class','source_budget','completed','checkpoint','lease','intent','receipt','RECON'])
def test_no_exemption_for_foreign_or_ambiguous_source(fault):
    m,p,b,root,sh,objects,rows,effects=fixture()
    if fault=='root':p['root']='f'*64
    if fault=='manifest':p['manifest_hash']='f'*64
    if fault=='RAW_effect':p['predicates']['zero_RAW_provenance']=False
    if fault=='class':p['validation_class']='HTTP_ERROR'
    if fault=='source_budget':p['source_budget']=1
    if fault=='completed':p['status']='COMPLETE'
    if fault=='checkpoint':p['checkpoint_hash']='f'*64
    if fault=='lease':p['lease_hash']='f'*64
    if fault=='intent':sh.pop(0)
    if fault=='receipt':sh[1]['payload']['receipt']['run_id']='foreign'
    if fault=='RECON':sh.append(dict(sh[0],kind='RECONCILED'))
    p=Z.sealed({k:v for k,v in p.items() if k!='hash'})
    with pytest.raises(BF.B.EvidenceError):Z.validate(p,b,m,root,final=False)
    assert not effects


def test_cloud_automatic_quarantine_does_not_turn_unrelated_failure_into_exemption(monkeypatch):
 from types import SimpleNamespace
 m,p,b,root,sh,objects,rows,effects=fixture();b.manifest=m
 monkeypatch.setattr(Z,'owner_policy',lambda *a:{'qualified':'owner'})
 doc=sh[0]['payload']['plan'];receipt=sh[1]['payload']['receipt']
 b.journal=m['project']+'.ozon_raw.OZON_INGESTION_RUNS'
 b.select=lambda *a:[{'evidence_json':'{"failure":true,"source_category":"HTTP_ERROR"}'}]
 assert Z.automatic(b,doc,receipt) is None and not effects


def test_closed_policy_does_not_authorize_diagnostics_source_budget(monkeypatch):
 m,p,b,root,sh,objects,rows,effects=fixture()
 p['source_budget']=12;p=Z.sealed({k:v for k,v in p.items() if k!='hash'})
 with pytest.raises(BF.B.EvidenceError):Z.header(p,m)
 assert not effects


def accepted_quarantine():
    """Completed closure with the factual SELECT * projection of its RUNNING checkpoint."""
    m,p,b,root,sh,objects,rows,effects=fixture()
    # REST epoch TIMESTAMP, exactly as BigQuery returns it to the controller.
    cp=[dict(rows[0],run_id=p['run_id'],lease_generation=p['generation'],lease_until='1.791731789489259E9')]
    p=Z.sealed(dict({k:v for k,v in p.items() if k!='hash'},checkpoint_hash=D.digest(cp)))
    b.select=lambda *a:deepcopy(cp)
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',b.c['project_id']])[:16]
    sh.append(Z.closure(p));objects[f'LD_{cid}_0190']=Z.release_value(p)
    objects[Z.cert(p)]=Z.value(p);objects[Z.auth(p)]=Z.value(p)
    root.append(dict(version=D.VERSION,root_hash=m['hash'],kind=Z.KIND,sequence=p['index'],payload=p))
    assert Z.load(b,m,root)==[p]
    return m,p,b,root,objects,cp,cid,effects


@pytest.mark.parametrize('case',['present','present_after_term','absent_before','absent_at_kept_expiry','absent_after',
    'absent_after_LD_changed','absent_after_LD_missing','absent_after_checkpoint_changed','absent_after_foreign_checkpoint_owner',
    'absent_after_term_differs_from_certificate','absent_after_term_unparseable','present_bytes_changed'])
def test_original_lease_may_vanish_only_after_certificate_bound_ttl(case):
    from datetime import timedelta
    from tools.tenancy import full_pretransport_recovery as PT
    m,p,b,root,objects,cp,cid,effects=accepted_quarantine()
    deadline=PT.lease_until(cp[0]['lease_until'])+BF.CK.LEASE_TABLE_KEEP+PT.LEASE_EXPIRY_GRACE
    lease=BF.CK.lease_name(cid,190);before=deepcopy(effects)
    if case.startswith('absent'):del objects[lease]
    b.clock={'absent_before':lambda:deadline-timedelta(microseconds=1),
             'absent_at_kept_expiry':lambda:deadline-PT.LEASE_EXPIRY_GRACE}.get(case,lambda:deadline)
    if case=='absent_after_LD_changed':objects[f'LD_{cid}_0190']=({'owner':p['run_id']},'{}')
    if case=='absent_after_LD_missing':del objects[f'LD_{cid}_0190']
    if case=='absent_after_checkpoint_changed':cp[0]['lease_until']='1.791727789489259E9'
    # A certificate re-sealed over altered checkpoint bytes is still bound by its lease hash.
    if case in ('absent_after_foreign_checkpoint_owner','absent_after_term_differs_from_certificate','absent_after_term_unparseable'):
        cp[0].update({'absent_after_foreign_checkpoint_owner':dict(lease_owner='bf-foreign'),
                      'absent_after_term_differs_from_certificate':dict(lease_until='1.791731788489259E9'),
                      'absent_after_term_unparseable':dict(lease_until='soon')}[case])
        q=Z.sealed(dict({k:v for k,v in p.items() if k!='hash'},checkpoint_hash=D.digest(cp)))
        root[-1]=dict(root[-1],payload=q);objects[Z.cert(q)]=Z.value(q);objects[Z.auth(q)]=Z.value(q)
        objects[f'LD_{cid}_0190']=Z.release_value(q);p=q
    b.select=lambda *a:deepcopy(cp)
    if case=='present_bytes_changed':objects[lease]=({'owner':p['run_id'],'until':'1791731788'},objects[lease][1])
    if case in ('present','present_after_term','absent_after'):
        assert Z.load(b,m,root)==[p]
    else:
        reason={'absent_after_LD_changed':'closure absent','absent_after_LD_missing':'closure absent',
            'absent_after_checkpoint_changed':'checkpoint bytes','present_bytes_changed':'lease bytes'}.get(case,'qualified expiration')
        with pytest.raises(BF.B.EvidenceError,match=reason):Z.load(b,m,root)
    assert effects==before
    if case=='absent_after':
        with pytest.raises(BF.B.EvidenceError,match='qualified expiration'):Z.validate(p,b,m,root,final=False)
