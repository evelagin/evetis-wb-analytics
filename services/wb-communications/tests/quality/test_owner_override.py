"""Operator consent integration through fake WB: no live APIs, polls or callbacks."""
import copy
from datetime import datetime,timedelta,timezone
import pytest
from app.config import Settings
from app.services.pipeline import handle_update
from app.services.owner_override import sha,offer,safety_fixed
from tests.quality.test_response_quality import _pending,_cb


def setup():
    # Override is an R2 feature: it requires live publication on the v3.1E policy.
    d,c=_pending();d.settings.v31_owner_override_enabled=True;d.settings.v31_enforce_live_publication_policy=True
    d.repo.docs[c]['text']='Отличный продукт'
    return d,c


def first(d,c,n=101,user=302044578):
    doc=d.repo.get(c);off=offer(doc,d.settings)
    assert off
    cb=_cb('ov',c+':'+str(doc['generation_number'])+':'+str(off['source_version']),n=n)
    cb['callback_query']['from']['id']=user
    return handle_update(d,cb)


def second(d,c,n=102,user=302044578):
    nonce=d.repo.get(c)['owner_override_pending']['nonce']
    cb=_cb('oc',c+':'+nonce,n=n);cb['callback_query']['from']['id']=user
    return handle_update(d,cb)


def no_wb(d):
    assert not d.wb.published and not d.wb.published_questions
    assert d.wb.get_feedback_calls==0 and d.wb.get_question_calls==0


def test_disabled_by_default():
    assert Settings().v31_owner_override_enabled is False


def test_first_click_zero_wb_second_verified_and_duplicate_zero_write():
    d,c=setup()
    assert first(d,c)['status']=='override_confirmation_required';no_wb(d)
    assert '2,25%' in d.telegram.sent[-1][1]
    assert second(d,c)['status']=='published'
    doc=d.repo.get(c)
    assert len(d.wb.published)==1 and d.wb.get_feedback_calls==2 and doc['verified_at']
    audit=doc['owner_override_audit'][0]
    assert audit['override'] is True and audit['override_by']=='302044578'
    assert audit['override_confirmed_at'] and audit['override_at'] and audit['answer_hash']==sha(doc['final_answer'])
    assert audit['violations'] and audit['violation_spans'] and audit['source_version']==1
    assert audit['policy_version'] and audit['knowledge_snapshot'] and 'reason_optional' in audit
    assert second(d,c,n=103)['status']=='stale' and len(d.wb.published)==1
    # Spans remain in private operator audit; publication trace/outbox only safe hashes and IDs.
    assert 'violation_spans' not in str(doc['publish_trace'])


@pytest.mark.parametrize('stage',['first','second'])
def test_unauthorized_override_zero_wb(stage):
    d,c=setup()
    if stage=='second':first(d,c)
    r=first(d,c,user=999) if stage=='first' else second(d,c,user=999)
    assert r['status']=='unauthorized';no_wb(d)


def test_allowlisted_chat_with_empty_user_list_cannot_override():
    d,c=setup();d.settings.telegram_allowed_user_ids=set()
    assert first(d,c)['status']=='unauthorized';no_wb(d)


@pytest.mark.parametrize('change',['generation','hash','source','expiry','status','customer','product'])
def test_stale_confirmation_zero_wb(change):
    d,c=setup();first(d,c);doc=d.repo.docs[c]
    if change=='generation':doc['generation_number']+=1
    elif change=='hash':doc['final_answer']='Другой текст.'
    elif change=='source':doc['answer_versions'][0]['text']='Другой исходный текст.'
    elif change=='expiry':doc['owner_override_pending']['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    elif change=='customer':doc['text']='Трудно дышать, отёк губ'
    elif change=='product':doc['nm_id']='535581674'
    else:doc['status']='editing'
    assert second(d,c)['status']==('safety_fixed' if change=='customer' else 'stale');no_wb(d)


def test_edit_and_regenerate_invalidate_prior_confirmation():
    for action in ('edit','regen'):
        d,c=setup();first(d,c)
        handle_update(d,_cb(action,c,n=110))
        assert second(d,c,n=111)['status']=='stale';no_wb(d)


def test_policy_changed_requires_reconfirmation(monkeypatch):
    import app.services.owner_override as mod
    d,c=setup();first(d,c);original=mod.validate_for_publication
    monkeypatch.setattr(mod,'validate_for_publication',lambda *a,**kw:original(*a,**kw)|{'version':'changed'})
    assert second(d,c)['status']=='override_reconfirmation_required';no_wb(d)


def test_concurrent_draft_change_at_atomic_consume_zero_wb():
    d,c=setup();first(d,c);begin=d.repo.begin_publish
    def race(*a,**kw):
        d.repo.docs[c]['generation_number']+=1
        return begin(*a,**kw)
    d.repo.begin_publish=race
    assert second(d,c)['status']=='stale';no_wb(d)


@pytest.mark.parametrize('outcome',['missing','different','unknown_state'])
def test_override_still_requires_matching_public_readback(outcome):
    d,c=setup();first(d,c)
    original=d.wb.get_feedback;count=0
    def get(*a,**kw):
        nonlocal count
        count+=1
        if count==1:return original(*a,**kw)
        return {'id':d.repo.get(c)['source_id'],'answer':None if outcome=='missing' else
                {'text':'Другой ответ' if outcome=='different' else d.repo.get(c)['final_answer'],
                 'state':'unknown' if outcome=='unknown_state' else 'wbRu'}}
    d.wb.get_feedback=get
    r=second(d,c)
    assert r['status']!='published' and not d.repo.get(c).get('verified_at')
    assert len(d.wb.published)==1


@pytest.mark.parametrize('text',['Трудно дышать, отёк губ','Кожа немного щиплет','Жжение прошло'])
def test_safety_routes_not_overridden(text):
    d,c=setup();d.repo.docs[c]['text']=text
    assert safety_fixed(d.repo.get(c),d.settings)
    assert offer(d.repo.get(c),d.settings) is None
    assert handle_update(d,_cb('ov',c+':1:1'))['status']=='safety_fixed';no_wb(d)


def test_repaired_card_offers_original_but_consent_is_exact_original():
    d,c=setup()
    assert handle_update(d,_cb('pub',c))['status']=='policy_repaired';no_wb(d)
    from app.services.pipeline import _operator_keyboard
    keyboard=_operator_keyboard(d,c,d.repo.get(c))
    assert 'Опубликовать исправленный' in str(keyboard)
    assert 'Опубликовать исходный всё равно' in str(keyboard)
    assert first(d,c)['status']=='override_confirmation_required';no_wb(d)
    assert second(d,c)['status']=='published'
    assert d.repo.get(c)['final_answer'].startswith('В составе 2,25')


def test_firestore_override_confirmation_consumed_in_same_publish_transaction(monkeypatch):
    from google.cloud import firestore
    from app.services.repository import FirestoreRepository
    from tests.quality.test_shadow_and_fencing import Client,Ref
    monkeypatch.setattr(firestore,'transactional',lambda f:f)
    d,c=setup();first(d,c)
    doc=d.repo.get(c);pending=copy.deepcopy(doc['owner_override_pending'])
    client=Client(copy.deepcopy(doc));repo=FirestoreRepository.__new__(FirestoreRepository);repo._lease=300
    monkeypatch.setattr(repo,'_lazy',lambda:client);monkeypatch.setattr(repo,'_doc',lambda _:Ref(client))
    locked=repo.begin_publish(c,expected_generation=doc['generation_number'],override_confirmation=pending)
    assert len(client.tx.updates)==1
    update=client.tx.updates[0]
    assert update['status']=='publishing' and update['owner_override_pending']['consumed_at']
    assert update['owner_override_audit'][0]['override'] is True
    assert update['answer_versions'][-1]['source']=='owner_override'
    assert locked['final_answer']==doc['answer_versions'][0]['text']
    with pytest.raises(Exception):repo.begin_publish(c,expected_generation=doc['generation_number'],override_confirmation=pending)
    assert len(client.tx.updates)==1


def test_firestore_first_confirmation_transaction_rejects_generation_race(monkeypatch):
    from google.cloud import firestore
    from app.services.repository import FirestoreRepository
    from tests.quality.test_shadow_and_fencing import Client,Ref
    from app.domain.exceptions import InvalidTransition
    monkeypatch.setattr(firestore,'transactional',lambda f:f)
    d,c=setup();first(d,c);doc=d.repo.get(c)
    client=Client(copy.deepcopy(doc));repo=FirestoreRepository.__new__(FirestoreRepository)
    monkeypatch.setattr(repo,'_lazy',lambda:client);monkeypatch.setattr(repo,'_doc',lambda _:Ref(client))
    client.doc['generation_number']+=1
    with pytest.raises(InvalidTransition):repo.request_override(c,doc['owner_override_pending'],doc['generation_number'])
    assert not client.tx.updates


def test_locked_policy_drift_after_confirmation_zero_wb():
    from app.services.publication_policy import validate_for_publication
    d,c=setup();first(d,c)
    d.publication_validator=lambda *a,**kw:validate_for_publication(*a,**kw)|{'version':'changed-at-lock'}
    assert second(d,c)['status']=='policy_check_failed';no_wb(d)


def test_question_override_uses_existing_verified_question_transport():
    d,c=_pending('pH 9,0.',question=True);d.settings.v31_owner_override_enabled=True;d.settings.v31_enforce_live_publication_policy=True
    assert first(d,c)['status']=='override_confirmation_required';no_wb(d)
    assert second(d,c)['status']=='published'
    assert not d.wb.published and len(d.wb.published_questions)==1 and d.wb.get_question_calls==2
    assert d.repo.get(c)['verified_at']
