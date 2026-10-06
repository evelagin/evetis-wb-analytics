"""Residual distinctions and adversarial neighbours, independent publication gate."""
import pytest
from dataclasses import replace
from app.response_quality.core import make_plan,prepare,evaluate
from app.response_quality.clarification import check_questions
from app.response_quality.language import LanguageRenderer
from app.response_quality.expertise import matching_texts
from tests.quality.test_v31c_calibration import S,gate,msg,Model

@pytest.mark.parametrize('text',[
    'Это поможет точнее понять Ваше впечатление.',
    'Это поможет лучше понять Ваше впечатление.',
    'Ваш отзыв помогает нам становиться лучше.',
    'Благодарим за честный отзыв — он помогает нам становиться лучше.',
    'Спасибо за отзыв. Он помогает нам становиться лучше.',
])
def test_feedback_subject_and_experience_object_pass(text):
    assert gate(text)['verdict']=='PASS'

@pytest.mark.parametrize('text',[
    'Крем помогает нам становиться лучше.',
    'Спасибо за отзыв о креме. Он помогает нам становиться лучше.',
    'Это помогает увлажнять кожу.',
    'Это поможет точнее понять Ваше впечатление и помогает защищать кожу.',
    'Ваш отзыв помогает нам становиться лучше. Крем лечит дерматит.',
    'Он помогает поддерживать увлажнённость кожи.',
    'Гиалуроновая кислота помогает нам понять Ваше впечатление.',
    'Спасибо за отзыв. Сыворотка понравилась? Он помогает нам становиться лучше.',
])
def test_no_product_or_ingredient_efficacy_waiver(text):
    assert gate(text)['verdict']=='BLOCK'

@pytest.mark.parametrize('text',[
    'Пусть и дальше радует каждый день!',
    'Пусть средство будет приятной частью ежедневного ухода.',
    'Пусть Ваш выбор радует Вас каждый день.',
])
def test_affective_wish_passes_frequency_family_only(text):
    assert gate(text)['verdict']=='PASS'

@pytest.mark.parametrize('text',[
    'Используйте каждый день.', 'Наносите ежедневно.', 'Применяйте два раза в день.',
    'Пусть средство радует Вас: наносите ежедневно.',
    'Пусть ежедневное нанесение радует Вас.',
    'Пусть радует каждый день и увлажняет кожу.',
    'Пусть радует каждый день. Мы компенсируем покупку.',
    'Пусть средство помогает коже каждый день.',
])
def test_wish_marker_cannot_rescue_actual_advice_or_other_rules(text):
    assert gate(text)['verdict']=='BLOCK'

@pytest.mark.parametrize('review,answer',[
    ('Приятный аромат','Вы отметили приятный аромат.'),
    ('Приятно пахнет чем-то свежим','Вы отметили свежий аромат.'),
    ('Запах резковатый','Понимаем, Вы отметили резковатый запах.'),
    ('Крем хорошо впитывается','По Вашим ощущениям крем хорошо впитался.'),
    ('Приятный аромат','Рады, что аромат показался Вам приятным.'),
])
def test_customer_provenance_bounded_to_real_report(review,answer):
    assert gate(answer,msg(review,'252442517'))['verdict']=='PASS'

@pytest.mark.parametrize('review,answer',[
    ('Приятный аромат','У крема приятный аромат.'),
    ('Крем хорошо впитывается','Крем быстро впитывается.'),
    ('Крем хорошо впитывается','Вы отметили быстрое впитывание.'),
    ('Крем не быстро впитывается','Вы отметили быстрое впитывание.'),
    ('Приятный аромат','Вы отметили свежий аромат.'),
    ('Приятный аромат','Вы отметили приятный аромат, и крем быстро впитывается.'),
    ('Приятный аромат','Вы отметили приятный аромат, у крема приятный аромат.'),
    ('Приятный аромат','Вы отметили приятный аромат и крем лечит акне.'),
    ('Приятный аромат','Вы отметили приятный аромат с концентрацией 2,25%.'),
])
def test_customer_provenance_never_upgrades_or_generalizes(review,answer):
    assert gate(answer,msg(review,'252442517'))['verdict']=='BLOCK'

@pytest.mark.parametrize('answer',[
    'Жаль, что аромат не подошёл. Его восприятие у каждого человека своё.',
    'Понимаем, что аромат Вам не понравился: его восприятие у каждого может быть разным.',
])
def test_a1_meaning_requires_fragrance_referent_and_scope(answer):
    m=msg('Крем отличный, запах резковатый','252442517');p=make_plan(m,S)
    row=next(r for r in p.explanations if r.get('owner_key')=='А1')
    assert matching_texts(row,answer)
    assert gate(answer,m)['verdict']=='PASS'
    assert not matching_texts(row,'Текстура нравится. Его восприятие у каждого человека своё.')
    assert not matching_texts(row,'Аромат нравится, крем хороший. Его восприятие у каждого человека своё.')
    assert gate(answer,msg())['verdict']=='BLOCK'
    assert gate(answer+' Крем лечит акне.',m)['verdict']=='BLOCK'

@pytest.mark.parametrize('unknown,question',[
    ('expected_result','Какого результата Вы ожидали?'),
    ('specific_difficulty','Что именно затрудняет использование дозатора?'),
    ('sensation_timing','Липкость ощущается сразу или сохраняется долго?'),
])
def test_question_elicits_planned_unknown(unknown,question):
    text,corrections=check_questions('Понимаем Ваше впечатление. '+question,[{'unknown':unknown}])
    assert text.endswith(question) and not corrections

@pytest.mark.parametrize('question',[
    'Какая ситуация сейчас для Вас важнее всего?',
    'Что для Вас важнее, цена или липкость?',
    'На каком участке ощущается липкость?',
    'Какой результат Вы хотели бы получить?',
])
def test_question_must_elicit_timing_not_priority_or_other_unknown(question):
    text,corrections=check_questions('Понимаем Ваше замечание о липкости и цене. '+question,[{'unknown':'sensation_timing'}])
    assert '?' not in text and len(corrections)==1


def test_renderer_checks_actual_question_and_keeps_channels_separate():
    m=msg('Липко и дороговато','305101272');p=make_plan(m,S);model=Model('Понимаем Ваше замечание о липкости и цене. Какая ситуация сейчас для Вас важнее всего?')
    renderer=LanguageRenderer(model);text=renderer(m,p)
    assert '?' not in text and renderer.question_checks
    assert model.payload['customer_reported']['provenance']=='CUSTOMER_REPORTED'
    assert 'APPROVED_EXPERTISE' in model.system


def test_product_not_verified_blank_case_short_circuits_without_ready():
    m=msg('', '252441968');called=[]
    result=prepare(m,'Спасибо за высокую оценку!',S,render=lambda *a,**k:called.append(1),force_generation=True)
    assert not called and result.metadata()['operator_state']=='BLOCK'
    assert result.plan.block_reason=='PRODUCT_NOT_VERIFIED'


def test_safe_fallback_preserves_customer_signals_and_still_reports_missing_expertise():
    m=msg('Текстура приятная, но аромат не понравился','252442517')
    result=prepare(m,'Спасибо за отзыв.',S,render=lambda *a,**k:'Крем лечит акне.',force_generation=True)
    assert result.status=='READY' and 'CUSTOMER_SIGNALS_SAFE_FALLBACK' in result.reasons
    assert 'текстура' in result.text.lower() and 'аромат' in result.text.lower()
    assert result.final_policy['verdict']=='PASS'
    assert result.quality.verdict=='NEEDS_IMPROVEMENT' # no invented A1 to fake quality

@pytest.mark.parametrize('review,answer',[
    ('Отличный крем, рекомендую','Рады, что крем Вам нравится и Вы готовы рекомендовать его!'),
    ('Приятный аромат','Рады, что аромат покорил Вас!'),
    ('Постоянно покупаю крем, руки ухоженные','Рады, что любимый крем снова с Вами и руки выглядят ухоженно!'),
])
def test_bounded_morphology_coverage(review,answer):
    m=msg(review,'252442517');p=make_plan(m,S);q=evaluate(answer,m,p,{'verdict':'PASS'})
    assert not [r for r in q.reasons if r.startswith('missing_aspect:')],q.reasons


def test_approved_m1_is_not_a_customer_report_but_can_be_independent_context():
    m=msg('Крем хорошо впитывается','252442517')
    assert gate('Вы отметили, что крем рассчитан на быстрое впитывание без жирной плёнки.',m)['verdict']=='BLOCK'
    assert gate('Вы отметили хорошее впитывание. Крем рассчитан на быстрое впитывание без жирной плёнки.',m)['verdict']=='PASS'
    assert gate('Вы отметили хорошее впитывание, а крем рассчитан на быстрое впитывание без жирной плёнки.',m)['verdict']=='PASS'
