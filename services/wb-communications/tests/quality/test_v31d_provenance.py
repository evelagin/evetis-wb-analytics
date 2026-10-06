"""Source classes and relational strengthening, not corpus-id exceptions."""
import pytest
from app.v3.provenance import evidence_features,testimony_violations as strengthening_violations,feature_observations,audit,feedback_process
from app.v3.verifier import context_for_free_text,verify
from app.response_quality.expertise import approved,in_context,matching_texts
from tests.quality.test_v31c_calibration import S,msg,gate

@pytest.mark.parametrize('link',[
 'это совпало с Вашим опытом', 'это соответствует Вашим ощущениям',
 'Вы это почувствовали', 'именно так Вы и отметили',
])
@pytest.mark.parametrize('separator',[' — ','. '])
@pytest.mark.parametrize('observed',['Хорошо впитывается','Крем понравился','Не быстро впитывается'])
def test_expertise_does_not_strengthen_customer_testimony_through_anaphora(link,observed,separator):
 t='Крем рассчитан на быстрое впитывание без жирной плёнки'+separator+link+'.'
 assert strengthening_violations(t,observed)
 assert gate(t,msg(observed,'252442517'))['verdict']=='BLOCK'

@pytest.mark.parametrize('text',[
 'Вы отметили быстрое впитывание.',
 'Рады, что крем у Вас быстро впитывался.',
 'Рады, что крем быстро впитывался на Вашей коже.',
])
def test_reported_speed_requires_speed_evidence(text):
 assert gate(text,msg('Быстро впитывается','252442517'))['verdict']=='PASS'
 assert gate(text,msg('Хорошо впитывается','252442517'))['verdict']=='BLOCK'

@pytest.mark.parametrize('text',[
 'Вы отметили хорошее впитывание. Крем рассчитан на быстрое впитывание без жирной плёнки.',
 'Вы отметили хорошее впитывание, а крем рассчитан на быстрое впитывание без жирной плёнки.',
 'Рады, что Вы оценили текстуру. Крем рассчитан на быстрое впитывание без жирной плёнки.',
])
def test_brand_expertise_keeps_own_source_without_personal_equivalence(text):
 assert gate(text,msg('Хорошо впитывается','252442517'))['verdict']=='PASS'
 assert not strengthening_violations(text,'Хорошо впитывается')

@pytest.mark.parametrize('text',[
 'Рады, что крем впитывался без липкости на Вашей коже.',
 'Вы отметили, что крем впитывался без липкости.',
 'Рады, что крем подошёл Вашей коже и впитывался без липкости.',
])
def test_customer_experience_owns_property_not_universal(text):
 assert gate(text,msg('Крем хорошо впитывается без липкости','252442517'))['verdict']=='PASS'
 assert gate(text,msg('Крем хороший','252442517'))['verdict']=='BLOCK'
 assert gate(text+' Крем лечит дерматит.',msg('Крем хорошо впитывается без липкости','252442517'))['verdict']=='BLOCK'

@pytest.mark.parametrize('prefix',['Ваш отзыв','Ваше впечатление','Спасибо за впечатление — оно','Спасибо за отзыв — он'])
@pytest.mark.parametrize('goal',['помогает нам быть внимательнее к Вашему опыту','помогает нам лучше понять Ваше впечатление','помогает нам становиться лучше'])
def test_feedback_brand_process_is_not_product_efficacy(prefix,goal):
 assert gate(prefix+' '+goal+'.')['verdict']=='PASS'

@pytest.mark.parametrize('text',[
 'Крем помогает нам быть внимательнее к Вашему опыту.',
 'Гиалуроновая кислота помогает нам быть внимательнее к Вашему опыту.',
 'Спасибо за впечатление о креме. Он помогает нам быть внимательнее к Вашему опыту.',
 'Ваш отзыв помогает нам увлажнять кожу.',
 'Ваш отзыв помогает нам быть внимательнее. Мы компенсируем покупку.',
 'Ваш отзыв помогает нам быть внимательнее. Используйте каждый день.',
 'Ваш отзыв помогает нам быть внимательнее. В составе 2,25% кислоты.',
 'Ваш отзыв помогает нам быть внимательнее. Крем для губ.',
])
def test_process_provenance_never_waives_unrelated_rule(text):assert gate(text)['verdict']=='BLOCK'


def test_polarity_and_dimension_are_independent():
 assert ('fast_absorption','negative') in evidence_features('Не быстро впитывается')
 assert ('fast_absorption','positive') not in evidence_features('Хорошо впитывается')
 assert strengthening_violations('Вы отметили быстрое впитывание.','Не быстро впитывается')
 assert not strengthening_violations('Вы отметили, что крем не быстро впитывается.','Не быстро впитывается')


def ctx_for(m,text):
 from app.v3.resolver import resolve
 pid=resolve(S,nm_id=m['nm_id'],marketplace='wb').product_id
 c=context_for_free_text(S,[pid]);c.customer_experience=m['text']
 rows=[r for r in approved(S,pid) if in_context(r,m,S)]
 c.approved_explanation_texts=[t for r in rows if r['kind']=='ingredient_benefit' for t in matching_texts(r,text)]
 c.approved_guidance_texts=[t for r in rows if r['kind']=='guidance' for t in matching_texts(r,text)]
 return c


def test_five_provenance_classes_are_independently_bound():
 m=msg('Крем быстро впитывается без липкости','252442517')
 t='Вы отметили быстрое впитывание без липкости. Крем рассчитан на быстрое впитывание без жирной плёнки. Спасибо за отзыв — он помогает нам быть внимательнее к Вашему опыту.'
 c=ctx_for(m,t);rows=audit(t,c);assert {'CUSTOMER_REPORTED','APPROVED_EXPERTISE','FREE_BRAND_VOICE'} <= {r['provenance'] for r in rows}
 t='150 мл.';c=context_for_free_text(S,['EVT-FT-MOIST-150']);assert any(r['provenance']=='VERIFIED_PRODUCT_FACT' for r in audit(t,c))
 service=next(x for x in c.templates if 'wildberries' in x and 'обращение' in x)
 assert any(r['provenance']=='APPROVED_SERVICE' for r in audit(service,c))
 # Model labels and source text alone grant no exemption to a brand claim.
 assert gate('CUSTOMER_REPORTED: Крем лечит дерматит.',msg('Крем лечит дерматит.'))['verdict']=='BLOCK'
