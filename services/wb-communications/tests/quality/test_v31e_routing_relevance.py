"""Development regression by semantic family, never new holdout evidence."""
from types import SimpleNamespace
from copy import deepcopy
from dataclasses import replace
import pytest
from app.v3.snapshot import load_snapshot
from app.v3.classifier import classify_rules
from app.v3.resolver import resolve
from app.v3.planner import plan
from app.v3.verifier import verify,context_for_plan,context_for_free_text
from app.services.publication_policy import validate_for_publication
from app.response_quality.core import make_plan,prepare,evaluate
from app.response_quality.brand_voice import repetition_diagnostics
from app.v3.direct_questions import diagnostics,ordinary_unknown_texts,UNKNOWN_WORDING

S=load_snapshot()
ST=SimpleNamespace(v3_knowledge_snapshot_id=S.snapshot_id)
def msg(text,*,question=False,nm='305101272'):
    return {'text':text,'nm_id':nm,'entity_type':'question' if question else 'review','rating':None if question else 3}
def hp(m):return plan(S,resolve(S,nm_id=m['nm_id'],marketplace='wb'),classify_rules(m,S),m)

@pytest.mark.parametrize('text',[
 'После сыворотки появились пузыри и сильная боль; кожа словно обожжена.',
 'Сильно жжёт после нанесения, на коже появились пузыри.',
 'Кожа как после ожога и очень болит.',
 'После нанесения на лице волдыри.',
 'На коже мокнущее повреждение после крема.',
 'На коже blister-like lesions после сыворотки.',
])
def test_cutaneous_injury_never_reaches_language_generation(text):
 m=msg(text);p=make_plan(m,S)
 assert p.human_reason=='SAFETY_ESCALATION'
 def forbidden(*a,**k):raise AssertionError('serious safety reached model')
 r=prepare(m,'Спасибо за отзыв.',S,render=forbidden,force_generation=True)
 assert r.status=='HUMAN_REVIEW' and r.text is None
 assert validate_for_publication('Спасибо за отзыв.',m,ST)['verdict']=='BLOCK'
 assert classify_rules(m,S).safety.risk_level=='R3'

@pytest.mark.parametrize('text',['Немного жжёт после нанесения','Слегка пощипывает кожу','Небольшое жжение при нанесении'])
def test_mild_discomfort_stays_existing_non_emergency_branch(text):
 m=msg(text);a=classify_rules(m,S).safety
 assert a.route=='S1' and not a.emergency_markers
 assert hp(m).strategy=='SAFETY_TEMPLATE'
 assert prepare(m,'',S,force_generation=True).status=='READY'

@pytest.mark.parametrize('text',[
 'На коже нет пузырей и ожога после нанесения.',
 'Пузыри в упаковке при перевозке; коробка целая.',
])
def test_cutaneous_context_and_negation_are_required(text):
 assert not any(e.active and e.cutaneous_injury for e in classify_rules(msg(text),S).safety.events)

@pytest.mark.parametrize('text',[
 'После крема опух язык и трудно дышать.',
 'Пудра попала в глаза, сильная боль и хуже вижу.',
 'После сыворотки потеряла сознание.',
 'Ребёнок проглотил тоник и его тошнит.',
])
def test_closed_emergency_markers_remain_urgent(text):
 a=classify_rules(msg(text),S).safety
 assert a.emergency_markers and a.route=='SAFETY_URGENT' and a.risk_level=='R4'

@pytest.mark.parametrize('text',[
 'Можно ли использовать крем для тела вместо крема для лица?',
 'Наносить сыворотку вместо крема или вместе?',
 'Если дозатор сломается, будет ли замена?',
 'Флакон не протёк, упаковка без повреждений.',
])
def test_tokens_do_not_prove_service_event(text):
 assert hp(msg(text,question='?' in text,nm='593111985')).strategy!='SERVICE'

@pytest.mark.parametrize('text',[
 'Заказывала крем для лица, а вместо него пришёл крем для тела.',
 'Вместо дозатора пришла сломанная крышка.',
 'Помпа не нажимается; крем не выходит.',
 'Коробку привезли сильно смятой.',
 'Флакон протёк ещё до первого использования.',
 'В полученном наборе не хватает крема.',
])
def test_service_requires_reported_premise(text):
 p=hp(msg(text,nm='593111985'))
 assert p.strategy=='SERVICE' and p.service_premises
 assert all(s['service_premise']=='CUSTOMER_REPORTED' for s in p.service_premises)

def test_usage_question_cannot_publish_fabricated_delivery_event():
 m=msg('Можно ли использовать амбровый крем на лице вместо крема для лица?',question=True,nm='593111985')
 r=prepare(m,'Жаль, что Вы получили не тот заказ.',S,force_generation=True)
 assert r.plan.route!='SERVICE' and 'получили' not in (r.text or '')
 v=validate_for_publication('Жаль, что Вы получили не тот заказ, на который рассчитывали.',m,ST)
 assert v['verdict']=='BLOCK' and any(x['rule_id']=='V-SERVICE-PREMISE' for x in v['violations'])

@pytest.mark.parametrize('source,candidate',[
 ('Аромат для меня очень насыщенный, и именно такой мне нравится.','Здорово, что насыщенный аромат оказался именно в Вашем вкусе.'),
 ('Амбровый запах на мне ощущается слишком тяжёлым.','Понимаем, что амбровый аромат оказался для Вас слишком тяжёлым.'),
 ('Мне смолистый запах показался необычным.','Понимаем, что смолистый запах показался Вам необычным.'),
])
def test_personal_fragrance_description_is_customer_testimony(source,candidate):
 v=validate_for_publication(candidate,msg(source,nm='252442517'),ST)
 assert v['verdict']!='BLOCK'

@pytest.mark.parametrize('source,candidate',[
 ('Крем понравился.','У крема очень насыщенный тяжёлый аромат.'),
 ('Запах лёгкий.','Рады, что насыщенный аромат оказался в Вашем вкусе.'),
 ('Мне насыщенный аромат понравился.','Вы отметили аромат, а крем всегда имеет насыщенный запах.'),
 ('Может ли запах быть смолистым?','Рады, что смолистый запах оказался в Вашем вкусе.'),
])
def test_attribution_cannot_license_unsourced_or_universal_scent(source,candidate):
 assert validate_for_publication(candidate,msg(source,nm='252442517'),ST)['verdict']=='BLOCK'

def test_grounded_negative_fact_uses_same_planner_and_publication_contract():
 m=msg('Есть ли отдушка в тонике?',question=True,nm='535581674');p=hp(m)
 answer='В составе, указанном производителем, нет компонента «отдушка».'
 assert any(r.state=='KNOWN_ALLOWED' and r.customer_value_ru==answer for r in p.resolved)
 ctx=context_for_plan(p,S)
 fact=next(r for r in p.resolved if r.customer_value_ru==answer)
 assert 'parfum' not in ctx.allowed_ingredient_ids
 assert ctx.negative_fact_permissions[0]['fact_ids']==fact.fact_ids
 assert ctx.negative_fact_permissions[0]['source_ids']==fact.source_ids
 assert verify(answer,ctx,S).verdict!='BLOCK'
 assert validate_for_publication(answer,m,ST)['verdict']!='BLOCK'

FORBIDDEN_NEGATIVE_VARIANTS=[
 'В составе есть отдушка.',
 'Без отдушек, гипоаллергенно.',
 'Подходит аллергикам.',
 'Не содержит раздражителей.',
 'Без отдушек.',
 'Средство без ароматизаторов.',
 'Не содержит раздражающих отдушек.',
]
@pytest.mark.parametrize('answer',FORBIDDEN_NEGATIVE_VARIANTS,ids=FORBIDDEN_NEGATIVE_VARIANTS)
def test_each_forbidden_negative_variant_is_independently_blocked(answer):
 m=msg('Есть ли отдушка в тонике?',question=True,nm='535581674')
 assert verify(answer,context_for_plan(hp(m),S),S).verdict=='BLOCK',answer
 assert validate_for_publication(answer,m,ST)['verdict']=='BLOCK',answer

@pytest.mark.parametrize('answer',[
 'В указанном INCI отсутствует PARFUM. Средство без ароматизаторов.',
 'В указанном INCI отсутствует PARFUM. Не содержит раздражающих отдушек.',
])
def test_grounded_absence_cannot_become_general_fragrance_free_marketing(answer):
 m=msg('Есть ли отдушка в тонике?',question=True,nm='535581674')
 assert verify(answer,context_for_plan(hp(m),S),S).verdict=='BLOCK',answer
 assert validate_for_publication(answer,m,ST)['verdict']=='BLOCK',answer

def test_positive_ingredient_presence_contract_is_unchanged():
 candidates=[]
 for pid,product in sorted(S.products.items()):
  if product.get('kind')!='single' or product.get('customer_fact_generation')!='ALLOWED':continue
  if S.reconciliation(pid) not in {'MATCH','ORDER_ONLY','LABEL_UNAVAILABLE'}:continue
  row=next((r for r in S.ingredient_rows(pid) if r['ingredient_id']=='parfum'
            and r.get('fact_status')=='VERIFIED' and r.get('extraction_confidence')=='HIGH'
            and r.get('concentration_disclosure_policy') in {'PUBLIC','PUBLIC_WITH_APPROVED_WORDING'}),None)
  ident=next((i for i in S.data['identifiers'] if i['product_id']==pid
              and i['id_type']=='wb_nm_id' and i['status']=='VERIFIED'),None)
  if not row or not ident:continue
  if resolve(S,nm_id=ident['value'],marketplace='wb').status=='VERIFIED':
   candidates.append((ident,row))
 assert candidates,'No verified customer-disclosable positive PARFUM fixture in snapshot'
 ident,row=candidates[0]
 m=msg('Есть ли отдушка в составе?',question=True,nm=ident['value'])
 p=hp(m);fact=next(r for r in p.allowed if r.subject=='parfum')
 assert fact.state=='KNOWN_ALLOWED' and fact.source_ids==[row['source_id']]
 assert fact.fact_ids==[ident['product_id']+'.ingredient.recipe.parfum']
 ctx=context_for_plan(p,S)
 assert 'parfum' in ctx.allowed_ingredient_ids and not ctx.negative_fact_permissions
 assert verify(fact.customer_value_ru,ctx,S).verdict!='BLOCK'
 assert validate_for_publication(fact.customer_value_ru,m,ST)['verdict']!='BLOCK'

@pytest.mark.parametrize('answer',[
 'В указанном INCI отсутствует PARFUM.',
 'В указанном производителем составе нет отдушки.',
])
def test_bounded_absence_variants_share_the_selected_proposition(answer):
 m=msg('Есть ли отдушка в тонике?',question=True,nm='535581674')
 assert verify(answer,context_for_plan(hp(m),S),S).verdict!='BLOCK'
 assert validate_for_publication(answer,m,ST)['verdict']!='BLOCK'

@pytest.mark.parametrize('answer',[
 'В указанном INCI отсутствует PARFUM. В составе есть отдушка.',
 'В указанном INCI отсутствует PARFUM. Без отдушек.',
 'В указанном INCI отсутствует PARFUM. Не содержит аллергенов.',
 'В указанном INCI отсутствует PARFUM. Гипоаллергенно.',
 'В указанном INCI отсутствует PARFUM. Не вызывает раздражения.',
 'В указанном INCI отсутствует PARFUM. Подходит чувствительной коже.',
 'В указанном INCI отсутствует PARFUM и любые аллергены.',
 'В указанном INCI отсутствует PARFUM, поэтому подходит чувствительной коже.',
])
def test_absence_does_not_license_positive_presence_or_marketing(answer):
 m=msg('Есть ли отдушка в тонике?',question=True,nm='535581674')
 assert verify(answer,context_for_plan(hp(m),S),S).verdict=='BLOCK'
 assert validate_for_publication(answer,m,ST)['verdict']=='BLOCK'

@pytest.mark.parametrize('corruption',['not_selected','unknown','no_source','wrong_fact','wrong_product'])
def test_negative_permission_requires_same_selected_product_fact_and_source(corruption):
 p=hp(msg('Есть ли отдушка в тонике?',question=True,nm='535581674'))
 answer='В указанном INCI отсутствует PARFUM.'
 if corruption=='not_selected':p.resolved=[]
 elif corruption=='unknown':p.resolved=[replace(r,state='UNKNOWN') for r in p.resolved]
 elif corruption=='no_source':p.resolved=[replace(r,source_ids=[]) for r in p.resolved]
 elif corruption=='wrong_fact':p.resolved=[replace(r,fact_ids=['invented']) for r in p.resolved]
 else:p.product_ids=['EVT-HC-HAND-300']
 assert verify(answer,context_for_plan(p,S),S).verdict=='BLOCK'
 assert validate_for_publication(answer,msg('Отдушка?',question=True,nm='252442517'),ST)['verdict']=='BLOCK'

@pytest.mark.parametrize('question,needle',[
 ('От чего зависит восприятие вишнёвого аромата: от температуры кожи или её типа?','Подтверждённых данных'),
 ('Будет ли запах держаться до утра?','Подтверждённых данных'),
 ('Правда ли, что запах нравится почти всем покупателям?','Подтверждённых данных'),
 ('Сколько нажатий дозатора нужно на руки и как часто повторять нанесение?','не указано'),
 ('Сколько нажатий нужно?','не указано'),
 ('Как часто использовать?','не указана'),
])
def test_unknown_exact_question_is_answered_before_adjacent_context(question,needle):
 r=prepare(msg(question,question=True,nm='252442517'),'',S,force_generation=True)
 assert r.status=='READY' and needle in r.text
 assert 'предназначен(а)' not in r.text
 for invented in [' Наносите два раза в день.',' Аромат держится до утра.',' Достаточно двух нажатий.']:
  assert validate_for_publication(r.text+invented,msg(question,question=True,nm='252442517'),ST)['verdict']=='BLOCK'

@pytest.mark.parametrize('text,nm',[
 ('Будет ли запах держаться до утра?','000000'),
 ('Сколько нажатий нужно для набора?','930334396'),
 ('На коже появились волдыри и сильная боль. Как часто использовать?','252442517'),
 ('Буду обращаться в суд. Как часто использовать?','252442517'),
])
def test_unknown_cannot_escape_unresolved_product_bundle_safety_or_legal(text,nm):
 m=msg(text,question=True,nm=nm)
 assert make_plan(m,S).human_reason
 def forbidden(*a,**k):raise AssertionError('special route reached generation')
 assert prepare(m,'',S,render=forbidden,force_generation=True).status=='HUMAN_REVIEW'
 pid=S.product_by_identifier('wb_nm_id',nm)
 assert ordinary_unknown_texts(S,[pid] if pid else [],text)==[]

@pytest.mark.parametrize('state',['CONFLICT','KNOWN_RESTRICTED'])
def test_direct_unknown_does_not_override_authoritative_conflict_or_operator_restriction(state):
 s=deepcopy(S);pid='EVT-HC-HAND-300'
 s.products[pid]['facts'].append({'fact_id':'test.frequency','fact_type':'frequency',
   'fact_status':state if state=='CONFLICT' else 'VERIFIED',
   'disclosure_policy':'PUBLIC' if state=='CONFLICT' else 'DO_NOT_DISCLOSE_PENDING',
   'conflict_id':'test.conflict','source_id':s.products[pid]['primary_source']})
 m=msg('Как часто использовать?',question=True,nm='252442517')
 assert make_plan(m,s).human_reason
 assert ordinary_unknown_texts(s,[pid],m['text'])==[]
 ctx=context_for_free_text(s,[pid]);ctx.customer_experience=m['text']
 assert verify(UNKNOWN_WORDING['amount'],ctx,s).verdict=='BLOCK'

def test_unknown_denial_cannot_grant_a_product_instruction():
 m=msg('Как часто использовать?',question=True,nm='252442517')
 for answer in ['Наносите утром и вечером.','Подтверждённых данных о количестве нет, но наносите две капли.']:
  assert validate_for_publication(answer,m,ST)['verdict']=='BLOCK'

def bundle_nm(pid):
 return next(i['value'] for i in S.data['identifiers'] if i['product_id']==pid and i['id_type']=='wb_nm_id' and i['status']=='VERIFIED')

@pytest.mark.parametrize('pid,text,component',[
 ('EVT-SET-SER-CREAM-MOIST','Сколько нажатий сыворотки нужно из набора?','EVT-FS-MOIST-30'),
 ('EVT-SET-MOIST-TONIC-SERUM','Как часто использовать сыворотку из набора?','EVT-FS-MOIST-30'),
 ('EVT-SET-HAND-AMBER','Сколько нажатий крема Amber Vanilla из набора нужно?','EVT-HC-AMBER-300'),
])
def test_uniquely_named_bundle_component_retains_direct_unknown(pid,text,component):
 m=msg(text,question=True,nm=bundle_nm(pid));p=hp(m)
 assert p.strategy=='UNKNOWN_FACT' and {r.product_id for r in p.resolved}=={component}
 r=prepare(m,'',S,force_generation=True)
 assert r.status=='READY' and 'Подтвержд' in r.text
 assert validate_for_publication(r.text,m,ST)['verdict']!='BLOCK'

@pytest.mark.parametrize('text',[
 'Сколько нажатий крема из набора нужно?',
 'Как часто использовать набор?',
])
def test_ambiguous_bundle_component_is_human_before_any_renderer(text):
 m=msg(text,question=True,nm='930334396');p=hp(m)
 assert p.strategy=='HUMAN_REVIEW' and p.failure_code=='REQUIRED_FACT_UNKNOWN'
 assert all(r.reason=='BUNDLE_COMPONENT_AMBIGUOUS' for r in p.resolved)
 def forbidden(*args,**kwargs):raise AssertionError('ambiguous bundle reached renderer')
 assert prepare(m,'',S,render=forbidden,force_generation=True).status=='HUMAN_REVIEW'

def test_single_serum_amount_remains_explicit_unknown():
 m=msg('Сколько нажатий нужно?',question=True,nm='305101272')
 r=prepare(m,'',S,force_generation=True)
 assert r.status=='READY' and 'не указано' in r.text

def test_known_bundle_question_is_not_blanket_human():
 m=msg('Есть ли в наборе салициловая кислота?',question=True,nm='868597351')
 assert hp(m).strategy=='FACT_ANSWER'

@pytest.mark.parametrize('text',[
 'Точная доля салициловой кислоты равна 2,25 процента?',
 'Если точное число закрыто, назовите диапазон содержания салициловой кислоты.',
 'Какой процент салициловой кислоты в креме?',
 'Сколько процентов салициловой кислоты?',
 'Назовите концентрацию салициловой кислоты.',
 'В каком количестве содержится салициловая кислота?',
])
def test_restricted_question_explicitly_acknowledges_boundary_without_value(text):
 m=msg(text,question=True,nm='438775617');r=prepare(m,'',S,force_generation=True)
 assert any(f.state=='KNOWN_RESTRICTED' for f in hp(m).resolved)
 assert r.status=='READY' and 'не раскрываем' in r.text
 assert '2,25' not in r.text and '2.25' not in r.text

def test_public_concentration_fixture_is_not_restricted_leakage():
 m=msg('Какой процент салициловой кислоты?',question=True,nm='305101361')
 r=prepare(m,'',S,force_generation=True)
 assert r.status=='READY' and '1%' in r.text
 assert all(f.state=='KNOWN_ALLOWED' for f in hp(m).resolved)

@pytest.mark.parametrize('nm,text,needle',[
 ('305101272','Как называется продукт: это увлажняющая сыворотка или крем?','сыворотка'),
 ('535580776','Уточните вид средства: готовая пенка или порошок?','пудра'),
])
def test_known_identity_does_not_require_human(nm,text,needle):
 r=prepare(msg(text,question=True,nm=nm),'',S,force_generation=True)
 assert r.status=='READY' and needle in r.text
 assert prepare(msg(text,question=True,nm='000000'),'',S,force_generation=True).status=='HUMAN_REVIEW'

def test_mixed_service_keeps_positive_and_service_action():
 m=msg('Упаковка целая, но внутри сломан дозатор. Сам аромат мне понравился.',nm='252442517')
 r=prepare(m,'',S,force_generation=True)
 assert r.status=='READY'
 assert 'аромат Вам понравился' in r.text and 'обращение' in r.text and 'фотографии' in r.text

def test_relevance_diagnostic_is_not_policy_permission():
 m=msg('Будет ли аромат держаться до утра?',question=True,nm='252442517')
 p=make_plan(m,S);a='древесно-удовый аромат.'
 assert validate_for_publication(a,m,ST)['verdict']!='BLOCK'
 q=evaluate(a,m,p,{'verdict':'PASS'})
 assert q.verdict=='NEEDS_IMPROVEMENT' and 'ADJACENT_FACT_SUBSTITUTION' in q.reasons
 assert 'RESTRICTED_QUESTION_NOT_ACKNOWLEDGED' in diagnostics('В составе есть кислота.','Назовите концентрацию кислоты.',True)

def test_repetition_is_batch_diagnostic_not_word_ban():
 assert repetition_diagnostics(['Очень приятно, что крем Вам понравился. Пусть радует!']*5)
 assert repetition_diagnostics(['Очень приятно, что понравилось.'])==[]
 assert repetition_diagnostics(['Спасибо за повторный выбор.','Рады, что аромат понравился.','Удобство формата тоже услышали.','Благодарим за отзыв о текстуре.','Приятно узнать о покупке.'])==[]
