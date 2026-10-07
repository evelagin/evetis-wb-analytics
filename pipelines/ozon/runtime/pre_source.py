"""Pure, owner-attested PRE-SOURCE failure recovery. Never authorizes ambiguous POST.

BFP metadata lives in ref, writable only through the owner recovery publisher;
runtime/controller read it, and the append identity cannot forge it. Original
source unit, failed run, receipt, reservation and STOP remain immutable.
"""
import copy
import json
import re
import backfill_core as B

VERSION = 'PRE_SOURCE_FAILURE_V1'
FIELDS = frozenset({'version','root_hash','tenant','project','plan_id','dispatch_sequence',
                   'run_id','execution','image','source_sha','lease_generation','intent_hash',
                   'receipt_hash','stop_hashes','unit_sequence','state_hash','cohort_hash',
                   'exports_reserved','query_id','failure_stage','verified_at',
                   'post_attempts','uuid_present','sku_rows_written','hash'})


def validate(proof, p):
    if not isinstance(proof, dict) or set(proof) != FIELDS:
        raise B.EvidenceError('closed pre-source recovery evidence required')
    if proof['version'] != VERSION or proof['failure_stage'] != 'CAPABILITY_PROFILE_ACCESS_DENIED_BEFORE_REPORT_POST':
        raise B.EvidenceError('unsupported pre-source failure stage')
    if proof['project'] != p['project'] or proof['plan_id'] != p['plan_id'] or p['entity'] != 'ads_sku_daily':
        raise B.EvidenceError('pre-source recovery tenant/plan mismatch')
    if not isinstance(proof['tenant'],str) or not re.fullmatch('[a-z][a-z0-9_]{1,48}',proof['tenant']):
        raise B.EvidenceError('invalid pre-source tenant label')
    for key in ['root_hash','plan_id','intent_hash','receipt_hash','state_hash','cohort_hash','hash']:
        if not isinstance(proof[key],str) or not re.fullmatch('[0-9a-f]{64}',proof[key]):
            raise B.EvidenceError('invalid pre-source proof hash')
    if B.digest({k:v for k,v in proof.items() if k != 'hash'}) != proof['hash']:
        raise B.EvidenceError('pre-source proof integrity failed')
    if not re.fullmatch('[0-9a-f]{40}',proof['source_sha']) or not re.fullmatch(r'europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:[0-9a-f]{64}',proof['image']):
        raise B.EvidenceError('pre-source artifact provenance absent')
    if any(type(proof[k]) is not int or proof[k]<1 for k in ['dispatch_sequence','unit_sequence','lease_generation','exports_reserved']) or proof['exports_reserved']>10:
        raise B.EvidenceError('invalid pre-source sequence/reservation')
    if proof['post_attempts'] != 0 or type(proof['post_attempts']) is not int or proof['uuid_present'] is not False or proof['sku_rows_written'] != 0 or type(proof['sku_rows_written']) is not int:
        raise B.EvidenceError('source effects/ambiguous submission cannot be recovered')
    if not isinstance(proof['stop_hashes'],list) or not proof['stop_hashes'] or len(set(proof['stop_hashes']))!=len(proof['stop_hashes']) or any(not re.fullmatch('[0-9a-f]{64}',v) for v in proof['stop_hashes']):
        raise B.EvidenceError('exact predecessor STOP hashes required')
    from datetime import datetime
    stamp=datetime.fromisoformat(proof['verified_at'].replace('Z','+00:00'))
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds()!=0:
        raise B.EvidenceError('UTC recovery verification time required')
    for k in ['run_id','query_id','execution']:
        if not isinstance(proof[k],str) or not re.fullmatch('[A-Za-z0-9_-]{1,160}',proof[k]):
            raise B.EvidenceError('invalid pre-source provenance identifier')
    return proof


def validate_state(proof,p,state):
    validate(proof,p);B.validate(p,state)
    report=state['progress'].get('report')
    if state['complete'] or state['sequence']!=proof['unit_sequence'] or B.digest(state)!=proof['state_hash']:
        raise B.EvidenceError('failed checkpoint drift')
    if not isinstance(report,dict) or report.get('phase')!='INTENT' or report.get('uuid') is not None or report.get('execution')!=proof['run_id'] or report.get('cohort_hash')!=proof['cohort_hash'] or len(report.get('batch',[]))!=proof['exports_reserved']:
        raise B.EvidenceError('not the attested unsubmitted intent')
    return proof


def recover(proof,p,state):
    validate_state(proof,p,state)
    out=copy.deepcopy(state)
    out['progress']['report']=None
    # The caller appends a NEW source unit; never overwrites unit_sequence.
    return out,{'action':'PRE_SOURCE_RECOVERED','failed_unit_sequence':proof['unit_sequence'],
                'failed_run_id':proof['run_id'],'recovery_hash':proof['hash'],
                'historical_exports_reserved':proof['exports_reserved'],
                'source_post_attempts':0,'rows_written':0,'source_terminal':False}


def marker(proof):
    return 'BFP_'+proof['hash']


def marker_value(proof):
    return {'kind':'pre_source_failure','plan':proof['plan_id'][:16]}, json.dumps(proof,sort_keys=True,separators=(',',':'))
