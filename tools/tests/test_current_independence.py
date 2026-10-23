"""Heldi S4: CURRENT keeps loading while FULL_HISTORY is stopped, only on an exact proof. Offline; no cloud call."""
import pytest
from tools.tenancy import current_independence_image_check as X,current_independence as CI


def test_only_closed_full_leaf_stop_classes_are_local():
    assert X.classify_check()=='PASS'


def test_admission_requires_every_shared_authority_settled():
    assert X.admission_check()=='PASS'


def test_wake_gate_keeps_acceptance_and_recovery_and_opens_only_current():
    assert X.gate_check()=='PASS'


def test_v1_selects_four_entities_drains_existing_and_isolates_partial():
    assert X.selection_check()=='PASS'


@pytest.mark.parametrize('stage',X.SHARED_STAGES)
def test_every_shared_stage_is_global_even_with_a_leaf_context(stage):
    m,plans=X.leaves()
    assert CI.classify(X.typed_stop(m,stage,X.context(plans,465)),m,plans)[0]=='GLOBAL'


@pytest.mark.parametrize('outcome',['UNKNOWN','PRESENT'])
@pytest.mark.parametrize('write',['lease','checkpoint','intent','run','receipt'])
def test_any_sent_write_in_the_ledger_is_global(write,outcome):
    m,plans=X.leaves();ledger=dict.fromkeys(('lease','checkpoint','intent','run','receipt'),'ABSENT');ledger[write]=outcome
    assert CI.classify(X.typed_stop(m,'RUNTIME_TERMINAL',X.context(plans,465),ledger),m,plans)[0]=='GLOBAL'
