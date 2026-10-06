import copy
import pytest
from tools.tenancy import orchestration_identity as I


def test_query_authority_and_journal_mutation_are_separate_principals():
    rows=I.validate_matrix(I.matrix('client_001'),'client_001')
    writer=[r for r in rows if 'sa-backfill-append@' in r['principal']]
    assert len(writer)==4
    assert sorted(r['permissions'] for r in writer)==[['bigquery.tables.create'], *([['bigquery.tables.updateData']]*3)]
    assert not any('bigquery.jobs.create' in r['permissions'] for r in writer)
    reader=[r for r in rows if 'sa-backfill-controller@' in r['principal']]
    assert not any('bigquery.tables.updateData' in r['permissions'] or 'bigquery.tables.create' in r['permissions'] for r in reader)
    assert all('secretmanager.versions.access' not in r['permissions'] for r in rows)
    assert all('run.jobs.update' not in r['permissions'] for r in rows)


def test_only_exact_runtime_jobs_and_controller_are_executable():
    rows=I.matrix('client_001')
    invokes=[r for r in rows if 'run.jobs.run' in r['permissions']]
    names={r['resource'].rsplit('/',1)[-1] for r in invokes}
    assert names=={'ozon-runtime-daily','ozon-runtime-fast','ozon-runtime-weekly','tenant-backfill-controller'}
    assert not any('tenant-control' in r['resource'] for r in invokes)


def test_delegate_cannot_sign_or_impersonate_marketplace_runtime_or_control():
    grants=[r for r in I.matrix('client_001') if 'iam.serviceAccounts.getAccessToken' in r['permissions']]
    assert len(grants)==1 and grants[0]['permissions']==['iam.serviceAccounts.getAccessToken']
    assert grants[0]['resource'].endswith('/serviceAccounts/sa-backfill-append@mpa-t-client-001.iam.gserviceaccount.com')


@pytest.mark.parametrize('change',['extra','foreign','writer_query','old_runtime'])
def test_any_matrix_broadening_or_foreign_resource_is_rejected(change):
    rows=copy.deepcopy(I.matrix('client_001'))
    if change=='extra':rows[0]['permissions'].append('bigquery.tables.delete')
    if change=='foreign':rows[0]['resource']=rows[0]['resource'].replace('client-001','client-002')
    if change=='writer_query':rows[-3]['permissions'].append('bigquery.jobs.create')
    if change=='old_runtime':rows[0]['principal']='sa-ozon-runtime@mpa-t-client-001.iam.gserviceaccount.com'
    with pytest.raises(ValueError):I.validate_matrix(rows,'client_001')


def test_generic_tenant_identity_derivation_not_hardcoded_client001(monkeypatch):
    from tools.tenancy import registry as R
    c=copy.deepcopy(R.terraform_inputs('client_001'))
    c.update(tenant_id='client_002',project_id='mpa-t-client-002')
    monkeypatch.setattr(R,'terraform_inputs',lambda tenant:c)
    rows=I.validate_matrix(I.matrix('client_002'),'client_002')
    assert all('mpa-t-client-001' not in str(r) for r in rows)


def test_terraform_permissions_file_is_generated_canonical_projection():
    import json
    from tools.tenancy import registry as R
    got=json.loads((R.REPO/'infra/tenant/backfill-permissions.json').read_text())
    assert got=={k:list(v) for k,v in I.ROLE_PERMISSIONS.items()}
