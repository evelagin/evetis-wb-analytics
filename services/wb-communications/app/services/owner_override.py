"""Two-click, exact-version consent. This module has no marketplace client calls.

The first click writes only a local confirmation record + operator UI. The second
enters the ordinary verified publisher with one atomically consumed confirmation.
Spans are operator-only / Firestore audit; never put them into log/outbox traces.
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from app.domain.exceptions import InvalidTransition, NotFound
from app.utils.text import clean_answer, escape_html, is_within_wb_limit
from app.v3.snapshot import load_snapshot
from app.v3.classifier import classify_rules
from app.services.publication_policy import validate_for_publication


def sha(text):
    return hashlib.sha256(clean_answer(text).encode()).hexdigest()


def context_hash(doc):
    return hashlib.sha256(json.dumps({k:doc.get(k) for k in ('nm_id','supplier_article','barcode',
        'entity_type','channel','text','pros','cons','rating')},sort_keys=True,ensure_ascii=False,
        separators=(',',':')).encode()).hexdigest()


def fingerprint(policy):
    return hashlib.sha256(json.dumps({k:policy.get(k) for k in
        ('version','snapshot','snapshot_sha256','expertise_sha256','verdict','violations')},
        sort_keys=True,separators=(',',':')).encode()).hexdigest()


def enabled(settings):
    """LEGACY (R2 cards): override publishes past the v3.1E policy, so it exists only where
    live publication enforces that policy. R2.2 cards use owner_authority() instead."""
    return bool(getattr(settings,'v31_owner_override_enabled',False)
                and getattr(settings,'v31_enforce_live_publication_policy',False))


def owner_ids(settings):
    return {str(u) for u in (getattr(settings,'v31_owner_override_user_ids',None) or set())}


def owner_authority(settings):
    """R2.3: the owner's final word on R2.2 (v31_only) cards. Independent of the V2/R2.1
    flags; needs the override flag AND an explicit owner list (empty = nobody)."""
    return bool(getattr(settings,'v31_owner_override_enabled',False) and owner_ids(settings))


def _v31_only(doc):
    return (doc or {}).get('operator_mode')=='v31_only'


def allowed(deps,chat,user,doc=None):
    from app.utils.security import is_allowed
    users=getattr(deps.settings,'telegram_allowed_user_ids',set())
    base=(bool(users) and str(user) in {str(u) for u in users}
          and is_allowed(chat,user,deps.settings.allowed_chat_ids,users))
    if doc is not None and _v31_only(doc):
        # Only an explicitly listed owner, who is also an allowlisted operator in this chat.
        return base and owner_authority(deps.settings) and str(user) in owner_ids(deps.settings)
    return base and (enabled(deps.settings) or owner_authority(deps.settings))


def safety_fixed(doc,settings):
    snap=load_snapshot(settings.v3_knowledge_snapshot_id or None)
    cls=classify_rules(doc,snap)
    return 'SAFETY' in cls.escalation_domains or any(c.startswith(('REACTION.','EMERGENCY.')) for c in cls.codes)


def source_text(doc,version):
    rows=[v for v in doc.get('answer_versions',[]) if v.get('generation_number')==version]
    if len(rows)!=1:
        raise InvalidTransition('source version missing or ambiguous')
    return clean_answer(rows[0]['text'])


def validate_binding(doc,pending):
    try:
        expires=datetime.fromisoformat(pending['expires_at'])
        valid=(expires>datetime.now(timezone.utc) and pending['current_generation']==doc.get('generation_number',0)
            and pending['current_hash']==sha(doc.get('final_answer') or doc.get('ai_answer'))
            and pending['communication_context_sha256']==context_hash(doc)
            and pending['answer_hash']==sha(source_text(doc,pending['source_version'])))
    except (KeyError,TypeError,ValueError):
        valid=False
    if not valid: raise InvalidTransition('override expired or draft changed')


def _offer_v31(doc,settings):
    """R2.3: the exact active answer of an R2.2 card that the current policy BLOCKs or could
    not check. Serious safety: only operator-written text, never a machine draft. V2 never."""
    from app.services.pipeline import _answer_source, _serious, publication_mode_for
    from app.services.publication_policy import error_policy, validator_for
    if not owner_authority(settings): return None
    if doc.get('status') not in {'pending_approval','policy_blocked','policy_check_failed','publish_failed'}:
        return None
    rows=doc.get('answer_versions') or []
    if not rows or rows[-1].get('source') not in {'v31e','manual'}: return None
    row=rows[-1]
    if (_serious(doc) or safety_fixed(doc,settings)) and row['source']!='manual': return None
    text=clean_answer(row['text'])
    if not is_within_wb_limit(text): return None      # technical limits are never overridden
    try:
        policy=validator_for(publication_mode_for(doc),settings)(text,doc,settings,include_spans=True)
        if not isinstance(policy,dict) or policy.get('verdict') not in ('PASS','INFO','WARNING','BLOCK'):
            raise ValueError('invalid policy verdict')
    except Exception as exc:  # noqa: BLE001 — a policy outage must not lock manual work forever
        policy=error_policy(text,exc)
    if policy['verdict'] not in ('BLOCK','ERROR'): return None
    policy.setdefault('version',None);policy.setdefault('snapshot',None)
    policy.setdefault('snapshot_sha256',None);policy.setdefault('expertise_sha256',None)
    return dict(source_version=row['generation_number'],text=text,policy=policy)


def offer(doc,settings):
    if doc and _v31_only(doc):
        return _offer_v31(doc,settings)
    if not enabled(settings):
        return None
    if not doc or doc.get('status') not in {'pending_approval','policy_blocked','publish_failed'}:
        return None
    if safety_fixed(doc,settings): return None
    target=(doc.get('response_recovery') or {}).get('original_text_sha256')
    rows=list(reversed(doc.get('answer_versions') or []))
    if not rows: return None
    # Repair preserves original source. No hidden substitution with another generation.
    row=next((v for v in rows if target and sha(v['text'])==target),rows[0])
    text=clean_answer(row['text'])
    if not is_within_wb_limit(text): return None
    policy=validate_for_publication(text,doc,settings,include_spans=True)
    if policy['verdict']!='BLOCK': return None
    return dict(source_version=row['generation_number'],text=text,policy=policy)


def _deny():
    from app.utils.logging import audit_event
    audit_event('auth_denied',route='/telegram-webhook',mechanism='owner_override_allowlist',
                principal_class='unknown',result='not_allowlisted')
    return {'status':'unauthorized'}


def handle(deps,action,payload,chat,message_id,user):
    from app.services.pipeline import _publish, _stale
    if not allowed(deps,chat,user):
        return _deny()
    parts=payload.split(':');doc_id=parts[0]
    doc=deps.repo.get(doc_id)
    if not doc: return {'status':'not_found'}
    if not allowed(deps,chat,user,doc):
        return _deny()
    if not _v31_only(doc) and safety_fixed(doc,deps.settings): return {'status':'safety_fixed'}
    if action=='ov':
        if len(parts)!=3 or not all(v.isdigit() for v in parts[1:]): return {'status':'bad_request'}
        generation,source_version=map(int,parts[1:])
        candidate=offer(doc,deps.settings)
        if not candidate or candidate['source_version']!=source_version:
            return _stale(deps,chat,'⚠️ Исходный вариант изменился. Прочитайте новую карточку.')
        policy=candidate['policy'];now=datetime.now(timezone.utc)
        pending=dict(nonce=secrets.token_hex(12),current_generation=generation,
            communication_context_sha256=context_hash(doc),
            current_hash=sha(doc.get('final_answer') or doc.get('ai_answer')),
            source_version=source_version,answer_hash=sha(candidate['text']),
            override_by=str(user),override_at=now.isoformat(),
            expires_at=(now+timedelta(seconds=deps.settings.telegram_edit_timeout_seconds)).isoformat(),
            policy_fingerprint=fingerprint(policy),policy_version=policy['version'],
            knowledge_snapshot=policy['snapshot'],knowledge_snapshot_sha256=policy['snapshot_sha256'],
            expertise_sha256=policy['expertise_sha256'],violations=policy['violations'],
            violation_spans=policy.get('violation_spans',[]),reason_optional=None)
        try: deps.repo.request_override(doc_id,pending,generation)
        except InvalidTransition: return _stale(deps,chat)
        violations='; '.join(sorted({v['rule_id'] for v in policy['violations']}))
        spans='; '.join('«'+v['span']+'»' for v in policy.get('violation_spans',[]))
        if _v31_only(doc):
            finding=('Правила 3.1E: BLOCK — '+(violations or '—')+'.'+('\n'+spans if spans else '')
                     if policy['verdict']=='BLOCK' else
                     'Проверка правил 3.1E недоступна ('+str(policy.get('error_class') or 'ERROR')+').')
            message=('⚠️ Решение владельца.\n'+finding+'\n\nТочный текст для публикации:\n'+candidate['text']+
                     '\n\nПодтвердите, что публикуете именно этот текст под свою ответственность. '
                     'Подтверждение действует '+str(deps.settings.telegram_edit_timeout_seconds//60)+' мин.')
            confirm='⚠️ Подтверждаю: опубликовать этот текст'
        else:
            message=('⚠️ Ответ нарушает правила: '+violations+'.\n'+spans+
                     '\n\nИсходный текст:\n'+candidate['text']+'\n\nОпубликовать исходный текст всё равно?')
            confirm='⚠️ Подтверждаю публикацию исходного'
        deps.telegram.send_message(chat,escape_html(message),{'inline_keyboard':[[
            {'text':confirm,'callback_data':f"oc:{doc_id}:{pending['nonce']}"}],
            [{'text':'✏️ Редактировать','callback_data':f'edit:{doc_id}'}]]})
        return {'status':'override_confirmation_required'}
    if len(parts)!=2: return {'status':'bad_request'}
    pending=doc.get('owner_override_pending') or {}
    if pending.get('nonce')!=parts[1] or pending.get('override_by')!=str(user) or pending.get('consumed_at'):
        return _stale(deps,chat,'⚠️ Подтверждение недоступно. Нужны два новых нажатия.')
    try: validate_binding(doc,pending)
    except InvalidTransition: return _stale(deps,chat,'⚠️ Вариант изменился или подтверждение истекло.')
    text=source_text(doc,pending['source_version'])
    if _v31_only(doc):
        candidate=_offer_v31(doc,deps.settings)
        policy=candidate['policy'] if candidate and candidate['source_version']==pending['source_version'] else {}
    else:
        policy=validate_for_publication(text,doc,deps.settings)
    if fingerprint(policy)!=pending['policy_fingerprint']:
        deps.telegram.send_message(chat,'⚠️ Правила изменились. Прочитайте карточку и подтвердите заново.')
        return {'status':'override_reconfirmation_required'}
    return _publish(deps,doc_id,chat,message_id,user,expected_generation=pending['current_generation'],
                    override_confirmation=pending)
