"""Feedback-only proof state machine. No generation and no automatic WB writes."""
import time
from datetime import datetime, timezone
from app.domain.exceptions import WBApiError, WBPublishOutcomeUnknown, InvalidTransition
from app.domain.statuses import EventType
from app.utils.text import clean_answer


def now():
    return datetime.now(timezone.utc).isoformat()


def observe(deps, doc, text, trace, attempts=1, deadline=None):
    result = "verify_error"
    for i in range(attempts):
        if deadline is not None and time.monotonic() >= deadline:
            break
        if i:
            time.sleep(deps.settings.wb_feedback_verify_delay_seconds)
        trace["verification_attempts"] += 1
        try:
            data = deps.wb.get_feedback(doc["source_id"], retries=0,
                timeout_seconds=min(5.0, max(0.1, deadline-time.monotonic())) if deadline else 5.0)
            if str(data.get("id")) != str(doc["source_id"]) or "answer" not in data:
                raise ValueError("feedback identity mismatch")
            answer = data.get("answer")
            if answer is None:
                result = "not_visible"
                state = None
            elif not isinstance(answer, dict) or not isinstance(answer.get("text"), str):
                raise ValueError("malformed answer")
            else:
                actual = " ".join(answer["text"].split())
                state = answer.get("state")
                if not actual:
                    result = "not_visible"
                elif state != "wbRu":
                    # Absent/unknown state is not proof of public visibility.
                    result = "state_unconfirmed"
                else:
                    result = "verified" if actual == " ".join(text.split()) else "answered_externally"
            trace["wb_answer_state"] = state
        except Exception as exc:
            result = "verify_error"
            trace["verification_error_class"] = type(exc).__name__
        if result in ("verified", "answered_externally"):
            break
    trace.update(verification_result=result, verification_timestamp=now())
    return result


def finish(deps, cid, doc, text, trace, outcome, *, accepted=False, chat=None, message_id=None, user_id=None):
    from app.services.pipeline import _sync_current, _emit_event, _Q_MSG
    from app.utils.text import escape_html
    status = {"verified": "published", "answered_externally": "answered_externally"}.get(outcome)
    if status is None:
        status = "publish_accepted" if accepted else "publish_unknown"
    stamp = now()
    fields = {"status": status, "publication_state": status, "feedback_checked_at": stamp}
    if status == "published":
        fields.update(verified_at=stamp, published_at=doc.get("published_at") or stamp)
    if accepted:
        fields["publish_accepted_at"] = doc.get("publish_accepted_at") or stamp
    if user_id is not None:
        fields["published_by_telegram_user_id"] = str(user_id)
    trace.update(final_publication_state=status, local_state_after=status, finished_at=stamp,
                 telegram_state=status)
    unchanged = user_id is None and doc.get("status") == status
    fields["feedback_last_verification"] = {"result": outcome, "timestamp": stamp,
                                            "attempts": trace["verification_attempts"]}
    deps.repo.publication_update(cid, doc["lock_token"], fields, None if unchanged else trace, release=True)
    if unchanged:
        return {"status": status}
    after = _sync_current(deps, cid, doc)
    _emit_event(deps, after, cid, EventType(status), best_effort=False,
                status_before=trace["local_state_before"], status_after=status,
                telegram_user_id=user_id, attempt=trace["publication_attempt_id"], payload=trace)
    chat = chat or doc.get("telegram_chat_id")
    message_id = message_id or doc.get("telegram_message_id")
    if chat and message_id and (user_id is not None or doc.get("status") != status):
        if status == "publish_unknown":
            message = "⚠️ Публикация не подтверждена.\nПовторная отправка не выполнялась. Проверка продолжится автоматически."
        elif status == "publish_accepted":
            message = "⏳ Отправлено в Wildberries.\nПубликация пока не подтверждена."
        else:
            message = _Q_MSG[status].format(text=escape_html(text))
        deps.telegram.edit_message_text(chat, message_id, message, None)
    return {"status": status}


def publish(deps, cid, doc, text, trace, chat, message_id, user_id):
    from app.services.pipeline import _sync_current, _emit_event, _wb_publish_error_message, build_keyboard
    args = dict(chat=chat, message_id=message_id, user_id=user_id)
    trace.update(endpoint="/api/v1/feedbacks/answer", method=deps.settings.wb_answer_method,
                 write_attempted=False, write="not_attempted")
    outcome = observe(deps, doc, text, trace)
    trace["precheck"] = outcome
    if outcome in ("verified", "answered_externally"):
        return finish(deps,cid,doc,text,trace,outcome,**args)
    if outcome != "not_visible":
        return finish(deps,cid,doc,text,trace,"verify_error",**args)
    if doc.get("recovered_from_publishing"):
        deps.repo.publication_update(cid, doc["lock_token"], {"feedback_write_intent": True})
    if doc.get("feedback_write_intent") or doc.get("recovered_from_publishing"):
        # Persisted before network I/O. A crash, timeout or later callback cannot resend.
        return finish(deps,cid,doc,text,trace,"not_visible",**args)
    trace.update(write_attempted=True, write="attempted", request_started_at=now())
    deps.repo.publication_update(cid, doc["lock_token"], {"feedback_write_intent": True,
                               "publication_policy": trace["policy"]})
    accepted = False
    try:
        response = deps.wb.publish_answer(doc["source_id"], text)
        accepted = True
        trace.update(write="accepted", http_status=response.get("status_code"),
                     request_payload_sha256=response.get("request_sha256"))
    except WBPublishOutcomeUnknown as exc:
        trace.update(write="outcome_unknown", http_status=exc.status_code, error_class=type(exc).__name__)
    except WBApiError as exc:
        # Client guarantees these are definite rejections; unknown writes have a separate exception.
        trace.update(write="rejected", http_status=exc.status_code, error_class=type(exc).__name__,
                     final_publication_state="publish_failed", local_state_after="publish_failed",
                     telegram_state="publish_failed", finished_at=now())
        deps.repo.publication_update(cid,doc["lock_token"],{"status":"publish_failed",
            "publication_state":"publish_failed", "feedback_write_intent":False,
            "last_error_code":exc.status_code,"last_error_message":type(exc).__name__},trace,release=True)
        after=_sync_current(deps,cid,doc)
        _emit_event(deps,after,cid,EventType.FAILED,best_effort=False,status_after="publish_failed",
                    attempt=trace["publication_attempt_id"],payload=trace)
        deps.telegram.edit_message_text(chat,message_id,_wb_publish_error_message(exc),build_keyboard(cid))
        return {"status":"publish_failed", "code":exc.status_code}
    except Exception as exc:
        # Unexpected client failure may occur after send: never clear the durable intent.
        trace.update(write="outcome_unknown",error_class=type(exc).__name__)
    outcome=observe(deps,doc,text,trace,attempts=deps.settings.wb_feedback_verify_attempts)
    return finish(deps,cid,doc,text,trace,outcome,accepted=accepted,**args)


def reconcile(deps, legacy_ids=()):
    from app.services.pipeline import _new_trace
    deadline=time.monotonic()+deps.settings.wb_feedback_reconcile_budget_seconds
    limit=deps.settings.wb_feedback_reconcile_limit
    candidates=[]
    for status in ("publish_accepted","publish_unknown","publishing"):
        candidates.extend(deps.repo.list_by_status(status,100,entity_type="review"))
    candidates.extend((cid,deps.repo.get(cid)) for cid in legacy_ids)
    candidates.sort(key=lambda x:(x[1] or {}).get("feedback_checked_at") or "")
    checked=errors=0
    for cid, peek in candidates:
        if checked>=limit or time.monotonic()>=deadline:break
        if not peek or peek.get("entity_type")!='review':continue
        try:
            doc=deps.repo.claim_feedback_verification(cid)
            checked+=1
            trace=_new_trace(cid,doc,phase="feedback_reconcile",state_before=doc["status"])
            trace.update(write_attempted=False,write="not_attempted",policy=doc.get("publication_policy"))
            if doc["status"] in ("published", "publishing"):
                deps.repo.publication_update(cid, doc["lock_token"], {"feedback_write_intent": True})
            text=clean_answer(doc.get("final_answer") or doc.get("ai_answer"))
            outcome=observe(deps,doc,text,trace,deadline=deadline)
            finish(deps,cid,doc,text,trace,outcome,accepted=doc["status"]=="publish_accepted")
        except InvalidTransition:
            continue
        except Exception:
            errors+=1
    return {"checked":checked,"errors":errors}
