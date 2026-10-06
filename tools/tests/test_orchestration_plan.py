"""Controller opt-in mutation tests; synthetic Terraform show shape, no cloud."""
import copy
import json
import pytest
from tools.tenancy import tenant_backfill as BF, orchestration_contract as O, plan_scan as P
from tools.tests.test_cloud_controller import release
from tools.tests.test_tenancy_t32_remediation import _plan_for,_rc,_acl

@pytest.fixture
def qualified(tmp_path,monkeypatch):
    c=BF.target('client_001');r=release(c)
    # Test-only qualified release repository; never writes production release files.
    for name in O.SOURCE_FILES:
        path=tmp_path/'tools/tenancy'/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes((BF.REPO/'tools/tenancy'/name).read_bytes())
    f=tmp_path/'infra/tenant/releases/backfill'/('5'*40+'.json');f.parent.mkdir(parents=True);f.write_text(json.dumps(r))
    monkeypatch.setattr(BF,'REPO',tmp_path)
    c['orchestration']=O.block(c,{'release':'5'*40,'root_hash':'3'*64,'scheduler_state':'PAUSED'},tmp_path)
    plan=_plan_for(c);block=c['orchestration'];p=c['project_id']
    for rc in plan['resource_changes']:
        a=rc['change']['after']
        if rc['type']=='google_bigquery_dataset':
            for g in block['dataset_grants']:
                if a['dataset_id']==c['datasets'][g['dataset_key']]:a['access'].append(_acl(g['role'],user_by_email=g['email']))
    for key,sa in block['accounts'].items():
        plan['resource_changes'].append(_rc(f'google_service_account.backfill["{key}"]','google_service_account',{'project':p,'account_id':sa['id'],'email':sa['email'],'member':'serviceAccount:'+sa['email']}))
    for name,perms in block['roles'].items():
        plan['resource_changes'].append(_rc(f'google_project_iam_custom_role.backfill["{name}"]','google_project_iam_custom_role',{'project':p,'role_id':name,'permissions':perms,'stage':'GA','deleted':False}))
    for i,row in enumerate(block['matrix']):
        res=row['resource'];a={'role':row['role'],'member':'serviceAccount:'+row['principal'],'condition':[]};kind=None
        if res==f'projects/{p}':kind='google_project_iam_member';a['project']=p
        elif '/jobs/' in res:kind='google_cloud_run_v2_job_iam_member';a.update(project=p,location=c['region'],name=res.rsplit('/',1)[-1])
        elif '/tables/' in res:kind='google_bigquery_table_iam_member';a.update(project=p,dataset_id=res.split('/')[3],table_id=res.split('/')[5])
        elif '/serviceAccounts/' in res:kind='google_service_account_iam_member';a['service_account_id']=res
        if kind:plan['resource_changes'].append(_rc(f'{kind}.backfill[{i}]',kind,a))
    task={'service_account':block['accounts']['controller']['email'],'timeout':'600s','max_retries':0,
          'containers':[{'image':r['image'],'command':['python','-m','tools.tenancy.cloud_controller'],'args':[],
                         'env':[{'name':k,'value':v} for k,v in block['job']['env'].items()]}]}
    plan['resource_changes'].append(_rc('google_cloud_run_v2_job.backfill[0]','google_cloud_run_v2_job',{'project':p,'location':c['region'],'name':block['job']['name'],'template':[{'task_count':1,'parallelism':1,'template':[task]}]}))
    sc=block['scheduler'];plan['resource_changes'].append(_rc('google_cloud_scheduler_job.backfill[0]','google_cloud_scheduler_job',{'project':p,'name':sc['name'],'region':c['region'],'schedule':sc['schedule'],'time_zone':sc['time_zone'],'paused':True,'retry_config':[],'http_target':[{'uri':sc['uri'],'http_method':'POST','body':'e30=','oauth_token':[{'service_account_email':block['accounts']['wake']['email'],'scope':'https://www.googleapis.com/auth/cloud-platform'}]}]}))
    for kind in P.BP.TYPES:
        plan['configuration']['root_module']['resources'].append({'address':kind+'.backfill','mode':'managed','type':kind,'provider_config_key':'google'})
    return c,plan


def first(plan,kind):return next(r for r in plan['resource_changes'] if r['type']==kind)['change']['after']


def test_only_qualified_exact_matrix_is_permitted(qualified):
    c,p=qualified
    assert P.scan_plan(p,c)==[]
    unregistered=copy.deepcopy(c);unregistered.pop('orchestration')
    assert P.scan_plan(p,unregistered)


@pytest.mark.parametrize('fault',['extra_permission','foreign_job','writer_query','extra_table','credential_delegate','ordinary_wake','controller_override','controller_concurrency','historical_reretry','historical_early_enable','unknown_iam','delete','forged_contract'])
def test_backfill_expansion_or_bypass_stops_plan(qualified,fault):
    c,p=qualified
    if fault=='extra_permission':first(p,'google_project_iam_custom_role')['permissions'].append('secretmanager.versions.access')
    if fault=='foreign_job':first(p,'google_cloud_run_v2_job_iam_member')['name']='ozon-dev-trial'
    if fault=='writer_query':first(p,'google_project_iam_member')['member']='serviceAccount:sa-backfill-append@'+c['project_id']+'.iam.gserviceaccount.com'
    if fault=='extra_table':first(p,'google_bigquery_table_iam_member')['table_id']='CAPABILITY_PROFILE'
    if fault=='credential_delegate':first(p,'google_service_account_iam_member')['service_account_id']='projects/'+c['project_id']+'/serviceAccounts/'+c['control']['email']
    if fault=='ordinary_wake':first(p,'google_cloud_run_v2_job_iam_member')['member']='serviceAccount:sa-ozon-scheduler@'+c['project_id']+'.iam.gserviceaccount.com'
    job=next(r['change']['after'] for r in p['resource_changes'] if r['type']=='google_cloud_run_v2_job' and r['change']['after']['name']==O.JOB)
    if fault=='controller_override':job['template'][0]['template'][0]['containers'][0]['env'].append({'name':'TENANT_BINDING_REQUIRED','value':'0'})
    if fault=='controller_concurrency':job['template'][0]['task_count']=2
    scheduler=next(r['change']['after'] for r in p['resource_changes'] if r['type']=='google_cloud_scheduler_job' and r['change']['after']['name']==O.SCHEDULER)
    if fault=='historical_reretry':scheduler['retry_config']=[{'retry_count':3}]
    if fault=='historical_early_enable':scheduler['paused']=False
    if fault=='unknown_iam':next(r for r in p['resource_changes'] if r['type']=='google_service_account_iam_member')['change']['after_unknown']={'member':True}
    if fault=='delete':next(r for r in p['resource_changes'] if r['type']=='google_service_account_iam_member')['change']['actions']=['delete','create']
    if fault=='forged_contract':c['orchestration']['roles']['backfillRead'].append('bigquery.tables.delete')
    assert P.scan_plan(p,c)
