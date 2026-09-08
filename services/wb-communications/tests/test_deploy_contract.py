"""Контракт развёртывания сервиса коммуникаций. Stage A Closeout, находка F-19.

Тесты офлайновые: читают только файлы репозитория, ни Cloud Run, ни BigQuery,
ни Wildberries не трогают.

Что защищаем. `deploy/env.production.yaml` — снимок конфигурации на момент
сборки образа (2026-07-23). Production с тех пор ушёл вперёд: ревизии 11…25
меняли только переменные окружения. Команда `gcloud run deploy --env-vars-file`
перезаписывает НАБОР переменных целиком, поэтому деплой этим файлом молча
выключил бы публикацию ответов в Wildberries.

Тесты фиксируют это расхождение как ЯВНО ОБЪЯВЛЕННОЕ и падают, если появится
новое, не разобранное человеком. Постоянно красного CI не создаётся: известная
дельта перечислена в KNOWN_DIVERGENCE, а новая — это отказ.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = SERVICE_ROOT / "deploy"
BUILD_ENV = DEPLOY_DIR / "env.production.yaml"
LIVE_ENV = DEPLOY_DIR / "env.production.live.yaml"
PREFLIGHT = DEPLOY_DIR / "preflight_env.py"
DEPLOY_DOC = SERVICE_ROOT / "DEPLOY.md"

# Расхождение, зафиксированное и объяснённое на Stage A Closeout 2026-09-08.
# Значения: имя переменной -> (в env.production.yaml, в живом Cloud Run).
# Любое ДРУГОЕ расхождение обязано пройти через человека и обновление этого списка.
KNOWN_DIVERGENCE = {
    "WB_PUBLISH_ENABLED": ("false", "true"),
    "WB_QUESTIONS_ENABLED": ("false", "true"),
    "WB_QUESTION_PUBLISH_ENABLED": ("false", "true"),
    "COMMUNICATION_ENGINE_V2_ENABLED": ("false", "true"),
    "COMMUNICATION_ENGINE_V2_SHADOW_ONLY": ("true", "false"),
    "COMMUNICATION_ENGINE_V2_PRIMARY": ("false", "true"),
    "WB_QUESTIONS_FIRST_RUN_MAX": ("20", "10"),
    # Найдено самим гейтом на Stage A Closeout: в файле личный чат, в production
    # групповой (отрицательный id). Деплой файлом увёл бы согласование ответов
    # из рабочей группы в личку одного человека.
    "TELEGRAM_CHAT_ID": ("302044578", "-5578869057"),
}

# Переменные, которые в Cloud Run приходят из Secret Manager. В снимке живой
# конфигурации их нет по построению (значения секретов не выгружаются).
# Простой литерал в файле деплоя ЗАТЁР БЫ ссылку на секрет — это опаснее, чем
# расхождение значений, поэтому проверяется отдельно.
SECRET_BACKED = {
    "ADMIN_TOKEN", "OPENAI_API_KEY", "SCHEDULER_SECRET",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_WEBHOOK_URL",
    "WB_API_TOKEN",
}

# Переменные, которых в живом сервисе нет: код применяет к ним собственные
# значения по умолчанию. Деплой файлом их добавит — это допустимо и объявлено.
KNOWN_ADDED_BY_FILE = {
    "WB_QUESTIONS_PATH", "WB_QUESTION_ANSWER_METHOD", "WB_QUESTION_ANSWER_STATE",
}

# Переменные, чьё изменение меняет поведение во внешнем мире.
BEHAVIOURAL = {
    "WB_PUBLISH_ENABLED",
    "WB_QUESTIONS_ENABLED",
    "WB_QUESTION_PUBLISH_ENABLED",
    "WB_VERIFY_BEFORE_PUBLISH",
    "COMMUNICATION_ENGINE_V2_ENABLED",
    "COMMUNICATION_ENGINE_V2_SHADOW_ONLY",
    "COMMUNICATION_ENGINE_V2_PRIMARY",
}


def parse_env_yaml(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$', line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


@pytest.fixture(scope="module")
def build_env() -> dict[str, str]:
    return parse_env_yaml(BUILD_ENV)


@pytest.fixture(scope="module")
def live_env() -> dict[str, str]:
    return parse_env_yaml(LIVE_ENV)


def test_live_snapshot_exists_and_is_populated(live_env):
    """Без снимка живой конфигурации расхождение нечем измерить."""
    assert LIVE_ENV.exists(), f"нет снимка живой конфигурации: {LIVE_ENV}"
    assert len(live_env) >= 30, f"снимок подозрительно мал: {len(live_env)} переменных"


def test_no_secret_values_in_deploy_files(build_env, live_env):
    """Секреты приходят из Secret Manager; их значений в репозитории быть не может."""
    suspicious = re.compile(r"(TOKEN|SECRET|API_KEY|PASSWORD)$")
    for name, env in (("env.production.yaml", build_env), ("env.production.live.yaml", live_env)):
        for key, value in env.items():
            if suspicious.search(key):
                pytest.fail(f"{name}: переменная {key} не должна присутствовать со значением")
            assert not re.fullmatch(r"[A-Za-z0-9_\-]{32,}", value), (
                f"{name}: значение {key} похоже на секрет"
            )


def test_divergence_is_exactly_the_declared_one(build_env, live_env):
    """Главный гейт: новое, не разобранное человеком расхождение — это отказ."""
    actual = {
        k: (build_env[k], live_env[k])
        for k in build_env
        if k in live_env and build_env[k] != live_env[k]
    }
    undeclared = {k: v for k, v in actual.items() if k not in KNOWN_DIVERGENCE}
    assert not undeclared, (
        "появилось незадекларированное расхождение build-конфигурации и production: "
        f"{undeclared}. Разобрать причину и обновить KNOWN_DIVERGENCE вместе с "
        "deploy/env.production.live.yaml — молча деплоить нельзя."
    )
    resolved = {k: v for k, v in KNOWN_DIVERGENCE.items() if k not in actual}
    assert not resolved, (
        f"расхождение по {sorted(resolved)} исчезло — убрать их из KNOWN_DIVERGENCE, "
        "чтобы список не превратился в вечный allowlist."
    )
    for key, expected in KNOWN_DIVERGENCE.items():
        assert actual[key] == expected, (
            f"{key}: ожидалось {expected}, фактически {actual[key]}. "
            "Значение изменилось — требуется решение человека."
        )


def test_behavioural_flags_are_covered_by_the_guard(build_env, live_env):
    """Каждый поведенческий флаг обязан присутствовать в обоих файлах.

    Исчезновение флага из файла деплоя опаснее расхождения значений: деплой с
    --env-vars-file перезаписывает набор целиком, и переменная просто пропала бы,
    вернув сервис к дефолту из кода.
    """
    for flag in BEHAVIOURAL:
        assert flag in live_env, f"{flag} отсутствует в снимке production"
        assert flag in build_env, f"{flag} отсутствует в env.production.yaml"


def test_preflight_guard_exists_and_is_fail_closed():
    """Скрипт-гейт должен существовать и по умолчанию отказывать."""
    assert PREFLIGHT.exists(), "нет deploy/preflight_env.py"
    text = PREFLIGHT.read_text(encoding="utf-8")
    assert "--accept-changes" in text, "нет явного подтверждения изменений"
    assert "return 1" in text, "гейт не возвращает ненулевой код при дельте"
    assert "return 2" in text, "недоступность живой конфигурации обязана быть отказом"


def test_deploy_doc_warns_about_the_trap():
    """DEPLOY.md не должен предлагать голый --env-vars-file без преддеплойной проверки."""
    doc = DEPLOY_DOC.read_text(encoding="utf-8")
    assert "preflight_env.py" in doc, (
        "DEPLOY.md обязан вести через deploy/preflight_env.py: без этого инструкция "
        "снова предложит деплой, выключающий публикацию в Wildberries"
    )
    for line in doc.splitlines():
        if "--env-vars-file" in line and not line.lstrip().startswith(("#", ">", "*", "-")):
            assert "preflight" in doc[: doc.index(line)], (
                "команда с --env-vars-file встречается раньше упоминания preflight_env.py"
            )


def test_deploy_file_does_not_shadow_secret_backed_variables(build_env, live_env):
    """Литерал в файле деплоя не должен подменять переменную из Secret Manager."""
    shadowed = sorted(SECRET_BACKED & set(build_env))
    assert shadowed == ["TELEGRAM_WEBHOOK_URL"], (
        f"изменился набор переменных, затирающих секреты: {shadowed}. "
        "Деплой с --env-vars-file заменил бы ссылку на Secret Manager литералом."
    )
    # Известный случай зафиксирован явно: пустая строка вместо секретного URL.
    assert build_env["TELEGRAM_WEBHOOK_URL"] == "", (
        "TELEGRAM_WEBHOOK_URL в файле деплоя перестал быть пустым — разобрать, "
        "не попал ли в репозиторий реальный URL вебхука"
    )
    assert "TELEGRAM_WEBHOOK_URL" not in live_env, (
        "TELEGRAM_WEBHOOK_URL появился в снимке как обычная переменная — значит "
        "он больше не приходит из Secret Manager, контракт изменился"
    )


def test_variables_added_by_the_file_are_declared(build_env, live_env):
    """Набор переменных, которых нет в production, тоже под контролем."""
    added = set(build_env) - set(live_env) - SECRET_BACKED
    assert added == KNOWN_ADDED_BY_FILE, (
        f"изменился набор переменных, отсутствующих в production: {sorted(added)}. "
        f"Ожидалось {sorted(KNOWN_ADDED_BY_FILE)}."
    )
