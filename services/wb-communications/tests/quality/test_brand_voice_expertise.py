import copy,hashlib,json
from types import SimpleNamespace
import pytest
from app.v3.snapshot import load_snapshot,KnowledgeSnapshot
from app.v3.snapshot_builder import content_hash
from app.response_quality import expertise
from app.response_quality.core import prepare,make_plan,evaluate
from app.response_quality.brand_voice import render,AXES
from app.services.publication_policy import validate_for_publication

S=load_snapshot();ST=SimpleNamespace(v3_knowledge_snapshot_id=S.snapshot_id)
TONER=dict(nm_id='535581674',text='Тоник отличный, очень мягкий',rating=5,entity_type='review')


def approval_fixture(monkeypatch,*,wording=None,iid='hyaluronic_acid',pid='EVT-FT-MOIST-150'):
    data=copy.deepcopy(S.data)
    row=copy.deepcopy(next(r for r in expertise.load_registry()['explanations'] if r['explanation_id']=='EX-C-HA-HYDRATION'))
    row['source_ids']=['SRC-TU-SP-TS-9']
    row.pop('evidence_sha256',None)
    row.update(status='APPROVED',owner_decision_id='ODR-TEST',ingredient_id=iid,product_ids=[pid])
    if wording:row['variants_ru']=[wording]
    body={k:v for k,v in row.items() if k!='approval'}
    digest=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    row['approval']=dict(approved_by='test-owner',approved_at='2026-10-04',content_sha256=digest)
    data['owner_decisions']['ODR-TEST']=dict(status='CLOSED',topic='approved_expert_explanation',explanation_sha256=digest)
    data['content_sha256']=content_hash(data);snap=KnowledgeSnapshot(data)
    registry=dict(schema='evetis-approved-expertise/1',snapshot_sha256=snap.data['content_sha256'],explanations=[row])
    monkeypatch.setattr(expertise,'load_registry',lambda:registry)
    monkeypatch.setattr('app.services.publication_policy.load_snapshot',lambda *a:snap)
    return snap,registry


def test_verified_presence_without_approval_does_not_activate_benefit(monkeypatch):
    registry=copy.deepcopy(expertise.load_registry())
    for row in registry['explanations']:
        if row['kind']!='fact':row['status']='OWNER_APPROVAL_CANDIDATE'
    monkeypatch.setattr(expertise,'load_registry',lambda:registry)
    assert expertise.approved(S,'EVT-FT-MOIST-150')==[]
    assert validate_for_publication('В составе есть гиалуроновая кислота.',TONER,ST)['verdict']=='PASS'
    assert validate_for_publication('Гиалуроновая кислота способствует поддержанию увлажнённости кожи.',TONER,ST)['verdict']=='BLOCK'
    r=prepare(TONER,'Спасибо за отзыв.',S)
    assert r.status=='READY' and r.quality.verdict=='NEEDS_IMPROVEMENT'
    assert r.quality.dimensions['future_buyer_value']=='NEEDS_IMPROVEMENT'
    assert not any(x in r.text.lower() for x in ('увлажня','питает','обеспечивает'))


def test_explicit_product_and_snapshot_bound_approval_allows_reviewed_variants(monkeypatch):
    snap,registry=approval_fixture(monkeypatch)
    row=registry['explanations'][0]
    assert expertise.approved(snap,'EVT-FT-MOIST-150')==[row]
    for t in row['variants_ru']:
        assert validate_for_publication(t,TONER,ST)['verdict']=='PASS'
    r=prepare(TONER,'Спасибо за отзыв.',snap)
    assert r.status=='READY' and r.quality.verdict=='GOOD'
    assert r.plan.explanations and 'увлажнённост' in r.text
    # Exact semantic family is reviewed, not arbitrary fluent claims.
    assert validate_for_publication('Гиалуроновая кислота лечит кожу.',TONER,ST)['verdict']=='BLOCK'
    wrong=dict(TONER,nm_id='535581675')
    assert validate_for_publication(row['variants_ru'][0],wrong,ST)['verdict']=='BLOCK'


@pytest.mark.parametrize('fault',['approval_hash','decision_hash','source','snapshot','missing_actor','unverified_ingredient'])
def test_invalid_approval_not_generation_permission(monkeypatch,fault):
    snap,registry=approval_fixture(monkeypatch)
    row=registry['explanations'][0]
    if fault=='approval_hash':row['variants_ru']=['Гиалуроновая кислота лечит акне.']
    elif fault=='decision_hash':snap.decisions['ODR-TEST']['explanation_sha256']='wrong'
    elif fault=='source':row['source_ids']=['missing']
    elif fault=='snapshot':registry['snapshot_sha256']='wrong'
    elif fault=='missing_actor':row['approval']['approved_by']=''
    else:row['ingredient_id']='sodium_hyaluronate'
    assert not expertise.approved(snap,'EVT-FT-MOIST-150')


@pytest.mark.parametrize('text',['Гиалуроновая кислота лечит акне.','Салициловая кислота 2,25% увлажняет кожу.',
                               'Феноксиэтанол 1,5% безопасен.','Гиалуроновая кислота не вызывает раздражения.'])
def test_owner_expertise_cannot_bypass_other_hard_rules(monkeypatch,text):
    snap,_=approval_fixture(monkeypatch,wording=text)
    assert not expertise.approved(snap,'EVT-FT-MOIST-150')
    assert validate_for_publication(text,TONER,ST)['verdict']=='BLOCK'


def test_approved_fragrance_and_arbitrary_descriptor():
    m=dict(TONER,nm_id='252442517',text='Крем отличный, запах резковатый')
    assert validate_for_publication('Древесно-удовый аромат.',m,ST)['verdict']=='PASS'
    assert validate_for_publication('Цитрусовый аромат.',m,ST)['verdict']=='BLOCK'
    candidate='Жаль, что аромат показался Вам резким.'
    diagnostic=validate_for_publication(candidate,m,ST)
    # Dropping the mild hedge is uncertain testimony strength, not a hard ban.
    assert diagnostic['verdict']=='WARNING'
    assert diagnostic['violations']==[dict(rule_id='V-TESTIMONY',severity='WARNING',
                                          related_fact_id=None,related_claim_id=None)]
    assert not any(v['severity']=='BLOCK' for v in diagnostic['violations'])
    permission=prepare(m,candidate,S)
    assert permission.original_policy['verdict']=='WARNING'
    assert permission.status=='READY'
    r=prepare(m,'Спасибо за отзыв.',S)
    assert 'древесно-удовый' in r.text
    # The legacy offline renderer appends a standalone guidance sentence; the
    # new rubric must flag it, even though every claim independently passes.
    assert r.quality.dimensions['naturalness']=='NEEDS_IMPROVEMENT'
    assert 'резковат' in r.text and 'понрав' in r.text
    assert 'понимаем, что' not in r.text.lower()


@pytest.mark.parametrize('text',['Крем отличный, запах резковатый','Хороший эффект, но дорого',
    'Нравится текстура, не нравится аромат','Средство нравится, но упаковка неудобная','Беру второй раз, отличный крем',
    'Крем липкий, не понравился','Тоник сушит кожу','Эффекта никакого нет'])
def test_signal_acknowledgement(text):
    m=dict(TONER,nm_id='252442517',text=text)
    p=make_plan(m,S);t=render(m,p)
    if p.human_reason: return # unchanged safety/identity dominates voice
    q=evaluate(t,m,p,validate_for_publication(t,m,ST))
    assert q.dimensions['missed_customer_signal_penalty']=='GOOD',q.reasons
    assert not t.startswith('Спасибо за отзыв.')


def test_simple_praise_short_but_substantive_context_gap_visible():
    m=dict(TONER,text='Отлично!');r=prepare(m,'Спасибо за отзыв.',S)
    assert len(r.text)<180 and len(r.quality.dimensions)==len(AXES)==15
    for t in ('Спасибо за отзыв.','Рады, что Вам понравилось.','В составе есть гиалуроновая кислота.'):
        assert evaluate(t,TONER,make_plan(TONER,S),validate_for_publication(t,TONER,ST)).verdict=='NEEDS_IMPROVEMENT'
    assert evaluate('Вы сами виноваты. Это нормально.',TONER,make_plan(TONER,S),{'verdict':'PASS'}).dimensions['unnecessary_defensiveness']=='NEEDS_IMPROVEMENT'


def test_mixed_negative_fragrance_does_not_erase_positive_product_signal():
    m=dict(TONER,nm_id='252442517',text='Крем отличный, аромат не понравился')
    p=make_plan(m,S);keys={a.key for a in p.aspects}
    assert {'product_liked','fragrance_disliked'}<=keys
    assert 'fragrance_liked' not in keys
    r=prepare(m,'Спасибо за отзыв.',S)
    assert 'само средство Вам понравилось' in r.text and 'аромат Вам не понравился' in r.text


def test_public_concentration_fact_does_not_authorize_restricted_neighbor():
    assert validate_for_publication('Гиалуроновая кислота 0,2%.',TONER,ST)['verdict']=='PASS'
    restricted=dict(TONER,nm_id='438775617')
    assert validate_for_publication('Салициловая кислота 2,25%.',restricted,ST)['verdict']=='BLOCK'


@pytest.mark.parametrize('text',['Цитрусовый аромат.','Миндальный аромат.','Аромат фруктовый.','У крема цветочный запах.'])
def test_arbitrary_fragrance_adjectives_not_grounding(text):
    assert validate_for_publication(text,dict(TONER,nm_id='252442517'),ST)['verdict']=='BLOCK'


def test_positive_experience_is_not_negated_by_name_or_price_metaphor():
    for text in ('Мне понравился крем', 'Моя любовь! Запах нежный и дорогой'):
        msg=dict(TONER,nm_id='252442517',text=text)
        r=prepare(msg,'Спасибо за отзыв.',S)
        assert r.text and not any(x in r.text.lower() for x in ('разочаров', 'замечание о цене', 'жаль'))
    scent=dict(TONER,nm_id='593111986',text='Запах хороший')
    r=prepare(scent,'Спасибо за отзыв.',S)
    assert 'аромат' in r.text.lower() and 'вишн' in r.text.lower()


def test_mixed_feedback_acknowledges_both_parts_without_advertising_complaint(monkeypatch):
    msg=dict(TONER,nm_id='252442517',text='Средство нравится, но упаковка неудобная')
    r=prepare(msg,'Спасибо за отзыв.',S)
    assert 'Рады' in r.text and 'неудобной' in r.text
    msg=dict(TONER,nm_id='252442517',text='Нравится текстура, не нравится аромат')
    r=prepare(msg,'Спасибо за отзыв.',S)
    assert 'текстуру' in r.text and 'аромат Вам не понравился' in r.text
    snap,_=approval_fixture(monkeypatch)
    msg=dict(TONER,text='Текстура приятная, но нет эффекта')
    r=prepare(msg,'Спасибо за отзыв.',snap)
    assert 'текстуру' in r.text and 'результата' in r.text
    assert 'увлажнённость' not in r.text and not r.plan.explanations
    assert r.quality.verdict=='NEEDS_IMPROVEMENT'


def test_quality_requires_a_real_answer_to_a_question():
    msg=dict(TONER,entity_type='question',text='Какой аромат у крема?',nm_id='252442517',rating=None)
    r=prepare(msg,'Здравствуйте! Спасибо за вопрос.',S)
    assert r.repaired and 'древесно-удовый' in r.text
    msg=dict(TONER,entity_type='question',text='Можно ли использовать при беременности?',rating=None)
    r=prepare(msg,'Здравствуйте! Спасибо за вопрос.',S)
    assert r.repaired and r.text==make_plan(msg,S).direct_answer
    assert r.text!='Здравствуйте! Спасибо за вопрос.'
