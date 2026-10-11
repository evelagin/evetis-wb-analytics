"""Exact installed runtime, no network; reject boundaries before MERGE/state."""
import sys,socket
sys.path.insert(0,'/app')
from datetime import date
from unittest.mock import patch
from copy import deepcopy
import backfill_core as B,backfill as F,common as C

def denied(*a,**kw):raise AssertionError('network/source/persistence denied')
socket.socket=socket.create_connection=denied
A=B.utc_ms('2026-08-19');Z=B.utc_ms('2026-08-26')
for v in ('2026-08-18T21:00:00Z','2026-08-19T00:00:00+03:00','2026-08-25T20:59:59.999999Z','2026-08-25T23:59:59.999999+03:00'):
 F.validate_fbo_timestamp(v,A,Z,page_ordinal=1,request_ordinal=1,record_ordinal=1)
cases=[('2026-08-18T20:59:59.999999Z','OUTSIDE_REQUESTED_INTERVAL'),('2026-08-25T21:00:00Z','OUTSIDE_REQUESTED_INTERVAL'),('2026-08-26T00:00:00+03:00','OUTSIDE_REQUESTED_INTERVAL'),('2026-08-19T00:00:00','MISSING_TIMEZONE'),('invalid','INVALID_TIMESTAMP_FORMAT'),(None,'INVALID_TIMESTAMP_FORMAT')]
env=dict(BACKFILL_MODE=B.VERSION,BACKFILL_TARGET_PROJECT='synthetic-project',TENANT_BINDING_REQUIRED='1',STRICT_PAGE_CAPS='1',SINCE='2026-08-19',UNTIL='2026-08-25',BACKFILL_GENERATION='diagnostic',BACKFILL_ORIGIN='2026-10-11T00:00:00Z',BACKFILL_WINDOW_DAYS='7')
p=B.plan(env,'fbo_postings','synthetic-project','ozon_raw','ref',date(2026,10,11));state=B.initial(p)
for value,category in cases:
 with patch.object(F,'resume',lambda _:deepcopy(state)),patch.object(F,'internal_preflight',lambda _:None),patch.object(C,'seller_post',lambda *args:(200,dict(postings=[dict(created_at=value,products=[dict(sku='SYNTHETIC')])],has_next=False,cursor=''))),patch.object(C,'merge_rows',denied),patch.object(C,'record_run',denied):
  try:F.run_backfill(p,'synthetic-new-run','safe')
  except B.FboWindowError as error:
   d=error.backfill_failure_evidence['fbo_window_validation'];assert d['validation_class']==category and d['timestamp_value']==value
   assert error.backfill_failure_evidence['state']==state and d['requested_utc_from']==B.stamp(A) and d['utc_until_exclusive']==B.stamp(Z)
  else:raise AssertionError('invalid source accepted')
C.register_secret('SYNTHETIC_SECRET','offline')
try:F.validate_fbo_timestamp('SYNTHETIC_SECRET'*100,A,Z,page_ordinal=2,request_ordinal=2,record_ordinal=1)
except B.FboWindowError as error:assert 'SYNTHETIC_SECRET' not in str(error.fbo_window_evidence) and len(error.fbo_window_evidence['timestamp_value'])<=1024
else:raise AssertionError('invalid secret fixture accepted')
import json
print(json.dumps(dict(fbo_window_typed_diagnostics='PASS',fbo_window_strict_no_partial_merge='PASS',fbo_window_safe_evidence='PASS'),sort_keys=True))
