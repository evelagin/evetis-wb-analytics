"""Final grounding and question integrity; synthetic capability fixtures only."""
import pytest
from app.v3.grounding import unsupported_use,capabilities
from app.response_quality.core import make_plan,prepare,evaluate
from app.response_quality.language import LanguageRenderer
from app.response_quality.generation_contract import GenerationContractError,required_subjects
from app.response_quality.clarification import check_questions,elicits
from app.response_quality.expertise import matching_texts
from tests.quality.test_v31c_calibration import S,msg,gate,Model

BASE_CAPS={'generic_cosmetic_context':True,'application_areas':['face'],'use_domains':['face'],'explicit_roles':[]}
PRODUCT_USE=[
 ('makeup','Пусть пудра радует Вас и в макияже.'),
 ('makeup_removal','Средство можно использовать для снятия макияжа.'),
 ('makeup_base','Средство — база под макияж.'),
 ('body','Пусть средство радует Вас в уходе за телом.'),
 ('eye_area','Средство предназначено для области вокруг глаз.'),
 ('hair','Пусть средство радует Вас в уходе за волосами.'),
 ('lips','Средство подходит для губ.'),
 ('spf','Средство обеспечивает защиту от солнца.'),
 ('cleansing','Средство можно использовать для очищения.'),
 ('gift_suitability','Средство — универсальный подарок.'),
]
@pytest.mark.parametrize('domain,text',PRODUCT_USE)
def test_use_assignments_depend_on_verified_capability_even_in_wishes(domain,text):
    assert unsupported_use(text,BASE_CAPS)
    supplied=dict(BASE_CAPS,use_domains=BASE_CAPS['use_domains']+[domain])
    assert not unsupported_use(text,supplied)

@pytest.mark.parametrize('text',[
    'Пусть средство радует Вас.', 'Пусть средство радует Вас в уходе.',
    'Пусть средство приятно дополняет ежедневный уход.',
    'Рады, что Вы выбрали крем в подарок.', 'Пусть подарок радует!',
])
def test_sentiment_is_not_a_new_product_proposition(text):
    assert not unsupported_use(text,BASE_CAPS)
    assert gate(text,msg('Хороший крем','252442517'))['verdict']=='PASS'

@pytest.mark.parametrize('text',[
    'Пудра отлично подходит для макияжа.',
    'Пусть пудра приносит удовольствие в макияже.',
    'Энзимная пудра — основа под макияж.',
    'Пудра подходит для тела.', 'Пудра подходит для волос.',
    'Пудру можно использовать как маску.',
    'Пудра сочетается с ретинолом.', 'Средство подойдет всем.',
])
def test_independent_gate_blocks_missing_use_or_role(text):
    assert gate(text,msg('Отличная пудра','535580776'))['verdict']=='BLOCK'


def test_makeup_removal_permission_cannot_leak_into_makeup_purpose():
    caps=dict(BASE_CAPS,use_domains=['face','makeup_removal'])
    v=unsupported_use('Для снятия макияжа. Средство также подходит для макияжа.',caps)
    assert any(x['domain']=='makeup' for x in v)


def test_public_area_is_exact_not_face_implies_lips_or_eye_area():
    assert gate('Средство для лица.',msg('Отлично'))['verdict']=='PASS'
    assert gate('Средство для области вокруг глаз.',msg('Отлично'))['verdict']=='BLOCK'
    assert gate('Крем для рук и тела.',msg('Отлично','593111986'))['verdict']=='PASS'
    assert gate('Крем для тела.',msg('Отлично','252442517'))['verdict']=='BLOCK'
    c=capabilities(S,['EVT-EP-ENZYME-75'])
    assert c['application_areas']==['face'] and 'makeup' not in c['use_domains']
    assert 'cleansing' not in c['use_domains'] # internal display name isn't a public purpose claim


def test_supplied_use_cannot_waive_an_independent_claim_or_safety_rule():
    assert gate('Крем для рук лечит дерматит.',msg('Отлично','252442517'))['verdict']=='BLOCK'
    assert gate('Крем для рук подходит детям.',msg('Отлично','252442517'))['verdict']=='BLOCK'
    assert gate('Пусть крем для рук радует каждый день. Наносите два раза в день.',msg('Отлично','252442517'))['verdict']=='BLOCK'

SCOPE_NEGATIVE=[
 'Восприятие аромата всегда индивидуально.',
 'Ароматы воспринимаются по-разному.',
 'Жаль, что аромат не понравился: его восприятие у каждого своё.',
]
@pytest.mark.parametrize('text',SCOPE_NEGATIVE)
@pytest.mark.parametrize('nm',['535581674','252442517'])
def test_global_a1_not_a_case_capability_and_rejected_at_admission(text,nm):
    # Hand cream has A1 globally, but this simple-praise plan supplies none.
    m=msg('Средство хорошее',nm);p=make_plan(m,S);assert not p.explanations
    model=Model(text);renderer=LanguageRenderer(model)
    with pytest.raises(GenerationContractError):renderer(m,p)
    assert any(v['rule_id']=='GENERATION_CAPABILITY_SCOPE' for v in renderer.contract_checks)
    payload=model.payload
    assert payload['case_contract']['knowledge_mode']=='CLOSED_WORLD'
    assert not payload['selected_expertise'] and not payload['optional_background']
    assert 'индивидуаль' not in model.system
    assert 'индивидуаль' not in str(payload)


def test_provided_a1_retains_scope_and_final_protection():
    m=msg('Крем отличный, запах резковатый','252442517');p=make_plan(m,S)
    text='Рады, что крем Вам понравился. Жаль, что аромат оказался для Вас резковатым: его восприятие у каждого своё.'
    renderer=LanguageRenderer(Model(text))
    assert renderer(m,p)==text and not renderer.contract_checks
    assert gate(text,m)['verdict']=='PASS'
    assert gate(text,msg('Крем отличный, запах резковатый'))['verdict']=='BLOCK'


def test_failed_generation_contract_cannot_make_ready():
    m=msg('Средство хорошее');renderer=LanguageRenderer(Model(SCOPE_NEGATIVE[0]))
    r=prepare(m,'В составе 2,25% кислоты.',S,render=renderer,force_generation=True)
    assert r.metadata()['operator_state']=='BLOCK' and r.text is None
    assert any(v['rule_id']=='GENERATION_CAPABILITY_SCOPE' for v in r.final_policy['violations'])

WISH_POSITIVE=[
 'Пусть средство приятно дополняет ежедневный уход.',
 'Пусть тоник будет приятным дополнением ежедневного ухода.',
 'Пусть уход радует Вас каждый день.',
 'Пусть средство будет приятной частью ежедневного ухода.',
]
@pytest.mark.parametrize('text',WISH_POSITIVE)
def test_wish_modality_affective_role(text):assert gate(text)['verdict']=='PASS'

WISH_NEGATIVE=[
 'Используйте ежедневно.', 'Наносите каждый день.', 'Применяйте ежедневно.',
 'Рекомендуем использовать каждый день.',
 'Пусть средство приятно дополняет ежедневный уход: наносите каждый день.',
 'Пусть средство приятно дополняет ежедневный уход и лечит акне.',
 'Пусть пудра приятно дополняет ежедневный макияж.',
]
@pytest.mark.parametrize('text',WISH_NEGATIVE)
def test_wish_does_not_authorize_instruction_or_use(text):assert gate(text)['verdict']=='BLOCK'

QUESTION_KEEP=[
 ('Понимаем Ваше впечатление.','Липкость прошла спустя время или сохранялась на коже?'),
 ('После тоника у Вас появилось ощущение сухости.','Оно возникает сразу после нанесения или сохраняется позже?'),
 ('Жаль, что Вы ощущаете липкость.','Она прошла или осталась?'),
 ('Понимаем Ваше впечатление.','Как долго держалась липкость?'),
 ('Понимаем Ваше впечатление.','Когда появляется ощущение сухости?'),
 ('После тоника появилось ощущение сухости.','Это ощущение появилось сразу или позже?'),
]
@pytest.mark.parametrize('context,question',QUESTION_KEEP)
def test_question_timing_semantic_intent_and_antecedent_retained(context,question):
    text=context+' '+question;result,events=check_questions(text,[{'unknown':'sensation_timing'}])
    assert result==text and not events

QUESTION_REMOVE=[
 ('Ощущается липкость.','Что сейчас важнее, цена или липкость?'),
 ('Ощущается липкость.','Какое неудобство из-за липкости оказалось главным?'),
 ('Ощущается липкость.','Где липкость ощущается сразу?'),
 ('Ощущается липкость.','Вам нравится аромат?'),
 ('Ощущается липкость.','Какого результата Вы ожидали?'),
 ('Понравилась сыворотка.','Она появилась сразу или осталась позже?'),
 ('Вы ощущаете сухость. Кожа Вам понравилась.','Она ощущается сразу или сохраняется позже?'),
]
@pytest.mark.parametrize('context,question',QUESTION_REMOVE)
def test_irrelevant_or_ambiguous_question_removed_without_rewrite(context,question):
    result,events=check_questions(context+' '+question,[{'unknown':'sensation_timing'}])
    assert result==context and len(events)==1 and events[0]['removed']==question

@pytest.mark.parametrize('text',[
 'Ужасный! Липкий! Да еще и подешевел в 2 раза!',
 'Не понравился: липкий и дорогой товар.',
 'Крем липкий, аромат не понравился.',
 'Средство не понравилось, запах резковатый.',
 'Средство не понравилось, дозатор слишком туго нажимается.',
])
def test_already_known_complaints_are_not_generic_unknowns(text):
    assert not make_plan(msg(text,'535581675'),S).clarifications


def test_real_unresolved_unknowns_and_fixed_routes_preserved():
    for text,unknown in [('Липко','sensation_timing'),('Тоник сушит кожу','sensation_timing'),('Не понравился','specific_difficulty'),('Неудобный дозатор','specific_difficulty'),('Эффекта нет','expected_result')]:
        assert make_plan(msg(text,'535581674'),S).clarifications[0]['unknown']==unknown
    assert not make_plan(msg('Ожидала увлажнения, эффекта нет'),S).clarifications
    assert make_plan(msg('Крышка лопнула'),S).direct_answer

SUBJECT_CASES=[
 ('Сыворотка помогает поддерживать увлажнённость кожи.',True),
 ('Средство помогает поддерживать увлажнённость кожи.',True),
 ('Сыворотка, которая помогает поддерживать увлажнённость кожи.',True),
 ('Сыворотка понравилась. Она помогает поддерживать увлажнённость кожи.',False),
 ('Кожа после сыворотки нежная. Она помогает поддерживать увлажнённость кожи.',False),
 ('Крем помогает поддерживать увлажнённость кожи.',False),
 ('Формула помогает поддерживать увлажнённость кожи.',False),
 ('Он помогает поддерживать увлажнённость кожи.',False),
 ('Кожа нежная, формула понравилась. Она помогает поддерживать увлажнённость кожи.',False),
]
@pytest.mark.parametrize('text,expected',SUBJECT_CASES)
def test_explicit_required_subject_not_ambiguous_or_foreign(text,expected):
    m=msg('Сыворотка понравилась, кожа нежная','305101272');p=make_plan(m,S)
    row=next(r for r in p.explanations if r.get('owner_key')=='У2')
    assert bool(matching_texts(row,text))==expected
    assert (gate(text,m)['verdict']!='BLOCK')==expected
    assert required_subjects(row)==['средство','сыворотка']


def test_ingredient_subject_cannot_be_reassigned_to_product_or_other_ingredient():
    m=msg('Тоник очень мягкий');p=make_plan(m,S);row=next(r for r in p.explanations if r.get('owner_key')=='У1')
    assert required_subjects(row)==['гиалуроновая кислота']
    good='В формуле есть гиалуроновая кислота, которая помогает поддерживать увлажнённость кожи.'
    assert matching_texts(row,good) and gate(good,m)['verdict']=='PASS'
    for text in ['Средство помогает поддерживать увлажнённость кожи.','Глицерин помогает поддерживать увлажнённость кожи.','Кислота и кожа упомянуты. Она помогает поддерживать увлажнённость кожи.','Гиалуроновая кислота обеспечивает мягкость.']:
        assert not matching_texts(row,text) and gate(text,m)['verdict']=='BLOCK'

MIXED_POSITIVE=[
 'Рады, что текстура Вам понравилась, и жаль, что аромат не подошёл.',
 'Вы оценили приятную текстуру, но аромат Вам не понравился — жаль, что впечатление получилось неоднозначным.',
]
@pytest.mark.parametrize('text',MIXED_POSITIVE)
def test_mixed_acknowledges_customer_plus_not_importance(text):
    m=msg('Текстура приятная, но аромат не понравился','252442517');p=make_plan(m,S)
    q=evaluate(text,m,p,{'verdict':'PASS'})
    assert 'missing_aspect:texture' not in q.reasons

@pytest.mark.parametrize('text',[
 'Приятная текстура важна, но аромат Вам не понравился.',
 'Мы ценим приятную текстуру. Жаль, что аромат не понравился.',
 'Жаль, что аромат Вам не понравился.',
])
def test_generic_brand_commentary_not_positive_observation(text):
    m=msg('Текстура приятная, но аромат не понравился','252442517');p=make_plan(m,S)
    q=evaluate(text,m,p,{'verdict':'PASS'})
    assert 'missing_aspect:texture' in q.reasons
