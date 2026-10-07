"""Exact generic runtime dependency read; not a controller/provisioner grant."""
ROLE = 'runtimeCapabilityRead'
PERMISSIONS = ('bigquery.tables.getData',)


def matrix(c):
    o = c['marketplaces'].get('ozon')
    if not o:
        return []
    p = c['project_id']
    return [{'principal': f"{o['service_accounts']['runtime']}@{p}.iam.gserviceaccount.com",
             'role': f'projects/{p}/roles/{ROLE}',
             'resource': f"projects/{p}/datasets/{c['datasets']['tenant_ops']}/tables/CAPABILITY_PROFILE",
             'permissions': list(PERMISSIONS)}]
