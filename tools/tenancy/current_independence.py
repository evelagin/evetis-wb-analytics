"""S4 (2026-10-23): daily CURRENT keeps loading while FULL_HISTORY is stopped, only on an exact proof.

A historical STOP no longer ends every wake when, and only when, all of these hold (checked on every wake;
nothing here replaces the identity, binding, deployment-authority, claim or dispatch preflight checks, which
run unchanged on the CURRENT path):

  * FULL-local origin: every unresolved STOP has a closed FULL-leaf payload class — the FULL leaf
    shard STOP written by `leaf_wake`, or a typed leaf-reconciliation diagnostic whose context names a
    shard of this frozen manifest, without an unknown write in its ledger. An untyped STOP, an unknown
    write, identity/binding/lease/checkpoint/quota/dispatch/closure/manifest stages are GLOBAL;
  * no unknown source POST and no open shared authority: the newest pilot lease generation known to the
    ledger or the lock tables was released by its exact owner (LD marker owner == the ledger run of that
    generation), or it is held by the receipted run of one CURRENT shard that this wake drains first;
    no historical shard holds an elected intent without its reconciliation; no operator hold;
  * no parallel writer: every ordinary Ozon Scheduler is PAUSED (an active runtime execution is refused
    again at dispatch by the existing preflight).

Expiry of a lease, elapsed time or the absence of an execution are never admission evidence.
"""
from tools.tenancy import durable_plan as D,tenant_backfill as BF,full_history as F
from tools.tenancy import tenant_lifecycle as TL

LOCAL_STAGES=frozenset({'RUNTIME_TERMINAL','PERSISTED_SOURCE_RECONCILIATION','RECONCILIATION_APPEND'})
CONTEXT=frozenset({'index','shard','sequence','intent_hash','receipt_hash'})
TICK='tenant-backfill-tick'


def classify(stop,manifest,plans):
    """('LOCAL', index) for an exact FULL-leaf STOP class, else ('GLOBAL', reason)."""
    p=stop.get('payload')
    if stop.get('kind')!='STOPPED' or stop.get('root_hash')!=manifest['hash'] or not isinstance(p,dict):return 'GLOBAL','foreign or malformed STOP'
    n=len(manifest['programs'])
    if p.get('reason')=='FULL_DISPATCH_OR_REPORT_EVIDENCE_UNPROVEN' and set(p)=={'reason','index'}:
        if type(p['index']) is int and 0<=p['index']<n and p['index'] in plans:return 'LOCAL',p['index']
        return 'GLOBAL','leaf STOP outside the frozen manifest'
    if p.get('reason')!='CONTROLLER_GATE_OR_EVIDENCE_FAILURE' or set(p)!={'reason','controller_execution','diagnostic'}:
        return 'GLOBAL','untyped or non-leaf controller STOP'
    d=p['diagnostic']
    if not isinstance(d,dict) or d.get('version')!=1 or d.get('stage') not in LOCAL_STAGES:return 'GLOBAL','shared-stage diagnostic'
    if 'readback' in d and (not isinstance(d['readback'],dict) or any(v!='ABSENT' for v in d['readback'].values())):return 'GLOBAL','write with unproven outcome'
    c=d.get('context')
    if not isinstance(c,dict) or set(c)!=CONTEXT or type(c['index']) is not int or not 0<=c['index']<n or c['index'] not in plans:return 'GLOBAL','diagnostic without a FULL leaf context'
    if c['shard']!=F.shard_root(manifest,c['index'],plans[c['index']]['plan']):return 'GLOBAL','diagnostic shard is not a FULL leaf of this manifest'
    return 'LOCAL',c['index']


def ledger(b):
    """Pilot lease ledger exactly as the dispatch preflight reads it (and the operator hold)."""
    _chain,_decisions,rows,_ops,hold=TL.read_state(b.c,b.tables)
    return rows,hold


def lease_state(b,rows):
    """('RELEASED', G) | ('HELD', G, owner) | ('UNPROVEN', reason) for the newest pilot lease generation."""
    from tools.tenancy.validation import parse_tenant_json as parse
    cid=BF.B.digest(['BOUNDED_PILOT_EXCLUSIVE',b.c['project_id']])[:16];locks=b.c['datasets']['tenant_locks']
    runs={}
    for r in rows:
        if parse(r.get('evidence_json') or '{}').get('mode')!='BOUNDED_PILOT':continue
        g=r.get('lease_generation')
        if type(g) is not int or g<1:return 'UNPROVEN','ledger generation malformed'
        runs.setdefault(g,set()).add(r.get('run_id'))
    tables={}
    for n,labels,*_ in b.tables.list_tables(locks):
        m=BF.CK.LEASE_RE.match(n)
        if m and m.group(1)==cid:tables[int(m.group(2))]=labels
    g=max([*runs,*tables],default=0)
    if not g:return 'UNPROVEN','no pilot lease evidence'
    owners=runs.get(g,set())
    if g in tables:owners=owners|{tables[g].get('owner')}
    if len(owners)!=1 or not isinstance(next(iter(owners)),str):return 'UNPROVEN','lease owner ambiguous'
    owner=next(iter(owners))
    done=b.tables.get_table(locks,f'LD_{cid}_{g:04d}')
    if done is None:return 'HELD',g,owner
    if done[0].get('owner')!=owner:return 'UNPROVEN','release marker owner differs'
    return 'RELEASED',g


def current_receipt(b,run_id,generation):
    """True when the held lease belongs to the receipted, not yet reconciled run of exactly one CURRENT shard."""
    from datetime import date
    from tools.tenancy import current_controller as CURRENT
    p=CURRENT.policy(b)
    if p is None:return False
    found=0
    for text in CURRENT.cycles(b,p):
        _,items=CURRENT.plans(b,p,date.fromisoformat(text))
        for item in items.values():
            hs=b.store.history(item['shard'])
            intents=[r['sequence'] for r in hs if r['kind']=='DISPATCH_INTENT']
            if not intents:continue
            last=max(intents)
            receipt=[r for r in hs if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==last]
            if any(r['kind']=='RECONCILED' and r['sequence']==last for r in hs) or len(receipt)!=1:continue
            rc=receipt[0]['payload']['receipt']
            if rc.get('run_id')==run_id and rc.get('lease_generation')==generation:found+=1
    return found==1


def ordinary_schedulers_paused(b):
    base,_=BF.resources(b.c)
    s=b.request('GET',BF.SCHED_API+'/'+base+'/jobs?pageSize=100')
    jobs=s.get('jobs')
    if s.get('nextPageToken') or not isinstance(jobs,list):return False
    names={j.get('name','').rsplit('/',1)[-1]:j.get('state') for j in jobs}
    return TICK in names and all(state=='PAUSED' for n,state in names.items() if n!=TICK)


def admit(b,manifest,records,unresolved,plans,done,quarantined):
    """{'admitted': True, ...facts} or {'admitted': False, 'blocker': reason}; never raises a historical error away."""
    from tools.tenancy import recent_priority as RP,current_controller as CURRENT
    local=[]
    for stop in unresolved:
        cls,detail=classify(stop,manifest,plans)
        if cls!='LOCAL':return dict(admitted=False,blocker='GLOBAL_STOP: '+detail)
        local.append(detail)
    if RP.pending_receipts(b,CURRENT.historical_candidates(records,plans,done,quarantined)):
        return dict(admitted=False,blocker='HISTORICAL_AUTHORITY_OPEN')
    rows,hold=ledger(b)
    if hold:return dict(admitted=False,blocker='OPERATOR_HOLD')
    state=lease_state(b,rows)
    if state[0]=='UNPROVEN':return dict(admitted=False,blocker='LEASE_'+state[1])
    if state[0]=='HELD' and not current_receipt(b,state[2],state[1]):return dict(admitted=False,blocker='LEASE_HELD_WITHOUT_CURRENT_RECEIPT')
    if not ordinary_schedulers_paused(b):return dict(admitted=False,blocker='ORDINARY_SCHEDULER_NOT_PAUSED')
    return dict(admitted=True,local_full_leaves=sorted(set(local)),unresolved=len(unresolved),lease=state[0])
