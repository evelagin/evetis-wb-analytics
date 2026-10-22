"""The one STOP acceptance set for every consumer.

The FULL STOP gate, the progress monitor, the bounded FBO diagnostic and the
controller-closure recovery all ask the same question: which STOPPED records
are covered by an exact, revalidated owner recovery. Each used to keep its own
list, and the lists drifted (the diagnostic lacked STOP819, f507, R1 and R2).
Adding a recovery module here is the only way it becomes accepted anywhere.

Every module revalidates its own certificates on each read; nothing is cached
across wakes. The order and the verified_* side effects are those of the gate.
"""
import contextlib


class Scope:
    """One claimed wake's acceptance scope, shared by every backend object of that wake (gate, monitor)."""
    memo = None


def approved(backend, manifest, records, *, quarantines=None, gate=None, fresh=False):
    """Acceptance entries ({'stop_hash': ...}) of every exact owner recovery.

    Within one wake the same record slice gives the same answer: the result is
    kept on the backend keyed by the exact record and quarantine digests (any new
    record changes the key and forces a full revalidation). Never across wakes.
    fresh=True always revalidates: a decision right before a source/Run effect
    (diagnostic dispatch, automatic local isolation) never reuses the gate's answer.
    """
    from tools.tenancy import durable_plan as D
    scope = getattr(backend, 'stop_acceptance_scope', None)       # set only by a claimed wake (full_controller)
    if scope is None or fresh:
        return _revalidate(backend, manifest, records, quarantines, gate)[0]
    key = (id(scope), D.digest([sorted(D.digest(r) for r in records), D.digest(quarantines) if quarantines is not None else None]))
    holder = scope if isinstance(scope, Scope) else backend
    memo = getattr(holder, 'memo' if holder is scope else 'stop_acceptance_memo', None)
    if memo is not None and memo[0] == key:
        _, accepted, failed, rejected, zero_effect = memo
        backend.verified_cooldown_failures = failed
        backend.verified_pretransport_rejections = rejected
        backend.verified_fbo_zero_effect_failures = zero_effect
        return list(accepted)
    accepted, failed, rejected, zero_effect = _revalidate(backend, manifest, records, quarantines, gate)
    setattr(holder, 'memo' if holder is scope else 'stop_acceptance_memo', (key, list(accepted), failed, rejected, zero_effect))
    return accepted


def _revalidate(backend, manifest, records, quarantines, gate):
    from tools.tenancy import full_leaf_recovery as FL, full_cooldown_recovery as FC
    from tools.tenancy import full_policy_bootstrap_recovery as PB, full_pretransport_recovery as PT
    from tools.tenancy import full_first_receipt_recovery as FR, full_fbo_zero_effect_recovery as ZE
    from tools.tenancy import full_retained_pretransport_recovery as R1, full_inventory_gate_recovery as R2
    from tools.tenancy import full_controller_closure_recovery as CR, full_pre_authority_gate_recovery as PAG
    from tools.tenancy import full_fbo_diagnostic_closure as LQ
    from tools.tenancy import current_recon_recovery as CURRENT_RECON, f507_recovery as EXACT_F507
    from tools.tenancy import full_terminal_gate_recovery as TG
    has = lambda module: any(r['kind'] == module.KIND for r in records)
    with (gate('MANIFEST_ROOT') if gate else contextlib.nullcontext()):
        accepted = FL.load(backend, manifest, records) if has(FL) else []
        failed = FC.load(backend, manifest, records) if has(FC) else []
        paused_bootstrap = PB.load(backend, manifest, records) if has(PB) else []
        rejected = PT.load(backend, manifest, records) if has(PT) else []
        rejected = rejected + (R1.load(backend, manifest, records) if has(R1) else [])
    backend.verified_cooldown_failures = failed
    backend.verified_pretransport_rejections = rejected
    zero_effect = ZE.load(backend, manifest, records) if has(ZE) else []
    backend.verified_fbo_zero_effect_failures = zero_effect
    if quarantines is None:
        quarantines = LQ.load(backend, manifest, records) if has(LQ) else []
    accepted = accepted + failed + paused_bootstrap + rejected + (FR.load(backend, manifest, records) if has(FR) else [])
    accepted += [dict(stop_hash=h) for p in CR.load(backend, manifest, records) for h in p['stop_hashes']]
    accepted += zero_effect + quarantines + PAG.load(backend, manifest, records)
    accepted += CURRENT_RECON.load(backend, manifest, records)
    accepted += EXACT_F507.load(backend, manifest, records)
    accepted += R2.load(backend, manifest, records)
    accepted += TG.load(backend, manifest, records)
    return accepted, failed, rejected, zero_effect


def accepted_hashes(backend, manifest, records, **kw):
    return {x['stop_hash'] for x in approved(backend, manifest, records, **kw)}


def unresolved(backend, manifest, records, **kw):
    """STOPPED records no exact owner recovery covers (digest order)."""
    from tools.tenancy import durable_plan as D
    known = accepted_hashes(backend, manifest, records, **kw)
    return [r for r in records if r['kind'] == 'STOPPED' and D.digest(r) not in known]
