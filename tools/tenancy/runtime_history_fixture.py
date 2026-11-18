"""Offline image-check support: evaluate a frozen historical incident with its own runtime.

Retained recovery fixtures (f507, CURRENT recon, terminal gate, R1/R2) are byte-exact copies
of production states that were produced by controllers packaging the runtime registered in
their own release (0babe49f…). After a runtime succession the tree packages a newer runtime;
those incidents are then replayed *as they happened*: the packaged-runtime identity is pinned
to the historical release's registered runtime, and the tenant contract resolves the matching
registered runtime image through the ordinary registry (tenant_backfill.target). Nothing else
is stubbed. Retained validation under the NEW runtime is covered separately
(current_runtime_succession_image_check).
"""
from contextlib import contextmanager
from tools.tenancy.validation import parse_tenant_json as parse

# Registered controller release whose packaged runtime the frozen incidents ran with.
HISTORICAL_SOURCE = '8429b691f3c348e515a5db42d57c8ff040a46018'


def historical_hash(source=HISTORICAL_SOURCE):
    from tools.tenancy import tenant_backfill as BF
    return parse((BF.REPO / 'infra/tenant/releases/backfill' / (source + '.json')).read_text())['runtime_implementation_hash']


@contextmanager
def historical_runtime(source=HISTORICAL_SOURCE):
    from tools.tenancy import tenant_backfill as BF
    pinned = historical_hash(source); live = BF.B.implementation_hash
    if live() == pinned:
        yield pinned; return
    BF.B.implementation_hash = lambda: pinned
    try:
        yield pinned
    finally:
        BF.B.implementation_hash = live
