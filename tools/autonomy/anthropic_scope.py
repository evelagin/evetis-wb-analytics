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
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date

from tools.autonomy.policy import load_policy

TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
AUDIENCE = "https://api.anthropic.com"


class PolicyError(ValueError):
    pass


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


def exchange() -> tuple[str | None, int | None]:
    """Обмен по правилу из окружения job'а. Возвращает только (scope, expires_in)."""
    body = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": _oidc(),
            "federation_rule_id": os.environ["ANTHROPIC_FEDERATION_RULE_ID"],
            "organization_id": os.environ["ANTHROPIC_ORGANIZATION_ID"],
            "service_account_id": os.environ["ANTHROPIC_SERVICE_ACCOUNT_ID"]}
    if os.environ.get("ANTHROPIC_WORKSPACE_ID"):
        body["workspace_id"] = os.environ["ANTHROPIC_WORKSPACE_ID"]
    req = urllib.request.Request(TOKEN_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"обмен токена отклонён: HTTP {e.code}") from None
    scope, ttl = resp.get("scope"), resp.get("expires_in")
    resp.clear()                       # токен Claude не печатается и не живёт дальше этой функции
    return scope, ttl


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("check", choices=["check"])
    ap.add_argument("--role", required=True, choices=["engineer", "reviewer"])
    a = ap.parse_args(argv)
    rt = load_policy()["anthropic_runtime"]
    scope, ttl = exchange()
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
