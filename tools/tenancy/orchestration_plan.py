"""Exact opt-in plan boundary; no weakening of ordinary H2 activation safety."""
from tools.tenancy import orchestration_contract as O, orchestration_identity as I
from tools.tenancy import tenant_backfill as BF

TYPES=frozenset({'google_project_iam_custom_role','google_bigquery_table_iam_member',
                 'google_service_account_iam_member','google_cloud_run_v2_job_iam_member'})
CRITICAL={
 'google_project_iam_custom_role': [('project',),('role_id',),('permissions',),('stage',)],
 'google_bigquery_table_iam_member': [('project',),('dataset_id',),('table_id',),('role',),('member',),('condition',)],
 'google_service_account_iam_member': [('service_account_id',),('role',),('member',),('condition',)],
 'google_cloud_run_v2_job_iam_member': [('project',),('location',),('name',),('role',),('member',),('condition',)],
}


def verified(c):
    block=c.get('orchestration')
    if not block:return None
    env=block['job']['env']
    expected=O.block(c,{'release':env['CONTROLLER_SOURCE_SHA'],'root_hash':env['BACKFILL_ROOT_HASH'],
                       'scheduler_state':block['scheduler']['state']},BF.REPO)
    if block!=expected:raise ValueError('orchestration contract differs from qualified canonical release')
    return block


def iam(block,c):
    if not block:return set()
    result=set();p=c['project_id']
    for row in block['matrix']:
        resource=row['resource'];kind=None
        if resource==f'projects/{p}':kind='google_project_iam_member';resource=p
        elif '/jobs/' in resource:kind='google_cloud_run_v2_job_iam_member'
        elif '/tables/' in resource:kind='google_bigquery_table_iam_member'
        elif '/serviceAccounts/' in resource:kind='google_service_account_iam_member'
        if kind:result.add((kind,resource,row['role'],'serviceAccount:'+row['principal']))
    return result


def target(kind,a,p):
    if kind=='google_bigquery_table_iam_member':return f"projects/{p}/datasets/{a.get('dataset_id')}/tables/{a.get('table_id')}"
    if kind=='google_service_account_iam_member':return a.get('service_account_id')
    if kind=='google_cloud_run_v2_job_iam_member':return f"projects/{p}/locations/{a.get('location')}/jobs/{a.get('name')}"
    return None


def job_findings(a,block):
    out=[];outer=(a.get('template') or [{}])[0];task=(outer.get('template') or [{}])[0]
    cs=task.get('containers') or []
    if outer.get('task_count')!=1 or outer.get('parallelism')!=1 or task.get('timeout')!=block['job']['timeout'] or task.get('max_retries')!=0:
        out.append('controller task/retry/timeout drift')
    if task.get('service_account')!=block['accounts']['controller']['email'] or len(cs)!=1:
        return out+['controller service identity/container drift']
    cont=cs[0];entries=cont.get('env') or [];env={v.get('name'):v.get('value') for v in entries}
    if env!=block['job']['env'] or len(env)!=len(entries) or any(v.get('value_source') for v in entries) or cont.get('image')!=block['job']['image'] or cont.get('command')!=['python','-m','tools.tenancy.cloud_controller'] or (cont.get('args') or []):
        out.append('controller image/entrypoint/env drift')
    return out


def scheduler_findings(a,block):
    s=block['scheduler'];out=[];http=(a.get('http_target') or [{}])[0];oauth=(http.get('oauth_token') or [{}])[0]
    if a.get('paused') is not (s['state']=='PAUSED') or a.get('schedule')!=s['schedule'] or a.get('time_zone')!=s['time_zone']:
        out.append('historical Scheduler state/schedule drift')
    if http.get('uri')!=s['uri'] or http.get('http_method')!='POST' or http.get('body')!='e30=' or oauth.get('service_account_email')!=block['accounts']['wake']['email'] or oauth.get('scope')!='https://www.googleapis.com/auth/cloud-platform' or http.get('oidc_token') or a.get('retry_config'):
        out.append('historical Scheduler invocation/identity/retry drift')
    return out
