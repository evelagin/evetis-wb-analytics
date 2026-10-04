#!/usr/bin/env python3
"""Runtime ingestion домена Ozon — общий слой.

Изоляция маркетплейсов: ничего из wb_raw и wb_mart не читается и не пишется.
Идентификаторы товаров резолвятся только через evetis_ref.REF_SKU_CHANNEL_MAP.

Секреты живут в памяти процесса. Токен Performance API эфемерный: не сохраняется,
не логируется, не коммитится.

Арендатор (Tenancy T2, ADR-08). Код один для всех арендаторов: продавца выбирает
ТОЛЬКО конфигурация процесса — проект, датасеты и ИМЕНА секретов из переменных
окружения (resolve_config ниже). Ветвлений по арендатору в коде нет и быть не должно.
"""
import collections
import hashlib
import io
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

from google.cloud import bigquery, secretmanager

MSK = timezone(timedelta(hours=3))


# ───────────────────────────────────────────── конфигурация процесса (T2)
class ConfigError(RuntimeError):
    """Конфигурация процесса неполна или недопустима. Прогон не начинается."""


# Имена секретов EVETIS — значения ПО УМОЛЧАНИЮ на переходный период T2.
# Production-job'ы EVETIS переменные OZON_SECRET_* не задают (проверено 2026-09-24,
# gcloud run jobs describe по всем ozon-*), поэтому поведение EVETIS не меняется.
# Внешний арендатор передаёт свои имена явно; их выводит tools/tenancy/naming.py.
LEGACY_EVETIS_SECRET_DEFAULTS = {
    "OZON_SECRET_SELLER_CLIENT_ID": "EVETIS_OZON_CLIENT_ID",
    "OZON_SECRET_SELLER_API_KEY": "EVETIS_OZON_API_KEY",
    "OZON_SECRET_PERF_CLIENT_ID": "EVETIS_OZON_PERFORMANCE_CLIENT_ID",
    "OZON_SECRET_PERF_CLIENT_SECRET": "EVETIS_OZON_PERFORMANCE_CLIENT_SECRET",
}
# Справочный датасет EVETIS: переходное значение по умолчанию (T2), не переименовывается.
LEGACY_EVETIS_REF_DATASET = "evetis_ref"

# Ограничения GCP. ID секрета: буквы, цифры, «-» и «_», до 255 символов; символ «/»
# запрещён (Secret Manager REST projects.secrets.create, поле secretId).
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_DATASET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
_SECRET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_LOCATION_RE = re.compile(r"^[A-Za-z0-9-]{2,32}$")

RuntimeConfig = collections.namedtuple("RuntimeConfig", [
    "project", "raw_dataset", "ref_dataset", "location",
    "secret_seller_client_id", "secret_seller_api_key",
    "secret_perf_client_id", "secret_perf_client_secret",
    "strict_page_caps"])


def _env_value(env, name, default=None):
    """Значение переменной. Не задана — default; задана пустой строкой — ошибка.

    Пустая строка — явная, но сломанная конфигурация (например, пустое значение
    в Terraform). Молча подставить вместо неё значение EVETIS нельзя.
    """
    if name not in env:
        return default
    v = env[name].strip()
    if not v:
        raise ConfigError(f"{name} задана пустой строкой")
    return v


def resolve_config(env):
    """Конфигурация Ozon runtime из окружения. Чистая функция: сети и облака не трогает.

    GCP_PROJECT_ID обязателен (fail-closed). Прежний неявный fallback на проект
    EVETIS удалён: job без явного проекта мог бы писать не туда, куда его назначили.
    """
    project = _env_value(env, "GCP_PROJECT_ID")
    if project is None:
        raise ConfigError(
            "GCP_PROJECT_ID не задан. Проект указывается явно для каждого job'а "
            "(infra/terraform/ozon_ingestion.tf → ozon_common_env); неявного проекта "
            "по умолчанию больше нет")
    if not _PROJECT_ID_RE.match(project):
        raise ConfigError(f"GCP_PROJECT_ID не похож на ID проекта GCP: {project!r}")

    raw_dataset = _env_value(env, "BQ_RAW_DATASET", "ozon_raw")
    ref_dataset = _env_value(env, "BQ_REF_DATASET", LEGACY_EVETIS_REF_DATASET)
    for name, v in (("BQ_RAW_DATASET", raw_dataset), ("BQ_REF_DATASET", ref_dataset)):
        if not _DATASET_RE.match(v):
            raise ConfigError(f"{name} не является именем датасета BigQuery: {v!r}")
    location = _env_value(env, "BQ_LOCATION", "EU")
    if not _LOCATION_RE.match(location):
        raise ConfigError(f"BQ_LOCATION недопустима: {location!r}")

    refs = {}
    for var, default in LEGACY_EVETIS_SECRET_DEFAULTS.items():
        v = _env_value(env, var, default)
        if not _SECRET_ID_RE.match(v):
            # Значение в сообщение не попадает: вдруг туда по ошибке вставили сам ключ.
            raise ConfigError(
                f"{var} не является ID секрета Secret Manager "
                "(разрешены буквы, цифры, '-' и '_', до 255 символов)")
        refs[var] = v

    strict_raw = _env_value(env, "STRICT_PAGE_CAPS", "0")
    if strict_raw not in ("0", "1"):
        raise ConfigError(f"STRICT_PAGE_CAPS допускает только 0 или 1, получено {strict_raw!r}")

    return RuntimeConfig(
        project=project, raw_dataset=raw_dataset, ref_dataset=ref_dataset,
        location=location,
        secret_seller_client_id=refs["OZON_SECRET_SELLER_CLIENT_ID"],
        secret_seller_api_key=refs["OZON_SECRET_SELLER_API_KEY"],
        secret_perf_client_id=refs["OZON_SECRET_PERF_CLIENT_ID"],
        secret_perf_client_secret=refs["OZON_SECRET_PERF_CLIENT_SECRET"],
        strict_page_caps=(strict_raw == "1"))


CONFIG = resolve_config(os.environ)
PROJECT = CONFIG.project
DATASET = CONFIG.raw_dataset
REF_DATASET = CONFIG.ref_dataset
LOCATION = CONFIG.location
STRICT_PAGE_CAPS = CONFIG.strict_page_caps
RUNS_TABLE = "OZON_INGESTION_RUNS"

SELLER = "https://api-seller.ozon.ru"
PERF = "https://api-performance.ozon.ru"

# ───────────────────── белые списки вызываемых методов (Tenancy T5, «только чтение»)
# HTTP-метод не отличает чтение от записи: у Seller API почти всё POST, а у Performance API есть
# GET, которые меняют кабинет (/api/client/campaign/all_sku_promo/activate). Поэтому runtime и
# control вызывают ТОЛЬКО перечисленные пути; всё прочее — отказ до отправки запроса. Промо-
# наблюдатель ходит через свой список (promo.ALLOWED_PATHS). Класс каждого метода Seller —
# seller_method_policy.json; тест сверяет, что здесь только READ.
SELLER_ALLOWED_PATHS = frozenset({
    "/v3/product/list", "/v3/product/info/list", "/v5/product/info/prices", "/v1/seller/info",
    "/v1/rating/summary", "/v1/analytics/stocks", "/v3/posting/fbo/list", "/v1/finance/accrual/types",
    "/v1/finance/accrual/by-day", "/v1/cluster/list", "/v3/supply-order/list", "/v3/supply-order/get",
    "/v1/supply-order/bundle",
    # control (T5): инспекция ключа и проба FBS-активности
    "/v1/roles", "/v3/posting/fbs/list",
})
PERF_ALLOWED_GET = (
    re.compile(r"^/api/client/campaign$"),
    re.compile(r"^/api/client/campaign/\d+/v2/products\?page=\d+&pageSize=\d+$"),
    re.compile(r"^/api/client/statistics/(expense|daily)\?dateFrom=\d{4}-\d{2}-\d{2}&dateTo=\d{4}-\d{2}-\d{2}$"),
    re.compile(r"^/api/client/statistics/[0-9A-Za-z-]{1,64}$"),
    re.compile(r"^/api/client/statistics/report\?UUID=[0-9A-Za-z-]{1,64}$"),
)
PERF_ALLOWED_POST = frozenset({"/api/client/statistics"})


class ApiPathDenied(RuntimeError):
    """Путь не входит в белый список «только чтение»: запрос не отправляется."""
BACKOFF = [3, 6, 12, 24, 48]

class _CredentialCache:
    """No ordinary cache read outside the controlled loader (not a Python sandbox)."""
    def __init__(self):
        self._values = {}

    def __setitem__(self, key, value):
        self._values[key] = value

    def _check(self):
        if sys._getframe(2).f_code is not secret.__code__:
            raise ApiPathDenied("credential cache outside transport")

    def __getitem__(self, key):
        self._check()
        return self._values.__getitem__(key)

    def __contains__(self, key):
        self._check()
        return self._values.__contains__(key)

    def get(self, *args):
        self._check()
        return self._values.get(*args)

    def items(self):
        self._check()
        return self._values.items()

    def values(self):
        self._check()
        return self._values.values()

    def keys(self):
        self._check()
        return self._values.keys()

    def __iter__(self):
        self._check()
        return self._values.__iter__()

    def copy(self):
        self._check()
        return self._values.copy()

    def __repr__(self):
        return "<protected credential cache>"

    __str__ = __repr__


_secrets = _CredentialCache()
_bq = None
_perf_token = {"value": None, "at": 0}
STATS = {"requests": 0, "retries": 0}


# ─────────────────────── граница безопасности учётных данных (Tenancy T2.2)
# ЕДИНСТВЕННОЕ место, где решается, что из учётных данных может покинуть процесс.
#
# Классификация (docs/architecture/TECH_DEBT.md, P2-8):
#   СЕКРЕТ — Api-Key Seller API, client_secret Performance API, токен доступа
#     Performance API. Вырезаются из ЛЮБОГО текста, покидающего процесс, как
#     подстрока, включая экранированные формы, и заменяются маркером
#     «<redacted:роль>» без единого символа секрета.
#   ИДЕНТИФИКАТОР — client_id Performance API и Client-Id Seller API. Основания
#     разные, и завышать их нельзя:
#       * client_id Performance API — OAuth 2.0; RFC 6749 §2.2 прямо: «The client
#         identifier is not a secret»;
#       * Client-Id Seller API — документация Ozon требует его вместе с Api-Key в
#         каждом запросе, но прямого утверждения о его (не)конфиденциальности не
#         содержит. Считать его идентификатором — КЛАССИФИКАЦИЯ ПЛАТФОРМЫ: сам по себе
#         он не аутентифицирует, а вырезание короткого числа подавляло бы невинные
#         события (находка L4).
#     Идентификаторы не вырезаются.
#
# Выходы наружу — log() (stdout), record_run() (OZON_INGESTION_RUNS), текст ошибок
# (safe_error_text) и необработанные исключения (safe_excepthook → stderr). Решение
# принимается на СТРУКТУРИРОВАННЫХ значениях до сериализации (redact_value, L7);
# поиск по готовой строке — только страховка. Обрезка недоверенного текста — только
# ПОСЛЕ вырезания (safe_error_text): иначе фрагмент секрета на границе обрезки не
# узнаётся. Без загруженных секретов всё это тождественно прежнему поведению.
LEAK_MARKER = "credential_material_suppressed"
_redactions = {}             # вариант записи секрета → маркер
_redaction_re = None         # один regex на все варианты, длинные раньше коротких


def _escape_forms(s):
    """Формы, в которых строка встречается после одного шага сериализации."""
    return {s, json.dumps(s, ensure_ascii=False)[1:-1], json.dumps(s)[1:-1],
            repr(s)[1:-1], s.encode("unicode_escape").decode("ascii")}


def _secret_variants(value):
    """Все записи значения, в которых оно может дойти до выхода.

    До трёх уровней вложенного экранирования: сообщение с repr() словаря внутри
    repr() исключения внутри JSON-строки лога — обычный путь ошибки API.
    """
    forms, frontier = {value}, {value}
    for _ in range(3):
        grown = set().union(*(_escape_forms(f) for f in frontier)) - forms
        forms |= grown
        frontier = grown
    forms |= {repr(value.encode("utf-8"))[2:-1],
              urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)}
    return {f for f in forms if f}


def register_secret(value, role):
    """Внести значение класса СЕКРЕТ в вырезание. Роль — имя в маркере, не значение."""
    global _redaction_re
    if not value:
        return
    marker = f"<redacted:{role}>"
    for v in _secret_variants(value):
        _redactions.setdefault(v, marker)
    # Один проход по всем вариантам: вставленный маркер повторно не просматривается,
    # а более длинное значение (секрет, содержащий другой секрет) вырезается первым.
    _redaction_re = re.compile("|".join(
        re.escape(v) for v in sorted(_redactions, key=len, reverse=True)))


def redact_text(text):
    """Строка без секретов. Идентификаторы и прочие данные не трогаются."""
    if _redaction_re is None or not isinstance(text, str):
        return text
    return _redaction_re.sub(lambda m: _redactions[m.group(0)], text)


def redact_value(obj):
    """Структурированное значение без секретов — ДО сериализации (L7).

    Типы, которые json.dumps сам не знает, превращаются в str() здесь, а не в
    default=str: иначе их строковая форма миновала бы вырезание.
    """
    if _redaction_re is None:
        return obj
    if isinstance(obj, str):
        return redact_text(obj)
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, dict):
        return {(redact_text(k) if isinstance(k, str) else k): redact_value(v)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_value(v) for v in obj]
    return redact_text(str(obj))


def safe_error_text(err, limit=400):
    """Текст ошибки для журнала, OZON_INGESTION_RUNS и сообщений исключений.

    Сначала вырезание по ПОЛНОМУ тексту, потом обрезка: так на границе `limit`
    не остаётся фрагмента секрета, а остальная диагностика сохраняется.
    """
    return _hide_v2_fingerprint(redact_text(str(err)))[:limit]


def _hide_v2_fingerprint(text):
    return re.sub(r"ozon-seller-core-v2:sha256:[0-9a-f]{64}", "<identity:suppressed>", text)


def _log_identity_boundary(value):
    # Log-only boundary: stored observations/marker material must remain intact.
    if isinstance(value, dict):
        return {k: ("<identity:suppressed>" if k in {
            "identity_fingerprint", "seller_binding_fingerprint", "fingerprint_material",
            "company_inn", "company_ogrn", "legal_evidence"} else _log_identity_boundary(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_log_identity_boundary(v) for v in value]
    return _hide_v2_fingerprint(value) if isinstance(value, str) else value


class JournalWriteError(RuntimeError):
    """Запись в OZON_INGESTION_RUNS не удалась при обработке ошибки сущности.

    Заменяет неявную цепочку исключений (L3): прежде сырой текст исходной ошибки —
    вместе с повторённым сервером ключом — уходил в stderr через «During handling
    of the above exception…». Оба текста здесь уже очищены, категория обеих
    ошибок и имя сущности сохранены.
    """

    def __init__(self, entity, primary, journal):
        super().__init__(
            f"{entity}: запись в {RUNS_TABLE} не удалась — {type(journal).__name__}: "
            f"{safe_error_text(journal, 300)}; исходная ошибка сущности — "
            f"{type(primary).__name__}: {safe_error_text(primary, 300)}")


def format_exception_safely(exc_type, exc, tb):
    """Полная трассировка, включая цепочку, — с вырезанными секретами."""
    return redact_text("".join(traceback.format_exception(exc_type, exc, tb)))


def safe_excepthook(exc_type, exc, tb):
    """sys.excepthook runtime: трассировка остаётся полезной, секреты — нет.

    Хук НИКОГДА не выбрасывает исключение. Если бы он упал, CPython напечатал бы
    «Error in sys.excepthook … Original exception was:» и следом — исходное
    исключение стандартным путём, то есть без вырезания. Поэтому:
      * сбой форматирования или вырезания → печатается только тип исключения;
      * сбой записи в sys.stderr → попытка в исходный sys.__stderr__;
      * не удалось и это → молча: процесс и так завершается с ненулевым кодом.
    Без секретов вывод совпадает со стандартной трассировкой Python.
    """
    try:
        text = format_exception_safely(exc_type, exc, tb)
    except BaseException:                                         # noqa: BLE001
        text = (f"{getattr(exc_type, '__name__', 'Exception')}: "
                "<текст исключения не удалось безопасно отформатировать>\n")
    for stream in (sys.stderr, sys.__stderr__):
        try:
            stream.write(text)
            stream.flush()
            return
        except BaseException:                                     # noqa: BLE001
            continue


def log(**kw):
    """Структурный лог. Секреты вырезаются из значений до сериализации."""
    line = json.dumps(_log_identity_boundary(redact_value(kw)), ensure_ascii=False, default=str)
    if _redaction_re is not None and _redaction_re.search(line):
        # Страховка: сюда нельзя попасть, если вырезание значений отработало.
        # Печатать строку с секретом нельзя ни при каких условиях.
        line = json.dumps({"event": LEAK_MARKER,
                           "suppressed_event": redact_text(str(kw.get("event")))[:80]},
                          ensure_ascii=False)
    print(line, flush=True)


_sm = None
# Имя секрета → роль класса СЕКРЕТ. Имена идентификаторов сюда не входят (L4).
_SECRET_ROLE_BY_NAME = {CONFIG.secret_seller_api_key: "seller_api_key",
                        CONFIG.secret_perf_client_secret: "performance_client_secret"}


def secret(name):
    """Значение секрета из Secret Manager. В логи и на диск не попадает."""
    global _sm
    caller = sys._getframe(1).f_code
    readers = {
        CONFIG.secret_seller_api_key: (_seller_headers,),
        CONFIG.secret_seller_client_id: (_seller_headers, seller_client_id),
        CONFIG.secret_perf_client_id: (perf_token, perf_client_id),
        CONFIG.secret_perf_client_secret: (perf_token,),
    }
    if caller not in tuple(f.__code__ for f in readers.get(name, ())):
        raise ApiPathDenied("credential read outside controlled transport")
    if name not in _secrets:
        if _sm is None:
            _sm = secretmanager.SecretManagerServiceClient()
        path = f"projects/{PROJECT}/secrets/{name}/versions/latest"
        _secrets[name] = _sm.access_secret_version(
            request={"name": path}).payload.data.decode("utf-8").strip()
        if name in _SECRET_ROLE_BY_NAME:
            register_secret(_secrets[name], _SECRET_ROLE_BY_NAME[name])
    return _secrets[name]


def seller_client_id():
    """Client-Id Seller API — ИДЕНТИФИКАТОР (не секрет, T2.2 L4): нужен для отпечатка кабинета."""
    return secret(CONFIG.secret_seller_client_id)


def perf_client_id():
    """client_id Performance API — ИДЕНТИФИКАТОР (RFC 6749 §2.2): нужен для отпечатка аккаунта."""
    return secret(CONFIG.secret_perf_client_id)


def _seller_headers():
    """Заголовки Seller API по ИМЕНАМ секретов из конфигурации процесса.

    Единственное место, где собираются учётные данные Seller API: им пользуются
    только controlled seller_call; normal runtime modules не получают этот интерфейс.
    """
    if sys._getframe(1).f_code is not seller_call.__code__:
        raise ApiPathDenied("Seller headers outside controlled transport")
    return {"Client-Id": secret(CONFIG.secret_seller_client_id),
            "Api-Key": secret(CONFIG.secret_seller_api_key),
            "Content-Type": "application/json"}


def bq():
    global _bq
    if _bq is None:
        _bq = bigquery.Client(project=PROJECT, location=LOCATION)
    return _bq


def now_msk():
    return datetime.now(MSK)


def h(*parts):
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:32]


# ------------------------------------------------------------------ HTTP
def _request(req, attempt=0, raw_text=False):
    credentialed = (hasattr(req, "_seller_profile") or urllib.parse.urlsplit(req.full_url).hostname == "api-seller.ozon.ru"
                    or any(k.lower() in ("api-key", "client-id") for k, _ in req.header_items()))
    if credentialed:
        _validate_seller_route(req.full_url, req.get_method(), getattr(req, "_seller_profile", None))
    STATS["requests"] += 1
    try:
        opener = _seller_open if credentialed else urllib.request.urlopen
        with opener(req, timeout=180) as r:
            body = r.read()
            # surrogateescape: отчёты Performance API приходят ZIP-архивом, строгий
            # utf-8 на них падает. Round-trip .encode("utf-8","surrogateescape")
            # в entities.py восстанавливает байты один в один.
            return r.status, (body.decode("utf-8", "surrogateescape")
                              if raw_text else json.loads(body))
    except ApiPathDenied:
        raise
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")
        if e.code in (429, 500, 502, 503, 504) and attempt < len(BACKOFF):
            STATS["retries"] += 1
            time.sleep(BACKOFF[attempt])
            return _request(req, attempt + 1, raw_text)
        return e.code, {"_error": safe_error_text(payload, 400)}
    except Exception as e:                                        # SSL, таймаут, обрыв
        if attempt < len(BACKOFF):
            STATS["retries"] += 1
            time.sleep(BACKOFF[attempt])
            return _request(req, attempt + 1, raw_text)
        return "NET_ERROR", {"_error": safe_error_text(repr(e), 300)}


PROMO_ALLOWED_ROUTES = frozenset({
    ("GET", "/v1/actions"), ("POST", "/v1/actions/products"),
    ("POST", "/v1/actions/candidates"), ("POST", "/v1/actions/auto-add/products/list"),
    ("POST", "/v1/actions/auto-add/products/candidates"), ("POST", "/v5/product/info/prices"),
})
SELLER_PROFILES = MappingProxyType({
    "runtime": frozenset(("POST", p) for p in SELLER_ALLOWED_PATHS),
    "control": frozenset(("POST", p) for p in SELLER_ALLOWED_PATHS),
    "promo": PROMO_ALLOWED_ROUTES,
})
# A profile argument cannot enable a profile denied by the actual entrypoint.
_seller_execution_scope = None
_execution_contract = json.loads(Path(__file__).with_name("runtime_execution_contract.json").read_text())
if (type(_execution_contract.get("schema_version")) is not int or _execution_contract["schema_version"] != 1
        or _execution_contract.get("external_binding_required") is not True
        or _execution_contract.get("external_promo_enabled") is not False
        or not isinstance(_execution_contract.get("legacy_ingestion_project"), str)):
    raise ConfigError("unsupported runtime execution contract")
LEGACY_INGESTION_PROJECT = _execution_contract["legacy_ingestion_project"]


def _validate_seller_route(url, method, profile):
    if profile not in SELLER_PROFILES:
        raise ApiPathDenied("Seller execution profile denied")
    if _seller_execution_scope is not None and profile not in _seller_execution_scope:
        raise ApiPathDenied("Seller profile outside entrypoint scope")
    # Dedicated-tenant namespace is protected even for direct entity entrypoints.
    if CONFIG.project.startswith("mpa-t") and profile == "promo":
        raise ApiPathDenied("Promo not in dedicated-tenant callable contract")
    if not isinstance(url, str) or any(c in url for c in "%?#\\") or any(ord(c) <= 32 or ord(c) >= 127 for c in url):
        raise ApiPathDenied("Seller URL syntax denied")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        raise ApiPathDenied("Seller URL syntax denied") from None
    if parsed.scheme != "https" or parsed.netloc != "api-seller.ozon.ru":
        raise ApiPathDenied("Seller origin denied")
    if not re.fullmatch(r"/[A-Za-z0-9/_-]+", parsed.path) or url != SELLER + parsed.path:
        raise ApiPathDenied("Seller path syntax denied")
    if (method, parsed.path) not in SELLER_PROFILES[profile]:
        raise ApiPathDenied("Seller callable route denied")


class _NoSellerRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiPathDenied("Seller redirect denied")


def _seller_open(req, timeout):
    return urllib.request.build_opener(_NoSellerRedirect()).open(req, timeout=timeout)


def seller_call(path, body, *, method="POST", profile="runtime"):
    """Only credentialed Seller transport. No query/template/prefix authorization."""
    if not isinstance(path, str):
        raise ApiPathDenied("Seller path type denied")
    url = SELLER + path
    _validate_seller_route(url, method, profile)  # before credentials
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers=_seller_headers(), method=method)
    req._seller_profile = profile
    return _request(req)  # revalidates final URL/method/profile immediately before dispatch


def seller_post(path, body):
    profile = "control" if _seller_execution_scope == frozenset({"control"}) else "runtime"
    return seller_call(path, body, profile=profile)


def perf_token():
    """Эфемерный токен Performance API. Живёт 1800 с, обновляем каждые 25 минут."""
    if _perf_token["value"] and time.time() - _perf_token["at"] < 1500:
        return _perf_token["value"]
    body = json.dumps({"client_id": secret(CONFIG.secret_perf_client_id),
                       "client_secret": secret(CONFIG.secret_perf_client_secret),
                       "grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(PERF + "/api/client/token", data=body,
                                 headers={"Content-Type": "application/json"})
    code, d = _request(req)
    if code != 200:
        raise RuntimeError("не удалось получить токен Performance API")
    _perf_token["value"] = d["access_token"]
    # Токен — класс СЕКРЕТ. Старые токены из вырезания не удаляются: процесс мог
    # запомнить их в тексте ошибки до обновления.
    register_secret(_perf_token["value"], "performance_access_token")
    _perf_token["at"] = time.time()
    return _perf_token["value"]


def perf_get(path, raw_text=True):
    if not any(rx.match(path) for rx in PERF_ALLOWED_GET):
        raise ApiPathDenied(f"Performance API: GET {path.split('?')[0]} вне белого списка")
    req = urllib.request.Request(PERF + path,
                                 headers={"Authorization": f"Bearer {perf_token()}"})
    return _request(req, raw_text=raw_text)


def perf_post(path, body):
    if path not in PERF_ALLOWED_POST:
        raise ApiPathDenied(f"Performance API: POST {path} вне белого списка")
    req = urllib.request.Request(
        PERF + path, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {perf_token()}",
                 "Content-Type": "application/json"})
    return _request(req)


# ------------------------------------------------------- загрузка в BigQuery
# Страховка на случай, когда процесс убит между созданием staging и finally
# (таймаут Cloud Run, OOM): таблица исчезнет сама. Прогон длится минуты, сутки —
# с запасом. Датасетный TTL сознательно не трогаем: он действовал бы и на RAW.
STAGING_TTL = timedelta(hours=24)

# Режимы обработки строк источника с одинаковым ключом слияния.
#   collapse_identical — полностью одинаковые строки схлопываются, различающиеся
#                        роняют загрузку (по умолчанию);
#   reject             — любой повтор ключа роняет загрузку. Для финансов: две
#                        одинаковые строки там могут быть двумя реальными
#                        начислениями на одну сумму, схлопнуть их — потерять деньги.
DUPLICATE_KEY_MODES = ("collapse_identical", "reject")

# Тот же маркер NULL, что в условии ON оператора MERGE ниже.
_NULL_KEY = "\x00"


class MergeKeyConflictError(RuntimeError):
    """Несколько строк источника претендуют на один ключ слияния.

    Раньше такие строки молча схлопывались ROW_NUMBER() с недетерминированным
    выбором победителя. Теперь это явный отказ загрузки до создания staging.

    Сообщение уходит в лог и в OZON_INGESTION_RUNS.error_message, поэтому в нём
    нет ни значений полезной нагрузки, ни значений ключа (accrual_id, sku,
    posting_number…). Только таблица, имена колонок ключа, режим, число ключей и
    строк, имена расходящихся колонок и отпечаток ключа (merge_key_fingerprint).
    """


def merge_key_fingerprint(key):
    """Короткий детерминированный отпечаток нормализованного ключа слияния.

    SHA-256 от частей merge_key(), разделённых \\x1f; первые 12 hex-символов.
    Один и тот же ключ даёт один и тот же отпечаток в любом прогоне, поэтому по
    нему можно найти конфликт повторно, не записывая сами значения в лог.
    """
    return hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()[:12]


def _clean(v):
    # BigQuery NUMERIC принимает не более 9 знаков после запятой, а repr(float)
    # даёт артефакты вида 2000.3700000000001 — округляем до 4 знаков.
    return round(v, 4) if isinstance(v, float) else v


def _loaded_value(v):
    """Значение ровно в том виде, в каком оно уходит в staging."""
    return json.dumps(_clean(v), ensure_ascii=False, default=str, sort_keys=True)


def merge_key(row, keys):
    """Ключ слияния так, как его сравнивает MERGE: COALESCE(CAST(k AS STRING), '\\x00').

    Ключевые колонки Ozon — STRING, INT64 и DATE, в Python это str/int. Для них
    str() совпадает с CAST(... AS STRING), поэтому группировка здесь не мельче,
    чем в BigQuery: всё, что MERGE сочтёт одним ключом, здесь тоже один ключ.
    """
    return tuple(_NULL_KEY if row.get(k) is None else str(row.get(k)) for k in keys)


def validate_merge_batch(table, rows, keys, cols, on_duplicate_key="collapse_identical"):
    """Проверка партии ДО staging и MERGE. Возвращает число схлопнутых дублей.

    Сравниваются все сохраняемые колонки (`cols` — схема целевой таблицы).
    Технические extracted_at / ingestion_run_id / source_endpoint внутри одного
    вызова одинаковы, так что ложных конфликтов не дают, а схлопнутые строки
    побайтно равны по всему, что попадёт в таблицу.
    """
    if on_duplicate_key not in DUPLICATE_KEY_MODES:
        raise ValueError(f"неизвестный режим дублей: {on_duplicate_key!r}")
    groups = {}
    for r in rows:
        groups.setdefault(merge_key(r, keys), []).append(r)
    dups = {k: g for k, g in groups.items() if len(g) > 1}
    if not dups:
        return 0
    conflicts = []
    for k, g in dups.items():
        differing = [c for c in cols if len({_loaded_value(r.get(c)) for r in g}) > 1]
        if on_duplicate_key == "reject" or differing:
            conflicts.append((k, len(g), differing))
    if conflicts:
        shown = "; ".join(
            f"key_fp={merge_key_fingerprint(k)} rows={n} "
            f"differing_columns={','.join(d) or 'none (identical)'}"
            for k, n, d in conflicts[:5])
        raise MergeKeyConflictError(
            f"{table}: {len(conflicts)} ключ(ей) слияния ({','.join(keys)}) "
            f"с несколькими строками источника, строк {sum(n for _, n, _ in conflicts)}, "
            f"режим {on_duplicate_key}; первые: {shown}")
    return sum(len(g) - 1 for g in dups.values())


def _drop_staging(client, staging_id):
    """Удаление staging, которое никогда не бросает.

    Вызывается из finally: исключение отсюда заменило бы исходную ошибку
    загрузки. Сбой очистки логируется; остаток подчистит STAGING_TTL.
    """
    try:
        client.delete_table(staging_id, not_found_ok=True)
    except Exception as e:                                        # noqa: BLE001
        log(event="staging_cleanup_failed", staging=staging_id,
            error=f"{type(e).__name__}: {safe_error_text(e, 200)}",
            ttl_hours=STAGING_TTL.total_seconds() / 3600)


def merge_rows(table, rows, keys, run_id, on_duplicate_key="collapse_identical"):
    """Идемпотентная запись: проверка партии → staging → MERGE по ключу → удаление staging.

    Повторный прогон на том же окне не создаёт дублей и не удваивает суммы.
    Конфликт ключей роняет загрузку ДО создания staging (MergeKeyConflictError).
    Staging удаляется в finally при любом исходе; вторая линия — expires на самой
    staging-таблице.
    """
    if not rows:
        return {"received": 0, "inserted": 0, "updated": 0}
    client = bq()
    tgt = client.get_table(f"{PROJECT}.{DATASET}.{table}")
    cols = [f.name for f in tgt.schema]
    collapsed = validate_merge_batch(table, rows, keys, cols, on_duplicate_key)
    if collapsed:
        log(event="merge_identical_duplicates_collapsed", table=table, rows=collapsed)
    staging = f"_rt_{table}_{run_id.replace('-', '')[:10]}"
    staging_id = f"{PROJECT}.{DATASET}.{staging}"

    data = "\n".join(
        json.dumps({k: _clean(r.get(k)) for k in cols}, ensure_ascii=False, default=str)
        for r in rows).encode()
    on = " AND ".join(
        f"COALESCE(CAST(T.{k} AS STRING),'\\x00')=COALESCE(CAST(S.{k} AS STRING),'\\x00')"
        for k in keys)
    setter = ", ".join(f"T.{c}=S.{c}" for c in cols if c not in keys)
    # Текст MERGE не изменён. ROW_NUMBER остаётся: MERGE требует не более одной
    # строки источника на ключ. После validate_merge_batch строки одного ключа
    # равны по всем колонкам, поэтому выбор победителя ни на что не влияет.
    q = (f"MERGE `{PROJECT}.{DATASET}.{table}` T USING "
         f"(SELECT * EXCEPT(_rn) FROM (SELECT *, ROW_NUMBER() OVER "
         f"(PARTITION BY {','.join(keys)} ORDER BY extracted_at DESC) _rn "
         f"FROM `{PROJECT}.{DATASET}.{staging}`) WHERE _rn=1) S ON {on} "
         f"WHEN MATCHED THEN UPDATE SET {setter} "
         f"WHEN NOT MATCHED THEN INSERT ({','.join(cols)}) "
         f"VALUES ({','.join('S.'+c for c in cols)})")
    try:
        # Остаток с тем же именем (повтор с явным INGESTION_RUN_ID) убираем до
        # создания: иначе WRITE_APPEND дописал бы партию к чужим строкам.
        client.delete_table(staging_id, not_found_ok=True)
        table_obj = bigquery.Table(staging_id, schema=tgt.schema)
        table_obj.expires = datetime.now(timezone.utc) + STAGING_TTL
        client.create_table(table_obj)
        # Таблица только что создана пустой, поэтому APPEND эквивалентен прежнему
        # TRUNCATE. TRUNCATE не используем: сохраняет ли он expires, в документации
        # клиента не сказано.
        cfg = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            schema=tgt.schema, write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            create_disposition=bigquery.CreateDisposition.CREATE_NEVER)
        job = client.load_table_from_file(io.BytesIO(data), staging_id,
                                          job_config=cfg, location=LOCATION)
        job.result()
        if job.errors:
            raise RuntimeError(f"load job {job.job_id}: {job.errors}")

        before = list(client.query(f"SELECT COUNT(*) c FROM `{PROJECT}.{DATASET}.{table}`",
                                   location=LOCATION).result())[0]["c"]
        m = client.query(q, location=LOCATION)
        m.result()
        after = list(client.query(f"SELECT COUNT(*) c FROM `{PROJECT}.{DATASET}.{table}`",
                                  location=LOCATION).result())[0]["c"]
    finally:
        _drop_staging(client, staging_id)
    return {"received": len(rows), "inserted": after - before,
            "updated": len(rows) - (after - before)}


# ─────────────────────────── append-only наблюдения (PR-PROMO-1)
# merge_rows выше схлопывает строки по ключу, содержащему snapshot_date: для
# суточных сущностей это правильно, для наблюдателя акций — губительно. Состав
# акции и акционные цены меняются ВНУТРИ суток, а наблюдений четыре в день, и
# каждое обязано остаться отдельной строкой.
#
# Поэтому здесь отдельный путь записи: WRITE_APPEND с ДЕТЕРМИНИРОВАННЫМ job_id.
# BigQuery дедуплицирует load-джобы по job_id, поэтому повтор того же слота не
# создаёт вторую копию строк, а новый слот (другой job_id) пишется всегда.
# Ровно та же механика, что у наблюдателя цен WB (cloud/src/loaders/prices/bq.ts).

# Часы запуска наблюдателя акций, UTC. Совпадают с расписанием в Terraform и с
# PROMO_SLOT_HOURS_UTC наблюдателя WB: два маркетплейса наблюдаются в одних слотах,
# иначе сравнивать снимки между площадками пришлось бы с поправкой на время.
PROMO_SLOT_HOURS_UTC = (4, 9, 14, 19)


def promo_slot(now=None):
    """Слот наблюдения YYYY-MM-DDTHH:00 (UTC) — ближайший предшествующий запуск.

    До первого слота суток относится к последнему слоту предыдущих суток.
    Ретрай внутри слота получает тот же идентификатор и не задваивает историю.
    """
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    past = [h for h in sorted(PROMO_SLOT_HOURS_UTC) if h <= now.hour]
    if past:
        return f"{now:%Y-%m-%d}T{past[-1]:02d}:00"
    prev = now - timedelta(days=1)
    return f"{prev:%Y-%m-%d}T{max(PROMO_SLOT_HOURS_UTC):02d}:00"


def promo_observation_id(environment, slot):
    """Детерминированный id снимка. Стабилен между попытками одного слота."""
    return "OZPROMO_%s_%s" % (environment, slot.replace("-", "").replace(":", "").replace("T", ""))


def promo_load_job_id(environment, slot, table):
    """Детерминированный job_id load-джобы: ключ идемпотентности записи."""
    t = "".join(c if c.isalnum() else "_" for c in table.lower()).strip("_")
    return "ozpromo_%s_%s_%s" % (environment, slot.replace("-", "").replace(":", "").replace("T", ""), t)


def append_rows(table, rows, job_id):
    """Append-only запись снимка. Возвращает ('LOADED'|'REUSED', число строк).

    REUSED означает, что load-джоба с таким job_id уже выполнялась: строки на
    месте, повтор их не задвоил. Это НЕ ошибка и НЕ повод переписывать историю.
    """
    if not rows:
        return "LOADED", 0
    client = bq()
    tgt = client.get_table(f"{PROJECT}.{DATASET}.{table}")
    cols = [f.name for f in tgt.schema]
    data = "\n".join(
        json.dumps({k: _clean(r.get(k)) for k in cols}, ensure_ascii=False, default=str)
        for r in rows).encode()
    cfg = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=tgt.schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_NEVER)
    try:
        job = client.load_table_from_file(io.BytesIO(data), f"{PROJECT}.{DATASET}.{table}",
                                          job_config=cfg, location=LOCATION, job_id=job_id)
        job.result()
        if job.errors:
            raise RuntimeError(f"load job {job.job_id}: {job.errors}")
        return "LOADED", len(rows)
    except Exception as e:                                            # noqa: BLE001
        if "Already Exists" in str(e) or getattr(e, "code", None) == 409:
            log(event="promo_append_reused", table=table, job_id=job_id, rows=len(rows))
            return "REUSED", len(rows)
        raise


def record_run(run_id, entity, started, src_from, src_to, res, status,
               error=None, requests_n=0, retries=0):
    error_message = safe_error_text(error) if error else None
    row = {"ingestion_run_id": run_id, "marketplace": "OZON", "entity": entity,
           "started_at": started.isoformat(), "completed_at": now_msk().isoformat(),
           "source_from": str(src_from), "source_to": str(src_to),
           "requests": requests_n, "rows_received": res.get("received", 0),
           "rows_inserted": res.get("inserted", 0), "rows_updated": res.get("updated", 0),
           "errors": 0 if status == "OK" else 1, "retry_count": retries,
           "status": status, "error_message": error_message,
           "job_execution": os.environ.get("CLOUD_RUN_EXECUTION")}
    bq().insert_rows_json(f"{PROJECT}.{DATASET}.{RUNS_TABLE}", [row])
    log(event="entity_done", **{k: row[k] for k in
        ("entity", "status", "rows_received", "rows_inserted", "rows_updated",
         "source_from", "source_to", "requests", "retry_count")})
