"""In-process simulation only: no URL, socket, HTTP client, subprocess or credentials."""
from dataclasses import dataclass, field, replace
import json
import re

from .domain import SafetyError, canonical, digest


@dataclass(frozen=True)
class Packet:
    operation_id: str
    kind: str
    environment: str
    participant: str
    business_hash: str
    transport: bytes = field(repr=False)
    signed: bytes = field(repr=False)
    wire: bytes = field(repr=False)
    attempt_id: str = ''


class FakeSigner:
    @staticmethod
    def packet(op, attempt_id=''):
        # Three representations are intentionally distinct. Synthetic signature ONLY.
        transport = json.dumps(json.loads(op['payload']), sort_keys=True, indent=1).encode()
        signed = transport
        wire = canonical({'simulation': True, 'attempt_id': attempt_id, 'payload': transport.decode(),
                          'fake_signature': digest(b'SYNTHETIC-SIGNATURE:' + signed)})
        return Packet(op['id'], op['kind'], op['environment'], op['participant'],
                      op['payload_hash'], transport, signed, wire, attempt_id)

    @staticmethod
    def verify(packet, payload):
        try:
            body = json.loads(packet.wire)
            valid = (packet.business_hash == digest(payload)
                     and canonical(json.loads(packet.transport)) == payload
                     and packet.signed == packet.transport
                     and body == {'simulation': True, 'attempt_id': packet.attempt_id, 'payload': packet.transport.decode(),
                                  'fake_signature': digest(b'SYNTHETIC-SIGNATURE:' + packet.signed)})
        except (ValueError, TypeError, UnicodeError):
            valid = False
        if not valid:
            raise SafetyError('Несовпадение business/transport/signed bytes')


@dataclass(frozen=True)
class Evidence:
    outcome: str
    operation_id: str
    payload_hash: str
    external_id: str | None
    http_status: int
    complete: bool = False
    codes: tuple = field(default=(), repr=False)
    environment: str = ''
    participant: str = ''
    kind: str = ''
    attempt_id: str = ''
    _origin: object = field(default=None, repr=False, compare=False)

    def body(self):
        # Private state evidence representation; never passed to Journal/stdout.
        return {'outcome': self.outcome, 'operation_id': self.operation_id,
                'payload_hash': self.payload_hash, 'external_id': self.external_id,
                'http_status': self.http_status, 'complete': self.complete,
                'codes': list(self.codes), 'environment': self.environment,
                'participant': self.participant, 'kind': self.kind, 'attempt_id': self.attempt_id}

    def safe(self):
        return {'outcome': self.outcome, 'external_id': self.external_id,
                'http_status': self.http_status,
                'response_hash': digest(canonical(self.body()))}


# Process-local provenance for the offline fake service, not an auth token/signature.
# Prevents a public Store caller from manufacturing a successful/rejected readback.
_ISSUED = {}


def _seal(result):
    result = replace(result, _origin=object())
    _ISSUED[result._origin] = digest(canonical(result.body()))
    return result


def verified_fake_evidence(result):
    if type(result) is not Evidence:
        return False
    try:
        return _ISSUED.get(result._origin) == digest(canonical(result.body()))
    except (TypeError, ValueError):
        return False


class _MemoryService:
    __slots__ = ('environment', 'participant', 'records', 'submission_count', 'mode', 'visible', 'closed')
    kinds = frozenset()

    def __init__(self, environment, participant):
        if environment not in {'PROD', 'SANDBOX'} or participant != 'SYN:participant':
            raise SafetyError('Неверная synthetic service identity')
        self.environment, self.participant = environment, participant
        self.records = {}
        self.submission_count = 0
        self.mode = 'success'
        self.visible = True
        self.closed = False

    def submit(self, packet):
        if (packet.kind not in self.kinds or (packet.environment, packet.participant) != (self.environment, self.participant)
                or not re.fullmatch('[0-9a-f]{32}', packet.attempt_id)):
            raise SafetyError('Неверный fake adapter')
        self.submission_count += 1
        if self.mode == 'reject':
            result = Evidence('REJECTED', packet.operation_id, packet.business_hash, None, 400, True,
                              environment=self.environment, participant=self.participant, kind=packet.kind, attempt_id=packet.attempt_id)
        else:
            # Deliberately NOT server-idempotent: duplicate submits create distinct IDs.
            ext = 'SYN:external-' + str(self.submission_count)
            payload = json.loads(packet.transport)
            codes = tuple('SYN:block-' + str(self.submission_count) + '-' + str(i)
                          for i in range(payload.get('quantity', 0))) if packet.kind == 'RETRIEVE' else ()
            outcome = 'PARTIAL' if self.mode == 'partial' else 'SUCCEEDED'
            result = Evidence(outcome, packet.operation_id, packet.business_hash, ext, 200, True, codes,
                              self.environment, self.participant, packet.kind, packet.attempt_id)
        result = _seal(result)
        key = result.external_id or 'SYN:rejection-' + str(self.submission_count)
        self.records[key] = result
        if self.mode == 'timeout':
            raise TimeoutError('synthetic timeout')
        return result if result.outcome == 'REJECTED' else _seal(Evidence('ACCEPTED', result.operation_id, result.payload_hash, result.external_id, 201,
                                                                        environment=self.environment, participant=self.participant, kind=packet.kind, attempt_id=packet.attempt_id))

    def blocks(self):
        return tuple(k for k, v in self.records.items() if v.codes) if self.visible else ()

    def retry_block(self, block_id):
        if self.closed or not self.visible:
            return None
        return self._readback_fault(self.records.get(block_id))

    def readback(self, operation_id, external_id=None):
        matches = [v for v in self.records.values() if v.operation_id == operation_id
                   and (external_id is None or v.external_id == external_id)] if self.visible else []
        return self._readback_fault(matches[0]) if len(matches) == 1 else None

    def _readback_fault(self, result):
        if result is None:
            return None
        faults = {'readback_wrong_id': {'external_id': 'SYN:external-999'},
                  'readback_incomplete': {'complete': False},
                  'readback_wrong_hash': {'payload_hash': '0' * 64},
                  'readback_wrong_env': {'environment': 'PROD' if self.environment == 'SANDBOX' else 'SANDBOX'},
                  'readback_wrong_kind': {'kind': 'NK_SIGN'},
                  'readback_wrong_participant': {'participant': 'SYN:other'},
                  'readback_contradiction': {'outcome': 'REJECTED'},
                  'readback_wrong_status': {'http_status': 400}}
        return _seal(replace(result, **faults[self.mode])) if self.mode in faults else result


class FakeSUZ(_MemoryService):
    kinds = frozenset({'ORDER', 'RETRIEVE', 'UTILISATION'})


class FakeTrueAPI(_MemoryService):
    kinds = frozenset({'INTRODUCTION', 'SET'})


class FakeNK(_MemoryService):
    kinds = frozenset({'NK_FEED', 'NK_SIGN'})
