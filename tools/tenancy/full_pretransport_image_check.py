"""Real FULL dispatch boundary and closed exact recovery checks, network free."""
from copy import deepcopy
from tools.tenancy import cloud_access as A,tenant_backfill as BF,durable_plan as D,full_history as F
from tools.tenancy import full_pretransport_recovery as PT
from pipelines.ozon.runtime import full_resume as FR
from datetime import date,datetime,timezone

def fixture():
 m=FR.manifest();doc=F.render_leaf(m,89,date(2026,10,10));p=doc['runtime_plan'];run=PT.EXACT['run_id']
 authority={'root':m['hash'],'index':89,'shard':F.shard_root(m,89,doc),'receipt_sequence':1,'plan_id':p['plan_id']}
 env={'ENTITIES':p['entity'],'SINCE':p['from'],'UNTIL':p['to'],'INGESTION_RUN_ID':run,
      'BACKFILL_MODE':BF.B.VERSION,'BACKFILL_TARGET_PROJECT':p['project'],'BACKFILL_GENERATION':p['generation'],
      'BACKFILL_ORIGIN':p['origin'],'BACKFILL_MAX_REQUESTS':str(doc['max_requests']),'BACKFILL_MAX_UNITS':str(doc['max_units'])}
 env.update(BF.continuation_overrides(doc));env['BACKFILL_FULL_AUTHORITY']=D.encoded(authority)
 body={'overrides':{'containerOverrides':[{'env':[{'name':k,'value':v} for k,v in env.items()]}]}}
 base,jobs=BF.resources(BF.target('client_001'));job=next(n for n,v in jobs.items() if p['entity'] in v['entities'])
 prep={'run_id':run,'lease_generation':170,'ack_hash':doc['ack_hash'],'job':base+'/jobs/'+job,'overrides':body}
 intent={'version':D.VERSION,'root_hash':authority['shard'],'kind':'DISPATCH_INTENT','sequence':1,'payload':{'plan':doc,'preparation':prep}}
 assert D.digest(intent)==PT.EXACT['intent_hash']
 return m,doc,job,body,authority,intent

def check():
 m,doc,job,body,authority,intent=fixture();calls=[];tokens=[]
 class Tokens:
  def token(self,kind):tokens.append(kind);return 'SYNTHETIC_NON_CREDENTIAL'
 client=A.CloudAccess(BF.target('client_001'),Tokens(),lambda *args:calls.append(args) or {'name':'synthetic-operation'})
 client.dispatch(doc,job,body,full_authority=authority,full_intent=intent)
 assert len(calls)==1 and tokens==['reader']
 for key in authority:
  q=deepcopy(authority);q[key]='unknown'
  calls.clear();tokens.clear()
  try:client.dispatch(doc,job,body,full_authority=q,full_intent=intent)
  except (A.TT.TableError,BF.B.EvidenceError):pass
  else:raise AssertionError('unknown FULL authority sent')
  assert not calls and not tokens
 p=PT.sealed(dict(PT.EXACT,version=1,type=PT.TYPE,tenant=m['tenant'],project=m['project'],manifest_hash=D.digest(m),
      new_source='2'*40,new_image=PT.EXACT['original_image'].split('@')[0]+'@sha256:'+'2'*64,
      new_implementation_hash='3'*64,runtime_image=BF.target('client_001')['marketplaces']['ozon']['runtime_image'],
      owner_decision_sha256='4'*64,packaged_probe_hash='5'*64,replacement_check_hash='6'*64,
      protected_record_hashes=['7'*64],predicates={k:True for k in PT.PREDICATES},verified_at='2026-10-10T15:00:00Z',
      source_budget=0,source_state_hash=D.digest(BF.B.initial(doc['runtime_plan'])),closure_hash='8'*64))
 p['closure_hash']=D.digest(PT.closure(p));p=PT.sealed({k:v for k,v in p.items() if k!='hash'});PT.header(p,m)
 history=[intent,PT.closure(p)]
 written=BF.checkpoint(doc,p['run_id'],'RUNNING',170,datetime(2026,10,10,tzinfo=timezone.utc))
 raw=written['evidence_json'];assert raw=='{"mode": "BOUNDED_PILOT", "proof": null}'
 normalized=PT.checkpoint_row(written)
 assert normalized['evidence_json']==D.encoded({'mode':'BOUNDED_PILOT','proof':None}) and written['evidence_json']==raw
 assert D.decide_tick(history,p['shard'],{'status':'ELIGIBLE'},verified_pretransport=[p])['sequence']==2
 assert D.decide_tick([intent],p['shard'],{'status':'ELIGIBLE'})['action']=='RECOVER_RECEIPT_OR_STOP'
 for key in PT.PREDICATES:
  for bad in (False,None,1,'UNKNOWN'):
   q=deepcopy(p);q['predicates'][key]=bad;q=PT.sealed({k:v for k,v in q.items() if k!='hash'})
   try:PT.header(q,m)
   except BF.B.EvidenceError:pass
   else:raise AssertionError('unknown recovery predicate accepted')
 return {'full_authority_real_boundary':'PASS','full_authority_unknown_zero_transport':'PASS',
         'pretransport_exact_closed_incident':'PASS','pretransport_no_success_receipt_recon':'PASS',
         'pretransport_unknown_blocks':'PASS','source_budget_zero':'PASS',
         'checkpoint_real_writer_json_without_rewrite':'PASS'}

if __name__=='__main__':
 import json
 print(json.dumps(check(),sort_keys=True))
