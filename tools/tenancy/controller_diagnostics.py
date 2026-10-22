"""Closed, payload-free gate diagnostics shared by imported and -m entrypoints."""
from contextlib import contextmanager
from tools.tenancy import tenant_backfill as BF

STAGES = frozenset({'MANIFEST_ROOT','BINDING','QUOTA','LEASE','CHECKPOINT',
    'RUNTIME_TERMINAL','PERSISTED_SOURCE_RECONCILIATION','RECONCILIATION_APPEND',
    'MONITORING_WATERMARK','SOURCE_DISPATCH_BOUNDARY','CONTROL_CLOSURE'})
# Closed write ledger of one source dispatch (S‴-a, 2026-10-22): each durable write
# of BF.start is ABSENT (never attempted), UNKNOWN (sent, outcome unproven) or
# PRESENT (acknowledged). It travels only inside a typed STOP diagnostic.
WRITES = ('lease','checkpoint','intent','run','receipt')
OUTCOMES = frozenset({'ABSENT','UNKNOWN','PRESENT'})
PILOT_HELD = 'pilot lease held; reconcile terminal execution before continuation'


class GateFailure(BF.B.EvidenceError):
    def __init__(self, stage, category, readback=None):
        if stage not in STAGES or category not in {'EVIDENCE','CLOUD_METADATA','SCHEMA','LOCAL_IO'}:
            raise ValueError('closed diagnostic code required')
        self.diagnostic = {'version':1,'stage':stage,'category':category}
        if readback is not None:
            self.diagnostic['readback'] = ledger(readback)
        super().__init__('typed controller gate failure')


def ledger(effects):
    if not isinstance(effects,dict) or set(effects)!=set(WRITES) or any(effects[k] not in OUTCOMES for k in WRITES):
        raise ValueError('closed write ledger required')
    return {k:effects[k] for k in WRITES}


def written(effects):
    return any(v!='ABSENT' for v in ledger(effects).values())


@contextmanager
def write_boundary(effects):
    """Typed STOP for every failure once a dispatch write was sent; never a wait.

    Before the first write a transient read keeps its bounded retry and a held
    pilot lease keeps its exact text (typed by the cloud entrypoint). After a
    write was sent an unknown outcome is a durable STOP carrying the ledger:
    a lost response can never become a routine wait or a repeated POST.
    """
    try:yield
    except GateFailure as failure:
        failure.diagnostic['readback']=ledger(effects);raise
    except BF.B.EvidenceError as error:
        from tools.tenancy.cloud_tick import SourceDispatchPaused,OverlapWait
        if not written(effects) and (isinstance(error,(SourceDispatchPaused,OverlapWait)) or str(error)==PILOT_HELD):raise
        failure=GateFailure('SOURCE_DISPATCH_BOUNDARY','EVIDENCE',effects)
        failure.unclosed_holder=getattr(error,'unclosed_holder',None)
        raise failure from None
    except BF.TT.TableError as error:
        from tools.tenancy.cloud_access import TransientReadError
        if not written(effects) and isinstance(error,TransientReadError):raise
        raise GateFailure('SOURCE_DISPATCH_BOUNDARY','CLOUD_METADATA',effects) from None
    except (ValueError,KeyError,TypeError,IndexError):raise GateFailure('SOURCE_DISPATCH_BOUNDARY','SCHEMA',effects) from None
    except OSError:raise GateFailure('SOURCE_DISPATCH_BOUNDARY','LOCAL_IO',effects) from None
    except Exception:
        # Any other failure after a sent write is still a typed STOP (never a traceback that leaves the claim open).
        if not written(effects):raise
        raise GateFailure('SOURCE_DISPATCH_BOUNDARY','SCHEMA',effects) from None


@contextmanager
def gate(stage):
    if stage not in STAGES:raise ValueError('unknown controller stage')
    try:yield
    except GateFailure:raise
    except BF.B.EvidenceError as error:
        from tools.tenancy.cloud_tick import SourceDispatchPaused,OverlapWait
        if isinstance(error,(SourceDispatchPaused,OverlapWait)) or str(error)==PILOT_HELD:raise
        failure=GateFailure(stage,'EVIDENCE')
        # The diagnostic stays the closed {version,stage,category}; only the
        # recognised cause travels with it (never payloads or identifiers).
        failure.unclosed_holder=getattr(error,'unclosed_holder',None)
        raise failure from None
    except BF.TT.TableError as error:
        from tools.tenancy.cloud_access import TransientReadError
        # Normal bounded read retry remains distinct from a durable hard STOP.
        if isinstance(error,TransientReadError):raise
        raise GateFailure(stage,'CLOUD_METADATA') from None
    except (ValueError,KeyError,TypeError,IndexError):raise GateFailure(stage,'SCHEMA') from None
    except OSError:raise GateFailure(stage,'LOCAL_IO') from None


def safe(error):
    if not isinstance(error,GateFailure):return None
    d=error.diagnostic
    if (not isinstance(d,dict) or type(d.get('version')) is not int or d.get('version')!=1
            or d.get('stage') not in STAGES or d.get('category') not in {'EVIDENCE','CLOUD_METADATA','SCHEMA','LOCAL_IO'}):return None
    out={k:d[k] for k in ('version','stage','category')}
    context=d.get('context')
    keys={'index','shard','sequence','intent_hash','receipt_hash'}
    if isinstance(context,dict) and set(context)==keys:
        import re
        if (type(context['index']) is int and context['index']>=0 and type(context['sequence']) is int and context['sequence']>=1
                and all(isinstance(context[k],str) and re.fullmatch('[0-9a-f]{64}',context[k]) for k in keys-{'index','sequence'})):
            out['context']=dict(context)
    readback=d.get('readback')
    if isinstance(readback,dict) and set(readback)==set(WRITES) and all(readback[k] in OUTCOMES for k in WRITES):
        out['readback']={k:readback[k] for k in WRITES}
    return out
