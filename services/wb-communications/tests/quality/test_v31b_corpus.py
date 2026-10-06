"""Corpus coverage + independent publication/safety invariants. No expected score mirroring."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.v3.snapshot import load_snapshot
from app.v3.classifier import classify_rules
from app.v3.planner import plan
from app.v3.resolver import resolve
from app.response_quality.core import prepare
from app.response_quality.brand_voice import AXES
from app.services.publication_policy import validate_for_publication
DATA=json.loads(Path(__file__).with_name('golden_v31b.json').read_text())
CASES=DATA['cases'];S=load_snapshot();ST=SimpleNamespace(v3_knowledge_snapshot_id=S.snapshot_id)


def test_corpus_coverage_and_provenance():
    assert len(CASES)>=80 and len({c['id'] for c in CASES})==len(CASES)
    assert sum(c['id'].startswith('shadow_') for c in CASES)==36
    assert {'A_positive_toner','B_positive_powder','C_package_damage','D_age_question'}<={c['id'] for c in CASES}
    ids={c['id'] for c in CASES}
    assert {'authored_wood_mixed','authored_sticky','authored_drying','authored_dispenser',
            'authored_pregnancy','authored_combination','authored_usage','authored_fragrance_question',
            'serious_breathing','serious_swelling','pao_known','shelf_life','restricted_salicylic'}<=ids
    required={'message','strategy','allowed_facts','approved_explanations','forbidden_claims','answer',
              'hard_verdict','quality_scores','human_review_reason','expected_owner_actions','baseline_source'}
    assert all(required<=c.keys() for c in CASES)
    assert all(set(c['quality_scores'])==set(AXES) for c in CASES)


@pytest.mark.parametrize('c',CASES,ids=lambda c:c['id'])
def test_corpus_publication_and_unchanged_safety(c):
    m=c['message'];r=prepare(m,c['v2'],S,safe_v3_draft=c['v3'])
    assert set(c['must_address'])<={a.key for a in r.plan.aspects}
    if r.status=='READY':
        assert r.text and len(r.text)<=1000
        assert validate_for_publication(r.text,m,ST)['verdict']!='BLOCK'
        assert r.quality.dimensions['missed_customer_signal_penalty']=='GOOD'
    else:
        assert r.text is None and (r.plan.human_reason or r.final_policy['verdict']=='BLOCK')
    cls=classify_rules(m,S)
    if cls.safety and cls.safety.has_safety:
        hard=plan(S,resolve(S,nm_id=m.get('nm_id')),cls,m)
        assert r.plan.route==hard.strategy
        if hard.strategy=='SAFETY_TEMPLATE':assert r.text==hard.deterministic_text
        assert 'confirm_original_override_same_authorized_user' not in c['expected_owner_actions']
    from app.response_quality.expertise import approved, in_context
    if r.plan.explanations:
        product=resolve(S,nm_id=m.get('nm_id')).product_id
        allowed=approved(S,product)
        assert all({k:v for k,v in x.items() if k!='execution'} in allowed and in_context(x,m)
                   for x in r.plan.explanations)
