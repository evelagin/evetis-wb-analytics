"""Discourse is not permission; same semantic payload must survive every frame."""
import pytest
from tests.quality.test_v31c_calibration import gate,msg,S
from app.v3.provenance import semantic_segments,strength_findings,fragrance_perception_spans
from app.response_quality.expertise import approved,matching_texts,registry_sha

@pytest.mark.parametrize('text',[
 'Пусть крем продолжает Вас радовать.',
 'Желаем приятных впечатлений от покупки.',
 'Будем рады видеть Вас снова.',
 'Пусть знакомство с EVETIS оставляет приятные эмоции.',
 'Пусть каждое использование приносит только приятные ощущения.',
])
def test_pure_affect_payload_remains_non_factual(text):
 assert gate(text,msg('Хороший крем','252442517'))['verdict']=='PASS'
 assert all(not p['predicates'] for p in semantic_segments(text))

@pytest.mark.parametrize('frame',['','Пусть ','Надеемся, что ','Желаем, чтобы '])
@pytest.mark.parametrize('payload',[
 'крем уберёт воспаления',
 'средство избавит от акне',
 'кожа быстрее восстановится благодаря крему',
 'средство очистило поры',
 'салициловая кислота быстрее очистит поры',
 'гиалуроновая кислота восстановит кожу',
 'использовать крем два раза в день',
 'ежедневное нанесение станет частью ухода',
 'Вы будете наносить его вечером',
 'аромат держится весь день',
 'крем будет быстро впитываться весь день',
 'эффект сохраняется 24 часа',
])
def test_all_substantive_families_keep_hard_gate_in_every_frame(frame,payload):
 t=frame+payload+'.'
 assert gate(t,msg('Крем понравился','438775617'))['verdict']=='BLOCK'

@pytest.mark.parametrize('ending',['ий','ая','ое','ие','им','ой','их','ого','ому','ими'])
def test_intensity_relation_is_inflection_independent(ending):
 answer='Вы отметили чрезвычайно резк'+ending+' аромат.'
 assert any(r['severity']=='BLOCK' for r in strength_findings(answer,'немного резкий запах'))
 assert gate(answer,msg('немного резкий запах','252442517'))['verdict']=='BLOCK'

@pytest.mark.parametrize('source,answer',[
 ('слегка липкий','Вы отметили очень липкую текстуру'),
 ('довольно насыщенный','аромат показался Вам невероятно насыщенным'),
 ('неплохой','Вы в полном восторге'),
])
def test_confident_degree_increase_is_blocked(source,answer):
 assert gate(answer,msg(source,'252442517'))['verdict']=='BLOCK'

@pytest.mark.parametrize('source,answer',[
 ('немного резкий','довольно насыщенным'),
 ('неплохой','Рады, что крем Вам понравился.'),
 ('очень резкий запах','Вы отметили резкий запах.'),
 ('невероятно насыщенный','аромат показался Вам довольно насыщенным'),
])
def test_different_adjective_or_weaker_degree_not_hard_violation(source,answer):
 assert not any(r['severity']=='BLOCK' for r in strength_findings(answer,source))
 # Bare adjective without a factual proposition is not a product assertion.
 assert gate(answer,msg(source,'252442517'))['verdict']!='BLOCK'

A1=[
 'Восприятие аромата индивидуально.',
 'Парфюмерные композиции воспринимаются индивидуально.',
 'Восприятие аромата у каждого своё.',
 'Восприятие парфюмерной композиции индивидуально.',
 'Аромат воспринимается индивидуально.',
 'Восприятие запаха у каждого человека своё.',
]
@pytest.mark.parametrize('nm',['252442517','593111986','593111985'])
@pytest.mark.parametrize('answer',A1)
def test_a1_meaning_preserves_exact_approved_scope(nm,answer):
 m=msg('Запах резковатый',nm)
 assert gate(answer,m)['verdict']!='BLOCK'

@pytest.mark.parametrize('answer',A1)
@pytest.mark.parametrize('nm',['535581674','305101272'])
def test_same_meaning_cannot_extend_a1_product_scope(nm,answer):
 assert gate(answer,msg('Запах резковатый',nm))['verdict']=='BLOCK'

@pytest.mark.parametrize('answer',[
 'Этот аромат обычно нравится большинству.',
 'Насыщенность аромата зависит от типа кожи.',
 'Аромат раскрывается по-разному из-за температуры кожи.',
 'Восприятие аромата индивидуально. Аромат держится весь день.',
 'Восприятие аромата индивидуально, и аромат обычно нравится большинству.',
 'Восприятие аромата индивидуально. В составе парфюмерная композиция.',
 'В составе парфюмерная композиция воспринимается индивидуально.',
 'Крем содержит парфюмерные композиции, которые воспринимаются индивидуально.',
 'Восприятие аромата индивидуально. В составе 2,25% салициловой кислоты.',
 'Восприятие аромата индивидуально. Крем лечит акне.',
])
def test_a1_approval_cannot_admit_expansion_composition_or_other_claim(answer):
 assert gate(answer,msg('Запах резковатый','252442517'))['verdict']=='BLOCK'


def test_frame_and_payload_are_distinct_inspectable_fields():
 for t,kind in [('Надеемся, что аромат будет держаться весь день.','PRODUCT_PERFORMANCE_CLAIM'),('Желаем наносить крем дважды в день.','PRODUCT_USE_CLAIM')]:
  p=semantic_segments(t)[0]
  assert p['frame']=='FREE_BRAND_VOICE' and not p['non_substantive']
  assert any(x['kind']==kind for x in p['predicates'])
 p=semantic_segments('Пусть крем уберёт воспаления.')[0]
 assert p['canonical']=='крем убирает воспаления' and p['normalized_predicate']
 assert gate(p['canonical'],msg('Хорошо','438775617'))['verdict']=='BLOCK'


def test_exact_a1_authority_unchanged_and_meaning_spans_are_bounded():
 a1=next(r for r in approved(S,'EVT-HC-HAND-300') if r.get('owner_key')=='А1')
 assert a1['variants_ru']==['Восприятие аромата индивидуально.']
 assert a1['product_ids']==['EVT-HC-HAND-300','EVT-HC-CHERRY-300','EVT-HC-AMBER-300']
 assert registry_sha()=='e73989c116a17b36eb2088c5939f47ed815335a2ca29b0076b0b319e2bbd1bad'
 text='Восприятие аромата индивидуально, а крем уберёт воспаления.'
 spans=matching_texts(a1,text)
 assert spans and all('убер' not in s for s in spans)
 assert not fragrance_perception_spans('Крем хороший. Его восприятие у каждого своё.')
