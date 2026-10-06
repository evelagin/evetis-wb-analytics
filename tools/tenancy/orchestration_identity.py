"""Proposed least-privilege cloud orchestration boundary; generates no mutations.

The query reader cannot write tenant_ops. The append writer cannot create query
jobs, change schemas, write RAW/ref, invoke jobs or read secrets. Delegation is
only getAccessToken on this dedicated writer, never the existing tenant-control.
Cloud activation also requires tested adapters, CI, image qualification and a
reviewed tenant-only Terraform plan; this matrix is not deployed-state evidence.
"""
from tools.tenancy import registry as R

ROLE_PERMISSIONS = {
    'backfillRead': ('bigquery.datasets.get','bigquery.tables.get','bigquery.tables.getData'),
    'backfillReadRef': ('bigquery.tables.get','bigquery.tables.getData','bigquery.tables.list'),
    'backfillLockRead': ('bigquery.datasets.get','bigquery.tables.get','bigquery.tables.list'),
    'backfillLockCreate': ('bigquery.tables.create',),
    'backfillQuery': ('bigquery.jobs.create',),
    'backfillAppend': ('bigquery.tables.updateData',),
    'backfillDelegateAppend': ('iam.serviceAccounts.getAccessToken',),
    'backfillRuntimeExecute': ('run.jobs.get','run.jobs.run','run.jobs.runWithOverrides','run.executions.get','run.executions.list'),
    'backfillInventoryRead': ('run.jobs.list','run.operations.get','cloudscheduler.jobs.list'),
    'backfillControllerRead': ('run.jobs.get','run.executions.get','run.executions.list'),
    'backfillWake': ('run.jobs.run',),
}


def matrix(tenant):
    return matrix_for_contract(R.terraform_inputs(tenant))


def matrix_for_contract(c):
    p=c['project_id'];base=f"projects/{p}/locations/{c['region']}"
    controller=f'sa-backfill-controller@{p}.iam.gserviceaccount.com'
    writer=f'sa-backfill-append@{p}.iam.gserviceaccount.com'
    wake=f'sa-backfill-wake@{p}.iam.gserviceaccount.com'
    role=lambda x:f'projects/{p}/roles/{x}'
    rows=[]
    def add(principal,r,resource,permissions):
        rows.append({'principal':principal,'role':r,'resource':resource,'permissions':list(permissions)})
    for key in ('ozon_raw','ref','tenant_ops'):
        r = 'backfillReadRef' if key=='ref' else 'backfillRead'
        add(controller,role(r),f"projects/{p}/datasets/{c['datasets'][key]}",ROLE_PERMISSIONS[r])
    for principal,r in ((controller,'backfillLockRead'),(writer,'backfillLockCreate')):
        add(principal,role(r),f"projects/{p}/datasets/{c['datasets']['tenant_locks']}",ROLE_PERMISSIONS[r])
    for r in ('backfillQuery','backfillInventoryRead'):
        add(controller,role(r),f'projects/{p}',ROLE_PERMISSIONS[r])
    for name in c['marketplaces']['ozon']['jobs']:
        add(controller,role('backfillRuntimeExecute'),base+'/jobs/'+name,ROLE_PERMISSIONS['backfillRuntimeExecute'])
    for table in ('BACKFILL_CHECKPOINTS','DATA_COVERAGE','DQ_RESULTS'):
        add(writer,role('backfillAppend'),f"projects/{p}/datasets/{c['datasets']['tenant_ops']}/tables/{table}",ROLE_PERMISSIONS['backfillAppend'])
    add(controller,role('backfillDelegateAppend'),f'projects/{p}/serviceAccounts/{writer}',ROLE_PERMISSIONS['backfillDelegateAppend'])
    add(controller,role('backfillControllerRead'),base+'/jobs/tenant-backfill-controller',ROLE_PERMISSIONS['backfillControllerRead'])
    add(controller,role('backfillControllerRead'),base+'/jobs/tenant-control',ROLE_PERMISSIONS['backfillControllerRead'])
    add(wake,role('backfillWake'),base+'/jobs/tenant-backfill-controller',ROLE_PERMISSIONS['backfillWake'])
    return rows


def validate_matrix(rows,tenant):
    if rows!=matrix(tenant):
        raise ValueError('orchestration permission/resource matrix drift')
    # Unlike a standard TokenCreator role, the delegate has no signing or
    # implicit delegation capability. The existing credential-bearing SAs are
    # never delegated to, or assigned additional permissions.
    from tools.tenancy import control_identity as CI
    p=R.terraform_inputs(tenant)['project_id']
    old=(CI.control_email(p),f'sa-ozon-runtime@{p}.iam.gserviceaccount.com')
    for row in rows:
        if row['principal'] in old or ('serviceAccounts/' in row['resource'] and any(v in row['resource'] for v in old)):
            raise ValueError('unrelated credential-bearing identity expansion')
    return rows
