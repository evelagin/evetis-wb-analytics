"""Closed safe diagnostic stage boundaries, including -m/import identity."""
import json
import pytest
from tools.tenancy import controller_diagnostics as D, tenant_backfill as BF
from tools.tenancy import cloud_tick,cloud_access


@pytest.mark.parametrize('stage',sorted(D.STAGES))
@pytest.mark.parametrize('error',[BF.B.EvidenceError, BF.TT.TableError,ValueError,KeyError,TypeError,IndexError,OSError])
def test_stage_is_preserved_without_untrusted_message(stage,error):
    with pytest.raises(D.GateFailure) as caught:
        with D.gate(stage):raise error('SYNTHETIC_SENSITIVE_PAYLOAD_DO_NOT_PERSIST')
    evidence=json.dumps(D.safe(caught.value))
    assert 'SYNTHETIC_SENSITIVE' not in evidence and D.safe(caught.value)['stage']==stage
    assert 'SYNTHETIC_SENSITIVE' not in str(caught.value)


def test_nested_gate_retains_precise_inner_failure():
    with pytest.raises(D.GateFailure) as caught:
        with D.gate('MANIFEST_ROOT'):
            with D.gate('BINDING'):raise BF.B.EvidenceError('untrusted')
    assert caught.value.diagnostic['stage']=='BINDING'


def test_paused_and_transient_wait_remain_distinct_from_stop():
    for exception in (cloud_tick.SourceDispatchPaused('paused'),cloud_access.TransientReadError('retry'),cloud_tick.OverlapWait('WAIT_ACTIVE_CONTROLLER')):
        with pytest.raises(type(exception)) as caught:
            with D.gate('SOURCE_DISPATCH_BOUNDARY'):raise exception
        assert caught.value is exception and D.safe(exception) is None


def test_no_unknown_stage_or_category():
    with pytest.raises(ValueError):D.GateFailure('UNKNOWN','EVIDENCE')
    with pytest.raises(ValueError):D.GateFailure('LEASE','untrusted payload')
