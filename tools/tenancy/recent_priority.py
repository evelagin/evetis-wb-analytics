"""Owner-only priority over unchanged FULL leaves; observation is never GO.

No source transport, RAW copy, alternate checkpoint, quota override or business
mapping. Progress is reconstructed from canonical evidence and logged durably by
the existing controller; this view never appends ingestion state.
"""
from datetime import date, timedelta
from statistics import median
import re
from tools.tenancy import durable_plan as D, full_history as F, tenant_backfill as BF
from tools.tenancy.validation import parse_tenant_json as parse

VERSION = 1
TYPE = 'FULL_RECENT_PRIORITY'
RANK = {'catalog':0,'seller_info':1,'clusters':2,'prices':3,'stocks':4,
        'supplies':5,'ads_campaigns':6,'fbo_postings':7,'finance_accrual':8,
        'ads_expense_daily':9,'ads_sku_daily':10}
FIELDS = frozenset({'version','type','tenant','project','root','manifest_hash',
    'since','until','days','design_hash','owner_ack_sha256','controller_source',
    'controller_image','controller_implementation_hash','runtime_source',
    'runtime_image','runtime_implementation_hash','selection','parents',
    'historical_recovery_policies','activated_at','hash'})


def fail(message):raise BF.B.EvidenceError('recent priority: '+message)
def sealed(p):return dict(p,hash=D.digest(p))
def name(root,source):return 'BFRP_AUTH_'+D.check_hash(root)+'_'+source
def value(p):return {'kind':'recent_priority','root':p['root'][:16]},D.encoded(p)


def selection(manifest,days=180):
    end=date.fromisoformat(manifest['cutover']);start=end-timedelta(days=days-1)
    indices=[i for i,p in enumerate(manifest['programs'])
             if p['kind']=='DATED' and p['from']<=str(end) and p['to']>=str(start)]
    indices.sort(key=lambda i:(-date.fromisoformat(manifest['programs'][i]['to']).toordinal(),
                              RANK[manifest['programs'][i]['entity']],i))
    return indices


def make(manifest,release,*,design_hash,owner_ack_sha256,historical_recovery_policies,activated_at):
    indices=selection(manifest)
    return sealed(dict(version=VERSION,type=TYPE,tenant=manifest['tenant'],project=manifest['project'],
        root=manifest['hash'],manifest_hash=D.digest(manifest),days=180,
        since=str(date.fromisoformat(manifest['cutover'])-timedelta(days=179)),until=manifest['cutover'],
        design_hash=design_hash,owner_ack_sha256=owner_ack_sha256,
        controller_source=release['source_sha'],controller_image=release['image'],
        controller_implementation_hash=release['controller_implementation_hash'],
        runtime_source=manifest['runtime_source_sha'],runtime_image=manifest['runtime_image'],
        runtime_implementation_hash=manifest['runtime_implementation_hash'],
        selection=[dict(index=i,program_hash=D.digest(manifest['programs'][i])) for i in indices],
        parents=sorted({manifest['programs'][i]['t5_chunk_id'] for i in indices}),
        historical_recovery_policies=historical_recovery_policies,activated_at=activated_at))


def validate(p,manifest,release):
    F.validate_manifest(manifest)
    if not isinstance(p,dict) or set(p)!=FIELDS or type(p['version']) is not int or p['version']!=VERSION or p['type']!=TYPE:fail('closed owner authority required')
    if p['hash']!=D.digest({k:v for k,v in p.items() if k!='hash'}):fail('authority digest differs')
    if (p['tenant'],p['project'],p['root'],p['manifest_hash'])!=(manifest['tenant'],manifest['project'],manifest['hash'],D.digest(manifest)):fail('foreign root or manifest')
    if (p['tenant'],p['project'])!=('client_001','mpa-t-client-001'):fail('unsupported tenant')
    if type(p['days']) is not int or p['days']!=180 or p['until']!=manifest['cutover'] or p['since']!=str(date.fromisoformat(p['until'])-timedelta(days=179)):fail('frozen cutoff differs')
    indices=selection(manifest)
    if p['selection']!=[dict(index=i,program_hash=D.digest(manifest['programs'][i])) for i in indices] or p['parents']!=sorted({manifest['programs'][i]['t5_chunk_id'] for i in indices}):fail('whole canonical leaf selection differs')
    for key in ('design_hash','owner_ack_sha256','hash'):D.check_hash(p[key])
    for key in ('runtime_source','runtime_image','runtime_implementation_hash'):
        if p[key]!=manifest[key.replace('runtime_source','runtime_source_sha')]:fail('runtime changed')
    if (p['controller_source'],p['controller_image'],p['controller_implementation_hash'])!=(release['source_sha'],release['image'],release['controller_implementation_hash']):fail('controller release differs')
    if release.get('schema_version') not in {3,4} or release.get('verification',{}).get('recent_priority_adapter')!='PASS':fail('priority release unqualified')
    F.stamp(p['activated_at'])
    refs=p['historical_recovery_policies']
    if not isinstance(refs,list) or not 1<=len(refs)<=8 or len({r.get('source') for r in refs})!=len(refs):fail('historical authority chain ambiguous')
    for r in refs:
        if set(r)!={'source','policy_hash'} or not re.fullmatch('[0-9a-f]{40}',r['source']) or r['source']==p['controller_source']:fail('historical release scope differs')
        D.check_hash(r['policy_hash'])
    return p


def load(backend,manifest,release=None):
    from tools.tenancy import cloud_controller as C
    env=backend.c['orchestration']['job']['env'];source=env['CONTROLLER_SOURCE_SHA']
    if release is None:
        registered=BF.REPO/'infra/tenant/releases/backfill'/f'{source}.json'
        if registered.exists() and parse(registered.read_text())['schema_version']<3:return None
        marker=backend.tables.get_table(backend.c['datasets']['tenant_locks'],C.descriptor_name(source,manifest['hash'],env['HISTORICAL_SCHEDULER_STATE']))
        if not marker:fail('deployment descriptor absent')
        release=parse(marker[1])['release']
    marker=backend.tables.get_table(backend.c['datasets']['ref'],name(manifest['hash'],source))
    if marker is None:
        if release['schema_version']>=3:fail('owner priority authority absent')
        return None
    p=parse(marker[1]);validate(p,manifest,release)
    if marker!=value(p):fail('owner authority metadata differs')
    return p


def publish(backend,p,manifest,release):
    """Explicit owner-only ref marker; no source or ingestion-state write."""
    validate(p,manifest,release)
    if (backend.c['tenant_id'],backend.c['project_id'])!=(p['tenant'],p['project']):fail('publisher target differs')
    n=name(p['root'],p['controller_source']);old=backend.tables.get_table(backend.c['datasets']['ref'],n)
    if old is None:backend.tables.create_marker(backend.c['datasets']['ref'],n,*value(p))
    if backend.tables.get_table(backend.c['datasets']['ref'],n)!=value(p):fail('authority CAS/readback conflict')
    return n


def historical_policy(backend,manifest,active,source):
    """Validate retained certificates against their original owner authority.

    Only exact owner-pinned predecessor artifacts, already committed proofs and
    identical initial incident/runtime are eligible. Future observation/apply
    still uses the active policy and every original live predicate.
    """
    from tools.tenancy import full_leaf_recovery as FL, orchestration_contract as O
    p=load(backend,manifest)
    refs=[r for r in p['historical_recovery_policies'] if r['source']==source] if p else []
    if len(refs)!=1:fail('historical recovery authority not pinned')
    marker=backend.tables.get_table(backend.c['datasets']['ref'],FL.policy_name(manifest['hash'],source))
    old=parse(marker[1]) if marker else {}
    release=parse((BF.REPO/'infra/tenant/releases/backfill'/f'{source}.json').read_text())
    # block checks registered immutable release gates; an old artifact must not
    # pretend to match the executing new source implementation hash.
    O.block(backend.c,dict(release=source,root_hash=manifest['hash'],scheduler_state='PAUSED'),BF.REPO,release)
    FL.validate_policy(old,manifest,release,historical=True)
    if old['hash']!=refs[0]['policy_hash'] or marker!=FL.policy_value(old):fail('historical policy changed')
    keys=('initial_stop','initial_receipt','initial_intent','initial_generation','initial_unit_start','initial_unit_end','automatic_class_c')
    if any(old[k]!=active[k] for k in keys):fail('class-C incident/authority broadened')
    return old


def ordered(manifest,done,p):
    pending=[i for i in range(len(manifest['programs'])) if i not in done]
    old=sorted(pending,key=lambda i:(-1 if manifest['programs'][i].get('accepted_qualification_plan') else RANK[manifest['programs'][i]['entity']],i))
    if p is None:return old
    recent=[x['index'] for x in p['selection'] if x['index'] not in done]
    # Whole boundary siblings close the existing canonical parent, never become
    # invented selected leaves or a false parent completion.
    closure=[i for i in pending if manifest['programs'][i]['t5_chunk_id'] in p['parents'] and i not in recent]
    closure.sort(key=lambda i:(-date.fromisoformat(manifest['programs'][i]['to']).toordinal(),RANK[manifest['programs'][i]['entity']],i))
    front=recent+closure
    return front+[i for i in old if i not in front]


def pending_receipts(backend,plans):
    """Consistent election/commit inventory, never a streaming-row MAX shortcut.

    Only a shard with unfinished elected work needs its full history read. This
    avoids reloading every completed shard on every priority wake. A fence with
    no committed RECON remains pending and cannot permit queue reordering.
    """
    locks=backend.c['datasets']['tenant_locks']
    metadata=backend.store.metadata
    names={n for n,*_ in metadata.list_tables(locks)}
    by_shard={item['shard_root']:i for i,item in plans.items()}
    latest={}
    for n in names:
        match=re.fullmatch(r'BFQ_([0-9a-f]{64})_([0-9]{10})_(DISPATCH_INTENT|DISPATCH_RECEIPT|RECONCILED)',n)
        if not match or match[1] not in by_shard:continue
        shard,seq,kind=match[1],int(match[2]),match[3]
        latest[(shard,kind)]=max(seq,latest.get((shard,kind),0))
    pending=[]
    for shard,index in by_shard.items():
        intent=latest.get((shard,'DISPATCH_INTENT'),0)
        receipt=latest.get((shard,'DISPATCH_RECEIPT'),0)
        recon=latest.get((shard,'RECONCILED'),0)
        if receipt>intent or recon>intent:fail('receipt fence sequence exceeds elected intent')
        if not intent:continue
        elected=metadata.get_table(locks,f'BFQ_{shard}_{intent:010d}_DISPATCH_INTENT')
        if not elected or elected[0]!={'root':shard[:16],'kind':'dispatch_intent'}:fail('elected intent fence metadata differs')
        election=parse(elected[1])
        if set(election)!={'record_hash'}:fail('elected intent fence schema differs')
        h=D.check_hash(election['record_hash'])
        commit=metadata.get_table(locks,'BFR_'+h) if 'BFR_'+h in names else None
        expected=({'root':shard[:16],'kind':'dispatch_intent'},D.encoded(dict(root_hash=shard,record_hash=h,kind='DISPATCH_INTENT',sequence=intent)))
        if commit!=expected:fail('elected intent uncommitted; no reordering')
        accepted=0
        if recon:
            marker=metadata.get_table(locks,f'BFQ_{shard}_{recon:010d}_RECONCILED')
            labels={'root':shard[:16],'kind':'reconciled'}
            if not marker or marker[0]!=labels:fail('reconciliation fence metadata differs')
            desc=parse(marker[1])
            if set(desc)!={'record_hash'}:fail('reconciliation fence schema differs')
            h=desc['record_hash'];D.check_hash(h)
            commit=metadata.get_table(locks,'BFR_'+h) if 'BFR_'+h in names else None
            expected=(labels,D.encoded(dict(root_hash=shard,record_hash=h,kind='RECONCILED',sequence=recon)))
            if commit is not None:
                if commit!=expected:fail('reconciliation commit metadata differs')
                accepted=recon
        if intent>accepted:
            proofs=[p for p in getattr(backend,'verified_cooldown_failures',[]) if p['shard']==shard and p['receipt_sequence']==intent]
            if proofs:
                from tools.tenancy import full_cooldown_recovery as FC
                proof=proofs[0]
                sh=backend.store.history(shard,max_records=F.MAX_LEAF_RECORDS)
                if len(proofs)!=1 or not any(r['kind']==FC.SHARD_KIND and r['sequence']==intent and D.digest(r)==proof['closure_hash'] for r in sh):fail('failed cooldown closure uncommitted')
            else:pending.append((index,plans[index]))
    return pending


def throughput(manifest,done,p,now,started_at,wait_samples=()):
    """Rolling observed wall throughput; starts are committed intent timestamps.

    Includes source/reconciliation/wait time in wall throughput. WAIT share is
    separately supplied from read-only controller logs, never assumed zero.
    Estimates are conditional on the observed mix; they are not quota promises.
    """
    selected={x['index'] for x in p['selection']}
    fresh=[(i,x) for i,x in done.items() if i in selected and not x.get('reused_qualification')
           and F.stamp(x['completed_at'])>=F.stamp(p['activated_at'])]
    if len(fresh)<5:return dict(status='WAITING_FOR_5_POST_ACTIVATION_RECENT_LEAVES',completed=len(fresh),quota_wait_share='UNPROVEN')
    fresh.sort(key=lambda item:item[1]['completed_at']);fresh=fresh[-20:]
    if any(i not in started_at for i,_ in fresh):return dict(status='INTENT_TIMES_UNPROVEN',completed=len(fresh),quota_wait_share='UNPROVEN')
    durations=[]
    for i,proof in fresh:
        seconds=(F.stamp(proof['completed_at'])-F.stamp(started_at[i])).total_seconds()
        if seconds<=0 or F.stamp(proof['completed_at'])>now:fail('throughput timestamp order differs')
        durations.append(seconds)
    start=max(F.stamp(p['activated_at']),min(F.stamp(started_at[i]) for i,_ in fresh))
    elapsed=(now-start).total_seconds()
    if elapsed<=0:fail('throughput elapsed time invalid')
    rate=len(fresh)*3600/elapsed
    samples=[s for s in wait_samples if start<=F.stamp(s['at'])<=now]
    wait=sum(s['quota_wait'] is True for s in samples)/len(samples) if samples else 'UNPROVEN'
    estimates={}
    for days in (30,90,180):
        remaining=sum(i not in done for i in selection(manifest,days))
        estimates[f'RECENT_{days}']={'remaining_leaves':remaining,'estimated_hours':round(remaining/rate,2),
            'estimated_completion':(now+timedelta(hours=remaining/rate)).isoformat(),
            'boundary':'SOURCE_LEAF_ESTIMATE; PARENT_CLOSURE_DEPENDENCY_DQ_READY_NOT_PROMISED'}
    ordered=sorted(durations)
    return dict(status='OBSERVED_ROLLING_THROUGHPUT',completed=len(fresh),observed_since=start.isoformat(),
        leaves_per_hour=round(rate,3),median_leaf_completion_seconds=round(median(durations),1),
        p90_leaf_completion_seconds=round(ordered[__import__('math').ceil(.9*len(ordered))-1],1) if len(ordered)>=10 else None,
        quota_wait_share=wait,quota_wait_basis='OBSERVED_WAKE_SHARE' if samples else 'LOG_SAMPLES_REQUIRED',estimates=estimates)


def _quality(proofs):
    rows=[x for p in proofs for x in p['coverage']['readback']]
    keyed=[r for r in rows if 'rows' in r and 'keys' in r]
    dq='PASS' if proofs and keyed and all(r['rows']==r['keys'] for r in keyed) else 'UNPROVEN'
    if any(r.get('rows')!=r.get('keys') for r in keyed):dq='FAIL'
    linkage=[r['sku_linkage'] for r in rows if 'sku_linkage' in r]
    link='PASS' if linkage and all(x.get('unresolved_sku_rows')==0 for x in linkage) else 'UNPROVEN'
    if any(x.get('unresolved_sku_rows',0)>0 for x in linkage):link='FAIL'
    return dq,link


def progress(manifest,records,done,p,*,now,waiting_receipts=None,unresolved_stops=None):
    """Pure read-only view; UNKNOWN retention and partial siblings never COMPLETE."""
    plans={(r['payload']['index'],r['payload']['plan']['ack_hash']):r['payload']['plan'] for r in records if r['kind']=='CHUNK_PLAN'}
    parents=set()
    for r in records:
        if r['kind']!='T5_PARENT_COMPLETE':continue
        parent=r['payload']['parent'];siblings=[i for i,x in enumerate(manifest['programs']) if x['t5_chunk_id']==parent]
        if not siblings or any(i not in done for i in siblings) or r['payload']['leaf_proofs']!=[D.digest(done[i]) for i in siblings]:fail('false canonical parent telemetry')
        parents.add(parent)
    snapshots={}
    for i,prog in enumerate(manifest['programs']):
        if prog['kind']=='DATED':continue
        proof=done.get(i);doc=plans.get((i,proof['ack_hash'])) if proof else None
        limited=proof and any(r.get('content_unavailable_supplies',0)>0 or r.get('unfinished_bundles',0)>0
                             or r.get('source_traversal_complete') is False for r in proof['coverage']['readback'])
        snapshots[prog['entity']]={'status':'PARTIAL' if limited else 'COMPLETE' if proof else 'WAITING','observation_date':doc['runtime_plan'].get('observation_date') if doc else None,'historical':False}
    windows={};end=date.fromisoformat(manifest['cutover'])
    for days in (30,60,90,180):
        start=end-timedelta(days=days-1);indices=selection(manifest,days);selected=set(indices)
        expected_parents={manifest['programs'][i]['t5_chunk_id'] for i in indices}
        domains={};gaps={};covered=[]
        for entity in sorted(BF.B.DATED):
            relevant=[i for i in indices if manifest['programs'][i]['entity']==entity]
            observed={}
            for i in relevant:
                if i not in done:continue
                prog=manifest['programs'][i];coverage=done[i]['coverage']['readback']
                for r in coverage:
                    day=r.get('day')
                    if day and str(start)<=day<=str(end) and r.get('coverage')=='COMPLETE':observed[day]=r
                if prog.get('accepted_qualification_plan') and prog['from']==prog['to'] and str(start)<=prog['from']<=str(end):observed[prog['from']]={'accepted_qualification':True}
            first=max(start,date.fromisoformat(manifest['starts'][entity]));expected=[];cur=first
            while cur<=end:expected.append(str(cur));cur+=timedelta(days=1)
            missing=[d for d in expected if d not in observed];gaps[entity]=missing;covered.extend(observed)
            n=sum(i in done for i in relevant)
            status=('NOT_APPLICABLE' if not expected else 'BLOCKED' if unresolved_stops else
                    'COMPLETE' if not missing and n==len(relevant) else 'PARTIAL' if n or observed else 'WAITING')
            domains[entity]={'status':status,'leaves_complete':n,'leaves_required':len(relevant),'covered_days':len(observed),'required_days':len(expected),'gaps':len(missing)}
        for entity,snapshot in snapshots.items():domains[entity]=snapshot
        proofs=[done[i] for i in indices if i in done];dq,link=_quality(proofs)
        complete=sum(i in done for i in indices);pn=len(parents&expected_parents)
        supplies=next((done.get(i) for i,x in enumerate(manifest['programs']) if x['entity']=='supplies'),None)
        all_proofs=proofs+[done[i] for i,x in enumerate(manifest['programs']) if x['kind']!='DATED' and i in done]
        dq_all,_=_quality(all_proofs)
        catalog_day=str(now.astimezone(BF.B.MSK).date())
        cert=any(r['kind']=='SNAPSHOT_CERT' and r['payload'].get('day')==catalog_day for r in records)
        finance_dq='PASS' if proofs and not any(x['coverage'].get('unknown_finance_type_ids') for x in proofs) else 'UNPROVEN'
        ready=(complete==len(indices) and pn==len(expected_parents) and not any(gaps.values())
               and waiting_receipts==0 and unresolved_stops==0 and dq_all=='PASS' and link=='PASS'
               and finance_dq=='PASS' and cert and supplies is not None and all(s['status']=='COMPLETE' for s in snapshots.values()))
        status='COMPLETE' if ready else ('BLOCKED' if unresolved_stops or dq=='FAIL' or link=='FAIL' else 'PARTIAL' if complete else 'WAITING')
        latest=max(((i,done[i]) for i in indices if i in done),key=lambda item:item[1]['completed_at'],default=None)
        windows[f'RECENT_{days}']={'since':str(start),'until':str(end),'status':status,'ready':ready,
            'leaves_complete':complete,'leaves_total':len(indices),'parents_complete':pn,'parents_total':len(expected_parents),
            'leaves_percent':round(100*complete/len(indices),2) if indices else 0,
            'parents_percent':round(100*pn/len(expected_parents),2) if expected_parents else 0,
            'domains':domains,'coverage_gaps':gaps,'oldest_covered_date':min(covered,default=None),'newest_covered_date':max(covered,default=None),
            'duplicate_key_dq':dq_all,'catalog_linkage':link,'current_day_catalog_certificate':'PASS' if cert else 'UNPROVEN',
            'receipts_waiting_reconciliation':waiting_receipts,'unresolved_STOP':unresolved_stops,
            'prices_current_snapshot':snapshots.get('prices'),'stocks_current_snapshot':snapshots.get('stocks'),
            'finance_economic_finality':'PROVISIONAL','financial_pnl_finality':'UNPROVEN_CLIENT_COGS_TAX_REQUIRED',
            'finance_classification_dq':finance_dq,
            'last_successful_recent_leaf':{'index':latest[0],'entity':manifest['programs'][latest[0]]['entity'],'completed_at':latest[1]['completed_at']} if latest else None,
            'last_progress_timestamp':latest[1]['completed_at'] if latest else None}
    fresh=[(i,x) for i,x in done.items() if i in {v['index'] for v in p['selection']} and not x.get('reused_qualification')
           and x['completed_at']>=p['activated_at']] if p else []
    return {'type':TYPE,'authority_hash':p['hash'] if p else None,'priority_active':p is not None,
        'root_hash':manifest['hash'],'windows':windows,'post_activation_completed_leaves':len(fresh),
        'throughput_eta':{'status':'WAITING_FOR_5_POST_ACTIVATION_RECENT_LEAVES' if len(fresh)<5 else 'REQUIRES_OBSERVED_INTENT_TIMES_AND_WAIT_LOGS','quota_wait_share':'UNPROVEN'},
        'regular_schedulers':'PAUSED','full_history_continues':True}


def monitoring(backend,manifest,records,done,p,now):
    """Bounded read projection; no evidence append/reconciliation side effects."""
    from tools.tenancy import full_leaf_recovery as FL
    approved=FL.load(backend,manifest,records) if any(r['kind']==FL.KIND for r in records) else []
    from tools.tenancy import full_cooldown_recovery as FC
    failed=FC.load(backend,manifest,records) if any(r['kind']==FC.KIND for r in records) else []
    backend.verified_cooldown_failures=failed
    approved=approved+failed
    stops={D.digest(r) for r in records if r['kind']=='STOPPED'}-{x['stop_hash'] for x in approved}
    plans={n:r['payload'] for n,r in enumerate(records) if r['kind'] in {'CHUNK_PLAN','DEPENDENCY_PLAN'}}
    waiting=len(pending_receipts(backend,plans))
    out=progress(manifest,records,done,p,now=now,waiting_receipts=waiting,unresolved_stops=len(stops))
    if out['post_activation_completed_leaves']>=5:
        created={n:at for n,_,at in backend.store.metadata.list_tables(backend.c['datasets']['tenant_locks'])}
        starts={}
        for i,proof in done.items():
            prefix=f"BFQ_{proof['shard_root']}_"
            times=[at for n,at in created.items() if n.startswith(prefix) and n.endswith('_DISPATCH_INTENT') and at]
            if times:starts[i]=min(times).isoformat()
        out['throughput_eta']=throughput(manifest,done,p,now,starts)
    return out
