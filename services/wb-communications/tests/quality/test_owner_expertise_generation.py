"""Owner scope, attached evidence, context and independent gate regressions."""
import copy
import json
from types import SimpleNamespace
import pytest
from app.response_quality import expertise
from app.response_quality.core import make_plan, prepare, evaluate
from app.response_quality.language import LanguageRenderer
from app.services.publication_policy import validate_for_publication
from app.v3.snapshot import load_snapshot

S=load_snapshot()
ST=SimpleNamespace(v3_knowledge_snapshot_id=S.snapshot_id)

def gate(text,nm='535581674',review='Тоник отличный, очень мягкий'):
    return validate_for_publication(text,dict(nm_id=nm,text=review,entity_type='review'),ST)


def test_owner_delta_is_exact_and_attached_source_is_required(monkeypatch):
    d=copy.deepcopy(expertise.load_registry())
    new=[r for r in d['explanations'] if r.get('owner_key')]
    assert {r['owner_key'] for r in new}=={'У1','У2','У3','М1','А1'}
    assert sum(r['kind']=='fact' for r in d['explanations'])==3
    assert len(expertise.approved(S,'EVT-FT-MOIST-150'))==1
    d['supporting_sources']['SRC-HA-PAVICIC-2011']['file_sha256']='tampered'
    monkeypatch.setattr(expertise,'load_registry',lambda:d)
    assert not expertise.approved(S,'EVT-FT-MOIST-150')
    assert gate('Гиалуроновая кислота помогает поддерживать увлажнённость кожи.')['verdict']=='BLOCK'


@pytest.mark.parametrize('nm',['535581675','438775617','438775437','305101272'])
def test_ha_scope_does_not_borrow_other_form_or_product(nm):
    assert gate('Гиалуроновая кислота помогает поддерживать увлажнённость кожи.',nm)['verdict']=='BLOCK'


@pytest.mark.parametrize('text',[
    'Гиалуроновая кислота помогает поддерживать увлажнённость кожи и обеспечивает мягкость.',
    'Гиалуроновая кислота обеспечивает мягкость и помогает поддерживать увлажнённость кожи.',
    'Гиалуроновая кислота помогает поддерживать увлажнённость кожи и лечит дерматит.',
    'Гиалуроновая кислота помогает поддерживать увлажнённость кожи. Применяйте два раза в день.',
    'Гиалуронат натрия помогает поддерживать увлажнённость кожи.',
    'Гиалуроновая кислота восстанавливает барьер кожи.',
])
def test_approved_span_cannot_hide_an_additional_unapproved_claim(text):
    assert gate(text)['verdict']=='BLOCK'


def test_benefits_and_texture_have_strict_product_and_context_scope():
    u2='Сыворотка помогает поддерживать увлажнённость кожи.'
    assert gate(u2,'305101272','Сыворотка понравилась')['verdict']=='PASS'
    assert gate(u2,'868597351','Сыворотка понравилась')['verdict']=='BLOCK'
    u3='Крем помогает защищать кожу рук от сухости.'
    assert gate(u3,'252442517','Руки ухоженные')['verdict']=='PASS'
    assert gate(u3,'593111986','Руки ухоженные')['verdict']=='BLOCK'
    assert gate(u3,'252442517','Кожа немного щиплет')['verdict']=='BLOCK'
    m1='Крем рассчитан на быстрое впитывание без жирной плёнки.'
    assert gate(m1,'252442517','Нравится текстура')['verdict']=='PASS'
    p=make_plan(dict(nm_id='252442517',text='Крем быстро впитывается',entity_type='review'),S)
    assert any(r.get('owner_key')=='М1' for r in p.explanations)
    for negative in ['Крем липкий','Крем плохо впитывается','Крем не понравился','Приятная текстура, но нет эффекта',
                     'Нравится текстура, но кожа щиплет']:
        assert gate(m1,'252442517',negative)['verdict']=='BLOCK'


def test_fragrance_meaning_not_a_fixed_sentence_or_quota():
    m=dict(nm_id='252442517',text='Крем отличный, запах резковатый',rating=5,entity_type='review')
    p=make_plan(m,S)
    a1=next(r for r in p.explanations if r.get('owner_key')=='А1')
    integrated='Рады, что крем Вам понравился. Аромат каждый воспринимает по-своему, поэтому жаль, что для Вас он оказался резким.'
    assert expertise.matching_texts(a1,integrated)
    diagnostic=validate_for_publication(integrated,m,ST)
    # In-scope A1 approval does not erase uncertainty in the customer paraphrase.
    assert diagnostic['verdict']=='WARNING'
    assert diagnostic['violations']==[dict(rule_id='V-TESTIMONY',severity='WARNING',
                                          related_fact_id=None,related_claim_id=None)]
    assert not any(v['severity']=='BLOCK' for v in diagnostic['violations'])
    permission=prepare(m,integrated,S)
    assert permission.original_policy['verdict']=='WARNING'
    assert permission.status=='READY'
    assert evaluate(integrated,m,p,validate_for_publication(integrated,m,ST)).verdict=='GOOD'
    for extra in ['Цитрусовый аромат.','Аромат не вызывает раздражения.','В составе ниацинамид.',
                  'В составе парфюмерная композиция.']:
        assert validate_for_publication(integrated+' '+extra,m,ST)['verdict']=='BLOCK'
    short=make_plan(dict(m,text='Беру второй раз'),S)
    assert not short.explanations
    simple=make_plan(dict(m,text='Отлично'),S)
    assert not simple.explanations and simple.information_budget<=1
    model=Model(integrated);LanguageRenderer(model)(m,p)
    serialized=json.dumps(model.calls,ensure_ascii=False)
    assert 'Восприятие аромата индивидуально.' not in serialized
    assert 'content_sha256' not in serialized and 'owner_decision_id' not in serialized


@pytest.mark.parametrize('nm',['252442517','593111986','593111985'])
def test_a1_paraphrase_keeps_hard_permission_in_approved_scope(nm):
    m=dict(nm_id=nm,text='Запах резковатый',entity_type='review',rating=5)
    answer='Аромат каждый воспринимает по-своему.'
    diagnostic=validate_for_publication(answer,m,ST)
    assert diagnostic['verdict']=='PASS'
    assert not any(v['severity']=='BLOCK' for v in diagnostic['violations'])
    assert prepare(m,answer,S).status=='READY'


@pytest.mark.parametrize('customer,answer,nm,rule',[
    ('Запах немного резкий','Жаль, что аромат показался Вам чрезвычайно резким.','252442517','V-TESTIMONY'),
    ('Запах резковатый','Насыщенность аромата зависит от типа кожи.','252442517','V-FACT'),
    ('Запах резковатый','Аромат каждый воспринимает по-своему.','535581674','V-GENERAL'),
    ('Крем понравился','Цитрусовый аромат.','252442517','V-FACT'),
    ('Крем понравился','В составе ниацинамид.','252442517','V-FACT'),
    ('Крем понравился','Крем лечит акне.','252442517','V-MEDICAL'),
])
def test_testimony_and_a1_alignment_preserves_hard_negative_controls(customer,answer,nm,rule):
    m=dict(nm_id=nm,text=customer,entity_type='review',rating=5)
    diagnostic=validate_for_publication(answer,m,ST)
    assert diagnostic['verdict']=='BLOCK'
    assert any(v['severity']=='BLOCK' and v['rule_id']==rule for v in diagnostic['violations'])


class Model:
    def __init__(self,text):self.text=text;self.calls=[]
    def structured_timed(self,system,user,name,schema):
        self.calls.append(json.loads(user))
        return {'text':self.text},{'input_tokens':10,'output_tokens':10},2,'fake'


def test_language_pass_receives_no_unsafe_original_and_is_reverified():
    m=dict(nm_id='438775617',text='Крем хороший',entity_type='review',rating=5)
    llm=Model('Салициловая кислота 2,25%.');renderer=LanguageRenderer(llm)
    r=prepare(m,'Салициловая кислота 2,25%.',S,render=renderer,force_generation=True)
    assert len(llm.calls)==1 and '2,25' not in json.dumps(llm.calls)
    assert r.status=='HUMAN_REVIEW' and r.final_policy['verdict']=='BLOCK' and r.text is None


@pytest.mark.parametrize('text',['Кожа немного щиплет','Отёк и трудно дышать','Можно беременным?','Какой аромат у крема?'])
def test_safety_and_direct_questions_do_not_call_language_model(text):
    m=dict(nm_id='252442517',text=text,entity_type='question' if '?' in text else 'review',rating=5)
    llm=Model('Недопустимая новая калибровка');r=prepare(m,'Спасибо.',S,render=LanguageRenderer(llm),force_generation=True)
    assert not llm.calls
    p=make_plan(m,S)
    assert r.text==p.direct_answer or (p.human_reason and r.text is None)
