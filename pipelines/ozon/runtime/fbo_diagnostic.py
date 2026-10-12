"""Bounded owner-authorized FBO read-only session, never an ingestion execution.

No transport retries, RAW writes, source units, checkpoints or reconciliation.
The execution-specific owner grant prevents another Run invocation consuming
the same permission. Ordinary strict FBO validation remains unchanged.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

FIELDS = frozenset({'version','purpose','tenant','project','root','leaf','shard',
    'plan_id','failed_run','failed_generation','failed_receipt_hash','closure_hash',
    'run_id','generation','since','to','until_exclusive','cursor','limit',
    'request_limit','runtime_implementation_hash','controller_source','controller_image',
    'runtime_image','owner_decision_sha256','expires_at','hash'})

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def validate(p, project, implementation, now):
    if not isinstance(p,dict) or set(p)!=FIELDS or p['hash']!=digest({k:v for k,v in p.items() if k!='hash'}):
        raise ValueError('closed diagnostic owner policy required')
    import full_resume as FR,backfill_core as B
    from datetime import date,timedelta
    m=FR.manifest()
    if type(p['leaf']) is not int or not 0<=p['leaf']<len(m['programs']):raise ValueError('frozen FBO leaf required')
    program=m['programs'][p['leaf']]
    if program['entity']!='fbo_postings' or program['kind']!='DATED':raise ValueError('frozen FBO dated source required')
    a=B.utc_ms(date.fromisoformat(program['from']));b=B.utc_ms(date.fromisoformat(program['to'])+timedelta(days=1))
    if (type(p['version']) is not int or p['version']!=1 or p['purpose']!='FBO_BOUNDED_READ_NO_BUSINESS_EFFECT_V1'
        or (p['tenant'],p['project'],p['root'])!=(m['tenant'],m['project'],m['hash']) or p['project']!=project
        or type(p['failed_generation']) is not int or p['failed_generation']<1
        or type(p['generation']) is not int or p['generation']<=p['failed_generation']
        or (p['since'],p['to'],p['until_exclusive'])!=(B.stamp(a),B.stamp(b-1),B.stamp(b))
        or p['cursor']!='' or type(p['limit']) is not int or p['limit']!=100
        or type(p['request_limit']) is not int or not 1<=p['request_limit']<=12
        or p['runtime_implementation_hash']!=implementation):
        raise ValueError('diagnostic scope/budget/implementation differs')
    for k in ('root','shard','plan_id','closure_hash','failed_receipt_hash','runtime_implementation_hash','owner_decision_sha256','hash'):
        if not isinstance(p[k],str) or not re.fullmatch('[0-9a-f]{64}',p[k]):raise ValueError('diagnostic digest required')
    if not re.fullmatch(r'bf-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',p['run_id']):raise ValueError('new diagnostic run required')
    if not isinstance(p['failed_run'],str) or not re.fullmatch(r'bf-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',p['failed_run']) or p['run_id']==p['failed_run']:raise ValueError('old failed run never replayed')
    expiry=datetime.fromisoformat(p['expires_at'].replace('Z','+00:00'))
    if expiry.utcoffset() is None or not 0 < (expiry-now).total_seconds() <= 3600:raise ValueError('bounded fresh diagnostic grant required')
    return p

def time_evidence(value, a, b, *, page, request, record):
    # Retain only a bounded timestamp scalar, never an arbitrary source object.
    from backfill import validate_fbo_timestamp
    from backfill_core import FboWindowError
    import common as C
    try:
        validate_fbo_timestamp(value,a,b,page_ordinal=page,request_ordinal=request,record_ordinal=record)
        category='IN_REQUESTED_INTERVAL'
    except FboWindowError as error:category=error.fbo_window_evidence['validation_class']
    safe=C.safe_error_text(value,256) if isinstance(value,str) else None
    return {'value':safe,'value_type':type(value).__name__,'validation_class':category}

def request_once(body):
    """Reviewed Seller gateway; no transport retry or second identity call."""
    import common as C
    return C.seller_call('/v3/posting/fbo/list',body,profile='runtime',allow_retries=False)

def collect(p, send=request_once, emit=lambda page:None):
    a=int(datetime.fromisoformat(p['since'].replace('Z','+00:00')).timestamp()*1000)
    b=int(datetime.fromisoformat(p['until_exclusive'].replace('Z','+00:00')).timestamp()*1000)
    cursor=p['cursor'];seen=set();pages=[]
    for ordinal in range(1,p['request_limit']+1):
        body={'cursor':cursor,'filter':{'since':p['since'],'to':p['to']},'limit':p['limit'],
              'with':{'analytics_data':True,'financial_data':True}}
        code,result=send(body)  # One attempt per request; never transport retry.
        page={'page_ordinal':ordinal,'request_ordinal':ordinal,'http_status':code,'records':[],
              'request_cursor_sha256':digest(cursor),'since':p['since'],'to':p['to']}
        pages.append(page)
        if code!=200:
            page['validation_class']='SOURCE_HTTP_OR_TRANSPORT_FAILURE';emit(page);break
        if not isinstance(result,dict) or not isinstance(result.get('postings'),list) or len(result['postings'])>100:
            page['validation_class']='INVALID_PAGE_ENVELOPE';emit(page);break
        nxt=result.get('cursor')
        if nxt is not None and (not isinstance(nxt,str) or len(nxt.encode())>4096):
            page['validation_class']='INVALID_CURSOR';emit(page);break
        page.update(response_cursor_sha256=digest(nxt),has_next=result.get('has_next') if type(result.get('has_next')) is bool else 'UNPROVEN')
        sufficient=False
        for n,item in enumerate(result['postings'],1):
            if not isinstance(item,dict):raise ValueError('diagnostic record envelope differs')
            status=item.get('status');status=status if isinstance(status,str) and status in {'awaiting_packaging','awaiting_deliver','delivering','delivered','cancelled','in_process','returned'} else 'UNRECOGNIZED'
            created=time_evidence(item.get('created_at'),a,b,page=ordinal,request=ordinal,record=n)
            processed=time_evidence(item.get('in_process_at'),a,b,page=ordinal,request=ordinal,record=n)
            page['records'].append({'page_ordinal':ordinal,'request_ordinal':ordinal,'record_ordinal':n,
                'created_at':created,'in_process_at':processed,'status':status})
            if created['validation_class']!='IN_REQUESTED_INTERVAL':sufficient=True
        # Stop at first discriminating page, malformed pair, terminal page or
        # unknown/repeated cursor. Never read further for a larger sample.
        emit(page)
        if sufficient or page['has_next'] is not True or not nxt or nxt in seen:break
        seen.add(nxt);cursor=nxt
    return {'version':1,'pages':pages,'source_requests':len(pages),'request_limit':p['request_limit'],
        'semantics':'UNPROVEN','raw_writes':0,'source_units':0,'checkpoints':0,'receipts':0,'reconciliation':0}

def run(policy_hash, env, *, sleep=None):
    import os,time
    import common as C,backfill_core as B,main as M
    from datetime import datetime,timezone
    sleep=sleep or time.sleep
    if not re.fullmatch('[0-9a-f]{64}',policy_hash):raise ValueError('diagnostic policy hash required')
    if env.get('TENANT_BINDING_REQUIRED')!='1' or env.get('STRICT_PAGE_CAPS')!='1' or env.get('ENTITIES')!='fbo_postings':raise ValueError('strict bound diagnostic required')
    if any(k in env for k in ('BACKFILL_MODE','BACKFILL_FULL_AUTHORITY','TENANCY_INTERNAL_PREFLIGHT')):raise ValueError('diagnostic never enters ingestion authority')
    if (env.get('CLOUD_RUN_TASK_INDEX'),env.get('CLOUD_RUN_TASK_COUNT'),env.get('CLOUD_RUN_TASK_ATTEMPT'))!=('0','1','0'):raise ValueError('single task/no retry required')
    if not isinstance(env.get('CLOUD_RUN_EXECUTION'),str) or not re.fullmatch(r'ozon-runtime-fast-[a-z0-9]+',env['CLOUD_RUN_EXECUTION']):raise ValueError('canonical diagnostic runtime required')
    client=C.bq();ref=f'{C.PROJECT}.{C.REF_DATASET}'
    # The runtime already has ref metadata read; no new IAM or locks access.
    p=json.loads(client.get_table(ref+'.BFDA_'+policy_hash).description)
    validate(p,C.PROJECT,B.implementation_hash(),datetime.now(timezone.utc))
    if p['hash']!=policy_hash or env.get('INGESTION_RUN_ID')!=p['run_id']:raise ValueError('diagnostic run identity differs')
    grant=None
    for _ in range(60):
        try:grant=json.loads(client.get_table(ref+'.BFDE_'+policy_hash).description);break
        except Exception:sleep(3)
    if grant!={'policy_hash':policy_hash,'execution':env.get('CLOUD_RUN_EXECUTION'),'run_id':p['run_id'],'request_limit':p['request_limit']}:raise ValueError('exact execution-specific owner grant absent')
    denied,_=M.binding_gate(['fbo_postings'],datetime.now(timezone.utc),metadata_only=True)
    if denied:raise ValueError('stored tenant binding denied')
    def emit(page):
        C.log(event='fbo_bounded_read_diagnostic_page',policy_hash=policy_hash,diagnostic_run_id=p['run_id'],diagnostic_generation=p['generation'],page=page,raw_writes=0,source_units=0,checkpoints=0,receipts=0,reconciliation=0)
    evidence=collect(p,emit=emit)
    C.log(event='fbo_bounded_read_diagnostic_end',policy_hash=policy_hash,diagnostic_run_id=p['run_id'],diagnostic_generation=p['generation'],**{k:v for k,v in evidence.items() if k!='pages'})
    return evidence
