import copy
from datetime import date,datetime,timezone
import pytest
from tools.tenancy import current_policy as P,current_contract as C

NOW=datetime(2026,10,12,17,tzinfo=timezone.utc)
ROOT='a'*64
SOURCE='b'*40
CONTRACT={'tenant_id':'client_001','project_id':'mpa-t-client-001','marketplaces':{'ozon':{'runtime_image':'runtime@sha256:'+('c'*64)}},'orchestration':{'job':{'image':'controller@sha256:'+('d'*64),'env':{'CONTROLLER_SOURCE_SHA':SOURCE}}}}
def policy():return P.sealed(dict(version=1,type=C.TYPE,tenant='client_001',project='mpa-t-client-001',historical_root=ROOT,owner_decision_sha256='e'*64,created_at=NOW.isoformat(),first_day='2026-10-12',entities=list(P.ORDER),snapshot_sla_seconds=86400,fbo_sla_seconds=86400,controller_source=SOURCE,controller_image=CONTRACT['orchestration']['job']['image'],runtime_image=CONTRACT['marketplaces']['ozon']['runtime_image'],runtime_implementation_hash='f'*64,fbo_contract_hash=None,source_concurrency=1,finance_finality='PROVISIONAL'))
def fake_plan(tenant,entity,frm,to,gen,origin,**kw):
    return {'runtime_plan':{'entity':entity,'from':frm,'to':to,'generation':gen,'origin':origin},'budgets':kw,'ack_hash':'1'*64}

def test_frozen_current_scope_has_distinct_daily_and_source_namespaces():
    p=policy();assert P.validate(p,CONTRACT,ROOT,NOW)==p
    a=date(2026,10,12);b=date(2026,10,13)
    assert P.cycle_root(p,a)!=P.cycle_root(p,b)!=ROOT
    assert P.generation(p,a,'stocks')!=P.generation(p,a,'prices')
    assert len(P.generation(p,a,'ads_expense_daily'))<=64

@pytest.mark.parametrize('key,value',[('source_concurrency',2),('source_concurrency',True),('finance_finality','FINAL'),('entities',['catalog']),('historical_root','1'*64),('tenant','client_002'),('first_day','2026-10-11'),('snapshot_sla_seconds',172800),('controller_source','2'*40),('runtime_image','unqualified')])
def test_unapproved_scope_release_budget_and_semantics_fail_closed(key,value):
    p=policy();p.pop('hash');p[key]=value;p=P.sealed(p)
    with pytest.raises(ValueError):P.validate(p,CONTRACT,ROOT,NOW)

def test_historical_quarantine_does_not_block_independent_operational_domains():
    p=policy();assert P.eligible(p,{})==['catalog']
    eligible=P.eligible(p,{'catalog':True,'ads_campaigns':True})
    assert 'fbo_postings' not in eligible
    assert {'stocks','prices','finance_accrual','ads_expense_daily','ads_sku_daily'}<=set(eligible)

def test_unknown_completion_cannot_be_treated_as_empty_complete():
    with pytest.raises(ValueError):P.eligible(policy(),{'stocks':None})

def test_current_snapshot_is_actual_day_without_historical_root_extension():
    p=policy();doc=P.document(p,date(2026,10,12),'stocks',make_plan=fake_plan)
    assert doc['runtime_plan']['from']==doc['runtime_plan']['to']=='2026-10-11'
    assert doc['budgets']['today']==date(2026,10,12)
    assert P.validate_document(p,date(2026,10,12),doc,make_plan=fake_plan)
    changed=copy.deepcopy(doc);changed['runtime_plan']['to']='2026-10-12'
    with pytest.raises(ValueError):P.validate_document(p,date(2026,10,12),changed,make_plan=fake_plan)

def test_current_fbo_requires_proven_contract_instead_of_legacy_bypass():
    with pytest.raises(ValueError):P.document(policy(),date(2026,10,12),'fbo_postings',make_plan=fake_plan)

def test_provisional_finance_current_window_is_deterministic():
    doc=P.document(policy(),date(2026,10,12),'finance_accrual',make_plan=fake_plan)
    assert (doc['runtime_plan']['from'],doc['runtime_plan']['to'])==('2026-09-12','2026-10-11')
