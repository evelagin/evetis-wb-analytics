"""Network-free in-image proof of Heldi S‴-a (2026-10-22).

  * the exact terminal-gate certificate: an untyped STOP after a persisted,
    fenced, lease-released page is recoverable only while no page, fence,
    lease, claim, runtime execution or journal row exists beyond that page;
  * every write of one source dispatch is typed on failure with the closed
    ledger ABSENT/UNKNOWN/PRESENT; an unknown write outcome is never a wait
    and never a repeated POST; a transient read before any write still waits;
  * a held pilot lease inside a claimed wake and a lost closure read are
    typed STOPs (LEASE/EVIDENCE, CONTROL_CLOSURE/CLOUD_METADATA);
  * the shared STOP acceptance set includes the new recovery.
Every patched seam is restored before returning.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import json,shutil,tempfile
from tools.tenancy import full_terminal_gate_recovery as R,full_history as F,durable_plan as D,tenant_backfill as BF
from tools.tenancy import orchestration_contract as O,cloud_controller as C,controller_cadence as CC,controller_diagnostics as CD
from pipelines.ozon.runtime import full_resume as FR
NOW=datetime(2026,10,22,10,tzinfo=timezone.utc)
INDEX=465;PAGE=103;LEASE=345;OLD='39bc343e48e95e068a9f4df7c5991c8114dee8ab'
JOB='projects/mpa-t-client-001/locations/europe-west1/jobs/tenant-backfill-controller'
EXEC=JOB+'/executions/tenant-backfill-controller-synth1'
RUN='bf-00000000-0000-4000-8000-000000000103'
OWNER=dict(owner_decision_sha256='4'*64,qualification_hash='5'*64,security_hash='6'*64,source_probe_hash='8'*64)

@contextmanager
def patched(obj,name,value):
    saved=getattr(obj,name);setattr(obj,name,value)
    try:yield
    finally:setattr(obj,name,saved)

@contextmanager
def repo_with(release):
    """A registry view: real tools/, real releases plus the candidate's own registration."""
    tmp=tempfile.mkdtemp()
    try:
        root=tempfile.mkdtemp(dir=tmp);(__import__('pathlib').Path(root)/'tools').symlink_to(BF.REPO/'tools',target_is_directory=True)
        rel=__import__('pathlib').Path(root)/'infra/tenant/releases';shutil.copytree(BF.REPO/'infra/tenant/releases',rel)
        (rel/'backfill'/f"{release['source_sha']}.json").write_text(json.dumps(release,indent=2)+'\n')
        with patched(BF,'REPO',__import__('pathlib').Path(root)):yield
    finally:shutil.rmtree(tmp,ignore_errors=True)

def rec(root,kind,sq,p):return dict(version=D.VERSION,root_hash=root,kind=kind,sequence=sq,payload=p)

def fixture():
    m=FR.manifest();doc=F.render_leaf(m,INDEX,NOW.astimezone(BF.B.MSK).date());shard=F.shard_root(m,INDEX,doc)
    old=D.parse_tenant_json((BF.REPO/'infra/tenant/releases/backfill'/f'{OLD}.json').read_text())
    new=deepcopy(old);new.update(source_sha='f'*40,image=old['image'].split('@')[0]+'@sha256:'+'f'*64,controller_implementation_hash=O.implementation_hash(BF.REPO),terminal_gate_recovery_version=1)
    new['verification']=dict(old['verification'],terminal_gate_recovery_adapter='PASS')
    c=deepcopy(BF.target('client_001'));c['marketplaces']['ozon']['runtime_image']=old['runtime_image']
    settings=dict(release=new['source_sha'],root_hash=m['hash'],scheduler_state='PAUSED');c['orchestration']=O.block(c,settings,BF.REPO,new)
    hist=O.block(O.historical_contract(c,old,m),dict(release=OLD,root_hash=m['hash'],scheduler_state='ENABLED'),BF.REPO,old)
    started=NOW-timedelta(hours=3);stopped=started+timedelta(minutes=14)
    x=dict(name=EXEC,createTime=started.isoformat(),startTime=(started+timedelta(seconds=4)).isoformat(),completionTime=(stopped+timedelta(seconds=6)).isoformat(),failedCount=1,taskCount=1,
        template=dict(timeout=hist['job']['timeout'],maxRetries=0,serviceAccount=hist['accounts']['controller']['email'],
            containers=[dict(image=old['image'],command=['python','-m','tools.tenancy.cloud_controller'],env=[dict(name=k,value=v) for k,v in hist['job']['env'].items()])]))
    stop=rec(m['hash'],'STOPPED',0,dict(reason=R.REASON,controller_execution=EXEC))
    root=[rec(m['hash'],'FULL_MANIFEST',0,m),rec(m['hash'],'CHUNK_PLAN',INDEX,dict(index=INDEX,plan=doc,shard_root=shard)),stop]
    state=BF.B.initial(doc['runtime_plan'])
    receipt=dict(operation='projects/mpa-t-client-001/locations/europe-west1/operations/synthetic',run_id=RUN,lease_generation=LEASE,ack_hash=doc['ack_hash'])
    sh=[rec(shard,'DISPATCH_INTENT',PAGE,dict(plan=doc,preparation={k:receipt[k] for k in ('run_id','lease_generation','ack_hash')})),
        rec(shard,'DISPATCH_RECEIPT',PAGE,dict(plan=doc,receipt=receipt)),
        rec(shard,'RECONCILED',PAGE,dict(sequence=1626,state_hash=D.digest(state),persisted_reconciled=True,source_complete=False))]
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',c['project_id']])[:16];objects={}
    def put(n,labels,desc,created):objects[n]=(labels,desc,created)
    # Exact durable-store markers (as DurableRecords.commit writes them): a BFR_ commit marker per record, a BFQ_ fence per page record.
    minutes={'DISPATCH_INTENT':1,'DISPATCH_RECEIPT':2,'RECONCILED':7}
    for r in root+sh:
        h=D.digest(r);at=stopped if r is stop else started+timedelta(minutes=minutes.get(r['kind'],-30))
        put('BFR_'+h,{'root':r['root_hash'][:16],'kind':r['kind'].lower()},D.encoded({'root_hash':r['root_hash'],'record_hash':h,'kind':r['kind'],'sequence':r['sequence']}),at)
        if r in sh:put(f"BFQ_{shard}_{PAGE:010d}_{r['kind']}",{'root':shard[:16],'kind':r['kind'].lower()},D.encoded({'record_hash':h}),at)
    put(f'L_{cid}_{LEASE:04d}',{'owner':RUN,'until':str(int((started+timedelta(hours=2)).timestamp()))},json.dumps({'mode':'BOUNDED_PILOT','ack_hash':doc['ack_hash']}),started)
    put(f'LD_{cid}_{LEASE:04d}',{'owner':RUN},json.dumps({'operation':receipt['operation'],'ack_hash':doc['ack_hash']}),started+timedelta(minutes=7))
    for gen,(exe,status,dispatches,created) in enumerate(((JOB+'/executions/prev','MONITORING',1,started-timedelta(minutes=6)),(EXEC,'STOPPED',0,started+timedelta(minutes=2)),(JOB+'/executions/next','MONITORING',0,started+timedelta(minutes=17))),1):
        claim=CC.sealed(dict(version=1,tenant=m['tenant'],project=m['project'],root=m['hash'],policy_hash='a'*64,source=OLD,image=old['image'],execution=exe,generation=gen,created_at=created.isoformat()))
        put(CC.names(m['hash'],gen)[0],*CC.claim_value(claim),created)
        close=CC.sealed(dict(version=1,claim_hash=claim['hash'],status=status,source_dispatches=dispatches,closed_at=(created+timedelta(minutes=10)).isoformat()))
        put(CC.names(m['hash'],gen)[1],{'kind':'controller_close','root':m['hash'][:16]},D.encoded(close),created+timedelta(minutes=10))
    put(C.descriptor_name(new['source_sha'],m['hash'],'PAUSED'),{},D.encoded(dict(settings=settings,release=new)),NOW-timedelta(hours=1))
    base,_=BF.resources(c)
    ctx=dict(sched='PAUSED',journal_during=[{'ingestion_run_id':RUN},{'ingestion_run_id':RUN+'-u1616'}],journal_after=[],ledger_foreign=0,state=state,
        executions={JOB:[x,dict(name=JOB+'/executions/next',createTime=(started+timedelta(minutes=15)).isoformat(),completionTime=(started+timedelta(minutes=18)).isoformat(),succeededCount=1,taskCount=1)],
                    base+'/jobs/ozon-runtime-daily':[dict(name=base+'/jobs/ozon-runtime-daily/executions/page',createTime=(started-timedelta(seconds=13)).isoformat(),completionTime=(started+timedelta(seconds=85)).isoformat(),succeededCount=1,
                        template=dict(containers=[dict(env=[dict(name='INGESTION_RUN_ID',value=RUN),dict(name='ENTITIES',value='supplies')])]))]},
        recent=dict(version=1,type='FULL_RECENT_PRIORITY',root=m['hash'],days=180))
    effects=[]
    def request(method,url,body=None):
        if method!='GET' or body is not None:raise AssertionError('owner recovery is read-only')
        if url==BF.RUN_API+'/'+EXEC:return deepcopy(x)
        if url.startswith(BF.RUN_API+'/'+base+'/jobs/') and '/executions?' in url:return {'executions':deepcopy(ctx['executions'].get(url[len(BF.RUN_API)+1:].split('/executions?')[0],[]))}
        if url==BF.SCHED_API+'/'+base+'/jobs?pageSize=100':return {'jobs':[{'name':base+'/jobs/'+n,'state':ctx['sched']} for n in ('tenant-backfill-tick','ozon-daily','ozon-fast','ozon-weekly')]}
        raise AssertionError('unmocked cloud read')
    def select(sql,params):
        if '@start' in sql and set(params)=={'start'}:return deepcopy(ctx['journal_during'])
        if '@stop' in sql and set(params)=={'stop'}:return deepcopy(ctx['journal_after'])
        if 'BACKFILL_CHECKPOINTS' in sql and set(params)=={'start','lease','run'} and params['lease']==('INT64',LEASE) and params['run']==('STRING',RUN):return [{'n':ctx['ledger_foreign']}]
        raise AssertionError('unmocked query')
    def create(ds,n,labels,description):
        if n in objects:return False
        objects[n]=(labels,description,NOW);effects.append(('marker',n));return True
    def commit(root_hash,kind,seq,payload,now):
        r=rec(root_hash,kind,seq,deepcopy(payload));target=root if root_hash==m['hash'] else sh
        if r not in target:target.append(r);effects.append(('record',kind))
        return D.digest(r)
    tables=SimpleNamespace(get_table=lambda ds,n:objects[n][:2] if n in objects else None,list_tables=lambda ds,**kw:[(n,v[0],v[2]) for n,v in objects.items()],create_marker=create)
    store=SimpleNamespace(history=lambda r,**kw:deepcopy(root) if r==m['hash'] else (deepcopy(sh) if r==shard else []),commit=commit)
    b=SimpleNamespace(c=c,current_execution=None,clock=lambda:NOW,request=request,select=select,tables=tables,store=store,state=lambda d:deepcopy(ctx['state']),journal='mpa-t-client-001.ozon_raw.OZON_INGESTION_RUNS')
    return SimpleNamespace(m=m,b=b,root=root,sh=sh,objects=objects,effects=effects,stop=stop,ctx=ctx,new=new,old=old,shard=shard,cid=cid,doc=doc,started=started,stopped=stopped,x=x)

@contextmanager
def observing(f):
    """Seams whose own adapters prove them: current GO/bindings and the owner RECENT authority."""
    from tools.tenancy import full_controller as H,recent_priority as RP
    with repo_with(f.new),patched(H,'verify_authority',lambda b,m:None),patched(RP,'load',lambda b,m,release=None:deepcopy(f.ctx['recent'])):yield

def observe(f,stop_hash=None):
    with observing(f):return R.observe(f.b,f.m,stop_hash or D.digest(f.stop),index=INDEX,**OWNER)[0]

def certificate_check():
    f=fixture();p=observe(f)
    assert (p['shard'],p['page_sequence'],p['lease_generation'],p['claim_generation'],p['index'])==(f.shard,PAGE,LEASE,2,INDEX) and p['recent_policy_hash']==D.digest(f.ctx['recent'])
    assert p['old_source']==OLD and p['new_source']==f.new['source_sha'] and not f.effects
    with repo_with(f.new):R.header(p,f.m);R.validate(p,f.b,f.m,f.root)
    for k in R.PREDICATES:
        q=deepcopy(p);q['predicates'][k]=None;q=R.sealed({k2:v for k2,v in q.items() if k2!='hash'})
        try:R.header(q,f.m)
        except BF.B.EvidenceError:pass
        else:raise AssertionError('unknown predicate accepted')
    typed=deepcopy(f.stop);typed['payload']['diagnostic']=dict(version=1,stage='LEASE',category='EVIDENCE');f.root[-1]=typed
    try:observe(f,D.digest(typed))
    except BF.B.EvidenceError as e:assert 'exact untyped' in str(e)
    else:raise AssertionError('typed STOP accepted by the untyped recovery')
    return 'PASS'

def negatives():
    def next_page(f):f.sh.append(rec(f.shard,'DISPATCH_INTENT',PAGE+1,dict(plan=f.doc,preparation={})))
    def next_fence(f):f.objects[f'BFQ_{f.shard}_{PAGE+1:010d}_DISPATCH_INTENT']=({}, '',f.stopped-timedelta(seconds=1))
    def lease_beyond(f):f.objects[f'L_{f.cid}_{LEASE+1:04d}']=({'owner':'bf-next','until':'0'},'',f.stopped-timedelta(seconds=1))
    def lease_unreleased(f):del f.objects[f'LD_{f.cid}_{LEASE:04d}']
    def lease_foreign_owner(f):f.objects[f'LD_{f.cid}_{LEASE:04d}']=({'owner':'bf-other'},'',f.started)
    def claim_dispatched(f):
        name=CC.names(f.m['hash'],3)[1];claim=D.parse_tenant_json(f.objects[CC.names(f.m['hash'],3)[0]][1])
        close=CC.sealed(dict(version=1,claim_hash=claim['hash'],status='DISPATCHED',source_dispatches=1,closed_at=NOW.isoformat()));f.objects[name]=(f.objects[name][0],D.encoded(close),f.objects[name][2])
    def claim_unclosed(f):del f.objects[CC.names(f.m['hash'],3)[1]]
    def runtime_after(f):f.ctx['executions'][JOB.rsplit('/jobs/',1)[0]+'/jobs/ozon-runtime-fast']=[dict(name='x',createTime=(f.stopped+timedelta(minutes=1)).isoformat(),completionTime=(f.stopped+timedelta(minutes=2)).isoformat())]
    def runtime_foreign_during(f):f.ctx['executions'][JOB.rsplit('/jobs/',1)[0]+'/jobs/ozon-runtime-fast']=[dict(name='x',createTime=(f.started+timedelta(minutes=1)).isoformat(),completionTime=(f.started+timedelta(minutes=2)).isoformat(),template=dict(containers=[dict(env=[dict(name='INGESTION_RUN_ID',value='bf-other')])]))]
    def runtime_active(f):f.ctx['executions'][JOB][1].pop('completionTime')
    def journal_after(f):f.ctx['journal_after']=[{'ingestion_run_id':RUN}]
    def journal_foreign(f):f.ctx['journal_during'].append({'ingestion_run_id':'bf-other-run'})
    def journal_saturated(f):f.ctx['journal_during']=[{'ingestion_run_id':RUN}]*1000
    def ledger_foreign(f):f.ctx['ledger_foreign']=1
    def page_before_wake(f):f.objects[f'BFQ_{f.shard}_{PAGE:010d}_RECONCILED']=({},'',f.started-timedelta(seconds=1))
    def later_fence_only(f):f.objects[f'BFQ_{f.shard}_{PAGE+7:010d}_RECONCILED']=({},'',f.started)
    def state_changed(f):f.ctx['state']=dict(f.ctx['state'],sequence=f.ctx['state'].get('sequence',0)+1)
    def scheduler_enabled(f):f.ctx['sched']='ENABLED'
    def unqualified_replacement(f):f.new.pop('terminal_gate_recovery_version');f.new['verification'].pop('terminal_gate_recovery_adapter')
    def page_not_persisted(f):f.sh[-1]['payload']['persisted_reconciled']=False
    def stop_before_page(f):f.objects[f'BFQ_{f.shard}_{PAGE:010d}_RECONCILED']=({},'',f.stopped+timedelta(seconds=1))
    def other_stop(f):f.root.append(rec(f.m['hash'],'STOPPED',0,dict(reason='X')))
    def controller_succeeded(f):f.x.update(failedCount=0,succeededCount=1)
    return [next_page,next_fence,lease_beyond,lease_unreleased,lease_foreign_owner,claim_dispatched,claim_unclosed,runtime_after,runtime_foreign_during,
            runtime_active,journal_after,journal_foreign,journal_saturated,ledger_foreign,page_before_wake,later_fence_only,state_changed,scheduler_enabled,unqualified_replacement,page_not_persisted,stop_before_page,other_stop,controller_succeeded]

def negative_check():
    for fault in negatives():
        f=fixture();fault(f)
        try:observe(f)
        except BF.B.EvidenceError:pass
        else:raise AssertionError('accepted: '+fault.__name__)
        assert not f.effects,fault.__name__
    return 'PASS'

def publish_check():
    f=fixture();p=observe(f);before=deepcopy(f.root)
    with observing(f):q=R.publish(f.b,p,f.m)
    assert q==p and f.effects==[('marker',R.authority(p)),('marker',R.certificate(p)),('record',R.KIND)]
    # Freshness expiring between the re-observation and the first write publishes nothing (0 writes).
    g=fixture();p2=observe(g);ticks=[NOW,NOW+timedelta(seconds=R.FRESH_SECONDS+1)];g.b.clock=lambda:ticks[0] if len(ticks)>1 and ticks.pop(0) else ticks[0]
    try:
        with observing(g):R.publish(g.b,p2,g.m)
    except BF.B.EvidenceError as e:assert 'fresh proof required' in str(e) and not g.effects
    else:raise AssertionError('expired proof published')
    # Exact HTTP write set of a publication through the real durable store: 2 markers + commit (BFR_ marker + checkpoint row) = 4.
    h=fixture();p3=observe(h);writes=[]
    class Meta:
        def list_tables(self,ds,**kw):return [(n,v[0],v[2]) for n,v in h.objects.items()]
        def get_table(self,ds,n):return h.objects[n][:2] if n in h.objects else None
        def create_marker(self,ds,n,labels,description):
            writes.append((ds,n))
            if n in h.objects:return False
            h.objects[n]=(labels,description,NOW);return True
    rows=[dict(plan_hash=r['root_hash'],backfill_id=D.digest(r)[:16],evidence_json=D.encoded(r)) for r in h.root+h.sh]   # committed rows behind the exact markers
    def write_record(row):writes.append(('tenant_ops','BACKFILL_CHECKPOINTS'));rows.append(row)
    def select(c_,sql,params):return [{'evidence_json':r['evidence_json']} for r in rows if r['plan_hash']==params['root'][1] and ('record' not in params or r['backfill_id']==params['record'][1])]
    h.b.tables=Meta();h.b.store=D.DurableRecords(h.b.c,Meta(),select,write_record)
    assert h.b.store.history(h.m['hash'])==sorted(h.root,key=lambda r:(r['sequence'],r['kind'],D.digest(r)))
    with observing(h):R.publish(h.b,p3,h.m)
    assert writes==R.publication_writes(h.b,p3,h.m) and len(writes)==4,writes
    assert f.root[:len(before)]==before and len(f.root)==len(before)+1 and f.root[-1]['kind']==R.KIND and f.root[-1]['sequence']==0
    with repo_with(f.new):assert R.load(f.b,f.m,f.root)==[p]
    from tools.tenancy import stop_acceptance as SA
    with repo_with(f.new):assert SA.accepted_hashes(f.b,f.m,f.root,fresh=True)=={p['stop_hash']} and not SA.unresolved(f.b,f.m,f.root,fresh=True)
    g=fixture();p2=observe(g);g.ctx['recent']=dict(g.ctx['recent'],days=90)
    try:
        with observing(g):R.publish(g.b,p2,g.m)
    except BF.B.EvidenceError as e:assert 'fresh predicates changed' in str(e) and not g.effects
    else:raise AssertionError('changed RECENT authority published')
    return 'PASS'

# ── typed write path ───────────────────────────────────────────────────────────
def dispatch_harness(*,lease_post=None,checkpoint=None,prepared=None,run_post=None,receipt=None):
    """Real BF.start over fakes; each hook may raise. Returns (run, counts)."""
    from tools.tenancy.cloud_access import TransientReadError
    m=FR.manifest();doc=F.render_leaf(m,INDEX,NOW.astimezone(BF.B.MSK).date());c=BF.target('client_001');counts=dict(lease=0,run=0)
    def append_request(method,url,body=None):
        counts['lease']+=1
        if lease_post:raise lease_post
        return {}
    def dispatch(d,name,body):
        counts['run']+=1
        if run_post:raise run_post
        return {'name':f"projects/{c['project_id']}/locations/{c['region']}/operations/synthetic"}
    def append(ds,table,rows):
        if checkpoint:raise checkpoint
    tables=SimpleNamespace(list_tables=lambda ds,**kw:[],get_table=lambda ds,n:None,append=append)
    backend=SimpleNamespace(c=c,append_request=append_request,dispatch=dispatch,tables=tables)
    def before(preparation):
        if prepared:raise prepared
    def after(r):
        if receipt:raise receipt
    def run():
        with patched(BF,'preflight',lambda *a,**k:(tables,[])):
            return BF.start(doc,doc['ack_hash'],backend=backend,on_prepared=before,on_receipt=after)
    return run,counts,backend

def write_path_check():
    from tools.tenancy.cloud_access import TransientReadError
    L=lambda **kw:dict(dict.fromkeys(CD.WRITES,'ABSENT'),**kw)
    cases=[  # (fault kwargs, expected stage, category, ledger, run POSTs)
        (dict(lease_post=BF.TT.TableError('lost')),'LEASE','CLOUD_METADATA',L(lease='UNKNOWN'),0),
        (dict(checkpoint=BF.TT.TableError('lost')),'CHECKPOINT','CLOUD_METADATA',L(lease='PRESENT',checkpoint='UNKNOWN'),0),
        (dict(prepared=TransientReadError('read')),'SOURCE_DISPATCH_BOUNDARY','CLOUD_METADATA',L(lease='PRESENT',checkpoint='PRESENT',intent='UNKNOWN'),0),
        (dict(prepared=BF.B.EvidenceError('intent differs')),'SOURCE_DISPATCH_BOUNDARY','EVIDENCE',L(lease='PRESENT',checkpoint='PRESENT',intent='UNKNOWN'),0),
        (dict(run_post=BF.TT.TableError('lost response')),'SOURCE_DISPATCH_BOUNDARY','CLOUD_METADATA',L(lease='PRESENT',checkpoint='PRESENT',intent='PRESENT',run='UNKNOWN'),1),
        (dict(run_post=TransientReadError('read')),'SOURCE_DISPATCH_BOUNDARY','CLOUD_METADATA',L(lease='PRESENT',checkpoint='PRESENT',intent='PRESENT',run='UNKNOWN'),1),
        (dict(receipt=TransientReadError('read')),'SOURCE_DISPATCH_BOUNDARY','CLOUD_METADATA',L(lease='PRESENT',checkpoint='PRESENT',intent='PRESENT',run='PRESENT',receipt='UNKNOWN'),1),
        (dict(receipt=KeyError('x')),'SOURCE_DISPATCH_BOUNDARY','SCHEMA',L(lease='PRESENT',checkpoint='PRESENT',intent='PRESENT',run='PRESENT',receipt='UNKNOWN'),1),
        (dict(run_post=RuntimeError('unlisted')),'SOURCE_DISPATCH_BOUNDARY','SCHEMA',L(lease='PRESENT',checkpoint='PRESENT',intent='PRESENT',run='UNKNOWN'),1)]
    for kw,stage,category,ledger,posts in cases:
        run,counts,backend=dispatch_harness(**kw)
        try:run()
        except CD.GateFailure as failure:
            assert CD.safe(failure)==dict(version=1,stage=stage,category=category,readback=ledger),(kw,CD.safe(failure))
            assert backend.dispatch_effects==ledger
        else:raise AssertionError('write failure untyped: %r'%kw)
        assert counts['run']==posts and counts['lease']==1,(kw,counts)
    run,counts,_=dispatch_harness();r=run();assert r['run_id'].startswith('bf-') and counts==dict(lease=1,run=1)
    # Before any write: a transient read and a held lease keep their exact routes (bounded wait / typed by main).
    for error in (TransientReadError('read'),BF.B.EvidenceError(CD.PILOT_HELD)):
        tables_error=error
        def listing(ds,**kw):raise tables_error
        run,counts,backend=dispatch_harness();backend.tables.list_tables=listing
        try:run()
        except type(error) as raised:assert not isinstance(raised,CD.GateFailure) and str(raised)==str(error)
        else:raise AssertionError('pre-write failure typed')
        assert counts==dict(lease=0,run=0)
    # Lost response of the Run POST never repeats it on the same wake; receipt-unproven after start is typed.
    from tools.tenancy import full_controller as H
    m=FR.manifest();doc=F.render_leaf(m,INDEX,NOW.astimezone(BF.B.MSK).date())
    silent=SimpleNamespace(start=lambda d,before,after:None,clock=lambda:NOW,store=SimpleNamespace(commit=lambda *a:None))
    try:H._dispatch(silent,m,INDEX,doc,F.shard_root(m,INDEX,doc),PAGE+1)
    except CD.GateFailure as failure:assert CD.safe(failure)['stage']=='SOURCE_DISPATCH_BOUNDARY' and CD.safe(failure)['readback']==dict.fromkeys(CD.WRITES,'UNKNOWN')
    else:raise AssertionError('receipt unproven untyped')
    assert CD.safe(CD.GateFailure('LEASE','EVIDENCE'))==dict(version=1,stage='LEASE',category='EVIDENCE')
    bad=CD.GateFailure('LEASE','EVIDENCE');bad.diagnostic['readback']={'lease':'MAYBE'}
    assert 'readback' not in CD.safe(bad)
    return 'PASS'

def main_harness(*,cadence,wake,closure=None):
    """cloud_controller.main over a bootstrap stub; returns (rc, printed, committed STOP payloads)."""
    import io,contextlib
    c=deepcopy(BF.target('client_001'));c['orchestration']={'job':{'env':{}},**({'cadence':{'root':'1'*64}} if cadence else {})}
    commits=[]
    backend=SimpleNamespace(c=c,current_execution=EXEC,clock=lambda:NOW,store=SimpleNamespace(commit=lambda root,kind,seq,payload,now:commits.append((kind,payload)) or 'h'*64),
        tables=SimpleNamespace(list_tables=lambda ds:[]),monitoring=lambda root,result:{})
    out=io.StringIO()
    with patched(C,'bootstrap',lambda env:(backend,'1'*64)),patched(C,'bounded_wake',wake),patched(C,'close_decision',closure or (lambda b,r:None)),contextlib.redirect_stdout(out):
        rc=C.main()
    return rc,[D.parse_tenant_json(line) for line in out.getvalue().splitlines()],commits

def main_check():
    held=lambda root,backend:(_ for _ in ()).throw(BF.B.EvidenceError(CD.PILOT_HELD))
    rc,printed,commits=main_harness(cadence=False,wake=held)
    assert rc==1 and printed==[{'status':'WAITING_LEASE','reason':'CANONICAL_LEASE_NOT_YET_RECLAIMABLE'}] and not commits
    rc,printed,commits=main_harness(cadence=True,wake=held)
    diag=dict(version=1,stage='LEASE',category='EVIDENCE')
    assert rc==2 and printed[-1]=={'status':'STOPPED','reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','diagnostic':diag}
    assert commits==[('STOPPED',{'reason':'CONTROLLER_GATE_OR_EVIDENCE_FAILURE','controller_execution':EXEC,'diagnostic':diag})]
    from tools.tenancy.cloud_access import TransientReadError
    def lost(b,r):raise TransientReadError('read')
    rc,printed,commits=main_harness(cadence=True,wake=lambda root,backend:{'status':'WAITING_READ_RETRY','reason':'TRANSIENT_READ_TRANSPORT','progress':{}},closure=lost)
    diag=dict(version=1,stage='CONTROL_CLOSURE',category='CLOUD_METADATA')
    assert rc==2 and printed[-1]['diagnostic']==diag and commits[0][1]['diagnostic']==diag
    assert 'CONTROL_CLOSURE' in CD.STAGES
    # A typed write-path STOP without RECONCILE context never enters automatic class-C recovery (no second STOP).
    from tools.tenancy import full_leaf_recovery as FL
    stop=rec('1'*64,'STOPPED',0,dict(reason='CONTROLLER_GATE_OR_EVIDENCE_FAILURE',controller_execution=EXEC,diagnostic=dict(version=1,stage='LEASE',category='EVIDENCE')))
    probe=SimpleNamespace(store=SimpleNamespace(history=lambda *a,**k:(_ for _ in ()).throw(AssertionError('candidate search for a context-free STOP'))))
    with patched(FL,'accepted_recoveries',lambda b,m,r:[]):
        assert FL._recover_pending(probe,{'hash':'1'*64},[stop]) is None
    return 'PASS'

def acceptance_check():
    from tools.tenancy import stop_acceptance as SA
    with patched(R,'load',lambda b,m,r:[{'stop_hash':'9'*64}]):
        assert SA.accepted_hashes(SimpleNamespace(),{},[{'kind':R.KIND}])=={'9'*64}
    assert R.KIND in D.KINDS and 'full_terminal_gate_recovery.py' in O.SOURCE_FILES and 'full_terminal_gate_image_check.py' in O.SOURCE_FILES
    return 'PASS'

def check():
    for name,fn in (('certificate',certificate_check),('negatives',negative_check),('publish',publish_check),('write_path',write_path_check),('main',main_check),('acceptance',acceptance_check)):
        assert fn()=='PASS',name
    return {'terminal_gate_recovery_adapter':'PASS'}

if __name__=='__main__':
    print(json.dumps(check(),sort_keys=True))
