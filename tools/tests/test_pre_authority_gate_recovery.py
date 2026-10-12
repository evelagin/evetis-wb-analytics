from copy import deepcopy
import pytest
from tools.tenancy import full_pre_authority_gate_recovery as P,tenant_backfill as BF,durable_plan as D
from tools.tenancy.fbo_diagnostic_image_check import fixture as qfixture

def fixture():
 m,q,b,root,*rest=qfixture()
 stop=dict(version=D.VERSION,root_hash=m['hash'],kind='STOPPED',sequence=0,payload={'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':'projects/'+m['project']+'/locations/europe-west1/jobs/tenant-backfill-controller/executions/tenant-backfill-controller-synthetic','diagnostic':{'version':1,'stage':'MANIFEST_ROOT','category':'EVIDENCE'}})
 root.append(stop)
 r=__import__('json').loads((BF.REPO/'infra/tenant/releases/backfill/f4ea7b320a4444d8eaa82d414273ddd139ebf445.json').read_text())
 p=dict(version=1,type=P.TYPE,tenant=m['tenant'],project=m['project'],root=m['hash'],manifest_hash=D.digest(m),stop_hash=D.digest(stop),execution=stop['payload']['controller_execution'],old_source=r['source_sha'],old_image=r['image'],new_source='2'*40,new_image=q['controller_image'],new_implementation_hash='3'*64,runtime_image=q['runtime_image'],owner_decision_sha256='4'*64,qualification_hash='5'*64,security_hash='6'*64,effect_hash='7'*64,source_probe_hash='8'*64,protected_root_hashes=sorted(D.digest(z) for z in root),predicates=dict.fromkeys(P.PREDICATES,True),verified_at=q['verified_at'],source_budget=0,historical_cause='UNPROVEN_PRE_CLAIM_GATE')
 return m,P.sealed(p),b,root,rest

@pytest.mark.parametrize('predicate',sorted(P.PREDICATES))
@pytest.mark.parametrize('value',[None,False,1,'UNKNOWN'])
def test_any_unknown_global_integrity_predicate_blocks_all_writes(predicate,value):
 m,p,b,root,rest=fixture();p['predicates'][predicate]=value;p=P.sealed({k:v for k,v in p.items() if k!='hash'})
 with pytest.raises(BF.B.EvidenceError):P.publish(b,p,m)
 assert not rest[-1]

def test_pre_authority_source_free_contract_does_not_invent_control_claim():
 m,p,b,root,rest=fixture();P.validate(p,b,m,root)
 assert 'claim' not in p and p['source_budget']==0 and not rest[-1]

@pytest.mark.parametrize('fault',['foreign_root','unproven_stage','source_budget','invented_cause','history'])
def test_local_bookkeeping_never_exempts_source_or_foreign_root(fault):
 m,p,b,root,rest=fixture()
 if fault=='foreign_root':p['root']='f'*64
 if fault=='unproven_stage':root[-1]['payload']['diagnostic']['stage']='SOURCE_DISPATCH_BOUNDARY';p['stop_hash']=D.digest(root[-1]);p['protected_root_hashes']=sorted(D.digest(z) for z in root)
 if fault=='source_budget':p['source_budget']=1
 if fault=='invented_cause':p['historical_cause']='TRANSIENT_OR_TIMEZONE_ASSUMED'
 if fault=='history':root.pop(0)
 p=P.sealed({k:v for k,v in p.items() if k!='hash'})
 with pytest.raises(BF.B.EvidenceError):P.validate(p,b,m,root)
 assert not rest[-1]
