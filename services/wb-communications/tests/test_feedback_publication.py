"""Feedback read-back and current-policy regression."""
import pytest
from app.domain.exceptions import WBPublishOutcomeUnknown
from app.services.pipeline import handle_update, run_poll
from tests.conftest import make_deps, SAMPLE_FEEDBACK
SAFE = 'Спасибо, что поделились впечатлением.'
STALE = 'В составе 2,25% салициловой кислоты.'
class FeedbackAPI:
    def __init__(self):
        self.answer=None; self.visible=True; self.error=None; self.calls=[]
    def iter_unanswered_feedbacks(self): return []
    def get_feedback(self,fid,**kwargs):
        self.calls.append('GET'); return {'id':fid,'answer':self.answer}
    def publish_answer(self,fid,text):
        self.calls.append('POST')
        if self.visible: self.answer={'text':text,'state':'wbRu'}
        if self.error: raise self.error
        return {'status_code':204,'response':None}
def setup(text=SAFE,source='ai'):
    d=make_deps([dict(SAMPLE_FEEDBACK)]);run_poll(d);c=next(iter(d.repo.docs))
    d.repo.docs[c].update(nm_id='438775617',supplier_article='',final_answer=text,answer_versions=[{'source':source,'text':text}])
    d.publication_validator=None;d.wb=FeedbackAPI()
    return d,c
def tap(d,c):
    return handle_update(d,{'callback_query':{'id':'c','data':'pub:'+c,'from':{'id':302044578},'message':{'message_id':1001,'chat':{'id':302044578}}}})
def test_fpub01_read_write_read():
    d,c=setup();assert tap(d,c)['status']=='published'
    assert d.wb.calls==['GET','POST','GET'];assert d.repo.get(c)['verified_at']
def test_fpub02_204_is_only_accepted():
    d,c=setup();d.wb.visible=False
    assert tap(d,c)['status']=='publish_accepted'
    assert all('Опубликовано' not in x[1] for x in d.telegram.edits)
@pytest.mark.parametrize('source',['ai','manual','regenerated'])
def test_fpub14_16_17_stale_text_blocked(source):
    d,c=setup(STALE,source);assert tap(d,c)['status']=='policy_blocked';assert 'POST' not in d.wb.calls
def test_fpub18_verifier_exception_closed():
    d,c=setup()
    def broken(*args):raise RuntimeError('unavailable')
    d.publication_validator=broken
    assert tap(d,c)['status']=='policy_check_failed';assert 'POST' not in d.wb.calls
def test_fpub19_unknown_product_closed():
    d,c=setup();d.repo.docs[c]['nm_id']='unknown'
    assert tap(d,c)['status']=='policy_blocked';assert 'POST' not in d.wb.calls


def test_fpub03_accepted_later_matching_read_only():
    d,c=setup();d.wb.visible=False;tap(d,c)
    d.wb.answer={'text':SAFE,'state':'wbRu'};run_poll(d)
    assert d.repo.get(c)['status']=='published';assert d.wb.calls.count('POST')==1

@pytest.mark.parametrize('code',[None,500,503])
@pytest.mark.parametrize('lands',[True,False])
def test_fpub04_05_06_unknown_outcome_never_resends(code,lands):
    d,c=setup();d.wb.visible=lands;d.wb.error=WBPublishOutcomeUnknown('unknown',status_code=code)
    result=tap(d,c)
    assert result['status']==('published' if lands else 'publish_unknown')
    tap(d,c);run_poll(d)
    assert d.wb.calls.count('POST')==1

@pytest.mark.parametrize('actual,result',[(SAFE,'published'),('Другой ответ','answered_externally'),('  Спасибо,  что поделились\nвпечатлением.  ','published')])
def test_fpub08_09_pre_read_zero_write(actual,result):
    d,c=setup();d.wb.answer={'text':actual,'state':'wbRu'}
    assert tap(d,c)['status']==result;assert d.wb.calls==['GET']

def test_fpub10_double_click():
    d,c=setup();tap(d,c);tap(d,c)
    assert d.wb.calls.count('POST')==1

def test_fpub11_12_legacy_unanswered_is_not_verified_or_rewritten():
    d,c=setup();d.repo.docs[c].update(status='published',published_at='2026-09-01T00:00:00+00:00')
    d.wb.iter_unanswered_feedbacks=lambda:[dict(SAMPLE_FEEDBACK)]
    run_poll(d);assert d.repo.get(c)['status']=='publish_unknown'
    assert not d.repo.get(c).get('verified_at')
    tap(d,c);run_poll(d);assert 'POST' not in d.wb.calls

def test_fpub15_manual_fix_revalidated():
    d,c=setup(STALE);tap(d,c)
    _,token,generation=d.repo.begin_edit(c)
    d.repo.commit_manual_answer(c,SAFE,token,generation)
    assert tap(d,c)['status']=='published'
    assert d.repo.get(c)['publish_trace'][0]['policy']['verdict']=='BLOCK'
    assert d.repo.get(c)['publish_trace'][-1]['policy']['verdict']!='BLOCK'

@pytest.mark.parametrize('state',[None,'none','unknown'])
def test_answer_state_unconfirmed_never_published(state):
    d,c=setup();d.wb.answer={'text':SAFE,'state':state}
    assert tap(d,c)['status']=='publish_unknown';assert 'POST' not in d.wb.calls

def test_mismatched_id_fails_closed():
    d,c=setup();d.wb.get_feedback=lambda fid,**kwargs:{'id':'other','answer':{'text':SAFE,'state':'wbRu'}}
    assert tap(d,c)['status']=='publish_unknown';assert 'POST' not in d.wb.calls

def test_policy_trace_no_pii_and_shadow_independent():
    d,c=setup();d.v3=object();d.settings.v3_shadow_enabled=False
    tap(d,c);tr=d.repo.get(c)['publish_trace'][-1]
    assert tr['policy']['snapshot'].startswith('ks_v3_')
    assert tr['policy']['version'];assert tr['policy']['text_sha256']
    assert tr['write_attempted'];assert tr['verification_result']=='verified'
    assert tr['telegram_state']=='published'
    assert SAFE not in str(tr)

def test_recovered_lease_never_resends_on_later_click():
    d,c=setup();d.repo.docs[c].update(status='publishing',lock_expires_at='2000-01-01')
    assert tap(d,c)['status']=='publish_unknown'
    tap(d,c);assert 'POST' not in d.wb.calls

def test_lease_expires_during_pre_read_no_write():
    d,c=setup();original=d.wb.get_feedback
    def expired(fid,**kwargs):
        d.repo.docs[c]['lock_expires_at']='2000-01-01'
        return original(fid)
    d.wb.get_feedback=expired
    from app.domain.exceptions import InvalidTransition
    with pytest.raises(InvalidTransition):tap(d,c)
    assert 'POST' not in d.wb.calls

@pytest.mark.parametrize('cid',['1d6c7beb793f4f1d8608','1f65fa9e27c0765c4514','8a4f21afc733ad6d6571'])
def test_known_stale_card_fixture_zero_write(cid):
    d,c=setup(STALE);d.repo.docs[cid]=d.repo.docs.pop(c)
    assert tap(d,cid)['status']=='policy_blocked'
    assert d.wb.calls==[]
    assert 'требует обновления' in d.telegram.edits[-1][1]
    buttons=str(d.telegram.edit_markups[-1][1])
    assert 'regen:' in buttons and 'edit:' in buttons and 'skip:' in buttons
    assert 'pub:' not in buttons


def test_active_publish_lock_not_stolen_by_poll():
    d,c=setup();doc=d.repo.begin_publish(c);run_poll(d)
    assert d.repo.get(c)['lock_token']==doc['lock_token'];assert d.wb.calls==[]

def test_expired_publish_recovered_read_only_by_poll():
    d,c=setup();d.repo.docs[c].update(status='publishing',lock_expires_at='2000-01-01')
    run_poll(d);tap(d,c)
    assert d.repo.get(c)['status']=='publish_unknown';assert 'POST' not in d.wb.calls


def test_regenerate_from_blocked_then_gate_runs_again():
    from app.domain.models import GenerationResult
    d,c=setup(STALE);tap(d,c)
    _,token=d.repo.begin_regenerate(c)
    d.repo.commit_regenerate(c,GenerationResult(text=STALE,model='fake',prompt_version='fake',usage={},latency_ms=0,request_id='fake'),token)
    assert tap(d,c)['status']=='policy_blocked';assert d.wb.calls==[]


def test_double_click_inflight_does_not_write_twice():
    d,c=setup();original=d.wb.publish_answer
    def during(fid,text):
        assert tap(d,c)['status']=='stale'
        return original(fid,text)
    d.wb.publish_answer=during;tap(d,c)
    assert d.wb.calls.count('POST')==1


def test_fpub07_connect_only_retry_and_body_error():
    import httpx
    from app.services.wb_client import WBClient
    from tests.conftest import make_settings
    calls=[]
    def handler(req):
        calls.append(req.method)
        if len(calls)==1:raise httpx.ConnectError('connect',request=req)
        return httpx.Response(204)
    client=WBClient(make_settings(),'fake',httpx.Client(base_url='https://example.test',transport=httpx.MockTransport(handler)))
    assert client.publish_answer('f',SAFE)['status_code']==204
    assert calls==['POST','POST']

@pytest.mark.parametrize('body',[{'error':True,'data':{}},{'data':{'id':'other','answer':None}},{'data':None},{'data':{'id':'f'}}])
def test_feedback_client_rejects_invalid_get(body):
    import httpx
    from app.services.wb_client import WBClient
    from app.domain.exceptions import WBApiError
    from tests.conftest import make_settings
    client=WBClient(make_settings(),'fake',httpx.Client(base_url='https://example.test',transport=httpx.MockTransport(lambda req:httpx.Response(200,json=body))))
    with pytest.raises(WBApiError):client.get_feedback('f')


def test_feedback_client_rejects_in_band_error():
    import httpx
    from app.services.wb_client import WBClient
    from app.domain.exceptions import WBApiError
    from tests.conftest import make_settings
    client=WBClient(make_settings(),'fake',httpx.Client(base_url='https://example.test',transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'error':True})) ))
    with pytest.raises(WBApiError):client.publish_answer('f',SAFE)


def test_auto_publish_false_in_publication_snapshot():
    from app.v3.snapshot import load_snapshot
    assert load_snapshot().policy['runtime']['auto_publish'] is False


def test_readback_http_5xx_only_one_post():
    import httpx
    from app.services.wb_client import WBClient
    d,c=setup();calls=[]
    def handler(req):
        calls.append(req.method)
        if req.method=='POST':return httpx.Response(503)
        answer={'text':SAFE,'state':'wbRu'} if 'POST' in calls else None
        return httpx.Response(200,json={'data':{'id':d.repo.get(c)['source_id'],'answer':answer}})
    d.wb=WBClient(d.settings,'fake',httpx.Client(base_url='https://example.test',transport=httpx.MockTransport(handler)))
    assert tap(d,c)['status']=='published';assert calls==['GET','POST','GET']


def test_missing_answer_field_cannot_authorize_write():
    d,c=setup()
    d.wb.get_feedback=lambda fid,**kwargs:{'id':fid}
    assert tap(d,c)['status']=='publish_unknown'
    assert 'POST' not in d.wb.calls


def test_legacy_unknown_without_intent_cannot_resend():
    d,c=setup()
    d.repo.docs[c].update(status='publish_unknown')
    assert tap(d,c)['status']=='publish_unknown'
    assert 'POST' not in d.wb.calls


def test_recovered_uncertainty_survives_policy_block_and_edit():
    d,c=setup(STALE)
    d.repo.docs[c].update(status='publishing',lock_expires_at='2000-01-01')
    assert tap(d,c)['status']=='policy_blocked'
    _,token,generation=d.repo.begin_edit(c)
    d.repo.commit_manual_answer(c,SAFE,token,generation)
    assert tap(d,c)['status']=='publish_unknown'
    assert 'POST' not in d.wb.calls
