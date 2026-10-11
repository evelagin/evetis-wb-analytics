"""Forward artifact pin retains only the previously accepted exact incident."""
from copy import deepcopy
import pytest
import backfill_core as B
import cooldown_failed as CF


def fixture():
    p=dict(tenant='client_001',project='mpa-t-client-001',root=CF.EXACT['root'],hash='a'*64)
    proof=dict(hash='b'*64)
    v=CF.sealed(dict(version=1,type='FAILED_COOLDOWN_EXACT_ARTIFACT_HANDOFF',
        tenant=p['tenant'],project=p['project'],root=p['root'],policy_hash=p['hash'],
        certificate_hash=proof['hash'],runtime_source='c'*40,
        runtime_image='europe-west1-docker.pkg.dev/mpa-platform/mpa-runtime/ozon-runtime@sha256:'+'d'*64,
        runtime_implementation_hash='e'*64,owner_ack_sha256='f'*64,source_budget=0))
    return p,proof,v


def test_forward_pin_never_changes_accepted_policy_or_proof():
    p,proof,v=fixture();before=deepcopy((p,proof))
    assert CF.compatible_attestation(v,p,proof,'e'*64)==v
    assert (p,proof)==before


@pytest.mark.parametrize('field,value',[
    ('tenant','foreign'),('project','foreign'),('root','0'*64),
    ('policy_hash','0'*64),('certificate_hash','0'*64),
    ('runtime_implementation_hash','0'*64),('runtime_source','unknown'),
    ('runtime_image','ozon-runtime:latest'),('owner_ack_sha256','UNKNOWN'),
    ('source_budget',1),('source_budget',False),('version',True),
    ('type','GENERIC_FAILED_RECOVERY'),('extra',True)])
def test_foreign_or_unqualified_pin_blocks(field,value):
    p,proof,v=fixture();v[field]=value;v=CF.sealed({k:x for k,x in v.items() if k!='hash'})
    with pytest.raises(B.EvidenceError):CF.compatible_attestation(v,p,proof,'e'*64)


def test_unsealed_pin_blocks():
    p,proof,v=fixture();v['hash']='0'*64
    with pytest.raises(B.EvidenceError):CF.compatible_attestation(v,p,proof,'e'*64)
