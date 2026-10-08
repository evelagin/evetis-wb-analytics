"""Bounded durable tick protocol. Backend owns canonical, qualified execution.

There is no marketplace transport here. One tick dispatches at most one runtime
execution, persists intent before POST, and never repeats uncertain dispatch.
Full-history activation requires a separate proven gate; qualification is not GO.
"""
from datetime import datetime, timezone
from tools.tenancy import durable_plan as D


class Tick:
    def __init__(self, root_hash, store, backend, clock=None):
        self.root = D.check_hash(root_hash)
        self.store, self.backend = store, backend
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def commit(self, kind, sequence, payload):
        return self.store.commit(self.root, kind, sequence, payload, self.clock())

    def run(self):
        records = self.store.history(self.root)
        manifests = [r for r in records if r['kind'] == 'MANIFEST']
        if len(manifests) != 1 or manifests[0]['sequence'] != 0:
            raise D.BF.B.EvidenceError('canonical committed manifest missing/ambiguous')
        manifest = manifests[0]['payload']
        D.validate_manifest(manifest)
        if manifest['hash'] != self.root:
            raise D.BF.B.EvidenceError('manifest root mismatch')
        if manifest['purpose'] != 'QUALIFICATION':
            raise D.BF.B.EvidenceError('full-history gate adapter unproven')
        # Even WAIT/recovery observes fresh binding, inventory and hold gates.
        # This method may only read; backend start repeats preflight before writes.
        self.backend.preflight(manifest)
        active = self.backend.active_runtime_execution()
        if any(r['kind']=='COMPLETE' for r in records) and not active:
            return {'status':'QUALIFICATION_COMPLETE','source_dispatches':0}
        from tools.tenancy import pre_source_recovery as PR
        recoveries=PR.load(self.backend,records,self.root) if any(r['kind']==PR.KIND for r in records) else []
        from tools.tenancy import controller_stop_recovery as CR
        controller_recoveries=CR.load(self.backend,records,self.root) if any(r['kind']==CR.KIND for r in records) else []
        self.backend.verified_pre_source_failures=recoveries
        quota = self.backend.quota(manifest, self.clock())
        decision = D.decide_tick(records, self.root, quota, active, recoveries, controller_recoveries)
        action = decision['action']
        if action == 'MONITOR':
            return {'status':'MONITORING','source_dispatches':0}
        if action == 'STOPPED':
            if decision.get('reason'):
                self.commit('STOPPED',0,{'reason':decision['reason']})
            return {'status':'STOPPED','source_dispatches':0}
        if action == 'WAITING' and self.backend.all_complete(manifest):
            evidence=self.backend.completion_evidence(manifest)
            if not evidence or evidence.get('source_complete') is not True or evidence.get('persisted_reconciled') is not True:
                raise D.BF.B.EvidenceError('qualification completion evidence unproven')
            self.commit('COMPLETE',0,evidence)
            return {'status':'QUALIFICATION_COMPLETE','source_dispatches':0}
        if action == 'WAITING':
            self.commit('WAITING', 0, {'eligible_at':decision['eligible_at'],
                                    'basis':'MODELED_ROLLING_BUDGET_NOT_SOURCE_REJECTION'})
            return {'status':'WAITING','eligible_at':decision['eligible_at'],'source_dispatches':0}
        sequence = decision['sequence']
        if action == 'RECOVER_RECEIPT_OR_STOP':
            intent = next(r['payload'] for r in records if r['kind']=='DISPATCH_INTENT' and r['sequence']==sequence)
            # No resubmission even if no execution is visible; absence after a
            # timeout is not proof that the original Run POST had no effect.
            receipt = self.backend.recover_receipt(intent)
            if receipt is None:
                self.commit('STOPPED',sequence,{'reason':'DISPATCH_RECEIPT_UNPROVEN'})
                return {'status':'STOPPED','reason':'DISPATCH_RECEIPT_UNPROVEN','source_dispatches':0}
            self.commit('DISPATCH_RECEIPT',sequence,receipt)
            return {'status':'RECEIPT_RECOVERED','source_dispatches':0}
        if action == 'RECONCILE':
            receipt = next(r['payload'] for r in records if r['kind']=='DISPATCH_RECEIPT' and r['sequence']==sequence)
            result = self.backend.reconcile(receipt)
            if result is None:
                return {'status':'MONITORING','source_dispatches':0}
            self.commit('RECONCILED',sequence,result)
            return {'status':'RECONCILED','source_dispatches':0}
        if action != 'PREPARE_NEXT':
            raise D.BF.B.EvidenceError('unsupported tick verdict')
        # Source/checkpoint/DQ readback chooses one frozen scope or the explicitly
        # authorized current-day Catalog dependency. It cannot invent a cohort,
        # replace a plan/generation, or transform partial into completion.
        plan = self.backend.next_plan(manifest, records, quota)
        if plan is None:
            evidence = self.backend.completion_evidence(manifest)
            if not evidence or evidence.get('source_complete') is not True or evidence.get('persisted_reconciled') is not True:
                raise D.BF.B.EvidenceError('qualification completion evidence unproven')
            self.commit('COMPLETE',0,evidence)
            return {'status':'QUALIFICATION_COMPLETE','source_dispatches':0}
        D.BF.validate_plan(plan, plan.get('ack_hash'))
        prepared, received = [], []
        def before(preparation):
            if prepared:
                raise D.BF.B.EvidenceError('one dispatch intent per tick')
            payload={'plan':plan,'preparation':preparation}
            self.commit('DISPATCH_INTENT',sequence,payload)
            prepared.append(payload)
        def after(receipt):
            if len(prepared)!=1 or received:
                raise D.BF.B.EvidenceError('receipt without exact committed intent')
            preparation=prepared[0]['preparation']
            if any(receipt.get(k)!=preparation[k] for k in ('run_id','lease_generation','ack_hash')):
                raise D.BF.B.EvidenceError('dispatch receipt scope mismatch')
            self.commit('DISPATCH_RECEIPT',sequence,{'plan':plan,'receipt':receipt})
            received.append(receipt)
        self.backend.start(plan,before,after)
        if len(prepared)!=1 or len(received)!=1:
            raise D.BF.B.EvidenceError('durable dispatch hooks not proven; never repeat POST')
        return {'status':'DISPATCHED','sequence':sequence,'source_dispatches':1}
