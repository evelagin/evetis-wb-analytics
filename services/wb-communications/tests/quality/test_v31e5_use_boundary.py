"""One repair: epistemic state is neither affirmative/negative use nor advice."""
from dataclasses import replace
from types import SimpleNamespace
import pytest
from tests.quality.test_v31e_routing_relevance import S,ST,msg,hp,bundle_nm
from app.v3.verifier import verify,context_for_plan,context_for_free_text
from app.v3.use_boundary import propositions,resolved_permissions
from app.v3.direct_questions import current_question_plan
from app.services.publication_policy import validate_for_publication
from app.response_quality.core import prepare

FACE_Q='Можно использовать на лице?'
FACE_MSG=msg(FACE_Q,question=True,nm='252442517')
POSITIVE=[
 'Применение этого средства на лице не подтверждено.',
 'Подтверждённых данных о применении средства на лице у нас нет.',
 'В доступной документации применение средства на лице не указано.',
 'Мы не можем подтвердить использование этого средства для лица.',
 'Подтверждённая информация об использовании этого крема на лице отсутствует.',
 'В технической документации не описано применение средства на лице.',
]
@pytest.mark.parametrize('answer',POSITIVE,ids=POSITIVE)
def test_positive_boundary_requires_same_unknown(answer):
 p=hp(FACE_MSG);assert p.question_intent.resolution=='UNKNOWN'
 ctx=context_for_plan(p,S)
 assert ctx.intended_use_boundaries
 # Two layers: the paraphrase is semantically publishable as free text, but a
 # generated plan answer must still carry the mandatory T-INTENDED-USE wording.
 assert validate_for_publication(answer,FACE_MSG,ST)['verdict']=='PASS'
 assert {v.rule_id for v in verify(answer,ctx,S).violations}=={'V-TEMPLATE'}


def test_plan_mode_mandatory_intended_use_template_passes():
 ctx=context_for_plan(hp(FACE_MSG),S)
 assert ctx.required_templates
 for t in ctx.required_templates:
  assert verify(t,ctx,S).verdict=='PASS'


@pytest.mark.parametrize('nm,q,answer',[
 ('535581674','Можно использовать на теле?','Применение этого крема на теле не подтверждено.'),
 ('930334396','Можно наносить крем из набора на лицо?','Применение этого крема на лице не подтверждено.'),
 ('252442517','Можно использовать на лице?','Подтверждённая информация об использовании этой сыворотки на лице отсутствует.'),
])
def test_generic_noun_never_switches_or_resolves_product(nm,q,answer):
 # «крем» refers back only to a uniquely resolved cream; it cannot name a
 # tonic's boundary, resolve a bundle, or hide a different named type.
 assert validate_for_publication(answer,msg(q,question=True,nm=nm),ST)['verdict']=='BLOCK'

NEGATIVE=[
 'Средство не предназначено для лица.',
 'Крем нельзя наносить на лицо.',
 'Не наносите крем на лицо.',
 'Средство безопасно использовать на лице.',
 'Крем можно наносить на тело.',
 'Подходит для лица.',
 'Можно использовать ежедневно.',
 'Не вызывает раздражения на лице.',
 'Используйте только на руках.',
 'Не применяйте ежедневно.',
]
@pytest.mark.parametrize('answer',NEGATIVE,ids=NEGATIVE)
def test_negative_boundary_cannot_grant_use_fact_or_directive(answer):
 assert validate_for_publication(answer,FACE_MSG,ST)['verdict']=='BLOCK'
 assert verify(answer,context_for_plan(hp(FACE_MSG),S),S).verdict=='BLOCK'

@pytest.mark.parametrize('answer',[
 'Применение на лице не подтверждено, но для тела крем подходит.',
 'Нет данных о применении на лице. Не наносите его туда.',
 'Нет подтверждённых данных о применении на лице, поэтому не наносите его туда.',
 'Мы не можем подтвердить применение на лице, зато средство подходит чувствительной коже.',
 'Применение на лице не подтверждено. В составе есть салициловая кислота.',
 'Применение на лице не подтверждено, поскольку крем опасен для лица.',
 'Применение на лице не подтверждено, используйте только на руках.',
 'Применение на лице не подтверждено, и крем можно наносить на тело.',
])
def test_mixed_clause_boundary_does_not_rescue_neighbor(answer):
 assert validate_for_publication(answer,FACE_MSG,ST)['verdict']=='BLOCK'


AMBER_FACE_MSG=msg('Можно использовать на лице?',question=True,nm='593111985')


def test_mixed_clause_independently_verified_ingredient_remains_valid():
 # The neighbour must be permitted on its own (Amber INCI is MATCH).
 assert hp(AMBER_FACE_MSG).question_intent.resolution=='UNKNOWN'
 assert validate_for_publication('В составе есть глицерин.',AMBER_FACE_MSG,ST)['verdict']=='PASS'
 answer='Применение на лице не подтверждено. В составе есть глицерин.'
 assert validate_for_publication(answer,AMBER_FACE_MSG,ST)['verdict']=='PASS'


def test_mixed_clause_boundary_does_not_rescue_blocked_ingredient():
 # Hand cream INCI is CONTENT_MISMATCH: the boundary must not rescue it.
 assert validate_for_publication('В составе есть отдушка.',FACE_MSG,ST)['verdict']=='BLOCK'
 answer='Применение на лице не подтверждено. В составе есть отдушка.'
 assert validate_for_publication(answer,FACE_MSG,ST)['verdict']=='BLOCK'


def test_mixed_clause_independently_verified_body_permission_remains_valid():
 known=hp(msg('Можно использовать на теле?',question=True,nm='593111985'))
 assert known.strategy=='FACT_ANSWER'
 # FACT_ANSWER carries approved wording in resolved facts, not deterministic_text.
 fact=next(r.customer_value_ru for r in known.resolved
           if r.fact_type=='intended_use' and r.subject=='body' and r.state=='KNOWN_ALLOWED')
 assert validate_for_publication(fact,AMBER_FACE_MSG,ST)['verdict']!='BLOCK'
 answer='Применение на лице не подтверждено. '+fact
 assert validate_for_publication(answer,AMBER_FACE_MSG,ST)['verdict']!='BLOCK'

@pytest.mark.parametrize('m,answer',[
 (FACE_MSG,'Применение на теле не подтверждено.'),
 (msg('Можно использовать на руках?',question=True,nm='252442517'),'Применение на руках не подтверждено.'),
 (msg('Хороший крем',nm='252442517'),'Применение на лице не подтверждено.'),
 (msg('Можно использовать на лице?',question=True,nm='000000'),'Применение на лице не подтверждено.'),
 (msg('Можно пудрой пользоваться для тела?',question=True,nm='535580776'),'Применение этого крема на теле не подтверждено.'),
])
def test_target_product_and_state_cannot_be_self_declared(m,answer):
 assert validate_for_publication(answer,m,ST)['verdict']=='BLOCK'

@pytest.mark.parametrize('q,nm,area',[
 ('Можно использовать на руках?','252442517','hands'),
 ('Можно использовать на теле?','593111985','body'),
 ('Можно использовать на лице?','535580776','face'),
])
def test_verified_use_is_not_converted_to_uncertainty(q,nm,area):
 m=msg(q,question=True,nm=nm);p=hp(m)
 assert p.question_intent.resolution=='VERIFIED'
 assert any(r.fact_type=='intended_use' and r.subject==area and r.state=='KNOWN_ALLOWED' for r in p.resolved)
 r=prepare(m,'',S,force_generation=True)
 assert r.status=='READY' and 'не можем' not in r.text
 assert validate_for_publication(r.text,m,ST)['verdict']!='BLOCK'


def test_bundle_ambiguous_use_does_not_receive_ordinary_epistemic_permission():
 m=msg('Можно наносить крем из набора на лицо?',question=True,nm='930334396')
 p=hp(m);assert len({r.product_id for r in p.resolved})>1
 assert current_question_plan(S,p.product_ids,m['text']) is None
 assert resolved_permissions(S,p.product_ids,hard_plan=p)==[]
 assert validate_for_publication('Применение на лице не подтверждено.',m,ST)['verdict']=='BLOCK'
 assert prepare(m,'',S,force_generation=True).status=='HUMAN_REVIEW'


def test_bundle_named_component_keeps_its_own_unknown_target():
 nm=bundle_nm('EVT-SET-SER-CREAM-MOIST')
 m=msg('Можно использовать сыворотку из набора на теле?',question=True,nm=nm);p=hp(m)
 assert {r.product_id for r in p.resolved}=={'EVT-FS-MOIST-30'}
 answer='Применение сыворотки на теле не подтверждено.'
 assert validate_for_publication(answer,m,ST)['verdict']=='PASS'
 assert validate_for_publication('Применение крема на теле не подтверждено.',m,ST)['verdict']=='BLOCK'
 assert prepare(m,'',S,force_generation=True).status=='READY'

@pytest.mark.parametrize('corruption',['target','resolution','product','state','source'])
def test_plan_label_alone_never_grants_boundary_permission(corruption):
 p=hp(FACE_MSG)
 if corruption=='target':p.question_intent.application_areas=['body']
 elif corruption=='resolution':p.question_intent.resolution='VERIFIED'
 elif corruption=='product':p.question_intent.product_id='EVT-FS-MOIST-30'
 elif corruption=='state':p.resolved=[replace(r,state='KNOWN_ALLOWED') for r in p.resolved]
 else:p.resolved=[replace(r,source_ids=['invented']) for r in p.resolved]
 assert verify(POSITIVE[0],context_for_plan(p,S),S).verdict=='BLOCK'

@pytest.mark.parametrize('answer,kind',[
 ('Крем можно наносить на лицо.','AFFIRMATIVE_PRODUCT_USE'),
 ('Применение на лице не подтверждено.','EPISTEMIC_UNKNOWN_BOUNDARY'),
 ('Средство не предназначено для лица.','NEGATIVE_PRODUCT_FACT'),
 ('Не наносите крем на лицо.','DIRECTIVE'),
])
def test_four_linguistic_classes_are_distinct(answer,kind):
 assert propositions(answer)[0].kind==kind

@pytest.mark.parametrize('answer',[
 'Применение на лице не подтверждено. В составе 2,25% салициловой кислоты.',
 'Применение на лице не подтверждено. Не вызывает раздражения.',
 'Применение на лице не подтверждено. Гиалуроновая кислота обеспечивает мягкость.',
])
def test_epistemic_span_never_exempts_other_rule_families(answer):
 assert validate_for_publication(answer,FACE_MSG,ST)['verdict']=='BLOCK'
