"""Semantic distinctions from observed failures; adversarial neighbours stay BLOCK."""
import json
from types import SimpleNamespace
import pytest
from app.v3.snapshot import load_snapshot
from app.response_quality.core import make_plan,evaluate
from app.response_quality.language import LanguageRenderer,SYSTEM
from app.response_quality.expertise import matching_texts
from app.services.publication_policy import validate_for_publication
S=load_snapshot();ST=SimpleNamespace(v3_knowledge_snapshot_id=S.snapshot_id)
def msg(text='Эффекта нет',nm='535581674'):
    return dict(text=text,nm_id=nm,entity_type='review',rating=5)
def gate(t,m=None):return validate_for_publication(t,m or msg(),ST,include_spans=True)

@pytest.mark.parametrize('t',[
    'Это поможет нам лучше разобраться в вашем опыте.',
    'Это поможет нам понять ваши ожидания.',
    'Липкость сохраняется в течение дня?',
    'Вы ощущаете липкость сразу или она сохраняется в течение дня?',
])
def test_process_or_sensation_question_not_product_or_service_promise(t):
    assert gate(t)['verdict']=='PASS'

@pytest.mark.parametrize('t',[
    'Крем поможет нам лучше разобраться в вашем опыте.',
    'Это поможет нам лучше разобраться в вашем опыте и увлажняет кожу.',
    'Это поможет нам лучше разобраться в вашем опыте. Крем увлажняет кожу.',
    'Крем помогает защищать кожу от сухости.',
    'Гиалуроновая кислота помогает устранить морщины.',
    'Мы решим обращение в течение дня?',
    'Доставка сохраняется в течение дня?',
    'Липкость пройдет в течение дня.',
    'Липкость исчезнет в течение дня?',
    'Компенсация за липкость в течение дня?',
    'Липкость сохраняется в течение дня? Мы компенсируем покупку.',
])
def test_neighbour_claims_and_promises_still_block(t):
    assert gate(t)['verdict']=='BLOCK'

@pytest.mark.parametrize('t',[
    'Вы отметили, что крем быстро впитывается без липкости.',
    'Рады, что крем у Вас быстро впитывается без липкости.',
])
def test_texture_attributed_to_buyer_is_not_universal_benefit(t):
    assert gate(t,msg('Крем быстро впитывается без липкости','930334396'))['verdict']=='PASS'

@pytest.mark.parametrize('t',[
    'Крем быстро впитывается без липкости.',
    'Вы отметили, что крем всегда быстро впитывается без липкости у всех.',
    'Вы отметили, что гиалуроновая кислота увлажняет кожу.',
    'Вы отметили, что крем быстро впитывается без липкости и лечит акне.',
    'Вы отметили, что крем содержит салициловую кислоту 2,25%.',
])
def test_attribution_is_not_blanket_waiver(t):
    assert gate(t,msg('Крем приятный','438775617'))['verdict']=='BLOCK'

class Model:
    def __init__(self,t='Спасибо за Ваш выбор!'):self.t=t;self.payload=None;self.system=None
    def structured_timed(self,system,user,name,schema):
        self.system=system;self.payload=json.loads(user)
        return {'text':self.t},{},0,'fake'

def test_case_capabilities_and_no_global_a1_instruction():
    p=make_plan(msg('Крем отличный, запах резковатый'),S);llm=Model()
    LanguageRenderer(llm)(msg('Крем отличный, запах резковатый'),p)
    assert llm.payload['verified_product_types']==['tonic']
    assert not llm.payload['selected_expertise'] and not llm.payload['optional_background']
    assert 'индивидуаль' not in llm.system and 'парфюмерн' not in llm.system
    assert 'owner_key' not in json.dumps(llm.payload) and 'nm_id' not in json.dumps(llm.payload)

@pytest.mark.parametrize('nm,text,key',[
    ('535581674','Тоник отличный, очень мягкий','У1'),
    ('305101272','Сыворотка понравилась, кожа нежная','У2'),
    ('252442517','Постоянно беру крем, руки ухоженные','У3'),
    ('252442517','Крем отличный, запах резковатый','А1'),
])
def test_selected_expertise_is_execution_obligation(nm,text,key):
    m=msg(text,nm);p=make_plan(m,S);row=next(r for r in p.explanations if r.get('owner_key')==key)
    assert row['execution']=='USEFUL_AND_RELEVANT'
    llm=Model();LanguageRenderer(llm)(m,p)
    assert any(x['execution']=='USEFUL_AND_RELEVANT' for x in llm.payload['selected_expertise'])
    q=evaluate('Спасибо за отзыв! Рады Вашему выбору.',m,p,{'verdict':'PASS'})
    assert 'selected_expertise_omitted:'+row['explanation_id'] in q.reasons


def test_m1_negation_and_mixed_does_not_counter_negative():
    p=make_plan(msg('Крем быстро впитывается, не оставляет липкой и жирной плёнки','252442517'),S)
    assert any(r.get('owner_key')=='М1' for r in p.explanations)
    p=make_plan(msg('Крем быстро впитывается, но жирная плёнка остается','252442517'),S)
    assert not any(r.get('owner_key')=='М1' for r in p.explanations)


def test_question_opportunity_has_decision_value_and_is_absent_when_already_known():
    p=make_plan(msg('Эффект есть, но вначале немного липко','305101272'),S)
    assert not p.clarifications
    p=make_plan(msg('Липко','305101272'),S)
    assert p.clarifications[0]['unknown']=='sensation_timing'
    assert p.clarifications[0]['changes']
    for t in ('Отлично','Беру второй раз','Рекомендую'):
        assert not make_plan(msg(t,'252442517'),S).clarifications

@pytest.mark.parametrize('review,answer',[
    ('Беру второй раз, отличный крем','Рады, что возвращаетесь к нам и крем Вам нравится!'),
    ('Покупаю третий раз, отличный крем','Рады, что крем нравится и Вы выбираете его в третий раз!'),
    ('Беру второй раз, хороший крем','Рады, что снова выбираете проверенный фаворит!'),
    ('Отличная пудра, доставка быстрая','Рады, что пудра понравилась и заказ быстро добрался!'),
    ('Крем отличный, запах резковатый','Рады, что крем понравился. Понимаем, что аромат оказался резким.'),
])
def test_proven_coverage_variants(review,answer):
    m=msg(review,'252442517');p=make_plan(m,S)
    q=evaluate(answer,m,p,{'verdict':'PASS'})
    assert not any(x.startswith('missing_aspect:') for x in q.reasons),q.reasons
    assert q.dimensions['empathy']=='GOOD'


def test_coverage_cannot_invent_mixed_acknowledgment():
    m=msg('Отличный крем, запах резковатый','252442517');p=make_plan(m,S)
    q=evaluate('Спасибо за отзыв, возвращайтесь!',m,p,{'verdict':'PASS'})
    assert 'missing_aspect:fragrance_harsh' in q.reasons
    assert q.dimensions['empathy']=='NEEDS_IMPROVEMENT'

@pytest.mark.parametrize('meaning',[
    'Ароматы воспринимаются очень индивидуально, поэтому понимаем Ваше впечатление.',
    'Жаль, что аромат не подошёл: его восприятие действительно очень индивидуально.',
])
def test_a1_natural_paraphrases_scoped_and_no_new_claim(meaning):
    m=msg('Крем отличный, запах резковатый','252442517');p=make_plan(m,S)
    row=next(r for r in p.explanations if r.get('owner_key')=='А1')
    assert matching_texts(row,meaning)
    assert gate(meaning,m)['verdict']=='PASS'
    assert gate(meaning,msg())['verdict']=='BLOCK'
    assert gate(meaning+' Крем лечит дерматит.',m)['verdict']=='BLOCK'


def test_attribution_cannot_invent_the_buyers_experience():
    assert gate('Вы отметили, что крем быстро впитывается без липкости.',msg('Крем хороший','930334396'))['verdict']=='BLOCK'
