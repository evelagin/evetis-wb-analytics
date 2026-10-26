"""Packaged, network-disabled terminal control certificate fault checks."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from tools.tenancy import full_controller_closure_recovery as R,controller_cadence as CC
from tools.tenancy import durable_plan as D,full_history as F,tenant_backfill as BF,orchestration_contract as O
from tools.tenancy import cloud_controller as C
from tools.tenancy.controller_cadence_image_check import protocol,NOW


def fixture():
 m,old,c,make,x,objects,_=protocol();b=make(0);claim=CC.acquire(b)
 new=deepcopy(old);new.update(source_sha='f'*40,image=old['image'].split('@')[0]+'@sha256:'+'f'*64,controller_timeout_seconds=1800)
 new['verification']['terminal_controller_adapter']='PASS'
 new['controller_implementation_hash']=O.implementation_hash(BF.REPO)
 settings=dict(release=new['source_sha'],root_hash=m['hash'],scheduler_state='PAUSED')
 b.c['orchestration']=O.block(c,settings,BF.REPO,new)
 objects[C.descriptor_name(new['source_sha'],m['hash'],'PAUSED')]=({},D.encoded(dict(settings=settings,release=new)))
 index=next(i for i,p in enumerate(m['programs']) if p['entity']=='fbo_postings')
 doc=F.render_leaf(m,index,NOW.astimezone(BF.B.MSK).date());sh=F.shard_root(m,index,doc)
 def rec(root,kind,sq,p):return dict(version=D.VERSION,root_hash=root,kind=kind,sequence=sq,payload=p)
 stop=rec(m['hash'],'STOPPED',0,dict(reason='CONTROLLER_GATE_OR_EVIDENCE_FAILURE',controller_execution=x[1]['name'],diagnostic=dict(version=1,stage='MANIFEST_ROOT',category='EVIDENCE')))
 receipt=rec(sh,'DISPATCH_RECEIPT',1,dict(plan=doc,receipt=dict(run_id='bf-example',lease_generation=1,ack_hash=doc['ack_hash'])))
 recon=rec(sh,'RECONCILED',1,dict(sequence=1,state_hash='b'*64,persisted_reconciled=True,source_complete=True))
 completion=rec(m['hash'],'CHUNK_COMPLETE',index,dict(index=index,shard_root=sh,ack_hash=doc['ack_hash'],state_hash='b'*64,sequence=1,dispatch_attempts=1,source_complete=True,persisted_reconciled=True,coverage=dict(source_sequence=1,readback=[]),completed_at=NOW.isoformat(),reused_qualification=None))
 root=[rec(m['hash'],'FULL_MANIFEST',0,m),rec(m['hash'],'CHUNK_PLAN',index,dict(index=index,plan=doc,shard_root=sh)),stop,completion]
 b.store=SimpleNamespace(history=lambda r,**kw:root if r==m['hash'] else [receipt,recon])
 b.current_execution=None
 p=R.sealed(dict(version=1,type=R.TYPE,tenant=m['tenant'],project=m['project'],root=m['hash'],manifest_hash=D.digest(m),claim=claim,
  stop_hashes=[D.digest(stop)],protected_root_hashes=sorted(D.digest(r) for r in root),new_source=new['source_sha'],new_image=new['image'],new_implementation_hash=new['controller_implementation_hash'],runtime_image=new['runtime_image'],
  owner_ack_sha256='a'*64,guard_probe_hash='a'*64,security_proof_hash='a'*64,source_proof_hash='a'*64,last_source=dict(index=index,shard=sh,receipt_hash=D.digest(receipt),recon_hash=D.digest(recon),completion_hash=D.digest(completion)),
  predicates=dict.fromkeys(R.PREDICATES,True),verified_at=NOW.isoformat(),source_budget=0,closure=CC.sealed(dict(version=1,claim_hash=claim['hash'],status='SOURCE_FREE_RECOVERED',source_dispatches=0,closed_at=NOW.isoformat()))))
 return m,p,b,root,objects,new,x


def check():
 m,p,b,root,objects,new,_=fixture();R.header(p,m);R.validate(p,b,m,root,final=False)
 before=deepcopy(objects)
 try:R.validate(p,b,m,root)
 except BF.B.EvidenceError:pass
 else:raise AssertionError('missing certified control closure accepted')
 for field in R.PREDICATES:
  q=deepcopy(p);q['predicates'][field]=None;q=R.sealed({k:v for k,v in q.items() if k!='hash'})
  try:R.header(q,m)
  except BF.B.EvidenceError:pass
  else:raise AssertionError('unknown control recovery predicate accepted')
 assert objects==before
 n=CC.names(m['hash'],p['claim']['generation'])[1]
 objects[n]=({'kind':'controller_close','root':m['hash'][:16]},D.encoded(p['closure']))
 for ds,n in ((b.c['datasets']['ref'],R.authority_name(p)),(b.c['datasets']['tenant_locks'],R.certificate_name(p))):objects[n]=R.value(p)
 root.append(dict(version=D.VERSION,root_hash=m['hash'],kind=R.KIND,sequence=p['claim']['generation'],payload=p))
 assert R.load(b,m,root)==[p] and CC.closed(b,p['claim'])
 assert objects[CC.names(m['hash'],p['claim']['generation'])[0]]==before[CC.names(m['hash'],p['claim']['generation'])[0]]
 assert b.c['orchestration']['job']['timeout']=='1800s'
 assert b.c['orchestration']['scheduler']['schedule']=='*/3 * * * *'
 retained=deepcopy(p);retained.update(version=2,type=R.RECONCILED_TYPE,predicates=dict.fromkeys(R.RECONCILED_PREDICATES,True))
 retained['protected_root_hashes']=[f'{n:064x}' for n in range(1000)]
 retained=R.sealed({k:v for k,v in retained.items() if k!='hash'});R.header(retained,m)
 description=R.value(retained)[1];ref=__import__('json').loads(description)
 assert len(description.encode())<16384 and ref['certificate_hash']==retained['hash']
 assert len(D.encoded(retained).encode())>16384 and ref['source_budget']==0
 assert R.value(p)[1]==D.encoded(p)
 for field in R.RECONCILED_PREDICATES:
  bad=deepcopy(retained);bad['predicates'][field]=None;bad=R.sealed({k:v for k,v in bad.items() if k!='hash'})
  try:R.header(bad,m)
  except BF.B.EvidenceError:pass
  else:raise AssertionError('unknown retained recovery predicate accepted')
 stop=next(r for r in root if r['kind']=='STOPPED');original=deepcopy(objects)
 holder={'completionTime':(NOW-timedelta(seconds=60)).isoformat()}
 overlap={'createTime':(NOW-timedelta(seconds=120)).isoformat(),'startTime':(NOW-timedelta(seconds=110)).isoformat(),'completionTime':NOW.isoformat(),'failedCount':1}
 row={'evidence_json':D.encoded(stop),'recorded_at':(NOW-timedelta(seconds=3)).isoformat()}
 b.select=lambda *a:[row]
 assert R.dependent_terminal(b,retained,stop,overlap,holder)
 for rows in ([],[row,row],[dict(row,recorded_at=(NOW-timedelta(seconds=70)).isoformat())],[dict(row,recorded_at=(NOW+timedelta(seconds=1)).isoformat())],[dict(row,evidence_json='{}')]):
  b.select=lambda *a,rows=rows:rows
  try:R.dependent_terminal(b,retained,stop,overlap,holder)
  except BF.B.EvidenceError:pass
  else:raise AssertionError('unknown/noncausal STOP event accepted')
 try:R.dependent_terminal(b,p,stop,overlap,holder)
 except BF.B.EvidenceError:pass
 else:raise AssertionError('legacy recovery changed')
 assert objects==original
 pending=deepcopy(p);sh=pending['last_source']['shard']
 pending.update(version=4,type=R.PENDING_TYPE,predicates=dict.fromkeys(R.PENDING_PREDICATES,True),
  last_source=dict(index=pending['last_source']['index'],shard=sh,intent_hash='c'*64,receipt_hash='d'*64,sequence=1))
 pending=R.sealed({k:v for k,v in pending.items() if k!='hash'});R.header(pending,m)
 assert __import__('json').loads(R.value(pending)[1])['certificate_hash']==pending['hash']
 for field in R.PENDING_PREDICATES:
  bad=deepcopy(pending);bad['predicates'][field]=None;bad=R.sealed({k:v for k,v in bad.items() if k!='hash'})
  try:R.header(bad,m)
  except BF.B.EvidenceError:pass
  else:raise AssertionError('unknown pending-receipt recovery predicate accepted')
 assert objects==original
 return dict(pending_receipt_control_closure='PASS',broken_http_response_typed=broken_http_response_typed(),terminal_controller_recovery='PASS',unknown_controller_predicate_blocks='PASS',original_control_claim_preserved='PASS',certified_additive_close='PASS',qualified_controller_timeout='PASS',source_replay_zero='PASS',bounded_control_certificate_reference='PASS',legacy_certificate_bytes_preserved='PASS',retained_control_predicates_closed='PASS',overlapping_stop_event_provenance='PASS',unknown_stop_event_blocks='PASS')


def broken_http_response_typed():
 """A truncated HTTP body is a typed transport failure: a read retries on a later wake,
 a write is an unknown result; never an untyped crash that leaves the claim open."""
 import http.client,urllib.request
 from tools.tenancy import cloud_access as A
 class Response:
  def __enter__(self):return self
  def __exit__(self,*a):return False
  def read(self,n):raise http.client.IncompleteRead(b'',1)
 class Opener:
  def open(self,request,timeout):return Response()
 original=urllib.request.build_opener;urllib.request.build_opener=lambda *a:Opener()
 try:
  for method,typed in (('GET',A.TransientReadError),('POST',A.TT.TableError)):
   try:A.wire(method,A.TT.BQ+'/projects/mpa-t-client-001/datasets/tenant_locks/tables',{} if method=='POST' else None)
   except A.TT.TableError as e:
    if type(e) is not typed:raise AssertionError('broken HTTP response mistyped')
   else:raise AssertionError('broken HTTP response accepted')
 finally:urllib.request.build_opener=original
 return 'PASS'


if __name__=='__main__':
 import json
 print(json.dumps(check(),sort_keys=True))
