"""Rejected timestamps remain rejected, with safe recoverable diagnostics."""
from datetime import date
from copy import deepcopy
import pytest
import backfill_core as B
import backfill as F
import common as C

A=B.utc_ms('2026-08-19'); Z=B.utc_ms('2026-08-26')

@pytest.mark.parametrize('value', ['2026-08-18T21:00:00Z','2026-08-19T00:00:00+03:00',
    '2026-08-25T20:59:59.999999Z','2026-08-25T23:59:59.999999+03:00'])
def test_exact_utc_and_offset_boundaries(value):
    assert F.validate_fbo_timestamp(value,A,Z,page_ordinal=1,request_ordinal=1,record_ordinal=1).utcoffset() is not None

@pytest.mark.parametrize('value,category',[
 ('2026-08-18T20:59:59.999999Z','OUTSIDE_REQUESTED_INTERVAL'),
 ('2026-08-25T21:00:00Z','OUTSIDE_REQUESTED_INTERVAL'),
 ('2026-08-26T00:00:00+03:00','OUTSIDE_REQUESTED_INTERVAL'),
 ('2026-08-19T00:00:00','MISSING_TIMEZONE'),('2026-08-19','MISSING_TIMEZONE'),
 ('malformed','INVALID_TIMESTAMP_FORMAT'),(None,'INVALID_TIMESTAMP_FORMAT'),
 (42,'INVALID_TIMESTAMP_FORMAT'),({},'INVALID_TIMESTAMP_FORMAT')])
def test_typed_failure_carries_exact_window_and_ordinals(value,category):
    with pytest.raises(B.FboWindowError,match='FBO_WINDOW_'+category) as raised:
        F.validate_fbo_timestamp(value,A,Z,page_ordinal=12,request_ordinal=13,record_ordinal=7)
    d=raised.value.fbo_window_evidence
    assert d['validation_class']==category and d['requested_utc_from']==B.stamp(A)
    assert d['requested_utc_to']==B.stamp(Z-1) and d['utc_until_exclusive']==B.stamp(Z)
    assert (d['page_ordinal'],d['request_ordinal'],d['record_ordinal'])==(12,13,7)
    assert d['timestamp_value']==(value if isinstance(value,str) else None)
    assert str(value) not in str(raised.value)

def test_secret_redacted_before_evidence_hash_or_truncation():
    secret='SYNTHETIC_WINDOW_SECRET_ONLY'
    C.register_secret(secret, 'test')
    with pytest.raises(B.FboWindowError) as raised:
        F.validate_fbo_timestamp(secret+'x'*2000,A,Z,page_ordinal=1,request_ordinal=1,record_ordinal=1)
    d=raised.value.fbo_window_evidence
    assert secret not in str(d) and len(d['timestamp_value'])==1024 and d['timestamp_truncated']
    assert len(d['timestamp_sha256'])==64

@pytest.mark.parametrize('value,category',[('2026-08-26T00:00:00+03:00','OUTSIDE_REQUESTED_INTERVAL'),
 ('2026-08-19T00:00:00','MISSING_TIMEZONE'),('invalid','INVALID_TIMESTAMP_FORMAT')])
def test_failed_second_page_persists_diagnostic_without_merge_or_progress(monkeypatch,value,category):
    env=dict(BACKFILL_MODE=B.VERSION,BACKFILL_TARGET_PROJECT='synthetic-project',TENANT_BINDING_REQUIRED='1',
      STRICT_PAGE_CAPS='1',SINCE='2026-08-19',UNTIL='2026-08-25',BACKFILL_GENERATION='diagnostic',BACKFILL_ORIGIN='2026-10-11T00:00:00Z',BACKFILL_WINDOW_DAYS='7')
    p=B.plan(env,'fbo_postings','synthetic-project','ozon_raw','ref',date(2026,10,11));s=B.initial(p)
    monkeypatch.setattr(F,'resume',lambda _:deepcopy(s));monkeypatch.setattr(F,'internal_preflight',lambda _:None)
    pages=iter([dict(postings=[dict(created_at=B.stamp(A),products=[dict(sku='SYNTHETIC')])],has_next=True,cursor='SYNTHETIC_CURSOR'),
                dict(postings=[dict(created_at=value,products=[dict(sku='SYNTHETIC')])],has_next=False,cursor='')])
    monkeypatch.setattr(C,'seller_post',lambda *args:(200,next(pages)))
    monkeypatch.setattr(F.E,'_fbo_rows',lambda *args:[{}])
    calls=[]
    monkeypatch.setattr(C,'merge_rows',lambda *args,**kwargs:calls.append('MERGE'))
    monkeypatch.setattr(C,'record_run',lambda *args,**kwargs:calls.append('UNIT'))
    with pytest.raises(B.FboWindowError) as raised:F.run_backfill(p,'synthetic-new-run','safe')
    assert not calls and raised.value.backfill_failure_evidence['state']==s
    d=raised.value.backfill_failure_evidence['fbo_window_validation']
    assert (d['validation_class'],d['page_ordinal'],d['request_ordinal'])==(category,2,2)
    assert 'SYNTHETIC_CURSOR' not in str(d) and 'products' not in str(d)
