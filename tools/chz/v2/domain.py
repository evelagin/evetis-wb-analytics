"""Versioned synthetic business identity; deliberately not a CHZ payload schema."""
from dataclasses import dataclass, field
import hashlib
import json
import re

KINDS = {'ORDER', 'RETRIEVE', 'UTILISATION', 'INTRODUCTION', 'SET', 'NK_FEED', 'NK_SIGN'}
STATES = {'PREPARED', 'APPROVED', 'SUBMITTING', 'UNKNOWN', 'RECONCILING',
          'ACCEPTED', 'SUCCEEDED', 'REJECTED', 'PARTIAL', 'INVALIDATED', 'CANCELLED'}
TRANSITIONS = {
    'PREPARED': {'APPROVED', 'CANCELLED'},
    'APPROVED': {'SUBMITTING', 'INVALIDATED', 'CANCELLED'},
    'SUBMITTING': {'UNKNOWN', 'ACCEPTED', 'REJECTED'},
    'UNKNOWN': {'RECONCILING'},
    'ACCEPTED': {'RECONCILING'},
    'RECONCILING': {'UNKNOWN', 'ACCEPTED', 'SUCCEEDED', 'REJECTED', 'PARTIAL'},
    'REJECTED': {'APPROVED'},  # explicit new approval, never an automatic retry
}


class SafetyError(Exception):
    """Only constant safe diagnostic messages; never interpolate payloads."""


class Crash(BaseException):
    """Synthetic process loss; intentionally bypasses ordinary error handling."""


def hit(fault, point):
    if fault == point:
        raise Crash(point)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def synthetic(value):
    if not isinstance(value, str) or not re.fullmatch(r'SYN:[A-Za-z0-9_-]{1,80}', value):
        raise SafetyError('Требуется синтетический идентификатор')
    return value


def gtin(value):
    if not isinstance(value, str) or not re.fullmatch(r'TEST-GTIN-[0-9]{3}', value):
        raise SafetyError('Реальные GTIN запрещены в offline core')
    return value


def validate_payload(kind, payload):
    if kind not in KINDS or not isinstance(payload, dict):
        raise SafetyError('Неподдерживаемая synthetic operation')
    # Closed grammar: no arbitrary headers, credentials, URLs, XML or parameters.
    allowed = {'gtin', 'quantity', 'codes', 'sets', 'order', 'version', 'value'}
    if set(payload) - allowed:
        raise SafetyError('Неизвестное поле synthetic payload')
    p = json.loads(canonical(payload))
    gtin(p.get('gtin'))
    if 'quantity' in p and (type(p['quantity']) is not int or p['quantity'] < 1):
        raise SafetyError('Количество должно быть положительным целым')
    for key in ('order', 'version', 'value'):
        if key in p:
            synthetic(p[key])
    codes = p.get('codes', [])
    if not isinstance(codes, list):
        raise SafetyError('Некорректный список')
    for code in codes:
        synthetic(code)
    if len(codes) != len(set(codes)):
        raise SafetyError('Повтор КИ')
    p['codes'] = sorted(codes) if 'codes' in p else []
    units = p.get('sets', [])
    if not isinstance(units, list):
        raise SafetyError('Некорректные наборы')
    parents, children = [], []
    for u in units:
        if not isinstance(u, dict) or set(u) != {'parent', 'children'} or not isinstance(u['children'], list) or not u['children']:
            raise SafetyError('Требуется явная матрица parent/children')
        parents.append(synthetic(u['parent']))
        children.extend(synthetic(c) for c in u['children'])
        u['children'] = sorted(u['children'])
    if len(parents) != len(set(parents)) or len(children) != len(set(children)) or set(parents) & set(children):
        raise SafetyError('Повтор КИ или циклический состав')
    if units:
        p['sets'] = sorted(units, key=lambda u: u['parent'])
    required = {'ORDER': ('quantity',), 'RETRIEVE': ('quantity', 'order'),
                'UTILISATION': ('codes',), 'INTRODUCTION': ('codes',), 'SET': ('sets',),
                'NK_FEED': ('version', 'value'), 'NK_SIGN': ('version', 'value')}
    if any(not p.get(k) for k in required[kind]):
        raise SafetyError('Неполный synthetic payload')
    return p


@dataclass(frozen=True)
class Prepared:
    operation_id: str
    fingerprint: str
    payload_hash: str
    environment: str
    participant: str
    kind: str
    demand: str
    payload: bytes = field(repr=False)

    @classmethod
    def create(cls, environment, participant, kind, demand, payload):
        if environment not in {'PROD', 'SANDBOX'}:
            raise SafetyError('Среда обязательна')
        synthetic(participant)
        synthetic(demand)
        body = canonical(validate_payload(kind, payload))
        content = {'schema': 'chz-offline/1', 'environment': environment,
                   'participant': participant, 'kind': kind, 'payload_hash': digest(body)}
        return cls(digest(canonical({**content, 'demand': demand})), digest(canonical(content)),
                   digest(body), environment, participant, kind, demand, body)

    def preview(self):
        p = json.loads(self.payload)
        units = p.get('sets', [])
        alias = lambda c: 'КИ#' + digest(c.encode())[:12]
        return {'simulation': True, 'environment': self.environment, 'kind': self.kind,
                'operation_id': self.operation_id, 'payload_hash': self.payload_hash,
                'gtin': p['gtin'], 'quantity': p.get('quantity', len(units) or len(p['codes'])),
                'codes': [alias(c) for c in p['codes']],
                'sets': [{'parent': alias(u['parent']), 'children': [alias(c) for c in u['children']]} for u in units],
                'effect': 'Только синтетическая модель; внешний эффект невозможен',
                'rollback': 'Не является разрешением реальной компенсации'}
