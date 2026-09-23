"""Машина состояний прогона и её долговременное хранилище.

Состояние системы — это файлы, а не память модели. Перезапущенный процесс читает
запись прогона и знает, что уже произошло. Недопустимый переход — исключение, а не
предупреждение: иначе оркестратор мог бы «перепрыгнуть» через ворота.

Хранилище — каталог. В GitHub Actions этот каталог — рабочая копия служебной ветки
`autonomy-state`, которую коммитит только детерминированный шаг; локально и в тестах —
любой временный каталог. Логика одна и та же.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.autonomy.schema import require_valid

TERMINAL = {"COMPLETED", "FAILED"}
PARKED = {"WAITING_FOR_HUMAN", "BLOCKED"}   # активны: блокируют дубликаты до решения человека

TRANSITIONS: dict[str | None, set[str]] = {
    None: {"RECEIVED"},
    "RECEIVED": {"DISCOVERING", "FAILED"},
    "DISCOVERING": {"PLANNING", "BLOCKED", "FAILED"},
    "PLANNING": {"IMPLEMENTING", "WAITING_FOR_HUMAN", "BLOCKED", "FAILED"},
    "IMPLEMENTING": {"TESTING", "WAITING_FOR_HUMAN", "BLOCKED", "FAILED"},
    "TESTING": {"REVIEWING", "FIXING", "BLOCKED", "FAILED"},
    "REVIEWING": {"READY_FOR_PR", "FIXING", "WAITING_FOR_HUMAN", "BLOCKED", "FAILED"},
    "FIXING": {"TESTING", "WAITING_FOR_HUMAN", "BLOCKED", "FAILED"},
    "READY_FOR_PR": {"COMPLETED", "FAILED"},
    "WAITING_FOR_HUMAN": {"PLANNING", "IMPLEMENTING", "COMPLETED"},
    "BLOCKED": {"PLANNING", "COMPLETED"},
    "FAILED": set(),
    "COMPLETED": set(),
}


class TransitionError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def dedup_slug(key: str) -> str:
    """Имя ветки выводится из ключа дедупликации детерминированно: тот же инцидент —
    та же ветка, поэтому второй прогон физически не может создать вторую ветку."""
    head = "".join(c if c.isalnum() else "-" for c in key.lower()).strip("-")
    head = "-".join(p for p in head.split("-") if p)[:40].strip("-")
    return f"ae/{head}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


class StateStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)
        (self.root / "index").mkdir(parents=True, exist_ok=True)

    # ---- низкий уровень --------------------------------------------------
    def _run_path(self, run_id: str) -> Path:
        return self.root / "runs" / f"{run_id}.json"

    def _index_path(self, key: str) -> Path:
        return self.root / "index" / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def _write(self, path: Path, doc: dict) -> None:
        # Атомарно: частично записанная запись не должна пережить падение процесса.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)

    def save(self, run: dict) -> None:
        require_valid(run, "run_state")
        self._write(self._run_path(run["run_id"]), run)

    def load(self, run_id: str) -> dict:
        run = json.loads(self._run_path(run_id).read_text(encoding="utf-8"))
        require_valid(run, "run_state")
        return run

    # ---- дедупликация ----------------------------------------------------
    def active_run(self, key: str) -> dict | None:
        p = self._index_path(key)
        if not p.exists():
            return None
        run = self.load(json.loads(p.read_text(encoding="utf-8"))["run_id"])
        return None if run["state"] in TERMINAL else run

    def last_run(self, key: str) -> dict | None:
        p = self._index_path(key)
        return self.load(json.loads(p.read_text(encoding="utf-8"))["run_id"]) if p.exists() else None

    def in_cooldown(self, key: str, hours: int) -> bool:
        last = self.last_run(key)
        if not last or last["state"] != "FAILED":
            return False
        failed_at = datetime.fromisoformat(last["updated_at"].replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - failed_at < timedelta(hours=hours)

    def open_or_get(self, objective: dict, repository_sha: str, cooldown_hours: int) -> tuple[dict, bool]:
        """Идемпотентное открытие прогона. Вернуть (прогон, создан_ли_новый).

        Повторный приход того же инцидента возвращает уже активный прогон и не создаёт
        ни второй ветки, ни второго вызова модели."""
        key = objective["deduplication_key"]
        active = self.active_run(key)
        if active:
            return active, False
        if self.in_cooldown(key, cooldown_hours):
            raise TransitionError(f"{key}: прошлый прогон FAILED меньше {cooldown_hours} ч назад — "
                                  "автоматический повтор запрещён, нужен человек")
        ts = now_iso()
        run = {
            "schema_version": 1, "run_id": new_id("run"),
            "objective_id": objective["objective_id"], "deduplication_key": key,
            "state": "RECEIVED", "created_at": ts, "updated_at": ts,
            "repository_sha": repository_sha, "branch": dedup_slug(key),
            "iteration": 0, "review_cycles": 0, "infra_retries": 0,
            "transitions": [{"from": None, "to": "RECEIVED", "at": ts, "reason": "цель принята"}],
            "last_gate": None, "last_review": None, "plan_sha256": None,
            "production_mutations": 0, "usage": [], "pr_url": None, "notes": [],
        }
        self.save(run)
        self._write(self._index_path(key), {"run_id": run["run_id"], "deduplication_key": key})
        return run, True

    # ---- переходы --------------------------------------------------------
    def transition(self, run: dict, to: str, reason: str, **fields) -> dict:
        frm = run["state"]
        if to not in TRANSITIONS.get(frm, set()):
            raise TransitionError(f"{run['run_id']}: переход {frm} → {to} запрещён")
        ts = now_iso()
        run = {**run, **fields, "state": to, "updated_at": ts,
               "transitions": run["transitions"] + [{"from": frm, "to": to, "at": ts, "reason": reason[:2000]}]}
        self.save(run)
        return run
