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

Привязка PR (до READY_FOR_HUMAN_REVIEW): PR прогона существует, открыт, draft, base = main, не из форка,
его ветка — ветка кандидата и head — РОВНО опубликованный SHA. Иначе FAIL: зелёный CI другого SHA или
чужой/подменённый PR не делает кандидата «готовым к ревью».

Подделать это недоверенный job не может: создать прогон workflow нужно `actions: write`,
а у job'ов, исполняющих модель или код кандидата, его нет. Статусы коммита (`statuses`,
`checks`) не читаются вообще — их выставить проще, чем запуск.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


PR_FIELDS = "url,state,isDraft,baseRefName,headRefName,headRefOid,isCrossRepository"


def pr_binding(pr: dict | None, pr_url: str | None, head_sha: str, branch: str, base: str = "main",
               repo: str | None = None) -> list[str]:
    """Нарушения привязки PR к проверенному кандидату. Пустой список — привязка доказана."""
    if not pr_url:
        return ["у прогона нет pr_url"]
    if repo and not re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/[0-9]+", pr_url):
        return [f"pr_url {pr_url!r} не PR репозитория {repo}"]
    if not isinstance(pr, dict):
        return ["PR не прочитан"]
    checks = [(pr.get("url") == pr_url, f"url {pr.get('url')!r} ≠ {pr_url!r}"),
              (pr.get("state") == "OPEN", f"состояние {pr.get('state')!r}, нужен OPEN"),
              (pr.get("isDraft") is True, "PR не draft"),
              (pr.get("baseRefName") == base, f"base {pr.get('baseRefName')!r} ≠ {base!r}"),
              (pr.get("isCrossRepository") is False, "PR из форка или признак не получен"),
              (pr.get("headRefName") == branch, f"ветка {pr.get('headRefName')!r} ≠ {branch!r}"),
              (pr.get("headRefOid") == head_sha, f"head {str(pr.get('headRefOid'))[:12]} ≠ проверенный {head_sha[:12]}")]
    return [msg for ok, msg in checks if not ok]


def evaluate(required: list[str], runs: dict[str, list[dict]], head_sha: str, branch: str,
             dispatched_at: str, skew_seconds: int = 120, *, pr: dict | None, pr_url: str | None,
             repo: str | None = None) -> dict:
    """runs: workflow-файл → список объектов прогона API Actions (`GET …/runs`); pr — `gh pr view` PR прогона."""
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
    binding = pr_binding(pr, pr_url, head_sha, branch, repo=repo)
    status = "FAIL" if "FAIL" in statuses or binding else "PENDING" if "PENDING" in statuses else "PASS"
    return {"status": status, "head_sha": head_sha, "branch": branch, "required": list(required), "runs": rows,
            "pr_binding": {"status": "FAIL" if binding else "PASS", "problems": binding},
            "decided_by": "verification (deterministic, Actions API)"}


def fetch_runs(repo: str, workflow: str, branch: str, head_sha: str) -> list[dict]:
    """Только чтение (`actions: read`)."""
    out = subprocess.run(["gh", "api", "-X", "GET", f"repos/{repo}/actions/workflows/{workflow}/runs",
                          "-f", f"branch={branch}", "-f", "event=workflow_dispatch", "-f", f"head_sha={head_sha}",
                          "-f", "per_page=20"], capture_output=True, text=True, check=True).stdout
    return json.loads(out).get("workflow_runs", [])


def fetch_pr(repo: str, pr_url: str) -> dict:
    """Только чтение (`pull-requests: read`)."""
    out = subprocess.run(["gh", "pr", "view", pr_url, "--repo", repo, "--json", PR_FIELDS],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


class GitHubVerifier:
    def __init__(self, repo: str, required: list[str]):
        self.repo, self.required = repo, required

    def __call__(self, run: dict) -> dict:
        v = run["verification"]
        runs = {wf: fetch_runs(self.repo, wf, run["branch"], v["head_sha"]) for wf in self.required}
        url = run.get("pr_url")
        own = bool(url) and bool(re.fullmatch(rf"https://github\.com/{re.escape(self.repo)}/pull/[0-9]+", url))
        pr = fetch_pr(self.repo, url) if own else None          # чужой URL не открываем вовсе
        return evaluate(self.required, runs, v["head_sha"], run["branch"], v["dispatched_at"],
                        pr=pr, pr_url=url, repo=self.repo)


def timed_out(run: dict, timeout_minutes: int, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return now - _ts(run["verification"]["dispatched_at"]) > timedelta(minutes=timeout_minutes)
