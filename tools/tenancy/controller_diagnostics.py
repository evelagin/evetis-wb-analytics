"""Closed, payload-free gate diagnostics shared by imported and -m entrypoints."""
from contextlib import contextmanager
from tools.tenancy import tenant_backfill as BF

STAGES = frozenset({'MANIFEST_ROOT','BINDING','QUOTA','LEASE','CHECKPOINT',
    'RUNTIME_TERMINAL','PERSISTED_SOURCE_RECONCILIATION','RECONCILIATION_APPEND',
    'MONITORING_WATERMARK','SOURCE_DISPATCH_BOUNDARY'})


class GateFailure(BF.B.EvidenceError):
    def __init__(self, stage, category):
        if stage not in STAGES or category not in {'EVIDENCE','CLOUD_METADATA','SCHEMA','LOCAL_IO'}:
            raise ValueError('closed diagnostic code required')
        self.diagnostic = {'version':1,'stage':stage,'category':category}
        super().__init__('typed controller gate failure')


@contextmanager
def gate(stage):
    if stage not in STAGES:raise ValueError('unknown controller stage')
    try:yield
    except GateFailure:raise
    except BF.B.EvidenceError as error:
        from tools.tenancy.cloud_tick import SourceDispatchPaused,OverlapWait
        if isinstance(error,(SourceDispatchPaused,OverlapWait)) or str(error)=='pilot lease held; reconcile terminal execution before continuation':raise
        raise GateFailure(stage,'EVIDENCE') from None
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
    return out
