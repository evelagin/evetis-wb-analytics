"""Network-free packaged checks of actual cadence CAS and -m wait handler."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import contextlib
import io
import json
import runpy
from tools.tenancy import controller_cadence as CC, orchestration_contract as O
from tools.tenancy import tenant_backfill as BF, cloud_tick as T, cloud_controller as C
from tools.tenancy.full_leaf_image_check import fixture, NOW


def protocol():
    m,_,_,_,_,_,release,*_=fixture()
    release=deepcopy(release);release['schema_version']=4
    release['verification'].update(recent_priority_adapter='PASS',cadence_overlap_adapter='PASS')
    release['cadence_policy']=dict(version=1,profile=CC.PROFILE,tenant='client_001',project='mpa-t-client-001',
        root=m['hash'],preferred='*/3 * * * *',stable_fallback='*/10 * * * *',owner_ack_sha256='9'*64)
    c=BF.target('client_001');c=dict(c,orchestration=O.block(c,dict(release=release['source_sha'],root_hash=m['hash'],scheduler_state='ENABLED'),BF.REPO,release))
    base,_=BF.resources(c);job_name=base+'/jobs/'+O.JOB;block=c['orchestration']
    task=dict(timeout='600s',maxRetries=0,serviceAccount=block['accounts']['controller']['email'],containers=[dict(image=release['image'],command=['python','-m','tools.tenancy.cloud_controller'],env=[dict(name=k,value=v) for k,v in block['job']['env'].items()])])
    def execution(suffix,seconds):return dict(name=job_name+'/executions/'+O.JOB+'-'+suffix,createTime=(NOW-timedelta(seconds=seconds)).isoformat(),taskCount=1,template=deepcopy(task))
    executions=[execution('older',90),execution('newer',40),execution('newest',10)]
    objects={};reads=[];commits=[]
    class Tables:
        def list_tables(self,ds,with_expiry=False):return [(n,v[0],NOW,None) if with_expiry else (n,v[0],NOW) for n,v in objects.items()]
        def get_table(self,ds,n):return objects.get(n)
        def create_marker(self,ds,n,labels,description):
            if n in objects:return False
            objects[n]=(deepcopy(labels),description);return True
    tables=Tables()
    def request(method,url,body=None):
        assert method=='GET','packaged overlap attempted source/mutation transport'
        reads.append(url)
        if url==BF.RUN_API+'/'+job_name:return dict(name=job_name,template=dict(taskCount=1,template=deepcopy(task)))
        if '/executions?' in url:return dict(executions=deepcopy(executions))
        return deepcopy(next(x for x in executions if url==BF.RUN_API+'/'+x['name']))
    def backend(index):return SimpleNamespace(c=deepcopy(c),tables=tables,request=request,clock=lambda:NOW,current_execution=executions[index]['name'],store=SimpleNamespace(commit=lambda *a:commits.append(a)))
    return m,release,c,backend,executions,objects,commits


def check():
    m,release,c,backend,executions,objects,commits=protocol()
    assert c['orchestration']['scheduler']['schedule']=='*/3 * * * *'
    for bad in ('*/1 * * * *','*/4 * * * *','0 * * * *','unregistered'):
        r=deepcopy(release);r['cadence_policy']['preferred']=bad
        try:O.block(c,dict(release=r['source_sha'],root_hash=m['hash'],scheduler_state='ENABLED'),BF.REPO,r)
        except BF.B.EvidenceError:pass
        else:raise AssertionError('unapproved cadence accepted')
    leader=backend(0);claim=CC.acquire(leader);assert claim['generation']==1
    for i in (1,2):
        try:CC.acquire(backend(i))
        except T.OverlapWait as wait:assert wait.status=='WAIT_ACTIVE_CONTROLLER'
        else:raise AssertionError('overlapping wake acquired source authority')
    assert len(objects)==1 and not commits
    CC.ensure(leader);CC.close(leader,dict(status='DISPATCHED',source_dispatches=1))
    executions[0].update(completionTime=NOW.isoformat(),succeededCount=1)
    successor=backend(1);assert CC.acquire(successor)['generation']==2
    CC.close(successor,dict(status='MONITORING',source_dispatches=0))
    # Shared exception class must survive the actual duplicated -m namespace.
    with contextlib.redirect_stderr(io.StringIO()):namespace=runpy.run_module('tools.tenancy.cloud_controller',run_name='__cadence_image_check__')
    assert namespace['OverlapWait'] is T.OverlapWait
    observer=backend(2);output=io.StringIO()
    with patch.object(C,'bootstrap',lambda env:(observer,m['hash'])),patch.object(C,'bounded_wake',lambda *a:(_ for _ in ()).throw(T.OverlapWait('WAIT_ACTIVE_CONTROLLER'))),contextlib.redirect_stdout(output):
        assert C.main()==0
    assert json.loads(output.getvalue())==dict(status='WAIT_ACTIVE_CONTROLLER',source_dispatches=0) and not commits
    output=io.StringIO()
    with patch.object(C,'bootstrap',lambda env:(observer,m['hash'])),patch.object(C,'bounded_wake',lambda *a:(_ for _ in ()).throw(BF.B.EvidenceError('synthetic unknown'))),contextlib.redirect_stdout(output):
        assert C.main()==2
    assert json.loads(output.getvalue())['status']=='STOPPED' and len(commits)==1
    return dict(cadence_owner_closed='PASS',cadence_atomic_overlap='PASS',cadence_two_waiters_one_authority='PASS',
                cadence_shared_entrypoint_wait='PASS',cadence_unknown_stops='PASS',cadence_source_transport_zero='PASS')


if __name__=='__main__':print(json.dumps(check(),sort_keys=True))
