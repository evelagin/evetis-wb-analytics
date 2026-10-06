"""Named Phase 3.1E.4 closure controls; no corpus-ID permission."""
from copy import deepcopy
import pytest
from app.v3.direct_questions import parse_intent, ordinary_unknown_texts
from app.v3.service_premise import unsupported_statement
from tests.quality.test_v31e_routing_relevance import S, ST, msg, hp
from app.response_quality.core import prepare, make_plan
from app.services.publication_policy import validate_for_publication
from app.v3.verifier import context_for_plan, verify

EVENT_REPORTS = [
 ('Пришёл пустой флакон', 'Жаль, что флакон оказался пустым.'),
 ('Флакон треснул', 'Жаль, что упаковка повреждена.'),
 ('Флакон протёк', 'Жаль, что средство протекло.'),
 ('В наборе не хватает сыворотки', 'Жаль, что в заказе не оказалось одной из позиций набора.'),
 ('Пришёл другой крем', 'Жаль, что Вы получили не тот заказ.'),
 ('Дозатор не работает', 'Жаль, что дозатор не работает.'),
 ('Лопнула крышка', 'Жаль, что покупка пришла с лопнувшей крышкой.'),
]
@pytest.mark.parametrize('source,candidate', EVENT_REPORTS)
def test_service_same_event(source, candidate):
 assert unsupported_statement(candidate,source,S.policy) is None
 p=hp(msg(source,nm='252442517'))
 ctx=context_for_plan(p,S)
 assert ctx.customer_experience and ctx.service_premises
 assert verify(p.deterministic_text,ctx,S).verdict!='BLOCK'
 assert validate_for_publication(candidate,msg(source,nm='252442517'),ST)['verdict']!='BLOCK'

@pytest.mark.parametrize('source,candidate',[
 ('Флакон пустой','Жаль, что Вы получили не тот заказ.'),
 ('В наборе не хватает сыворотки','Жаль, что Вы получили не тот заказ.'),
 ('Пришёл другой крем','Жаль, что в наборе не хватает сыворотки.'),
 ('Лопнула крышка','Жаль, что в заказе не оказалось одной из позиций набора.'),
 ('Дозатор не работает','Жаль, что флакон протёк.'),
 ('Флакон протёк','Жаль, что флакон оказался пустым.'),
 ('Флакон пустой','Жаль, что упаковка повреждена.'),
])
def test_service_event_cannot_substitute(source,candidate):
 assert unsupported_statement(candidate,source,S.policy)
 v=validate_for_publication(candidate,msg(source,nm='252442517'),ST)
 assert v['verdict']=='BLOCK' and any(x['rule_id']=='V-SERVICE-PREMISE' for x in v['violations'])

@pytest.mark.parametrize('q',[
 'Можно использовать на тело?', 'Можно применять на лицо?', 'Можно наносить на руки?',
 'Можно мазать тело?', 'Можно пользоваться для тела?', 'Подойдёт для рук?',
 'Можно на лицо?', 'Можно на тело?', 'Можно для рук?',
])
def test_shared_intended_use_grammar(q):
 i=parse_intent(q)
 assert i.intent_type=='INTENDED_USE' and i.application_areas
 m=msg(q,question=True,nm='252442517');p=hp(m)
 assert any(r.fact_type=='intended_use' for r in p.resolved)
 r=prepare(m,'',S,force_generation=True)
 assert r.status=='READY'
 assert validate_for_publication(r.text,m,ST)['verdict']!='BLOCK'

@pytest.mark.parametrize('q,unit,word',[
 ('Сколько капель тоника использовать?','DROPS','капель'),
 ('Сколько капель тоника?','DROPS','капель'),
 ('Сколько нажатий нужно?','PUMPS','нажатий'),
 ('Сколько средства?','GENERIC_AMOUNT','средства'),
 ('Какое количество наносить?','GENERIC_AMOUNT','средства'),
 ('Сколько наносить?','GENERIC_AMOUNT','средства'),
])
def test_direct_unknown_preserves_unit(q,unit,word):
 i=parse_intent(q);assert i.intent_type=='AMOUNT' and i.unit_kind==unit
 m=msg(q,question=True,nm='535581674');p=hp(m)
 assert p.question_intent.unit_kind==unit and p.question_intent.resolution=='UNKNOWN'
 r=prepare(m,'',S,force_generation=True)
 assert r.status=='READY' and word in r.text
 assert r.text in ordinary_unknown_texts(S,p.product_ids,q)
 if unit=='DROPS':assert 'нажат' not in r.text

@pytest.mark.parametrize('q',['Как часто?', 'Сколько раз?', 'Раз в день?', 'Каждый день?', 'Ежедневно?', 'Я наношу каждый день, это нормально?'])
def test_frequency_question_is_not_an_instruction(q):
 assert parse_intent(q).intent_type=='FREQUENCY'
 m=msg(q,question=True,nm='535581674');r=prepare(m,'',S,force_generation=True)
 assert r.status=='READY' and 'не указана' in r.text
 assert validate_for_publication(r.text+' Наносите каждый день.',m,ST)['verdict']=='BLOCK'
 assert not parse_intent(q,is_question=False).fact_types

def test_known_amount_stays_verified_not_denied():
 s=deepcopy(S);pid='EVT-FS-MOIST-30'
 # A hypothetical documented amount is tested under a compatible offline policy;
 # the real immutable registry deliberately retains its unknown-amount rule.
 s.policy['policy_unknown_facts'].pop('amount',None)
 s.products[pid]['facts'].append({'fact_id':'closure.amount','fact_type':'amount','fact_status':'VERIFIED',
  'disclosure_policy':'PUBLIC','source_id':s.products[pid]['primary_source'],'value':'1',
  'customer_value_ru':'На одно применение указана одна капля.'})
 m=msg('Сколько капель использовать?',question=True,nm='305101272')
 p=make_plan(m,s)
 assert not p.human_reason and 'одна капля' in p.direct_answer and 'не указано' not in p.direct_answer

@pytest.mark.parametrize('source,answer',[
 ('Приятный аромат','Вы отметили приятный аромат, у крема приятный аромат.'),
 ('Приятный аромат','Вы отметили приятный аромат, и у крема приятный аромат.'),
 ('Приятный аромат','Вам понравился аромат, он очень стойкий.'),
 ('Лёгкий аромат','Вы отметили лёгкий аромат, поэтому он подходит всем.'),
 ('Приятный аромат','Вы отметили приятный аромат; у крема приятный аромат.'),
])
def test_neighboring_brand_proposition_needs_independent_grounding(source,answer):
 assert validate_for_publication(answer,msg(source,nm='252442517'),ST)['verdict']=='BLOCK'

@pytest.mark.parametrize('answer',[
 'Рады, что аромат Вам понравился.',
 'Вы отметили приятный аромат.',
 'Вы отметили приятный аромат, его восприятие индивидуально.',
 'Рады, что аромат Вам понравился, и спасибо, что поделились впечатлением.',
])
def test_commas_are_not_a_blanket_block(answer):
 assert validate_for_publication(answer,msg('Приятный аромат',nm='252442517'),ST)['verdict']!='BLOCK'
