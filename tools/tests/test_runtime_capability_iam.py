"""Exact table custom role rejects all unrelated IAM expansions in reviewed plans."""
import copy
import pytest
from tools.tenancy import tenant_backfill as B,plan_scan as S,runtime_identity as I
from tools.tests.test_tenancy_t32_remediation import _plan_for,_rc


def plan():
    c=B.target('client_001');c.pop('orchestration',None);p=_plan_for(c);project=c['project_id']
    role=_rc('google_project_iam_custom_role.runtime_capability_read[0]','google_project_iam_custom_role',
        {'project':project,'role_id':I.ROLE,'stage':'GA','permissions':list(I.PERMISSIONS),'deleted':False})
    binding=_rc('google_bigquery_table_iam_member.runtime_capability_read[0]','google_bigquery_table_iam_member',
        {'project':project,'dataset_id':'tenant_ops','table_id':'CAPABILITY_PROFILE','role':I.matrix(c)[0]['role'],
         'member':'serviceAccount:'+I.matrix(c)[0]['principal'],'condition':[]})
    p['resource_changes'] += [role,binding]
    for kind in ['google_project_iam_custom_role','google_bigquery_table_iam_member']:
        p['configuration']['root_module']['resources'].append({'address':kind+'.runtime_capability_read','mode':'managed','type':kind,'provider_config_key':'google'})
    return c,p,role,binding


def test_minimal_table_read_plan_allowed():
    c,p,_,_=plan();assert S.scan_plan(p,c)==[]


@pytest.mark.parametrize('fault',['write','get','dataset','table','principal','project','role','conditional','unknown'])
def test_unrelated_grants_fail_closed(fault):
    c,p,r,b=plan();a=b['change']['after'];role=r['change']['after']
    if fault=='write':role['permissions'].append('bigquery.tables.updateData')
    if fault=='get':role['permissions'].append('bigquery.datasets.get')
    if fault=='dataset':a['dataset_id']='ozon_raw'
    if fault=='table':a['table_id']='TENANT_STATE_EVENTS'
    if fault=='principal':a['member']='serviceAccount:sa-tenant-control@'+c['project_id']+'.iam.gserviceaccount.com'
    if fault=='project':a['project']='mpa-t-other'
    if fault=='role':a['role']='roles/bigquery.dataViewer'
    if fault=='conditional':a['condition']=[{'expression':'true'}]
    if fault=='unknown':b['change']['after_unknown']={'table_id':True}
    assert S.scan_plan(p,c)
