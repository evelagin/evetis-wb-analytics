"""Network-free regression for the packaged -m/imported FULL backend boundary."""
import json
import runpy
from types import SimpleNamespace

import pytest
from tools.tenancy import cloud_controller as C, cloud_tick as T, full_controller as F
from tools.tenancy import tenant_backfill as BF


def entrypoint(monkeypatch, backend, wake):
    # -m executes a second module namespace while FULL imports its canonical
    # module. Avoid its automatic main invocation so no live bootstrap occurs.
    namespace = runpy.run_module('tools.tenancy.cloud_controller', run_name='offline_packaged_entrypoint')
    globals_ = namespace['main'].__globals__
    globals_['bootstrap'] = lambda env: (backend, '3' * 64)
    globals_['bounded_wake'] = wake
    return namespace


def test_packaged_paused_full_backend_is_quiescent_without_source_or_stop(monkeypatch, capsys):
    writes = []
    backend = object.__new__(F.Backend)
    backend.c = {'orchestration': {'job': {'env': {'HISTORICAL_SCHEDULER_STATE': 'PAUSED'}}}}
    backend.current_execution = 'offline-controller'
    backend.clock = lambda: 'offline'
    backend.store = SimpleNamespace(commit=lambda *args: writes.append(args))
    monkeypatch.setattr(BF, 'start', lambda *args, **kwargs: pytest.fail('lease/source touched'))
    def wake(root, b):
        b.start({}, lambda *a: pytest.fail('intent'), lambda *a: pytest.fail('receipt'))
    namespace = entrypoint(monkeypatch, backend, wake)
    assert namespace['SourceDispatchPaused'] is C.SourceDispatchPaused is T.SourceDispatchPaused
    assert namespace['main']() == 0
    assert json.loads(capsys.readouterr().out) == {'status': 'QUIESCENT_PAUSED', 'source_dispatches': 0}
    assert writes == []


@pytest.mark.parametrize('error', [BF.B.EvidenceError('wrong root'), BF.TT.TableError('reader denied'), ValueError('unknown predicate')])
def test_packaged_entrypoint_does_not_exempt_other_gate_failures(monkeypatch, capsys, error):
    existing = [('STOPPED', 'immutable-original')]
    writes = []
    backend = SimpleNamespace(current_execution='offline-controller', clock=lambda: 'offline',
                              store=SimpleNamespace(commit=lambda *a: writes.append(a)))
    def wake(*args):
        raise error
    namespace = entrypoint(monkeypatch, backend, wake)
    assert namespace['main']() == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'STOPPED'
    assert len(writes) == 1 and writes[0][1] == 'STOPPED'
    assert existing == [('STOPPED', 'immutable-original')]


def test_packaged_qualification_exercises_paused_entrypoint():
    from tools.tenancy.full_image_check import paused_entrypoint_check
    assert paused_entrypoint_check() == 'PASS'
