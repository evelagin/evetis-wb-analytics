"""Детерминированный публикатор. Единственный компонент AE v1 с правом записи в Git.

Модель к нему не имеет доступа: в GitHub Actions он работает в отдельном job без
агента, с токеном contents/pull-requests: write. Он повторно проверяет всё сам, не
доверяя предыдущим шагам: вердикт гейткипера, имя ветки, запрещённые пути, признаки
ослабления ворот. Пушит только явным refspec в refs/heads/ae/*, открывает только draft PR.

Публикация идемпотентна и привязана к проверенному кандидату: если ветка ae/* уже есть, её коммит
обязан быть ровно тем кандидатом (единственный родитель = repository_sha, то же дерево, что даёт
проверенный candidate.patch) — тогда он переиспользуется БЕЗ push; иначе отказ (force-push нет).
Открытый PR этой ветки переиспользуется, только если он draft и нацелен на base. Approve/merge/ready
публикатор не вызывает никогда. Без доверенного аудита «0 production-мутаций» публикации нет.

AE-R1 / B2: идентичность публикатора — отдельный интерфейс. Целевая — выделенный GitHub App с ключом
в Cloud KMS (docs/architecture/AE_V1_PUBLISHER_APP_DESIGN.md); App, ключ и учётные данные создаются
отдельными воротами, поэтому `GitHubAppIdentity` пока отказывает (fail closed). Широкое право
«Actions создаёт PR» не включается; `actions-token` оставлен только для стендов и dry-run.
Перед публикацией область класса задачи и лимит диффа проверяются ещё раз; тело PR — только
публичные метаданные (политика вывода B3).
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from tools.autonomy.audit import zero_mutations_proven
from tools.autonomy.output_policy import ensure_public
from tools.autonomy.policy import (branch_allowed, detect_gate_weakening, forbidden_paths, patch_data_findings,
                                   scope_violations, task_scope, tcb_paths)
from tools.autonomy.redact import diff_added_secrets, redact_text, safe_text
from tools.autonomy.report import render_report


class PublishRefused(RuntimeError):
    pass


def patch_files(patch: str) -> list[str]:
    files = set()
    for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", patch, re.M):
        files.update(m.groups())
    return sorted(files)


class ActionsTokenIdentity:
    """GITHUB_TOKEN job'а publish. Требует настройки «Allow GitHub Actions to create … pull requests»,
    которую владелец НЕ включает (B2). Только стенды и dry-run."""
    name = "actions-token"

    def ensure_ready(self) -> None:
        return None

    def env(self) -> dict:
        return {}


class GitHubAppIdentity:
    """Выделенный GitHub App (B2): installation token, подписанный ключом в Cloud KMS через WIF job'а
    publish. Не создан — отказ до любой записи. Реализация токена — отдельные ворота."""
    name = "github-app"

    def ensure_ready(self) -> None:
        raise PublishRefused("B2: публикатор GitHub App ещё не создан (App, ключ в KMS, WIF job'а publish) — "
                             "публикация невозможна; широкое право Actions создавать PR не используется")

    def env(self) -> dict:
        raise PublishRefused("B2: токен GitHub App недоступен")


IDENTITIES = {"actions-token": ActionsTokenIdentity, "github-app": GitHubAppIdentity}


class GitPublisher:
    def __init__(self, repo: Path, remote: str = "origin", base_branch: str = "main", dry_run: bool = False,
                 identity: str = "github-app"):
        self.repo, self.remote, self.base, self.dry_run = Path(repo), remote, base_branch, dry_run
        if identity not in IDENTITIES:
            raise PublishRefused(f"неизвестная идентичность публикатора {identity!r}")
        self.identity = IDENTITIES[identity]()
        self.log: list[list[str]] = []

    def _x(self, cmd: list[str], cwd: Path, mutating: bool = False) -> str:
        self.log.append(cmd)
        if mutating and self.dry_run:
            return "dry-run"
        env = None
        if (mutating or cmd[0] == "gh") and not self.dry_run:
            # Учётные данные записи даёт ТОЛЬКО идентичность публикатора (B2), а не окружение job'а.
            import os
            env = {**os.environ, **self.identity.env()}
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise PublishRefused(f"{' '.join(cmd[:3])}: {redact_text(r.stderr.strip()[:400])}")
        return r.stdout.strip()

    def preflight(self, run: dict, patch: str, art_dir: Path) -> list[str]:
        gate = json.loads((art_dir / "gate.json").read_text(encoding="utf-8"))
        if gate.get("verdict") != "READY_FOR_PR":
            raise PublishRefused(f"вердикт гейткипера {gate.get('verdict')}, а не READY_FOR_PR")
        if not branch_allowed(run["branch"]):
            raise PublishRefused(f"ветка {run['branch']} вне ae/* или защищена")
        files = patch_files(patch)
        if not files:
            raise PublishRefused("пустой кандидат — публиковать нечего")
        bad = forbidden_paths(files)
        if bad:
            raise PublishRefused(f"запрещённые пути (секреты): {bad}")
        leaked = diff_added_secrets(patch)
        if leaked:
            raise PublishRefused(f"секретоподобный материал в диффе: {leaked}")
        tcb = tcb_paths(files)
        if tcb:
            raise PublishRefused(f"запрещённые пути (TCB — только человек): {tcb}")
        weak = detect_gate_weakening(patch, files)
        if weak:
            raise PublishRefused(f"признаки ослабления ворот: {weak[0]}")
        if run.get("production_mutations"):
            raise PublishRefused("у прогона есть production-мутации")
        proven, why = zero_mutations_proven(run)
        if not proven:
            raise PublishRefused(f"нет доказательства 0 production-мутаций: {why}")
        objective_path = art_dir / "objective.json"
        objective = json.loads(objective_path.read_text(encoding="utf-8")) if objective_path.exists() else {}
        violations = scope_violations(files, patch, task_scope(objective))
        if violations:
            raise PublishRefused(f"кандидат вне области задачи: {violations[:3]}")
        data = patch_data_findings(patch)
        if data:
            raise PublishRefused(f"данные или бинарные изменения в диффе (политика вывода B3): {data[:3]}")
        return files

    def _gh(self, args: list[str], cwd: Path, mutating: bool = False) -> str:
        """Единственная точка вызова gh: только pr list/create (draft) и workflow run."""
        allowed = (["pr", "list"], ["pr", "create"], ["workflow", "run"])
        if args[:2] not in [list(a) for a in allowed]:
            raise PublishRefused(f"gh {' '.join(args[:2])}: публикатору не разрешено")
        if args[:2] == ["pr", "create"] and "--draft" not in args:
            raise PublishRefused("gh pr create без --draft запрещён")
        return self._x(["gh", *args], cwd, mutating=mutating)

    def _remote_sha(self, branch: str, cwd: Path) -> str | None:
        if self.dry_run:
            return None
        out = self._x(["git", "ls-remote", "--heads", self.remote, f"refs/heads/{branch}"], cwd)
        exact = [ln.split()[0] for ln in out.splitlines() if ln.split()[1:] == [f"refs/heads/{branch}"]]
        return exact[0] if exact else None

    def _existing_pr(self, branch: str, head_sha: str, cwd: Path) -> str | None:
        """Открытый PR ЭТОГО репозитория из ветки кандидата. PR из форков с тем же именем ветки не наши
        и игнорируются (gh pr list --head не различает владельца). Свой PR переиспользуется только если он
        draft, в base и его head — ровно проверенный head_sha; иначе отказ."""
        if self.dry_run:
            return None
        prs = json.loads(self._gh(["pr", "list", "--head", branch, "--state", "open", "--json",
                                   "url,isDraft,baseRefName,headRefOid,isCrossRepository"], cwd) or "[]")
        own = [pr for pr in prs if pr.get("isCrossRepository") is False]
        if not own:
            return None
        pr = own[0]
        if not pr.get("isDraft") or pr.get("baseRefName") != self.base or pr.get("headRefOid") != head_sha:
            raise PublishRefused(f"открытый PR ветки {branch} не draft, не в {self.base} или не на проверенном "
                                 f"{head_sha[:12]} — не трогаю")
        return pr["url"]

    def publish(self, run: dict, patch: str, art_dir: Path, required_workflows: list[str]) -> dict:
        """Опубликовать draft PR и запустить обязательные workflows на опубликованном SHA (S8).

        Вернуть {url, head_sha, dispatched_at, workflows}. «Опубликован» ещё не «готов»:
        готовность решает verification.py по фактическим прогонам."""
        from datetime import datetime, timezone
        if not self.dry_run:
            self.identity.ensure_ready()          # B2: без выделенной идентичности — ни одной записи
        files = self.preflight(run, patch, art_dir)
        branch = run["branch"]
        refspec = f"HEAD:refs/heads/{branch}"
        assert refspec.startswith("HEAD:refs/heads/ae/"), refspec   # последний рубеж
        body = ensure_public(safe_text(render_report(run, art_dir)))
        with tempfile.TemporaryDirectory(prefix="ae-publish-") as td:
            ws = Path(td) / "ws"
            self._x(["git", "worktree", "add", "--detach", str(ws), run["repository_sha"]], self.repo)
            try:
                # Без локальной ветки: коммит на detached HEAD, публикация только явным refspec ниже.
                subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=ws, input=patch, text=True,
                               check=True, capture_output=True)
                self._x(["git", "add", "-A"], ws)
                applied = sorted(self._x(["git", "-c", "core.quotepath=false", "diff", "--cached",
                                          "--name-only", "--no-renames"], ws).splitlines())
                if applied != files:
                    raise PublishRefused(f"применённый дифф не совпал с кандидатом: {applied} != {files}")
                self._x(["git", "-c", "user.name=evetis-autonomy", "-c", "user.email=autonomy@users.noreply.github.com",
                         "commit", "-q", "-m", f"AE {run['run_id']}: {run['objective_id']}\n\n"
                         f"Кандидат автономного контура. Вердикт гейткипера: READY_FOR_PR.\n"
                         f"Прогон: {run['run_id']}. Production-мутаций: 0."], ws)
                head_sha = self._x(["git", "rev-parse", "HEAD"], ws)
                remote_sha = self._remote_sha(branch, ws)
                if remote_sha is None:
                    self._x(["git", "push", self.remote, refspec], ws, mutating=True)
                else:
                    # Ветка уже опубликована (например, прошлый проход упал на создании PR): переиспользовать
                    # только если это ровно проверенный кандидат. Перезаписи нет.
                    self._x(["git", "fetch", "-q", self.remote, f"refs/heads/{branch}"], ws)
                    parents = self._x(["git", "rev-list", "--parents", "-n", "1", remote_sha], ws).split()[1:]
                    same_tree = (self._x(["git", "rev-parse", f"{remote_sha}^{{tree}}"], ws)
                                 == self._x(["git", "rev-parse", "HEAD^{tree}"], ws))
                    if parents != [run["repository_sha"]] or not same_tree:
                        raise PublishRefused(f"ветка {branch} уже существует и не совпадает с проверенным "
                                             f"кандидатом ({remote_sha[:12]}) — не перезаписываю")
                    head_sha = remote_sha
                (Path(td) / "body.md").write_text(body, encoding="utf-8")
                url = self._existing_pr(branch, head_sha, ws) or self._gh(
                    ["pr", "create", "--draft", "--base", self.base, "--head", branch,
                     "--title", f"[AE draft] {run['objective_id']}", "--body-file", str(Path(td) / "body.md")],
                    ws, mutating=True)
                dispatched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                for wf in required_workflows:
                    # Только файлы из политики и только на ветке кандидата. Параметров нет:
                    # прогон — ровно тот CI, что запускался бы на pull_request.
                    if not re.fullmatch(r"[a-z0-9-]+\.ya?ml", wf):
                        raise PublishRefused(f"недопустимое имя обязательного workflow: {wf!r}")
                    self._gh(["workflow", "run", wf, "--ref", branch], ws, mutating=True)
            finally:
                subprocess.run(["git", "worktree", "remove", "--force", str(ws)], cwd=self.repo,
                               capture_output=True)
        return {"url": url if not self.dry_run else f"dry-run://{branch}", "head_sha": head_sha,
                "dispatched_at": dispatched_at, "workflows": list(required_workflows)}
