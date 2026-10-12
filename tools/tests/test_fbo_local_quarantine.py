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
