"""Offline acceptance: pinned policy, observed pairs + explicitly synthetic cases."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.config import Settings
from app.response_quality.core import prepare, make_plan, evaluate, human_voice
from app.services.publication_policy import validate_for_publication
from app.v3.snapshot import load_snapshot
from app.v3.classifier import classify_rules
from app.v3.planner import plan
from app.v3.resolver import resolve
from tests.conftest import make_deps, SAMPLE_FEEDBACK
from app.services.pipeline import run_poll, handle_update

DATA = json.loads((Path(__file__).parent / 'golden_cases.json').read_text())
CASES = DATA['cases']
SNAP = load_snapshot()
SETTINGS = SimpleNamespace(v3_knowledge_snapshot_id=SNAP.snapshot_id)

@pytest.mark.parametrize('case', CASES, ids=lambda c:c['id'])
def test_golden_policy_and_quality(case):
    r = prepare(case['message'], case['v2'], SNAP, safe_v3_draft=case['v3'])
    assert r.status == case['expected_status']
    assert set(case['must_address']) <= {a.key for a in r.plan.aspects}
    assert r.plan.information_budget in (0, 1, 2)
    assert len(r.plan.facts) <= r.plan.information_budget
    if r.status == 'READY':
        assert validate_for_publication(r.text,case['message'],SETTINGS)['verdict'] != 'BLOCK'
        # Phase 3.1B: hard PASS can be weak; absent approved expertise remains a quality gap.
        assert r.quality.verdict in {'GOOD','NEEDS_IMPROVEMENT'}
        assert len(r.quality.dimensions)==15
        assert len(r.text) <= 1000
        if r.plan.route == 'SERVICE':
            assert r.plan.information_budget == 0 and not r.plan.facts
            assert 'решение по обращению принимает площадка' in r.text.lower()
            assert not any(x in r.text.lower() for x in ('церамид','гиалурон','купите'))
        if r.plan.level == 'DIRECT':
            assert not r.text.lower().startswith(('спасибо','здравствуйте','благодарим'))
    else:
        assert r.text is None and r.plan.human_reason

@pytest.mark.parametrize('text,iid', [
 ('гиалуроновая кислота','hyaluronic_acid'),('гиалуроновой кислоты','hyaluronic_acid'),
 ('Hyaluronic Acid','hyaluronic_acid'),('гиалуронат натрия','sodium_hyaluronate'),
 ('Sodium Hyaluronate','sodium_hyaluronate'),('салициловая кислота','salicylic_acid'),
 ('Salicylic Acid','salicylic_acid'),('ниацинамид','niacinamide'),('Niacinamide','niacinamide'),
 ('parfum','parfum'),('fragrance','parfum')])
def test_canonical_ingredient_name(text,iid):
    hits={i for i,_ in SNAP.ingredient_mentions(text.lower())}
    assert hits == {iid}

@pytest.mark.parametrize('text,verdict', [
 ('В формуле есть гиалуроновая кислота.','PASS'),
 ('В формуле есть гиалуронат натрия.','BLOCK'),
 ('Гиалуроновая кислота обеспечивает мягкость.','BLOCK'),
])
def test_fact_is_not_claim_or_related_ingredient(text,verdict):
    m=CASES[0]['message']
    assert validate_for_publication(text,m,SETTINGS)['verdict'] == verdict

@pytest.mark.parametrize('text', ['В составе 2,25% салициловой кислоты.','Salicylic Acid 2.25%.'])
def test_restricted_concentration_remains_blocked(text):
    m={'nm_id':'438775617','entity_type':'review','text':'Отлично'}
    assert validate_for_publication(text,m,SETTINGS)['verdict'] == 'BLOCK'

def test_customer_percentage_alone_is_not_a_publication_violation():
    m={'nm_id':'438775617','entity_type':'review','text':'У вас 2,25% кислоты?'}
    assert validate_for_publication('Спасибо, что поделились впечатлением.',m,SETTINGS)['verdict'] != 'BLOCK'

def test_repair_is_reverified_and_bounded():
    c=CASES[0];calls=[]
    def bad(msg,p,**kw):calls.append(1);return 'В составе 2,25% салициловой кислоты.'
    r=prepare(c['message'],c['v2'],SNAP,render=bad)
    assert calls == [1] and r.status=='HUMAN_REVIEW' and r.final_policy['verdict']=='BLOCK'
    good=prepare(c['message'],c['v2'],SNAP)
    assert good.repaired and good.final_policy['verdict']=='PASS'

def test_style_failure_cannot_be_a_hard_block():
    m=CASES[0]['message'];safe='Спасибо за отзыв.'
    r=prepare(m,safe,SNAP,render=lambda *a,**k:'В составе 2,25% салициловой кислоты.')
    assert r.status=='READY' and r.quality.verdict=='NEEDS_IMPROVEMENT'
    assert 'мягкость' in r.text and 'CUSTOMER_SIGNALS_SAFE_FALLBACK' in r.reasons
    assert r.final_policy['verdict'] != 'BLOCK'

def test_mixed_sentiment_covers_both_and_quality_is_separate():
    m={'entity_type':'review','nm_id':'535581674','text':'Крем отличный, запах резковатый','rating':5}
    p=make_plan(m,SNAP);safe='Спасибо, что отметили аромат.'
    policy=validate_for_publication(safe,m,SETTINGS)
    assert policy['verdict']!='BLOCK'
    q=evaluate(safe,m,p,policy)
    assert q.verdict=='NEEDS_IMPROVEMENT' and q.dimensions['missed_customer_signal_penalty']=='NEEDS_IMPROVEMENT'
    r=prepare(m,safe,SNAP)
    assert 'понрав' in r.text and 'резк' in r.text
    assert evaluate(r.text,m,r.plan,r.final_policy,previous_answers=[r.text]).dimensions['repetition_penalty']=='NEEDS_IMPROVEMENT'

@pytest.mark.parametrize('text', ['Жжение прошло','Кожа немного щиплет','Раздражение не проходит'])
def test_existing_mild_safety_template_is_unchanged(text):
    m={'entity_type':'review','nm_id':'535581674','text':text,'rating':2}
    hard=plan(SNAP,resolve(SNAP,nm_id=m['nm_id']),classify_rules(m,SNAP),m)
    p=make_plan(m,SNAP)
    assert p.route==hard.strategy
    if hard.strategy=='SAFETY_TEMPLATE':assert human_voice(m,p)==hard.deterministic_text

def _cb(action,c,version=None,n=100):
    return {'update_id':n,'callback_query':{'id':str(n),'data':f'{action}:{c}'+(f':{version}' if version is not None else ''),
        'from':{'id':302044578},'message':{'message_id':1001,'chat':{'id':302044578}}}}

def _pending(text='В составе 2,25% салициловой кислоты.', question=False):
    d=make_deps([dict(SAMPLE_FEEDBACK)],v31_operator_recovery_enabled=False)
    run_poll(d);c=next(iter(d.repo.docs));doc=d.repo.docs[c]
    d.wb.get_feedback_calls=0  # count only reads made after the card (the actionability read is ingestion)
    doc.update(nm_id='438775617',supplier_article='',text='Отличный продукт',final_answer=text,
               answer_versions=[{'text':text,'source':'ai','generation_number':1}],generation_number=1)
    if question:
        doc.update(entity_type='question',question_id='Q',text='Какой pH?',nm_id='535581674')
        d.settings.wb_question_publish_enabled=True
    d.publication_validator=None;d.settings.v31_operator_recovery_enabled=True
    return d,c

def test_old_pending_repairs_without_wb_read_or_write_then_new_consent():
    d,c=_pending();r=handle_update(d,_cb('pub',c))
    assert r=={'status':'policy_repaired','requires_new_publish':True}
    assert not d.wb.published and d.wb.get_feedback_calls==0
    doc=d.repo.get(c);assert doc['answer_versions'][0]['text'].startswith('В составе 2,25')
    assert doc['answer_versions'][-1]['source']=='policy_repair'
    assert not doc['publish_trace'][-1]['write_attempted']
    assert 'Проект ответа' in d.telegram.edits[-1][1]
    assert 'Черновик автоматически' in d.telegram.edits[-1][1]
    assert handle_update(d,_cb('pub',c,n=101))['status']=='stale'
    assert handle_update(d,_cb('pub',c,1,n=102))['status']=='stale'
    assert not d.wb.published
    assert handle_update(d,_cb('pub',c,doc['generation_number'],n=103))['status']=='published'
    assert len(d.wb.published)==1 and d.wb.get_feedback_calls==2
    assert d.repo.get(c)['verified_at']

def test_question_repair_requires_new_consent_and_readback():
    d,c=_pending('pH 9,0.',question=True)
    assert handle_update(d,_cb('pub',c))['status']=='policy_repaired'
    assert not d.wb.published_questions and d.wb.get_question_calls==0
    version=d.repo.get(c)['generation_number']
    assert handle_update(d,_cb('pub',c,version,n=101))['status']=='published'
    assert len(d.wb.published_questions)==1 and d.wb.get_question_calls==2
    assert d.repo.get(c)['verified_at']

def test_shadow_default_keeps_cards_and_primary_text_unchanged():
    a=make_deps([dict(SAMPLE_FEEDBACK)])
    b=make_deps([dict(SAMPLE_FEEDBACK)],v31_quality_shadow_enabled=False)
    run_poll(a);run_poll(b)
    assert a.telegram.sent==b.telegram.sent
    assert [d['final_answer'] for d in a.repo.docs.values()]==[d['final_answer'] for d in b.repo.docs.values()]
    assert not a.wb.published and not b.wb.published
    assert Settings().v31_operator_recovery_enabled is False

def test_initial_candidate_repaired_before_card_with_no_auto_publish():
    from app.domain.models import GenerationResult
    class Unsafe:
        def generate_answer(self,*a):return GenerationResult(CASES[0]['v2'],'fake','fake',{},0,'')
    fb=copy.deepcopy(SAMPLE_FEEDBACK);fb.update(text='Тоник отличный, очень мягкий',productDetails={'nmId':535581674,'supplierArticle':'','productName':'Тоник'})
    d=make_deps([fb],openai=Unsafe(),v31_operator_recovery_enabled=True)
    run_poll(d);doc=next(iter(d.repo.docs.values()))
    assert doc['response_recovery']['repaired'] and doc['status']=='pending_approval'
    assert 'мягк' in doc['final_answer'] and '0,2%' not in doc['final_answer']
    assert not d.wb.published and d.wb.get_feedback_calls==1  # only the pre-card actionability read
    assert [v['source'] for v in doc['answer_versions']]==['ai','policy_repair']

def test_manual_edit_and_regenerate_keep_versions_and_no_write():
    d,c=_pending()
    assert handle_update(d,_cb('edit',c))['status']=='editing_started'
    r=handle_update(d,{'update_id':101,'message':{'from':{'id':302044578},'chat':{'id':302044578},'reply_to_message':{'message_id':d.repo.get_editing_session(302044578)['prompt_message_id']},'text':'В составе 2,25% салициловой кислоты.'}})
    assert r['status']=='edited'
    doc=d.repo.get(c);assert [v['source'] for v in doc['answer_versions']][-2:]==['manual','policy_repair']
    assert not d.wb.published
    assert handle_update(d,_cb('regen',c,n=102))['status']=='regenerated'
    assert not d.wb.published

def test_serious_legacy_text_never_repaired_to_ordinary_publication():
    d,c=_pending('Спасибо за отзыв.');d.repo.docs[c]['text']='Трудно дышать, отёк губ'
    assert handle_update(d,_cb('pub',c))['status']=='policy_blocked'
    assert not d.wb.published and d.wb.get_feedback_calls==0

def test_legacy_button_race_does_not_approve_replacement():
    from app.domain.exceptions import InvalidTransition
    d,c=_pending();original=d.repo.begin_publish
    def raced(doc_id,**kw):
        d.repo.docs[doc_id]['generation_number']+=1
        return original(doc_id,**kw)
    d.repo.begin_publish=raced
    assert handle_update(d,_cb('pub',c))['status']=='stale'
    assert not d.wb.published
