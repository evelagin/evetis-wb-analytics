"""Identity кабинета продавца и действующая привязка (Tenancy T5, протокол F).

Чистый модуль: без сети, облака и конфигурации процесса — его импортируют runtime, control и
инструменты владельца (tools/tenancy/tenant_binding.py).

Отпечаток — sha256 канонического JSON несекретных полей. Ключи API в отпечаток не входят.
Сами значения identity (Client-Id, ИНН, ОГРН, название) живут только в проекте арендатора
(tenant_ops.SELLER_IDENTITY_OBSERVATIONS, ref.SELLER_BINDING): не в Git, не в артефактах и не в
журналах — наружу выходит лишь отпечаток и маска.

Действующая привязка вычисляется ровно как tenant_ops.V_SELLER_BINDING_STATUS (T4):
  * события — строки SELLER_BINDING со статусом CONFIRMED или REVOKED; момент события —
    revoked_at, иначе confirmed_at;
  * действует последнее событие; при равном моменте побеждает REVOKED, затем больший binding_id;
  * в последний момент подтверждены разные отпечатки, или момент в будущем — INVALID_BINDING;
  * последнее событие — отзыв или событий нет — UNBOUND;
  * если переданы наблюдения (control и владелец их читают; runtime — нет, у него нет доступа к
    tenant_ops), подтверждение без наблюдения того же API с тем же отпечатком — INVALID_BINDING.
    Runtime вместо этого сверяет отпечаток, снятый в своём прогоне (live_status).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

SELLER = "seller"
PERFORMANCE = "performance"
APIS = (SELLER, PERFORMANCE)

BOUND, UNBOUND, MISMATCH, INVALID_BINDING, NOT_OBSERVED = (
    "BOUND", "UNBOUND", "MISMATCH", "INVALID_BINDING", "NOT_OBSERVED")


class IdentityError(ValueError):
    """Наблюдение identity неполно: отпечаток не строится (fail closed)."""


def _canon(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _norm(v, what: str) -> str:
    s = "" if v is None else str(v).strip()
    if not s:
        raise IdentityError(f"пустое поле identity: {what}")
    return s


def seller_fingerprint(client_id, inn, ogrn) -> str:
    """Отпечаток кабинета Seller API: Client-Id + ИНН + ОГРН (все обязательны)."""
    return hashlib.sha256(_canon({"api": SELLER, "client_id": _norm(client_id, "client_id"),
                                  "inn": _norm(inn, "inn"), "ogrn": _norm(ogrn, "ogrn")}).encode()).hexdigest()


def performance_fingerprint(perf_client_id) -> str:
    """Отпечаток сервисного аккаунта Performance API (client_id — идентификатор, не секрет)."""
    return hashlib.sha256(_canon({"api": PERFORMANCE,
                                  "client_id": _norm(perf_client_id, "perf_client_id")}).encode()).hexdigest()


def mask(value, keep: int = 4) -> str:
    """Маска для показа оператору: только последние `keep` символов."""
    s = "" if value is None else str(value)
    return ("•" * max(len(s) - keep, 0)) + s[-keep:] if s else ""


def as_utc(v) -> datetime | None:
    """TIMESTAMP BigQuery из клиента (datetime), REST tabledata (секунды эпохи строкой) или ISO."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    s = str(v)
    try:
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    except ValueError:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Binding:
    api: str
    status: str                  # BOUND-кандидат (CONFIRMED) | UNBOUND | INVALID_BINDING
    fingerprint: str | None
    binding_id: str | None
    source_observation_id: str | None
    confirmed_at: datetime | None
    reason: str


def effective_binding(rows, api: str, now: datetime, observations=None) -> Binding:
    """Действующее событие привязки по API — семантика V_SELLER_BINDING_STATUS."""
    events = []
    for r in rows:
        if r.get("api") != api or r.get("status") not in ("CONFIRMED", "REVOKED"):
            continue
        at = as_utc(r.get("revoked_at")) or as_utc(r.get("confirmed_at"))
        if at is None:
            continue
        events.append((at, r))
    if not events:
        return Binding(api, UNBOUND, None, None, None, None, "подтверждений нет")
    top = max(at for at, _r in events)
    same = [r for at, r in events if at == top]
    latest = sorted(same, key=lambda r: (0 if r["status"] == "REVOKED" else 1,
                                         _neg(str(r.get("binding_id") or ""))))[0]
    if latest["status"] == "REVOKED" or latest.get("revoked_at"):
        return Binding(api, UNBOUND, None, latest.get("binding_id"), None, None, "последнее событие — отзыв")
    if top > now:
        return Binding(api, INVALID_BINDING, None, latest.get("binding_id"), None, None,
                       "подтверждение датировано будущим")
    if len({r.get("identity_fingerprint") for r in same if r["status"] == "CONFIRMED"}) > 1:
        return Binding(api, INVALID_BINDING, None, latest.get("binding_id"), None, None,
                       "в один момент подтверждены разные отпечатки")
    if observations is not None and not any(
            o.get("observation_id") == latest.get("source_observation_id") and o.get("api") == api
            and o.get("identity_fingerprint") == latest.get("identity_fingerprint") for o in observations):
        return Binding(api, INVALID_BINDING, None, latest.get("binding_id"), None, None,
                       "подтверждение не ссылается на наблюдение того же API с тем же отпечатком")
    return Binding(api, "CONFIRMED", latest.get("identity_fingerprint"), latest.get("binding_id"),
                   latest.get("source_observation_id"), as_utc(latest.get("confirmed_at")), "подтверждено")


class _neg(str):
    """Обратный порядок строк для сортировки (больший binding_id — раньше)."""

    def __lt__(self, other):
        return str.__gt__(self, other)


def live_status(binding: Binding, live_fingerprint: str | None) -> tuple[str, str]:
    """Сверка действующей привязки с отпечатком, снятым в ЭТОМ прогоне (TOCTOU-защита).

    Возвращает (статус, причина); загрузка допустима только при BOUND.
    """
    if binding.status in (UNBOUND, INVALID_BINDING):
        return binding.status, binding.reason
    if not live_fingerprint:
        return NOT_OBSERVED, "в этом прогоне identity не наблюдалась"
    if live_fingerprint != binding.fingerprint:
        return MISMATCH, "отпечаток кабинета изменился после подтверждения"
    return BOUND, "отпечаток совпадает с подтверждённым"


def observation_status(observations, api: str, now: datetime, max_age_hours: float = 24.0):
    """Последнее наблюдение API для подтверждения оператором.

    (observation | None, причина). Отказ: нет наблюдений; в последний момент несколько разных
    отпечатков или пустой отпечаток; наблюдению больше max_age_hours; наблюдение не OBSERVED.
    """
    obs = [o for o in observations if o.get("api") == api]
    if not obs:
        return None, "наблюдений нет"
    top = max(as_utc(o.get("observed_at")) for o in obs)
    last = [o for o in obs if as_utc(o.get("observed_at")) == top]
    fps = {o.get("identity_fingerprint") for o in last}
    if len(fps) != 1 or None in fps or "" in fps:
        return None, "последнее наблюдение неоднозначно или без отпечатка"
    o = sorted(last, key=lambda x: str(x.get("observation_id")))[-1]
    if o.get("status") != "OBSERVED":
        return None, f"последнее наблюдение в статусе {o.get('status')}"
    if (now - top).total_seconds() > max_age_hours * 3600:
        return None, f"последнему наблюдению больше {max_age_hours:g} ч"
    return o, "ok"


# ───────────────────────────────────────────── маркеры привязки владельца ref.OPB_<api>_<n>
# Истина о привязке — таблицы-маркеры владельца в ref (tables.insert, у control нет права создавать
# таблицы в ref). Описание маркера — строка события привязки целиком; метаданные читаются
# консистентно (tables.get / tables.list), тогда как строки ref.SELLER_BINDING видны tabledata.list
# с задержкой до ~90 мин (ревью PR #226). Строки SELLER_BINDING — зеркало для SQL.
import re as _re

BINDING_MARKER_RE = _re.compile(r"^OPB_(seller|performance)_(\d{4})$")


def binding_marker_name(api: str, n: int) -> str:
    return f"OPB_{api}_{int(n):04d}"


def binding_marker(row: dict) -> tuple[dict, str]:
    lab = lambda v: _re.sub(r"[^a-z0-9_-]", "_", str(v).lower())[:63]
    return ({"api": lab(row["api"]), "status": lab(row["status"]), "binding": lab(row["binding_id"])},
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))


def binding_head(items, api):
    """(номер последнего маркера API, его строка | None) по [(имя, метки, описание)]."""
    best = (0, None)
    for name, _labels, description in items:
        m = BINDING_MARKER_RE.match(name or "")
        if m and m.group(1) == api and int(m.group(2)) > best[0]:
            try:
                row = json.loads(description or "")
            except ValueError:
                row = {"_invalid": True}
            best = (int(m.group(2)), row if isinstance(row, dict) else {"_invalid": True})
    return best


def binding_from_markers(items, api: str, now: datetime, observations=None) -> Binding:
    """Действующая привязка — последний маркер владельца по API (порядок — номер, не часы)."""
    n, row = binding_head(items, api)
    if not n:
        return Binding(api, UNBOUND, None, None, None, None, "подтверждений нет")
    if row.get("_invalid") or row.get("api") != api:
        return Binding(api, INVALID_BINDING, None, None, None, None, "маркер привязки не разбирается")
    if row.get("status") == "REVOKED":
        return Binding(api, UNBOUND, None, row.get("binding_id"), None, None, "последнее событие — отзыв")
    if row.get("status") != "CONFIRMED" or not row.get("identity_fingerprint"):
        return Binding(api, INVALID_BINDING, None, row.get("binding_id"), None, None, "маркер без подтверждения")
    at = as_utc(row.get("confirmed_at"))
    if at is None or at > now:
        return Binding(api, INVALID_BINDING, None, row.get("binding_id"), None, None,
                       "подтверждение без даты или датировано будущим")
    if observations is not None and not any(
            o.get("observation_id") == row.get("source_observation_id") and o.get("api") == api
            and o.get("identity_fingerprint") == row.get("identity_fingerprint") for o in observations):
        return Binding(api, INVALID_BINDING, None, row.get("binding_id"), None, None,
                       "подтверждение не ссылается на наблюдение того же API с тем же отпечатком")
    return Binding(api, "CONFIRMED", row.get("identity_fingerprint"), row.get("binding_id"),
                   row.get("source_observation_id"), at, "подтверждено")
