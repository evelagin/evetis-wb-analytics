"""Сторож идентичности Claude API: фактический scope и срок токена — до запуска агента.

Проблема. Желаемый scope правил федерации — `workspace:inference`, а живые правила (2026-09-25)
созданы с `workspace:developer`: Console не предлагает inference. Кроме того, живую конфигурацию
правил нельзя прочитать машинно (Admin API федерации отвечает 404). Значит, единственное
машинно проверяемое свидетельство — ответ самого обмена токена: он содержит `scope` и
`expires_in`.

Что делает `check` (только в job'ах инженера и ревьюера, `id-token: write`):
  1. выпускает ОТДЕЛЬНЫЙ свежий OIDC-токен GitHub (не файл агента: JWT одноразовый, `jti`);
  2. обменивает его по тому же правилу, что и агент, на `POST /v1/oauth/token`;
  3. токен Claude отбрасывает (не печатает, не сохраняет), печатает только scope/expires_in/решение;
  4. решение по `policy.json → anthropic_runtime`:
       desired_scope                       → PASS
       шире, но в неистёкшем исключении   → PASS_WITH_EXCEPTION (видно в итоге job'а)
       org:admin, не-workspace, без scope → FAIL всегда
       срок > max_token_lifetime_seconds  → FAIL
Код выхода 1 при FAIL — агент не запускается.

Диагностика отказа (инцидент M6 2026-09-25: Anthropic отвечает одинаковым 401 на любой отказ,
причина — только в истории аутентификации Console). При отказе обмена печатаются НЕСЕКРЕТНЫЕ
claims OIDC-токена из белого списка и их расхождения со спецификацией правила этой роли
(`quality/autonomy/anthropic_federation.json`). Сам JWT, подпись, `jti` и токен Claude не печатаются.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date

from tools.autonomy.policy import REPO, load_policy
from tools.autonomy.redact import redact_text

TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
AUDIENCE = "https://api.anthropic.com"


# Claims GitHub OIDC, которые не являются учётными данными и нужны, чтобы понять отказ правила.
SAFE_CLAIMS = ("iss", "aud", "sub", "repository", "repository_id", "repository_owner", "repository_owner_id",
               "ref", "ref_type", "event_name", "workflow_ref", "job_workflow_ref", "runner_environment",
               "environment")


class PolicyError(ValueError):
    pass


class ExchangeRejected(RuntimeError):
    def __init__(self, status: int):
        super().__init__(f"обмен токена отклонён: HTTP {status}")
        self.status = status


def decode_claims(jwt: str) -> dict:
    """Полезная нагрузка JWT без проверки подписи — только для диагностики, никогда для доверия."""
    try:
        payload = jwt.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def diagnose(claims: dict, role: str, spec: dict) -> dict:
    """Несекретные claims + расхождения с правилом роли. Ничего вне SAFE_CLAIMS не возвращается."""
    safe = {k: claims[k] for k in SAFE_CLAIMS if k in claims}
    if "iat" in claims and "exp" in claims:
        safe["lifetime_seconds"] = int(claims["exp"]) - int(claims["iat"])
    rule = spec["rules"][role]["match"]
    mism = []
    sp = rule.get("subject_prefix")
    if sp is not None and not (claims.get("sub", "").startswith(sp[:-1]) if sp.endswith("*") else claims.get("sub") == sp):
        mism.append({"claim": "sub", "expected": sp, "got": claims.get("sub")})
    aud = claims.get("aud")
    if "audience" in rule and rule["audience"] not in (aud if isinstance(aud, list) else [aud]):
        mism.append({"claim": "aud", "expected": rule["audience"], "got": aud})
    for k, v in rule.get("claims", {}).items():
        if claims.get(k) != v:
            mism.append({"claim": k, "expected": v, "got": claims.get(k)})
    return {"role": role, "claims": safe, "mismatches_vs_spec": mism,
            "hint": ("claims токена совпадают со спецификацией — расхождение в живом правиле, issuer, "
                     "членстве SA в workspace или ID; см. историю аутентификации Console")
            if not mism else "токен не совпадает со спецификацией роли — см. mismatches_vs_spec"}


def validate_runtime_policy(rt: dict) -> None:
    """Исключение не может разрешить административный scope и не может быть бессрочным."""
    for ex in rt.get("scope_exceptions", []):
        if not str(ex.get("scope", "")).startswith("workspace:") or ex["scope"] in ("workspace:manage_tunnels",):
            raise PolicyError(f"исключение для scope {ex.get('scope')!r} недопустимо")
        span = (date.fromisoformat(ex["expires"]) - date.fromisoformat(ex["approved_on"])).days
        if span < 0 or span > rt["max_exception_days"]:
            raise PolicyError(f"срок исключения {span} дн. вне 0…{rt['max_exception_days']}")
        if not set(ex["applies_to"]) <= {"engineer", "reviewer"}:
            raise PolicyError(f"исключение для неизвестной роли: {ex['applies_to']}")


def decide(scope: str | None, expires_in: int | None, role: str, rt: dict, today: date) -> dict:
    validate_runtime_policy(rt)
    res = {"role": role, "scope": scope, "expires_in": expires_in, "desired_scope": rt["desired_scope"],
           "exception": None}
    if not scope or not scope.startswith("workspace:"):
        return {**res, "decision": "FAIL", "reason": f"scope {scope!r}: административный или неизвестный — никогда"}
    if not isinstance(expires_in, int) or expires_in <= 0 or expires_in > rt["max_token_lifetime_seconds"]:
        return {**res, "decision": "FAIL",
                "reason": f"expires_in {expires_in!r} вне 1…{rt['max_token_lifetime_seconds']} с"}
    if scope == rt["desired_scope"]:
        return {**res, "decision": "PASS", "reason": "scope совпадает с желаемым"}
    for ex in rt.get("scope_exceptions", []):
        if ex["scope"] == scope and role in ex["applies_to"]:
            if today > date.fromisoformat(ex["expires"]):
                return {**res, "decision": "FAIL", "reason": f"исключение для {scope} истекло {ex['expires']}"}
            return {**res, "decision": "PASS_WITH_EXCEPTION", "exception": ex,
                    "reason": f"{scope} шире {rt['desired_scope']}: исключение до {ex['expires']}"}
    return {**res, "decision": "FAIL", "reason": f"{scope} шире {rt['desired_scope']} и исключения нет"}


def _oidc() -> str:
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=" + AUDIENCE
    req = urllib.request.Request(url, headers={"Authorization": "bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    return json.load(urllib.request.urlopen(req, timeout=30))["value"]


def exchange(assertion: str) -> tuple[str | None, int | None]:
    """Обмен по правилу из окружения job'а. Возвращает только (scope, expires_in)."""
    body = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion,
            "federation_rule_id": os.environ["ANTHROPIC_FEDERATION_RULE_ID"],
            "organization_id": os.environ["ANTHROPIC_ORGANIZATION_ID"],
            "service_account_id": os.environ["ANTHROPIC_SERVICE_ACCOUNT_ID"]}
    if os.environ.get("ANTHROPIC_WORKSPACE_ID"):
        body["workspace_id"] = os.environ["ANTHROPIC_WORKSPACE_ID"]
    req = urllib.request.Request(TOKEN_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        raise ExchangeRejected(e.code) from None
    scope, ttl = resp.get("scope"), resp.get("expires_in")
    resp.clear()                       # токен Claude не печатается и не живёт дальше этой функции
    return scope, ttl


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("check", choices=["check"])
    ap.add_argument("--role", required=True, choices=["engineer", "reviewer"])
    a = ap.parse_args(argv)
    rt = load_policy()["anthropic_runtime"]
    assertion = _oidc()
    try:
        scope, ttl = exchange(assertion)
    except ExchangeRejected as e:
        spec = json.loads((REPO / "quality" / "autonomy" / "anthropic_federation.json").read_text(encoding="utf-8"))
        diag = diagnose(decode_claims(assertion), a.role, spec)
        del assertion
        ids = {k: os.environ.get(k) for k in ("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_SERVICE_ACCOUNT_ID",
                                              "ANTHROPIC_WORKSPACE_ID", "ANTHROPIC_ORGANIZATION_ID")}
        report = {"decision": "FAIL", "reason": str(e), "request_ids": ids, **diag}
        text = redact_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(text)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(f"### Claude API identity ({a.role}): обмен отклонён\n\n```json\n{text}\n```\n")
        return 1
    del assertion
    res = decide(scope, ttl, a.role, rt, date.today())
    print(json.dumps({k: v for k, v in res.items() if k != "exception"}, ensure_ascii=False))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Claude API identity ({a.role})\n\n- scope: `{scope}` (желаемый `{rt['desired_scope']}`)\n"
                    f"- expires_in: {ttl} с\n- решение: **{res['decision']}** — {res['reason']}\n")
    return 0 if res["decision"] in ("PASS", "PASS_WITH_EXCEPTION") else 1


if __name__ == "__main__":
    sys.exit(main())
