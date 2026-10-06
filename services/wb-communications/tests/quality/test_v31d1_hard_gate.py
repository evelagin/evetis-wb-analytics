"""General affect/use boundary and relational testimony strength; no corpus IDs."""
import pytest
from tests.quality.test_v31c_calibration import gate,msg,S
from tests.quality.test_v31d_provenance import ctx_for
from app.v3.provenance import audit,free_brand_voice,strength_findings
from app.v3.grounding import unsupported_use

@pytest.mark.parametrize('text',[
 'Пусть крем продолжает Вас радовать.',
 'Желаем только приятных впечатлений от использования.',
 'Будем рады видеть Вас снова.',
 'Спасибо, что возвращаетесь к EVETIS.',
 'Пусть уход приносит только приятные эмоции.',
 'Пусть каждое использование приносит только приятные ощущения.',
 'Надеемся, что аромат будет радовать Вас и дальше.',
 'Желаем использовать средство с удовольствием.',
 'Спасибо, что так подробно поделились впечатлением.',
])
def test_affect_is_free_but_not_a_product_use(text):
 m=msg('Хороший крем, беру повторно','252442517')
 assert free_brand_voice(text)
 assert gate(text,m)['verdict']=='PASS'
 assert any(r['provenance']=='FREE_BRAND_VOICE' for r in audit(text,ctx_for(m,text)))

@pytest.mark.parametrize('frame',['Пусть','Надеемся, что','Желаем, чтобы'])
@pytest.mark.parametrize('claim',[
 'крем продолжает лечить акне',
 'средство уберёт воспаления',
 'ежедневное использование восстановит защитный барьер',
 'салициловая кислота быстрее очистит поры',
 'крем наносили дважды в день',
 'средство подходит всем детям',
 'аромат будет держаться весь день',
 'средство увлажняет кожу',
])
def test_wish_never_waives_substantive_payload(frame,claim):
 t=frame+' '+claim+'.'
 assert not free_brand_voice(t)
 assert gate(t,msg('Крем понравился','438775617'))['verdict']=='BLOCK'

@pytest.mark.parametrize('instruction',[
 'Наносите крем вечером.', 'Используйте средство ежедневно.',
 'Достаточно небольшого количества крема.', 'Средство можно наносить на тело.',
 'Желаем наносить средство дважды в день.', 'Используйте средство.',
 'Наносите крем.',
])
def test_use_or_amount_requires_provenance(instruction):
 assert gate(instruction,msg('Понравился','535581674'))['verdict']=='BLOCK'

@pytest.mark.parametrize('source,answer',[
 ('запах резковатый','аромат показался Вам довольно насыщенным'),
 ('немного липкий','текстура показалась Вам немного липкой'),
 ('очень понравился','рады, что крем Вам понравился'),
 ('Крем отличный, запах резковатый','Спасибо, что поделились впечатлением. Рады, что сам крем Вам понравился. Восприятие парфюмерной композиции индивидуально, поэтому понимаем, что аромат мог показаться Вам более насыщенным, чем хотелось.'),
 ('Запах резковатый','Анна, понимаем: резкий запах оставил у Вас не самое приятное впечатление. Спасибо, что поделились ощущением.'),
])
def test_preservation_or_uncertain_paraphrase_keeps_hard_permission(source,answer):
 assert gate(answer,msg(source,'252442517'))['verdict']!='BLOCK'
 assert not any(r['severity']=='BLOCK' for r in strength_findings(answer,source))

@pytest.mark.parametrize('source,answer',[
 ('запах резковатый','Вы отметили очень резкий запах'),
 ('немного липкий','крем очень липкий'),
 ('неплохой крем','Вы остались в полном восторге'),
 ('слегка суховатый','Вы отметили очень сухой крем'),
 ('немного резкий','аромат показался Вам чрезвычайно резким'),
])
def test_explicit_strengthening_is_non_ready(source,answer):
 assert gate(answer,msg(source,'252442517'))['verdict']=='BLOCK'
 assert any(r['severity']=='BLOCK' for r in strength_findings(answer,source))

@pytest.mark.parametrize('source,answer',[
 ('очень резкий запах','Вы отметили резкий запах'),
 ('очень резкий запах','Вы отметили очень резкий запах'),
 ('крем очень липкий','текстура показалась Вам очень липкой'),
])
def test_degree_bound_to_property_allows_same_or_weaker(source,answer):
 assert not any(r['severity']=='BLOCK' for r in strength_findings(answer,source))

@pytest.mark.parametrize('text',[
 'Пусть использование приносит приятные впечатления.',
 'Спасибо, использование приятно.',
 'Применение приносит радость.',
 'Использование придаёт особое настроение.',
])
def test_preposition_is_a_whole_word_not_a_verb_prefix(text):
 assert not unsupported_use(text,{'use_domains':[],'generic_cosmetic_context':True})

@pytest.mark.parametrize('text',[
 'Используется при воспалениях.', 'Используется как основа под тональное средство.',
 'Применяется для восстановления кожи.',
])
def test_real_role_is_not_exempted_by_word_boundary(text):
 assert unsupported_use(text,{'use_domains':[],'generic_cosmetic_context':True})
