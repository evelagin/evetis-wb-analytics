"""Installed current adapter properties; synthetic protocol, no production proof."""
from datetime import datetime,timezone,date
from copy import deepcopy
from types import SimpleNamespace
from tools.tenancy import current_controller as C,current_policy as P,current_contract as K,durable_plan as D,tenant_backfill as BF

class Store:
    def __init__(self):self.records=[]
    def history(self,root):return [r for r in self.records if r['root_hash']==root]
    def commit(self,root,kind,seq,payload,now):
        r=dict(version=D.VERSION,root_hash=root,kind=kind,sequence=seq,payload=deepcopy(payload))
        if r not in self.records:self.records.append(r)
        return D.digest(r)

def check():
    now=datetime(2026,10,12,0,tzinfo=timezone.utc);day=now.astimezone(K.MSK).date()
    c=deepcopy(BF.target('client_001'));env=c['orchestration']['job']['env'];env['CURRENT_DATA_MODE']=K.TYPE
    p=P.sealed(dict(version=1,type=K.TYPE,tenant=c['tenant_id'],project=c['project_id'],historical_root=env['BACKFILL_ROOT_HASH'],owner_decision_sha256='1'*64,created_at=now.isoformat(),first_day=day.isoformat(),entities=list(P.ORDER),snapshot_sla_seconds=86400,fbo_sla_seconds=86400,controller_source=env['CONTROLLER_SOURCE_SHA'],controller_image=c['orchestration']['job']['image'],runtime_image=c['marketplaces']['ozon']['runtime_image'],runtime_implementation_hash=BF.B.implementation_hash(),fbo_contract_hash=None,source_concurrency=1,finance_finality='PROVISIONAL'))
    P.validate(p,c,p['historical_root'],now)
    docs={e:P.document(p,day,e) for e in P.ORDER if e!='fbo_postings'}
    assert all(P.validate_document(p,day,d) for d in docs.values())
    assert len({d['runtime_plan']['plan_id'] for d in docs.values()})==7
    assert P.cycle_root(p,day)!=p['historical_root']
    try:P.document(p,day,'fbo_postings')
    except ValueError:pass
    else:raise AssertionError('unknown FBO contract permitted source')
    q=deepcopy(p);q['finance_finality']='FINAL';q=P.sealed({k:v for k,v in q.items() if k!='hash'})
    try:P.validate(q,c,p['historical_root'],now)
    except ValueError:pass
    else:raise AssertionError('current source proof granted financial finality')
    assert {'stocks','prices','finance_accrual','ads_expense_daily','ads_sku_daily'}<=set(P.eligible(p,{'catalog':True,'ads_campaigns':True}))
    from tools.tenancy import orchestration_contract as O,cloud_controller as CLOUD
    qualified=dict(runtime_implementation_hash=BF.B.implementation_hash(),controller_implementation_hash=O.implementation_hash(BF.REPO),current_data_version=1,verification={'current_data_adapter':'PASS'})
    objects={('ref',P.name(p['controller_source'])):({'kind':'current_policy','root':p['hash'][:16]},D.encoded(p)),('tenant_locks',CLOUD.descriptor_name(p['controller_source'],p['historical_root'],'ENABLED')):({},D.encoded({'release':qualified}))}
    document_store=Store();root=P.cycle_root(p,day);doc=docs['stocks'];item=dict(day=day.isoformat(),policy_hash=p['hash'],plan=doc,shard=P.shard(p,day,doc))
    document_store.commit(root,'CURRENT_PLAN',P.ORDER.index('stocks'),item,now)
    actual=object.__new__(C.Backend);actual.c=c;actual.manifest={'hash':p['historical_root']};actual.current_policy=p;actual.current_day=day;actual.clock=lambda:now;actual.store=document_store
    actual.full_tables=SimpleNamespace(get_table=lambda ds,n:objects.get((ds,n)))
    assert actual.authorize_full_leaf(doc) is True
    outside=deepcopy(doc);outside['runtime_plan']['to']=day.isoformat()
    try:actual.authorize_full_leaf(outside)
    except (ValueError,BF.B.EvidenceError):pass
    else:raise AssertionError('unelected/expanded current scope accepted')
    assert len(document_store.records)==1
    doc=docs['catalog'];shard=P.shard(p,day,doc);store=Store();attempts=[]
    b=SimpleNamespace(clock=lambda:now,current_root=P.cycle_root(p,day),current_execution='projects/mpa-t-client-001/locations/europe-west1/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-synthetic',store=store,preflight=lambda leaf:None,quota=lambda *a:{'status':'ELIGIBLE','allowance':0},active_runtime_execution=lambda:False,state=lambda d:BF.B.initial(d['runtime_plan']))
    def uncertain(d,before,after):
        before(dict(run_id='bf-00000000-0000-0000-0000-000000000001',lease_generation=1,ack_hash=doc['ack_hash'],job='synthetic',overrides={}))
        assert store.history(shard)[0]['kind']=='DISPATCH_INTENT'
        attempts.append('synthetic_lost_reply');raise OSError('synthetic lost reply')
    b.start=uncertain;item={'plan':doc,'shard':shard}
    for _ in range(2):
        try:C.source_tick(b,p,day,item)
        except BF.B.EvidenceError:pass
        else:raise AssertionError('uncertain intent was repeated')
    assert len(attempts)==1
    assert not any(r['kind'] in {'CURRENT_CERT','RECONCILED','CHUNK_COMPLETE','FULL_COMPLETE'} for r in store.records)
    no_mode=SimpleNamespace(c={'orchestration':{'job':{'env':{'CONTROLLER_SOURCE_SHA':'a'*40}}}})
    assert C.wake(no_mode,[],{},set(),set()) is None
    assert K.readiness([],p['hash'],now,analytics_visible=False,historical_independent=True,systemic_risk=False,fbo_contract_hash=None)['operational_READY'] is False
    from tools.tenancy import fbo_diagnostic_owner as DIAG,cloud_tick as T
    queue_backend=SimpleNamespace(c=c,tables=SimpleNamespace(get_table=lambda ds,n:({'kind':'fbo_diagnostic_queue'},D.encoded({'policy_hash':'b'*64})) if n=='BFDQUEUE_'+p['controller_source'] else None),wake_activity={},clock=lambda:now)
    saved=(DIAG.policy,DIAG.dispatch,DIAG.await_terminal)
    try:
        DIAG.policy=lambda *a,**k:{'hash':'b'*64,'expires_at':'2026-10-12T01:00:00+00:00'}
        DIAG.dispatch=lambda *a:{'execution':'synthetic-diagnostic-child'}
        def unknown(*a):
            assert queue_backend.wake_activity['diagnostic_active'] is True
            raise OSError('synthetic unknown child')
        DIAG.await_terminal=unknown
        try:DIAG.queued(queue_backend)
        except OSError:pass
        else:raise AssertionError('unknown diagnostic child treated as terminal')
        assert queue_backend.wake_activity['diagnostic_active'] is True
    finally:DIAG.policy,DIAG.dispatch,DIAG.await_terminal=saved
    candidates=C.historical_candidates([{'kind':'DEPENDENCY_PLAN','payload':{'shard_root':'a'*64}}],{},set(),set())
    assert ('dependency',0) in candidates
    return dict(current_closed_authority='PASS',current_elected_plan_authorization='PASS',current_independent_source_namespaces='PASS',current_local_history_isolation='PASS',current_unknown_fbo_denied='PASS',current_lost_dispatch_no_repeat='PASS',current_provisional_not_financial_finality='PASS',current_no_theoretical_ready='PASS',current_disabled_preserves_history='PASS',current_dependency_receipt_drain='PASS',current_queued_diagnostic_unknown_retains_fence='PASS')

if __name__=='__main__':
    import json
    print(json.dumps(check(),sort_keys=True))
