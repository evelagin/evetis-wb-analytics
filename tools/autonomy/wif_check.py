"""Проверка инварианта S1: код в job'е AE не получает привилегированную идентичность GCP.

Что проверяется. GCP Workload Identity Federation решает в два шага:
  1. `attribute_condition` провайдера над claims OIDC-токена GitHub — принять ли токен вообще;
  2. `attribute_mapping` вычисляет атрибуты, а привязка `roles/iam.workloadIdentityUser` на SA
     (`principalSet://…/attribute.<имя>/<значение>`) решает, чей токен можно выпустить.
Этот модуль воспроизводит оба шага для ТОЧНЫХ claims, которые GitHub кладёт в токен
(docs.github.com/en/actions/reference/security/oidc), и отвечает: какие SA доступны job'у.

Три источника одной и той же модели:
  * желаемое состояние — Terraform (`infra/terraform/wif.tf`, `autonomy.tf`);
  * исторический снимок живого провайдера (`quality/autonomy/wif_live_snapshot_*.json`);
  * живой провайдер (`--live`, только чтение через gcloud) — проверка ПОСЛЕ применения.

CEL здесь — подмножество, которым пользуется провайдер: строки, `assertion.*`, `attribute.*`,
`+`, `==`, `!=`, `&&`, `||`, `!`, `?:`, скобки, методы `extract`, `startsWith`, `endsWith`,
`contains`. Неизвестная конструкция — исключение, а не «ложь»: проверка не угадывает.

Запуск:
  python -m tools.autonomy.wif_check                 # желаемое состояние (Terraform)
  python -m tools.autonomy.wif_check --snapshot FILE # исторический снимок
  python -m tools.autonomy.wif_check --live          # живой провайдер (read-only gcloud)
Код выхода 0 — все случаи A–E дали ожидаемый результат; 1 — инвариант нарушен.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TF_DIR = REPO / "infra" / "terraform"
GITHUB_REPO = "evelagin/evetis-wb-analytics"
# Неизменяемые числовые идентификаторы GitHub (строками, как в OIDC-токене). Имя репозитория можно
# освободить и занять заново; эти ID — нет. Источник: живое условие tenant-infra-pool и API GitHub.
GITHUB_REPOSITORY_ID = "1260095567"
GITHUB_REPOSITORY_OWNER_ID = "286048501"
PRIVILEGED = {"sa-deployer", "sa-terraform-apply", "sa-terraform-plan"}


# ------------------------------------------------------------------ CEL-подмножество ---
class CelError(ValueError):
    pass


_TOKEN = re.compile(r"""\s*(?:
    (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<op>==|!=|&&|\|\||[()+?:!.,])
  | (?P<id>[A-Za-z_][A-Za-z_0-9]*)
)""", re.X)


def _tokens(src: str) -> list[tuple[str, str]]:
    out, i = [], 0
    while i < len(src):
        if src[i:].strip() == "":
            break
        m = _TOKEN.match(src, i)
        if not m or m.end() == i:
            raise CelError(f"нераспознанный фрагмент CEL: {src[i:i + 30]!r}")
        kind = m.lastgroup
        val = m.group(kind)
        out.append((kind, val))
        i = m.end()
    return out


class _Parser:
    """Pratt-разбор. Приоритеты CEL: ?: < || < && < ==,!= < + < унарный ! < вызов/поле."""

    def __init__(self, src: str, env: dict):
        self.t, self.i, self.env = _tokens(src), 0, env

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, val=None):
        k, v = self.peek()
        if val is not None and v != val:
            raise CelError(f"ожидалось {val!r}, получено {v!r}")
        self.i += 1
        return k, v

    def parse(self):
        v = self.ternary()
        if self.i != len(self.t):
            raise CelError(f"лишний хвост CEL: {self.t[self.i:]}")
        return v

    def ternary(self):
        cond = self.binary(0)
        if self.peek()[1] == "?":
            self.take("?")
            a = self.ternary()
            self.take(":")
            b = self.ternary()
            if not isinstance(cond, bool):
                raise CelError("условие ?: не булево")
            return a if cond else b
        return cond

    LEVELS = [("||",), ("&&",), ("==", "!="), ("+",)]

    def binary(self, level):
        if level == len(self.LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while self.peek()[1] in self.LEVELS[level]:
            op = self.take()[1]
            right = self.binary(level + 1)
            if op == "||":
                left = _bool(left) or _bool(right)
            elif op == "&&":
                left = _bool(left) and _bool(right)
            elif op == "==":
                left = left == right
            elif op == "!=":
                left = left != right
            else:
                if not (isinstance(left, str) and isinstance(right, str)):
                    raise CelError("+ определён здесь только для строк")
                left = left + right
        return left

    def unary(self):
        if self.peek()[1] == "!":
            self.take("!")
            return not _bool(self.unary())
        return self.postfix(self.primary())

    def primary(self):
        k, v = self.take()
        if k == "str":
            return bytes(v[1:-1], "utf-8").decode("unicode_escape")
        if v == "(":
            val = self.ternary()
            self.take(")")
            return val
        if k == "id" and v in ("true", "false"):
            return v == "true"
        if k == "id":
            if v not in self.env:
                raise CelError(f"неизвестный идентификатор {v}")
            return self.env[v]
        raise CelError(f"неожиданный токен {v!r}")

    def postfix(self, val):
        while self.peek()[1] == ".":
            self.take(".")
            _, name = self.take()
            if self.peek()[1] == "(":
                self.take("(")
                args = [] if self.peek()[1] == ")" else [self.ternary()]
                while self.peek()[1] == ",":
                    self.take(",")
                    args.append(self.ternary())
                self.take(")")
                val = _method(val, name, args)
            else:
                if not isinstance(val, dict):
                    raise CelError(f"поле .{name} у не-объекта")
                # Отсутствующий claim в GCP — ошибка вычисления, токен отклоняется.
                if name not in val:
                    raise CelError(f"нет claim {name}")
                val = val[name]
        return val


def _bool(v):
    if not isinstance(v, bool):
        raise CelError(f"ожидалось булево значение, получено {type(v).__name__}")
    return v


def _extract(s: str, template: str) -> str:
    """CEL extract() в GCP: шаблон «префикс{name}суффикс»; значение — текст между первым
    вхождением префикса и первым последующим вхождением суффикса; нет совпадения — ''."""
    m = re.fullmatch(r"(.*)\{[A-Za-z_]+\}(.*)", template, re.S)
    if not m:
        raise CelError(f"неверный шаблон extract: {template!r}")
    pre, suf = m.groups()
    start = s.find(pre)
    if start < 0:
        return ""
    start += len(pre)
    if not suf:
        return s[start:]
    end = s.find(suf, start)
    return "" if end < 0 else s[start:end]


def _method(val, name, args):
    if not isinstance(val, str):
        raise CelError(f".{name}() у не-строки")
    if name == "extract":
        return _extract(val, args[0])
    if name == "startsWith":
        return val.startswith(args[0])
    if name == "endsWith":
        return val.endswith(args[0])
    if name == "contains":
        return args[0] in val
    raise CelError(f"метод .{name}() не поддержан проверкой — расширь подмножество осознанно")


def cel(expr: str, env: dict):
    return _Parser(expr, env).parse()


# --------------------------------------------------------------------- модель WIF ---
@dataclass
class WifConfig:
    source: str
    condition: str
    mapping: dict[str, str]
    # SA (account_id) → список (атрибут, значение). Атрибут "google.subject" — principal://…/subject/…
    bindings: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def attributes(self, claims: dict) -> dict[str, str] | None:
        """Атрибуты токена или None, если провайдер его отклоняет."""
        try:
            if not _bool(cel(self.condition, {"assertion": claims})):
                return None
            attrs = {}
            for target, expr in self.mapping.items():
                attrs[target] = cel(expr, {"assertion": claims})
            ok = True
        except CelError:
            return None
        return attrs if ok else None

    def obtainable(self, claims: dict) -> set[str]:
        attrs = self.attributes(claims)
        if attrs is None:
            return set()
        got = set()
        for sa, members in self.bindings.items():
            for attr, value in members:
                if attrs.get(attr) == value:
                    got.add(sa)
        return got


_POOL_MEMBER = re.compile(
    r"^principal(?P<set>Set)?://iam\.googleapis\.com/projects/[^/]+/locations/global/"
    r"workloadIdentityPools/[^/]+/(?:(?P<attr>attribute\.[a-z_0-9]+)|subject)/(?P<value>.+)$")


def _parse_member(member: str) -> tuple[str, str] | None:
    m = _POOL_MEMBER.match(member)
    if not m:
        return None
    if m.group("set"):
        return (m.group("attr"), m.group("value"))
    return ("google.subject", m.group("value"))


# -------------------------------------------------------------- Terraform (желаемое) ---
def _hcl_string(raw: str) -> str:
    """Строковый литерал HCL → Python (только экранирования, которые встречаются здесь)."""
    return json.loads(raw)


def _blocks(text: str, header: re.Pattern) -> list[tuple[re.Match, str]]:
    out = []
    for m in header.finditer(text):
        depth, i = 0, text.index("{", m.end() - 1)
        start = i
        while True:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            elif text[i] == '"':
                i += 1
                while text[i] != '"':
                    i += 2 if text[i] == "\\" else 1
            i += 1
        out.append((m, text[start + 1:i]))
    return out


def _interp(s: str, scope: dict[str, object]) -> str:
    def rep(m):
        key = m.group(1)
        if key not in scope:
            raise KeyError(f"интерполяция ${{{key}}} не разрешена проверкой")
        return str(scope[key])
    return re.sub(r"\$\{([a-z_.]+)\}", rep, s)


def load_terraform(tf_dir: Path = TF_DIR, github_repo: str = GITHUB_REPO) -> WifConfig:
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(tf_dir.glob("*.tf")))
    scope: dict[str, object] = {"var.github_repo": github_repo, "var.project_number": "PROJECT_NUMBER",
                                "google_iam_workload_identity_pool.github.workload_identity_pool_id": "github-pool"}
    # locals: строки и toset([...]) строк; порядок разрешения — многопроходный.
    pending: dict[str, str] = {}
    for _, body in _blocks(text, re.compile(r'^locals\s*\{', re.M)):
        for m in re.finditer(r'^\s*([a-z_0-9]+)\s*=\s*(toset\(\[.*?\]\)|"(?:[^"\\]|\\.)*")', body, re.M | re.S):
            pending[m.group(1)] = m.group(2)
    for _ in range(5):
        for name, raw in list(pending.items()):
            try:
                if raw.startswith("toset"):
                    items = re.findall(r'"((?:[^"\\]|\\.)*)"', raw)
                    scope[f"local.{name}"] = [_interp(_hcl_string(f'"{x}"'), scope) for x in items]
                else:
                    scope[f"local.{name}"] = _interp(_hcl_string(raw), scope)
                del pending[name]
            except KeyError:
                continue
    accounts = {m.group(1): m.group(2) for m in re.finditer(
        r'resource "google_service_account" "([a-z_0-9]+)"\s*\{[^}]*?account_id\s*=\s*"([^"]+)"', text, re.S)}

    prov = _blocks(text, re.compile(r'resource "google_iam_workload_identity_pool_provider" "github"\s*\{'))
    if len(prov) != 1:
        raise ValueError("ожидался ровно один провайдер github в Terraform")
    pbody = prov[0][1]
    mbody = _blocks(pbody, re.compile(r'attribute_mapping\s*=\s*\{'))[0][1]
    mapping = {m.group(1): _hcl_string(m.group(2)) for m in re.finditer(
        r'^\s*"([a-z_.]+)"\s*=\s*("(?:[^"\\]|\\.)*")', mbody, re.M)}
    cond = re.search(r'attribute_condition\s*=\s*("(?:[^"\\]|\\.)*")', pbody).group(1)
    condition = _interp(_hcl_string(cond), scope)

    bindings: dict[str, list[tuple[str, str]]] = {}
    hdr = re.compile(r'resource "google_service_account_iam_member" "([a-z_0-9]+)"\s*\{')
    for m, body in _blocks(text, hdr):
        if "roles/iam.workloadIdentityUser" not in body:
            continue
        sa_ref = re.search(r'service_account_id\s*=\s*google_service_account\.([a-z_0-9]+)\.name', body).group(1)
        member_raw = _hcl_string(re.search(r'member\s*=\s*("(?:[^"\\]|\\.)*")', body).group(1))
        fe = re.search(r'for_each\s*=\s*local\.([a-z_0-9]+)', body)
        values = scope[f"local.{fe.group(1)}"] if fe else [None]
        for v in values:
            member = _interp(member_raw.replace("${each.value}", v) if v is not None else member_raw, {
                **scope, "local.pool_principal": "principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/"
                                                 "locations/global/workloadIdentityPools/github-pool"})
            parsed = _parse_member(member)
            if parsed is None:
                raise ValueError(f"{m.group(1)}: нераспознанный member {member}")
            bindings.setdefault(accounts[sa_ref], []).append(parsed)
    return WifConfig("terraform", condition, mapping, bindings)


# ------------------------------------------------------------ снимок / живой провайдер ---
def from_snapshot(doc: dict, source: str) -> WifConfig:
    prov = doc["provider"]
    bindings: dict[str, list[tuple[str, str]]] = {}
    for sa, binds in doc["service_account_bindings"].items():
        for b in binds:
            fed = [m for m in b["members"] if m.startswith(("principal://", "principalSet://"))]
            if b["role"] not in ("roles/iam.workloadIdentityUser", "roles/iam.serviceAccountTokenCreator"):
                if fed:   # роль вне модели у федеративного принципала — не молчаливый пропуск (review PR #202)
                    raise ValueError(f"{sa}: роль {b['role']} у федеративного принципала — проверка её не моделирует")
                continue
            if b.get("condition"):
                raise ValueError(f"{sa}: IAM-условие на привязке — проверка его не моделирует")
            for member in b["members"]:
                parsed = _parse_member(member)
                if parsed:
                    bindings.setdefault(sa, []).append(parsed)
                elif member in fed:   # напр. весь пул `…/github-pool/*` — вне модели, а не «нет привязки»
                    raise ValueError(f"{sa}: федеративный участник вне модели: {member[:100]}")
    return WifConfig(source, prov.get("attributeCondition") or "true", prov["attributeMapping"], bindings)


def capture_live(project: str) -> dict:
    """Только чтение: описание провайдера и IAM-политики ВСЕХ SA проекта."""
    def j(*a):
        return json.loads(subprocess.check_output(["gcloud", *a, "--format=json", f"--project={project}"], text=True))
    prov = j("iam", "workload-identity-pools", "providers", "describe", "github-provider",
             "--workload-identity-pool=github-pool", "--location=global")
    sas = {}
    for acc in j("iam", "service-accounts", "list"):
        pol = j("iam", "service-accounts", "get-iam-policy", acc["email"])
        binds = [{"role": b["role"], "members": b["members"], **({"condition": b["condition"]} if "condition" in b else {})}
                 for b in pol.get("bindings", []) if any("principal" in m for m in b["members"])]
        if binds:
            sas[acc["email"].split("@")[0]] = binds
    return {"provider": {k: prov.get(k) for k in ("name", "attributeCondition", "attributeMapping", "state")},
            "service_account_bindings": sas}


# ----------------------------------------------------------------- claims и случаи ---
def claims(workflow: str, ref: str, event: str = "workflow_dispatch", job_workflow: str | None = None,
           repo: str = GITHUB_REPO, environment: str | None = None, job_ref: str | None = None,
           repository_id: str | None = None, repository_owner_id: str | None = None,
           runner_environment: str = "github-hosted") -> dict:
    """OIDC-токен GitHub так, как его выпускает token.actions.githubusercontent.com.

    workflow — файл верхнего уровня; job_workflow — файл, где объявлен job (переиспользуемый),
    по умолчанию тот же. Для локального `uses: ./.github/workflows/x.yml` ref вызываемого
    файла равен ref вызывающего."""
    wf_ref = f"{repo}/.github/workflows/{workflow}@{ref}"
    job_wf_ref = f"{repo}/.github/workflows/{job_workflow or workflow}@{job_ref or ref}"
    if environment:
        sub = f"repo:{repo}:environment:{environment}"
    elif event == "pull_request":
        sub = f"repo:{repo}:pull_request"
    else:
        sub = f"repo:{repo}:ref:{ref}"
    c = {"iss": "https://token.actions.githubusercontent.com", "aud": "https://iam.googleapis.com/…",
         "sub": sub, "repository": repo, "repository_owner": repo.split("/")[0], "ref": ref,
         "ref_type": "tag" if ref.startswith("refs/tags/") else "branch", "event_name": event,
         "workflow_ref": wf_ref, "job_workflow_ref": job_wf_ref, "sha": "0" * 40,
         "workflow_sha": "0" * 40, "job_workflow_sha": "0" * 40, "runner_environment": runner_environment,
         # Чужой репозиторий (или пересозданный с тем же именем) — другие ID.
         "repository_id": repository_id or (GITHUB_REPOSITORY_ID if repo == GITHUB_REPO else "999999999"),
         "repository_owner_id": repository_owner_id or (GITHUB_REPOSITORY_OWNER_ID
                                                        if repo.split("/")[0] == GITHUB_REPO.split("/")[0]
                                                        else "888888888")}
    if environment:
        c["environment"] = environment
    return c


MAIN = "refs/heads/main"
AE_RUN = "autonomy-run.yml"


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    claims: dict
    expect: frozenset[str]


def cases() -> list[Case]:
    """Ожидаемое множество SA для каждого job'а. Совпадение ДОЛЖНО быть точным: лишний SA —
    нарушение (привилегия), недостающий — поломка легитимного деплоя."""
    E = frozenset
    return [
        # A — легитимные привилегированные workflow: приняты
        Case("A1", "deploy-prod.yml, dispatch из main (environment production)",
             claims("deploy-prod.yml", MAIN, environment="production"), E({"sa-deployer"})),
        Case("A2", "deploy-shadow.yml, push в main", claims("deploy-shadow.yml", MAIN, event="push"), E({"sa-deployer"})),
        Case("A3", "infra.yml plan, dispatch с feature-ветки",
             claims("infra.yml", "refs/heads/pr-promo-1-observation-layer"), E({"sa-terraform-plan"})),
        Case("A4", "infra.yml apply/plan, dispatch из main (environment infra)",
             claims("infra.yml", MAIN, environment="infra"), E({"sa-terraform-plan", "sa-terraform-apply"})),
        Case("A5", "scheduler-control.yml, dispatch из main (environment production)",
             claims("scheduler-control.yml", MAIN, environment="production"), E({"sa-terraform-apply"})),
        Case("A6", "autonomy-watch.yml из main → только sa-ae-reader",
             claims("autonomy-watch.yml", MAIN, event="schedule"), E({"sa-ae-reader"})),
        Case("A7", "autonomy-run → job autonomy-gate.yml → только sa-ae-reader",
             claims(AE_RUN, MAIN, job_workflow="autonomy-gate.yml"), E({"sa-ae-reader"})),
        # B — инженер AE: только read-only
        Case("B1", "autonomy-run → job autonomy-engineer.yml (код агента)",
             claims(AE_RUN, MAIN, job_workflow="autonomy-engineer.yml"), E({"sa-ae-reader"})),
        Case("B2", "autonomy-run → job autonomy-test.yml (код кандидата)",
             claims(AE_RUN, MAIN, job_workflow="autonomy-test.yml"), E({"sa-ae-reader"})),
        # C — ревьюер AE: ничего
        Case("C1", "autonomy-run → job autonomy-review.yml", claims(AE_RUN, MAIN, job_workflow="autonomy-review.yml"),
             E()),
        # D — ветки кандидатов: ничего
        Case("D1", "deploy-prod.yml, dispatch на ветке кандидата ae/*",
             claims("deploy-prod.yml", "refs/heads/ae/fix-x-1234abcd", environment="production"), E()),
        Case("D2", "deploy-shadow.yml на ветке кандидата", claims("deploy-shadow.yml", "refs/heads/ae/fix-x-1234abcd"), E()),
        Case("D3", "infra.yml на ветке кандидата", claims("infra.yml", "refs/heads/ae/fix-x-1234abcd"), E()),
        Case("D4", "autonomy-run.yml на ветке кандидата → engineer",
             claims(AE_RUN, "refs/heads/ae/fix-x-1234abcd", job_workflow="autonomy-engineer.yml"), E()),
        Case("D5", "pull_request по PR кандидата (ci.yml)",
             claims("ci.yml", "refs/pull/200/merge", event="pull_request"), E()),
        Case("D6", "обязательная проверка: ci.yml dispatch на ветке кандидата",
             claims("ci.yml", "refs/heads/ae/fix-x-1234abcd"), E()),
        # E — попытки выдать себя за привилегированный workflow
        Case("E1", "файл «deploy-prod.yml@x.yml» на ветке x (extract до первого @)",
             claims("deploy-prod.yml@x.yml", "refs/heads/x"), E()),
        Case("E2", "файл «infra.yml@x.yml» на main (extract до первого @)", claims("infra.yml@x.yml", MAIN), E()),
        Case("E3", "ветка «main-evil»", claims("deploy-prod.yml", "refs/heads/main-evil"), E()),
        Case("E4", "тег refs/tags/main", claims("deploy-prod.yml", "refs/tags/main"), E()),
        Case("E5", "AE-workflow вызывает deploy-prod.yml как переиспользуемый",
             claims(AE_RUN, MAIN, job_workflow="deploy-prod.yml"), E()),
        Case("E6", "AE-workflow вызывает infra.yml как переиспользуемый",
             claims(AE_RUN, MAIN, job_workflow="infra.yml"), E()),
        Case("E7", "другой репозиторий с тем же путём",
             claims("deploy-prod.yml", MAIN, repo="evelagin/evetis-wb-analytics-fork"), E()),
        Case("E8", "environment production на непривилегированном файле из main",
             claims("sql-current.yml", MAIN, environment="production"), E()),
        Case("E9", "каталог-двойник .github/workflows/x/../deploy-prod.yml",
             claims("x/../deploy-prod.yml", MAIN), E()),
    ]


# Фазы раскатки (AE_V1_RUNBOOK.md §2). Полная матрица — целевое состояние после M3. Фаза 1
# (M1–M2) закрывает только привилегированные SA: sa-ae-reader ещё НЕ существует и существовать
# не должен, поэтому его ожидание снимается, а его появление — нарушение фазы.
PHASES = {"full": None, "wif-hardening": {"sa-ae-reader"}}


def verify(config: WifConfig, phase: str = "full") -> list[dict]:
    absent = PHASES[phase] or set()
    rows = []
    for c in cases():
        got = config.obtainable(c.claims)
        if absent:
            if got & absent:
                rows.append({"case": c.id, "title": c.title, "expect": sorted(c.expect - absent), "got": sorted(got),
                             "privileged_leak": sorted(got & absent),
                             "status": "FAIL"})
                continue
            c = Case(c.id, c.title, c.claims, frozenset(c.expect - absent))
        rows.append({"case": c.id, "title": c.title, "expect": sorted(c.expect), "got": sorted(got),
                     "privileged_leak": sorted((got - c.expect) & PRIVILEGED),
                     "status": "PASS" if got == c.expect else "FAIL"})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--snapshot", type=Path)
    src.add_argument("--live", action="store_true")
    ap.add_argument("--project", default="project-fa311fc0-4d87-4781-986")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--phase", choices=sorted(PHASES), default="full",
                    help="full — целевое состояние (после M3); wif-hardening — фаза 1 (M1–M2), sa-ae-reader отсутствует")
    a = ap.parse_args(argv)
    if a.live:
        cfg = from_snapshot(capture_live(a.project), "live")
    elif a.snapshot:
        cfg = from_snapshot(json.loads(a.snapshot.read_text(encoding="utf-8")), str(a.snapshot))
    else:
        cfg = load_terraform()
    rows = verify(cfg, a.phase)
    if a.json:
        print(json.dumps({"source": cfg.source, "phase": a.phase, "results": rows}, ensure_ascii=False, indent=2))
    else:
        print(f"источник: {cfg.source} · фаза: {a.phase}")
        for r in rows:
            leak = f"  УТЕЧКА {r['privileged_leak']}" if r["privileged_leak"] else ""
            print(f"{r['status']:4} {r['case']:3} {r['title'][:70]:70} → {r['got']}{leak}")
    return 0 if all(r["status"] == "PASS" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
