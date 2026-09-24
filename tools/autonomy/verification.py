"""Обязательная проверка опубликованного кандидата (S8). Детерминированная.

Проблема. PR, созданный `GITHUB_TOKEN`, не запускает `pull_request`/`push` workflows: события
этого токена новых запусков не создают. Без отдельного механизма AE мог бы выставить
«готово к ревью» по PR, на котором обязательный CI не исполнялся ни разу.

Решение без нового секрета. Единственные события `GITHUB_TOKEN`, которые ЗАПУСКАЮТ workflows, —
`workflow_dispatch` и `repository_dispatch`. Публикатор (доверенный job, `actions: write`)
после создания draft PR выполняет `workflow_dispatch` каждого обязательного workflow на ветке
кандидата. Этот модуль потом решает по API Actions, а не по отчётам агента:

  * прогон относится к кандидату, только если `head_sha` = опубликованный SHA, ветка = ветка
    кандидата, событие = `workflow_dispatch`, путь = `.github/workflows/<файл>` и прогон создан
    не раньше диспатча;
  * PASS — у КАЖДОГО обязательного workflow последний такой прогон завершён с `success`;
  * FAIL — хоть один завершён иначе; PENDING — какой-то ещё не завершён или не появился.

Подделать это недоверенный job не может: создать прогон workflow нужно `actions: write`,
а у job'ов, исполняющих модель или код кандидата, его нет. Статусы коммита (`statuses`,
`checks`) не читаются вообще — их выставить проще, чем запуск.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def evaluate(required: list[str], runs: dict[str, list[dict]], head_sha: str, branch: str,
             dispatched_at: str, skew_seconds: int = 120) -> dict:
    """runs: workflow-файл → список объектов прогона API Actions (`GET …/runs`)."""
    since = _ts(dispatched_at) - timedelta(seconds=skew_seconds)
    rows, statuses = [], []
    for wf in required:
        mine = [r for r in runs.get(wf, [])
                if r.get("head_sha") == head_sha and r.get("head_branch") == branch
                and r.get("event") == "workflow_dispatch"
                and r.get("path", "").split("@")[0] == f".github/workflows/{wf}"
                and _ts(r["created_at"]) >= since]
        if not mine:
            rows.append({"workflow": wf, "status": "PENDING", "reason": "прогон по опубликованному SHA не найден"})
            statuses.append("PENDING")
            continue
        last = max(mine, key=lambda r: (r["created_at"], r.get("run_attempt", 1)))
        row = {"workflow": wf, "run_id": last.get("id"), "url": last.get("html_url"),
               "status_api": last.get("status"), "conclusion": last.get("conclusion")}
        if last.get("status") != "completed":
            row["status"] = "PENDING"
        elif last.get("conclusion") == "success":
            row["status"] = "PASS"
        else:
            row["status"] = "FAIL"
        rows.append(row)
        statuses.append(row["status"])
    status = "FAIL" if "FAIL" in statuses else "PENDING" if "PENDING" in statuses else "PASS"
    return {"status": status, "head_sha": head_sha, "branch": branch, "required": list(required), "runs": rows,
            "decided_by": "verification (deterministic, Actions API)"}


def fetch_runs(repo: str, workflow: str, branch: str, head_sha: str) -> list[dict]:
    """Только чтение (`actions: read`)."""
    out = subprocess.run(["gh", "api", "-X", "GET", f"repos/{repo}/actions/workflows/{workflow}/runs",
                          "-f", f"branch={branch}", "-f", "event=workflow_dispatch", "-f", f"head_sha={head_sha}",
                          "-f", "per_page=20"], capture_output=True, text=True, check=True).stdout
    return json.loads(out).get("workflow_runs", [])


class GitHubVerifier:
    def __init__(self, repo: str, required: list[str]):
        self.repo, self.required = repo, required

    def __call__(self, run: dict) -> dict:
        v = run["verification"]
        runs = {wf: fetch_runs(self.repo, wf, run["branch"], v["head_sha"]) for wf in self.required}
        return evaluate(self.required, runs, v["head_sha"], run["branch"], v["dispatched_at"])


def timed_out(run: dict, timeout_minutes: int, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return now - _ts(run["verification"]["dispatched_at"]) > timedelta(minutes=timeout_minutes)
