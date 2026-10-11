"""Packaged owner settlement, immutable failed history and failed-retry fences."""
from copy import deepcopy
from datetime import date,datetime,timezone
from types import SimpleNamespace
from tools.tenancy import full_fbo_zero_effect_recovery as R,full_history as F,durable_plan as D
from tools.tenancy import tenant_backfill as BF,orchestration_contract as O,cloud_controller as C,full_controller as H
from pipelines.ozon.runtime import full_resume as FR
NOW=datetime(2026,10,11,18,tzinfo=timezone.utc)

def fixture():
 m=FR.manifest();doc=F.render_leaf(m,269,date(2026,10,11));rp=doc['runtime_plan']
 env=dict(ENTITIES=rp['entity'],SINCE=rp['from'],UNTIL=rp['to'],INGESTION_RUN_ID=R.EXACT['run_id'],
  BACKFILL_MODE=BF.B.VERSION,BACKFILL_TARGET_PROJECT=rp['project'],BACKFILL_GENERATION=rp['generation'],BACKFILL_ORIGIN=rp['origin'],BACKFILL_MAX_REQUESTS='400',BACKFILL_MAX_UNITS='20',BACKFILL_WINDOW_DAYS='7')
 env.update(BF.continuation_overrides(doc));env['BACKFILL_FULL_AUTHORITY']=D.encoded(dict(root=m['hash'],index=269,shard=R.EXACT['shard'],receipt_sequence=1,plan_id=rp['plan_id']))
 prep=dict(run_id=R.EXACT['run_id'],lease_generation=190,ack_hash=doc['ack_hash'],job=R.EXACT['execution'].split('/executions/')[0],overrides={'overrides':{'containerOverrides':[{'env':[dict(name=k,value=v) for k,v in env.items()]}]}})
 receipt={k:prep[k] for k in ('run_id','lease_generation','ack_hash')};receipt['operation']='projects/mpa-t-client-001/locations/europe-west1/operations/ca9ae7d1-19f7-455b-86da-85c6bcb5e904'
 def rec(root,kind,sq,p):return dict(version=D.VERSION,root_hash=root,kind=kind,sequence=sq,payload=p)
 intent=rec(R.EXACT['shard'],'DISPATCH_INTENT',1,dict(plan=doc,preparation=prep));received=rec(R.EXACT['shard'],'DISPATCH_RECEIPT',1,dict(plan=doc,receipt=receipt))
 assert D.digest(intent)==R.EXACT['intent_hash'] and D.digest(received)==R.EXACT['receipt_hash']
 stop=rec(m['hash'],'STOPPED',0,dict(reason='CONTROLLER_GATE_OR_EVIDENCE_FAILURE',controller_execution=R.EXACT['controller_execution'],diagnostic=dict(version=1,stage='RUNTIME_TERMINAL',category='EVIDENCE',context=dict(index=269,shard=R.EXACT['shard'],sequence=1,intent_hash=D.digest(intent),receipt_hash=D.digest(received)))))
 assert D.digest(stop)==R.EXACT['stop_hash']
 root=[rec(m['hash'],'FULL_MANIFEST',0,m),rec(m['hash'],'CHUNK_PLAN',269,dict(index=269,plan=doc,shard_root=R.EXACT['shard'])),stop];sh=[intent,received]
 c=deepcopy(BF.target('client_001'));release=D.parse_tenant_json((BF.REPO/'infra/tenant/releases/backfill/91c562a2994b5025fd66dfc7c09c68db43ded7ae.json').read_text())
 release.update(source_sha='2'*40,image=release['image'].split('@')[0]+'@sha256:'+'2'*64,controller_implementation_hash=O.implementation_hash(BF.REPO),runtime_image=R.EXACT['original_runtime_image'].split('@')[0]+'@sha256:'+'3'*64,runtime_implementation_hash=BF.B.implementation_hash())
 release['fbo_zero_effect_recovery_version']=1;release['verification']['fbo_zero_effect_adapter']='PASS'
 c['marketplaces']['ozon']['runtime_image']=release['runtime_image'];settings=dict(release=release['source_sha'],root_hash=m['hash'],scheduler_state='PAUSED');c['orchestration']=O.block(c,settings,BF.REPO,release)
 original=dict(status='RUNNING',lease_owner=R.EXACT['run_id'],error_code=None,evidence_json='{"mode": "BOUNDED_PILOT", "proof": null}')
 rows=[original];objects={'L_50c5d90dbf43ac9c_0190':({'owner':R.EXACT['run_id'],'until':'1791731789'},'{"mode": "BOUNDED_PILOT", "ack_hash": "'+doc['ack_hash']+'"}'),C.descriptor_name('2'*40,m['hash'],'PAUSED'):({},D.encoded(dict(settings=settings,release=release)))}
 p=dict(R.EXACT,version=1,type=R.TYPE,tenant=m['tenant'],project=m['project'],manifest_hash=D.digest(m),new_source=release['source_sha'],new_image=release['image'],new_implementation_hash=release['controller_implementation_hash'],runtime_source='3'*40,runtime_image=release['runtime_image'],runtime_implementation_hash=release['runtime_implementation_hash'],owner_decision_sha256='4'*64,qualification_hash='5'*64,security_hash='6'*64,source_effect_hash='7'*64,protected_root_hashes=sorted(D.digest(x) for x in root),protected_shard_hashes=sorted(D.digest(x) for x in sh),predicates=dict.fromkeys(R.PREDICATES,True),verified_at=NOW.isoformat(),source_budget=0,state_hash=D.digest(BF.B.initial(rp)),original_checkpoint_hash=D.digest(original),original_lease_hash=D.digest(objects['L_50c5d90dbf43ac9c_0190']),closure_hash='0'*64,legacy_cause='UNPROVEN_TIMESTAMP_NOT_RETAINED',retry_sequence=2,retry_limit=1)
 p['closure_hash']=D.digest(R.closure(p));p=R.sealed(p);effects=[]
 def create(ds,n,labels,description):
  assert n.startswith(('BFZE_','BFZE_AUTH_')) or n=='LD_50c5d90dbf43ac9c_0190'
  if n in objects:return False
  objects[n]=(labels,description);effects.append(('marker',n));return True
 def append(ds,table,rs):
  assert (ds,table)==('tenant_ops','BACKFILL_CHECKPOINTS') and len(rs)==1 and rs[0]['status']=='FAILED'
  rows.extend({k:r[k] for k in original} for r in rs);effects.append(('checkpoint','FAILED'))
 def commit(root_hash,kind,seq,payload,now):
  assert (root_hash,kind,seq) in {(p['root'],R.KIND,269),(p['shard'],R.SHARD_KIND,1)}
  r=rec(root_hash,kind,seq,deepcopy(payload));target=root if root_hash==p['root'] else sh
  if r not in target:target.append(r);effects.append(('record',kind))
  return D.digest(r)
 def denied(*a,**kw):raise AssertionError('unmocked source/transport')
 b=SimpleNamespace(c=c,current_execution=None,clock=lambda:NOW,request=denied,select=lambda *a:deepcopy(rows),tables=SimpleNamespace(get_table=lambda ds,n:objects.get(n),create_marker=create,append=append),store=SimpleNamespace(history=lambda root_hash,**kw:deepcopy(root if root_hash==p['root'] else sh),commit=commit))
 return m,p,b,root,sh,objects,rows,effects,doc,receipt

def check():
 m,p,b,root,sh,objects,rows,effects,doc,receipt=fixture();R.header(p,m);R.validate(p,b,m,root,final=False)
 assert D.decide_tick(sh,p['shard'],{'status':'ELIGIBLE'})['action']=='RECONCILE'
 sh.append(R.closure(p))
 assert D.decide_tick(sh,p['shard'],{'status':'ELIGIBLE'},verified_fbo_zero_effect=[p])['sequence']==2
 for field in R.PREDICATES:
  q=deepcopy(p);q['predicates'][field]=None;q=R.sealed({k:v for k,v in q.items() if k!='hash'})
  try:R.header(q,m)
  except BF.B.EvidenceError:pass
  else:raise AssertionError('unknown predicate accepted')
 b.verified_fbo_zero_effect_failures=[p]
 retry=dict(receipt,run_id='bf-new-diagnostic-run',lease_generation=191)
 try:H.Backend.reconcile_failed(b,doc,retry,{})
 except BF.B.EvidenceError as error:assert str(error)=='FBO_DIAGNOSTIC_RETRY_FAILED_NO_AUTOMATIC_REPEAT'
 else:raise AssertionError('new FAILED permitted another dispatch')
 assert not effects and len(rows)==1 and not any(r['kind']=='RECONCILED' for r in sh)
 # Exercise fresh observation up to the real priority authority gate inside
 # the packaged image. Only cloud preflight is substituted; loading the
 # descriptor and the missing owner marker uses the production loader.
 from unittest.mock import patch
 from tools.tenancy import full_leaf_recovery as FL
 observer=SimpleNamespace(preflight=lambda *a:None)
 with patch.object(FL,'observer',return_value=observer),patch.object(H,'verify_authority'):
  try:R.observe(b,m,owner_decision_sha256='4'*64,qualification_hash='5'*64,security_hash='6'*64)
  except BF.B.EvidenceError as error:assert 'owner priority authority absent' in str(error)
  else:raise AssertionError('fresh observation bypassed owner priority authority')
 assert not effects
 return dict(fbo_zero_effect_exact_owner_settlement='PASS',fbo_one_natural_attempt_failed_stops='PASS',fbo_unknown_predicate_blocks='PASS',fbo_no_success_recon_fabrication='PASS',fbo_fresh_observation_owner_authority='PASS')

if __name__=='__main__':
 import json
 print(json.dumps(check(),sort_keys=True))
