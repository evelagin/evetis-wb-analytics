"""E7: customer attribution survives word order; facts answer as sentences.

Constructed grammar families, not copies of any evaluation corpus. Customer
experience may be acknowledged; it never licenses a neighbouring brand claim.
"""
import pytest
from tests.quality.test_v31e_routing_relevance import S,ST,msg
from app.services.publication_policy import validate_for_publication
from app.v3.contextual_language import reported_features
from app.v3.direct_questions import parse_intent
from app.v3.planner import ResolvedFact
from app.response_quality.core import prepare
from app.response_quality.fact_response import compose,fact_fragment_only

HAND,AMBER,CHERRY='252442517','593111985','593111986'

def verdict(source,answer,nm):
 return validate_for_publication(answer,msg(source,nm=nm),ST)['verdict']

def rules(source,answer,nm):
 return {v['rule_id'] for v in validate_for_publication(answer,msg(source,nm=nm),ST)['violations']}

# A. customer attribution is order-invariant -----------------------------------
ORDER=[
 ('Аромат понравился.','Рады, что аромат Вам понравился.',AMBER),
 ('Понравился аромат.','Рады, что аромат Вам понравился.',AMBER),
 ('Мне понравился аромат.','Рады, что Вам понравился аромат.',AMBER),
 ('Аромат мне понравился.','Рады, что Вам понравился аромат.',AMBER),
 ('Запах хороший.','Рады, что хороший запах Вам по душе.',AMBER),
 ('Хороший запах.','Рады, что хороший запах Вам по душе.',AMBER),
 ('Аромат оказался приятным.','Рады, что приятный аромат пришёлся Вам по вкусу.',AMBER),
 ('Приятным оказался аромат.','Рады, что приятный аромат пришёлся Вам по вкусу.',AMBER),
 ('Аромат для меня насыщенный.','Здорово, что насыщенный аромат пришёлся Вам по вкусу.',HAND),
 ('Насыщенный аромат, мне такой нравится.','Здорово, что насыщенный аромат Вам зашёл.',HAND),
 ('Хорошо впитывается.','Рады, что крем у Вас хорошо впитывается.',HAND),
 ('Впитывается хорошо.','Рады, что крем у Вас хорошо впитывается.',HAND),
 ('Хорошо впитывается.','Рады, что крем у Вас впитывается хорошо.',HAND),
 ('Впитался быстро.','Рады, что крем у Вас быстро впитался.',HAND),
 ('Быстро впитался.','Рады, что крем у Вас впитался быстро.',HAND),
 ('Приятная текстура.','Рады, что текстура показалась Вам приятной.',AMBER),
 ('Текстура приятная.','Рады, что Вам понравилась приятная текстура.',AMBER),
 ('Немного липкий.','Понимаем, что крем показался Вам немного липким.',AMBER),
 ('Липкий немного.','Понимаем, что крем показался Вам немного липким.',AMBER),
]
@pytest.mark.parametrize('source,answer,nm',ORDER,ids=[f'{s}|{a}' for s,a,_ in ORDER])
def test_customer_attribution_survives_word_order(source,answer,nm):
 assert verdict(source,answer,nm)=='PASS'


@pytest.mark.parametrize('source,feature',[
 ('Хорошо впитывается.','good_absorption'),('Впитывается хорошо.','good_absorption'),
 ('Впитывается очень хорошо.','good_absorption'),('Быстро впитался.','fast_absorption'),
 ('Впитался быстро.','fast_absorption'),('Текстура приятная.','pleasant_texture'),
 ('Приятная консистенция.','pleasant_texture'),('Консистенция понравилась.','pleasant_texture'),
])
def test_customer_evidence_is_order_invariant(source,feature):
 assert feature in reported_features(source)


@pytest.mark.parametrize('source',['Впитывается не очень хорошо.','Не впитывается хорошо.','Быстро не впитался.'])
def test_negated_evidence_is_not_positive_testimony(source):
 assert not {'good_absorption','fast_absorption'} & set(reported_features(source))
 assert verdict(source,'Рады, что крем у Вас хорошо впитывается.',HAND)=='BLOCK'

# B. testimony strength is preserved ------------------------------------------
@pytest.mark.parametrize('source,answer',[
 ('Запах немного резкий.','Понимаем, что запах показался Вам чрезвычайно резким.'),
 ('Запах резкий немного.','Понимаем, что запах показался Вам очень резким.'),
 ('Крем липкий немного.','Понимаем, что крем показался Вам очень липким.'),
 ('Впитывается хорошо.','Рады, что крем у Вас очень хорошо впитывается.'),
 ('Хорошо впитывается.','Рады, что крем у Вас быстро впитывается.'),
 ('Впитывается хорошо.','Вы отметили быстрое впитывание.'),
])
def test_testimony_strength_cannot_be_raised(source,answer):
 assert verdict(source,answer,HAND if 'впит' in source else AMBER)=='BLOCK'


@pytest.mark.parametrize('source',['Крем немного липкий.','Крем липкий немного.'])
def test_lost_hedge_is_diagnostic_not_hard(source):
 result=validate_for_publication('Понимаем, что крем показался Вам липким.',msg(source,nm=AMBER),ST)
 assert result['verdict']=='WARNING' and {v['rule_id'] for v in result['violations']}=={'V-TESTIMONY'}


def test_supported_high_degree_may_be_kept():
 assert verdict('Впитывается очень хорошо.','Рады, что крем у Вас очень хорошо впитывается.',HAND)=='PASS'

# C. customer experience never licenses a neighbouring brand proposition -----
NEIGHBOURS=[
 ('Аромат приятный.','У крема приятный аромат.',AMBER),
 ('Аромат приятный.','Крем имеет приятный аромат, рады, что Вам понравилось.',AMBER),
 ('Аромат насыщенный.','Крем отличается насыщенным ароматом, рады, что Вам понравилось.',HAND),
 ('Впитывается хорошо.','Крем хорошо впитывается.',HAND),
 ('Впитывается хорошо.','Крем впитывается хорошо, и Вам это понравилось.',HAND),
 ('Впитывается хорошо.','Средство быстро впитывается.',HAND),
 ('Текстура приятная.','У крема приятная текстура.',AMBER),
 ('Текстура приятная.','Крем с приятной текстурой, рады, что Вам понравилось.',AMBER),
 ('Хороший крем.','Рады, что крем у Вас хорошо впитывается.',HAND),
 ('Хороший крем.','Здорово, что насыщенный аромат пришёлся Вам по вкусу.',HAND),
]
@pytest.mark.parametrize('source,answer,nm',NEIGHBOURS,ids=[a for _,a,_ in NEIGHBOURS])
def test_neighbouring_brand_proposition_blocked(source,answer,nm):
 assert verdict(source,answer,nm)=='BLOCK'


def test_universal_still_not_attributable():
 assert verdict('Аромат приятный.','Рады, что приятный аромат нравится всем.',AMBER)=='BLOCK'

# D/E. FACT_ANSWER is a composed sentence that adds nothing ------------------
def answer(q,nm):
 r=prepare(msg(q,question=True,nm=nm),'',S,force_generation=True)
 return r,r.text or ''

@pytest.mark.parametrize('q,nm,expected',[
 ('Какие именно ноты аромата заявлены для этого крема?',HAND,'У этой версии крема — древесно-удовый аромат.'),
 ('У крема с вишней запах вишнёвый или другой?',CHERRY,'У этой версии крема — вишнёвый аромат.'),
 ('В какой стране изготовлен тоник?','535581674','Страна производства — Китай.'),
 ('Как называется продукт: это сыворотка или крем?','305101272','Это увлажняющая сыворотка для лица EVETIS.'),
 ('Это готовая пенка или порошок?','535580776','Это энзимная пудра для лица EVETIS.'),
 ('Какой объём у крема?',HAND,'Объём — 300 мл.'),
])
def test_fact_answer_is_complete_sentence(q,nm,expected):
 r,text=answer(q,nm)
 assert r.status=='READY' and text==expected
 assert r.metadata()['operator_state']=='READY'
 assert not fact_fragment_only(text,[f['text'] for f in r.plan.facts])
 assert validate_for_publication(text,msg(q,question=True,nm=nm),ST)['verdict']=='PASS'


@pytest.mark.parametrize('q,nm',[
 ('Какие именно ноты аромата заявлены для этого крема?',HAND),('В какой стране изготовлен тоник?','535581674'),
 ('Это готовая пенка или порошок?','535580776'),('Какой объём у крема?',HAND),('Кто производитель крема?',HAND),
])
def test_composed_answer_adds_no_new_fact(q,nm):
 r,text=answer(q,nm)
 values=[f['text'].rstrip('.') for f in r.plan.facts]
 assert values and all(v in text for v in values)
 rest=text
 for v in values:rest=rest.replace(v,'')
 # Only the frame remains: no numbers, benefits, instructions or marketing.
 assert not any(c.isdigit() for c in rest)
 assert not __import__('re').search(r'подход|помога|увлажн|рекоменд|нанос|использ|лучш|идеальн|эффект',rest.lower())


def test_sentence_values_are_kept_verbatim():
 f=ResolvedFact('intended_use','EVT-HC-HAND-300','KNOWN_ALLOWED',customer_value_ru='По документации крем для рук EVETIS предназначен(а) для рук.')
 assert compose(f,S)==f.customer_value_ru


def test_verified_longevity_value_is_framed_not_invented():
 # Composer contract only; no registry data is created.
 f=ResolvedFact('fragrance_longevity','EVT-HC-HAND-300','KNOWN_ALLOWED',customer_value_ru='до 6 часов')
 assert compose(f,S)=='Стойкость аромата по документации — до 6 часов.'


@pytest.mark.parametrize('text,values,flag',[
 ('вишнёвый аромат.',['вишнёвый аромат'],True),('Китай.',['Китай'],True),('энзимная пудра.',[],True),
 ('150 мл.',[],True),('У этой версии крема — вишнёвый аромат.',['вишнёвый аромат'],False),
 ('Страна производства — Китай.',['Китай'],False),
 ('В составе есть гиалуроновая кислота (Hyaluronic Acid).',['В составе есть гиалуроновая кислота (Hyaluronic Acid).'],False),
])
def test_fact_fragment_only_invariant(text,values,flag):
 assert fact_fragment_only(text,values) is flag

# F. longevity intent ------------------------------------------------------------
@pytest.mark.parametrize('q,duration',[
 ('Сколько держится аромат?',None),('Долго держится запах?',None),('Аромат стойкий?',None),
 ('Аромата хватит до вечера?','до вечера'),('Аромат держится до утра?','до утра'),
 ('Остаётся ли запах надолго?',None),('Как долго ощущается аромат?',None),
 ('Аромат гарантированно остаётся на руках до следующего утра?','до утра'),
 ('Аромат приятный, держится ли он весь день?','в течение дня'),('Запах сохраняется сутки?','в течение суток'),
])
def test_longevity_intent(q,duration):
 i=parse_intent(q)
 assert i.intent_type=='LONGEVITY' and i.duration_ru==duration


@pytest.mark.parametrize('q',[
 'Как часто наносить крем с этим ароматом?','Долго пользуюсь, аромат нравится. Можно на лицо?',
 'Аромат приятный, а крем долго впитывается?','Держу крем на работе, аромат приятный. Можно на лицо?',
 'Сколько держится крем после вскрытия?','Какие ноты аромата?',
])
def test_not_longevity(q):
 assert 'fragrance_longevity' not in parse_intent(q).fact_types

# G/H. longevity UNKNOWN answers the question; adjacent fact never substitutes --
@pytest.mark.parametrize('q,nm,boundary',[
 ('Аромат держится до утра?',HAND,'Подтверждённых данных о стойкости аромата до утра у нас нет.'),
 ('Ваш древесный аромат гарантированно остаётся на руках до следующего утра?',HAND,'Подтверждённых данных о стойкости аромата до утра у нас нет.'),
 ('Сколько держится вишнёвый аромат?',CHERRY,'Подтверждённых данных о стойкости аромата у нас нет.'),
 ('Аромата хватит до вечера?',AMBER,'Подтверждённых данных о стойкости аромата до вечера у нас нет.'),
])
def test_longevity_unknown_answers_question(q,nm,boundary):
 r,text=answer(q,nm)
 assert r.status=='READY' and text.startswith(boundary)
 assert not __import__('re').search(r'\d|час|держится\s+до|гарант',text.replace(boundary,''))
 assert validate_for_publication(text,msg(q,question=True,nm=nm),ST)['verdict']=='PASS'
 assert 'DIRECT_QUESTION_UNANSWERED' not in r.quality.reasons


@pytest.mark.parametrize('q,nm,candidate',[
 ('Аромат держится до утра?',HAND,'У этой версии крема — древесно-удовый аромат.'),
 ('Аромат держится до утра?',HAND,'древесно-удовый аромат.'),
 ('Сколько держится вишнёвый аромат?',CHERRY,'У этой версии крема — вишнёвый аромат.'),
])
def test_adjacent_fact_cannot_substitute_for_longevity(q,nm,candidate):
 from app.response_quality.core import evaluate,make_plan
 m=msg(q,question=True,nm=nm);p=make_plan(m,S)
 report=evaluate(candidate,m,p,validate_for_publication(candidate,m,ST))
 assert report.verdict!='GOOD'
 assert 'ADJACENT_FACT_SUBSTITUTION' in report.reasons


@pytest.mark.parametrize('candidate',[
 'Аромат держится до утра.','Аромат держится всю ночь.','Аромат держится 8 часов.',
])
def test_longevity_claim_is_not_licensed_by_question(candidate):
 assert validate_for_publication(candidate,msg('Аромат держится до утра?',question=True,nm=HAND),ST)['verdict']=='BLOCK'
