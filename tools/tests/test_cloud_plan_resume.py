"""Publisher must retain budget history but reject conflicting current proofs."""
import copy
import json
from datetime import date
import pytest
from tools.tenancy import cloud_plan as P, tenant_backfill as BF


def fixture():
    doc=BF.make_plan('client_001','supplies','2026-09-17','2026-09-17',
                     'synthetic-retained','2026-10-05T00:00:00Z',date(2026,10,6),max_units=20)
    previous=copy.deepcopy(doc);previous['max_units']=1
    previous['ack_hash']=BF.B.digest({k:v for k,v in previous.items() if k!='ack_hash'})
    state=BF.B.initial(doc['runtime_plan']);state['sequence']=55
    proof={'plan':doc['runtime_plan'],'state':state}
    older=copy.deepcopy(proof);older['state']['sequence']=11
    rows=[{'plan_hash':previous['ack_hash'],'evidence_json':json.dumps({'proof':older})},
          {'plan_hash':doc['ack_hash'],'evidence_json':json.dumps({'proof':proof})}]
    return doc,proof,rows


def reader(proof,rows,calls):
    def read(c,sql,params):
        calls.append((sql,copy.deepcopy(params)))
        if 'MAX(backfill_sequence)' in sql:return [{'seq':55}]
        if 'OZON_INGESTION_RUNS' in sql:return [{'evidence_json':json.dumps(proof)}]
        # Model actual historical/current retained records, including LIMIT 2.
        selected=rows
        if '= @seq' in sql or '=@seq' in sql:
            selected=[r for r in rows if json.loads(r['evidence_json'])['proof']['state']['sequence']==params['seq'][1]]
        unique={json.dumps(r,sort_keys=True):r for r in selected}
        return list(unique.values())[:2]
    return read


def test_prior_budget_ack_is_preserved_and_latest_resume_is_proven():
    doc,proof,rows=fixture();before=copy.deepcopy((doc,proof,rows));calls=[]
    assert rows[0]['plan_hash']!=doc['ack_hash']
    P.verify_retained_plan(BF.target('client_001'),doc,reader(proof,rows,calls))
    assert (doc,proof,rows)==before
    assert calls[-1][1]['seq']==('INT64',55)


@pytest.mark.parametrize('change', ['missing','wrong_ack','conflicting_ack','conflicting_state','wrong_plan'])
def test_current_proof_failure_never_becomes_permission_to_publish(change):
    doc,proof,rows=fixture();current=rows[-1]
    if change=='missing':rows.pop()
    elif change=='wrong_ack':current['plan_hash']='a'*64
    elif change=='conflicting_ack':rows.append(dict(current,plan_hash='a'*64))
    else:
        value=json.loads(current['evidence_json'])
        if change=='conflicting_state':value['proof']['state']['rows']+=1
        else:value['proof']['plan']['generation']='different'
        current['evidence_json']=json.dumps(value)
    before=copy.deepcopy(rows)
    with pytest.raises(BF.B.EvidenceError,match='retained'):
        P.verify_retained_plan(BF.target('client_001'),doc,reader(proof,rows,[]))
    assert rows==before


def test_ambiguous_latest_source_proof_stops_before_checkpoint_read():
    doc,proof,rows=fixture()
    def read(c,sql,params):
        if 'MAX(backfill_sequence)' in sql:return [{'seq':55}]
        if 'OZON_INGESTION_RUNS' in sql:return [{'evidence_json':json.dumps(proof)}]*2
        pytest.fail('ambiguous source proof reached checkpoint/publication')
    with pytest.raises(BF.B.EvidenceError,match='ambiguous'):
        P.verify_retained_plan(BF.target('client_001'),doc,read)


def test_source_row_sequence_must_match_its_proof():
    doc,proof,rows=fixture();proof['state']['sequence']=54
    with pytest.raises(BF.B.EvidenceError,match='source sequence'):
        P.verify_retained_plan(BF.target('client_001'),doc,reader(proof,rows,[]))
