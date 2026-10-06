#!/usr/bin/env python3
"""Owner publisher for canonical qualification plans/deployment descriptors.

freeze is offline. publish appends/creates immutable tenant control evidence;
never runs jobs, source APIs, lifecycle, IAM or SQL DML. Exact registered
controller release/root are required before publication. Local files are inputs,
not an authoritative resume store.
"""
import argparse
import json
import sys
from datetime import datetime,timezone
from pathlib import Path
from tools.tenancy import tenant_backfill as BF, durable_plan as D
from tools.tenancy import orchestration_contract as O, cloud_controller as C
from tools.tenancy.validation import parse_tenant_json


def freeze(tenant,plans,created_at):
    c=BF.target(tenant)
    if any(p['runtime_plan']['entity'] not in {'ads_sku_daily','supplies'} for p in plans):
        raise BF.B.EvidenceError('qualification-only frozen SKU/Supplies scope')
    from tools.tenancy import platform as PL
    releases=[parse_tenant_json(f.read_text()) for f in (BF.REPO/PL.RUNTIME_RELEASES_DIR/'ozon').glob('*.json')]
    matches=[r for r in releases if r.get('image')==c['marketplaces']['ozon']['runtime_image']]
    if len(matches)!=1:raise BF.B.EvidenceError('runtime release provenance ambiguous')
    return D.root_manifest(tenant,matches[0]['source']['commit'],matches[0]['image'],created_at,'QUALIFICATION',plans)


def verify_retained_plan(c,doc,read=None):
    """Read-only latest source/checkpoint/ACK join, before publication writes."""
    read=read or BF.select
    p=doc['runtime_plan'];params={'pid':('STRING',p['plan_id']),'origin':('TIMESTAMP',p['origin']),'entity':('STRING',p['entity'])}
    journal=f"{c['project_id']}.ozon_raw.OZON_INGESTION_RUNS"
    rows=read(c,f"SELECT MAX(backfill_sequence) AS seq FROM `{journal}` WHERE started_at>=@origin AND backfill_plan_id=@pid AND entity=@entity AND status='OK'",params)
    if len(rows)!=1 or rows[0]['seq'] is None:
        raise BF.B.EvidenceError('retained qualification progress unproven')
    params['seq']=('INT64',rows[0]['seq'])
    rows=read(c,f"SELECT DISTINCT evidence_json FROM `{journal}` WHERE started_at>=@origin AND backfill_plan_id=@pid AND entity=@entity AND status='OK' AND backfill_sequence=@seq LIMIT 2",params)
    if len(rows)!=1:raise BF.B.EvidenceError('retained qualification checkpoint ambiguous')
    proof=parse_tenant_json(rows[0]['evidence_json'])
    if proof.get('plan')!=p:raise BF.B.EvidenceError('retained plan differs from frozen qualification')
    BF.B.validate(p,proof['state'])
    if proof['state']['sequence']!=params['seq'][1]:
        raise BF.B.EvidenceError('retained source sequence/proof differs')
    # Earlier owner-authorized execution budgets can have different ACK hashes
    # while preserving the same immutable runtime plan. Bind publication to the
    # latest source sequence and its reconciled proof; never erase that history.
    saved=read(c,f"SELECT DISTINCT plan_hash,evidence_json FROM `{c['project_id']}.tenant_ops.BACKFILL_CHECKPOINTS` WHERE entity=@entity AND JSON_VALUE(evidence_json,'$.proof.plan.plan_id')=@pid AND SAFE_CAST(JSON_VALUE(evidence_json,'$.proof.state.sequence') AS INT64)=@seq LIMIT 2",{'entity':params['entity'],'pid':params['pid'],'seq':params['seq']})
    if len(saved)!=1 or saved[0]['plan_hash']!=doc['ack_hash']:
        raise BF.B.EvidenceError('retained owner ACK/plan reconstruction differs')
    checkpoint=parse_tenant_json(saved[0]['evidence_json']).get('proof')
    if not isinstance(checkpoint,dict) or checkpoint.get('plan')!=p or checkpoint.get('state')!=proof['state']:
        raise BF.B.EvidenceError('retained latest checkpoint/source proof differs')


def publish(manifest):
    c=D.validate_manifest(manifest);profile=BF.R.load_tenant(manifest['tenant']).get('historical_orchestration')
    if not profile or profile['root_hash']!=manifest['hash']:
        raise BF.B.EvidenceError('exact canonical registry opt-in/root required before publication')
    from tools.tenancy import orchestration_plan as P
    block=P.verified(c)
    if block is None:raise BF.B.EvidenceError('qualified controller release unavailable')
    # Prove every retained scope before creating any deployment/commit marker.
    for doc in manifest['plans']:
        verify_retained_plan(c,doc)
    release=parse_tenant_json((BF.REPO/'infra/tenant/releases/backfill'/f"{profile['release']}.json").read_text())
    descriptor={'settings':profile,'release':release}
    name=C.descriptor_name(profile['release'],profile['root_hash'],profile['scheduler_state'])
    tables=BF.TT.Tables(c['project_id']);locks=c['datasets']['tenant_locks']
    labels={'root':manifest['hash'][:16],'kind':'deployment_spec'};description=D.encoded(descriptor)
    if len(description.encode())>16384:raise BF.B.EvidenceError('descriptor metadata size guard')
    existing=tables.get_table(locks,name)
    if existing is None:
        if not tables.create_marker(locks,name,labels,description):existing=tables.get_table(locks,name)
        else:existing=(labels,description)
    if existing!=(labels,description):raise BF.B.EvidenceError('immutable deployment descriptor conflict')
    store=D.DurableRecords(c,tables,BF.select,
        lambda row:tables.append(c['datasets']['tenant_ops'],'BACKFILL_CHECKPOINTS',[row]))
    record=store.commit(manifest['hash'],'MANIFEST',0,manifest,datetime.now(timezone.utc))
    store.read(manifest['hash'],record)
    return {'root_hash':manifest['hash'],'manifest_record_hash':record,'deployment_spec':name,'job_execution':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('freeze');p.add_argument('--tenant',required=True);p.add_argument('--plan',action='append',required=True);p.add_argument('--created-at',required=True)
    p=sub.add_parser('publish');p.add_argument('--manifest',required=True);p.add_argument('--ack-hash',required=True)
    args=parser.parse_args()
    if args.action=='freeze':
        out=freeze(args.tenant,[parse_tenant_json(Path(p).read_text()) for p in args.plan],args.created_at)
    else:
        manifest=parse_tenant_json(Path(args.manifest).read_text())
        if args.ack_hash!=manifest.get('hash'):raise BF.B.EvidenceError('exact immutable root ACK required')
        out=publish(manifest)
    print(json.dumps(out,sort_keys=True));return 0


if __name__=='__main__':sys.exit(main())
