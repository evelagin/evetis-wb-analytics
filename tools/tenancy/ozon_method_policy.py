#!/usr/bin/env python3
"""Политика «только чтение» для ключа Seller API Ozon (Tenancy T5, решение D2).

  python tools/tenancy/ozon_method_policy.py build <seller_swagger.json> <spec_date>
  python tools/tenancy/ozon_method_policy.py check

Источник истины о возможностях ключа — фактический набор методов из POST /v1/roles, а не
название роли. Каждый метод спецификации получает класс:

  READ      — только чтение состояния;
  REPORT    — заказ асинхронного отчёта или файла: состояние кабинета не меняет, создаёт файл;
  MUTATION  — меняет состояние кабинета: товары, цены, остатки, отправления, акции, чаты…;
  UNKNOWN   — классифицировать нельзя (новый метод, противоречивые признаки).

Решение по ключу (pipelines/ozon/runtime/credentials.py): любой метод ключа класса MUTATION
вне явно одобренного набора (сейчас пуст) или UNKNOWN (в т. ч. метод, которого нет в политике)
— ОТКАЗ проверки учётных данных. Ключ не меняется и не ограничивается автоматически.

Классификация детерминирована правилами ниже + явными поправками OVERRIDES (каждая с причиной).
HTTP-метод признаком не является: у Ozon почти всё POST, а у Performance API есть GET, которые
меняют кабинет (/api/client/campaign/all_sku_promo/activate).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
POLICY_FILE = REPO / "pipelines" / "ozon" / "runtime" / "seller_method_policy.json"

# Глагол описания, меняющий состояние, — сильнейший признак.
MUTATION_SUMMARY = re.compile(
    r"^(Создать|Обновить|Изменить|Удалить|Добавить|Загрузить|Установить|Отменить|Подтвердить|"
    r"Отправить|Передать|Собрать|Разделить|Архивировать|Разархивировать|Вернуть|Активировать|"
    r"Деактивировать|Включить|Выключить|Отключить|Привязать|Отвязать|Согласовать|Отклонить|"
    r"Одобрить|Поставить|Снять|Отметить|Перенести|Сохранить|Заменить|Указать|Назначить|Закрыть|"
    r"Открыть|Начать|Завершить|Редактировать|Удаление|Создание|Обновление|Изменение|Загрузка|"
    r"Отмена|Подтверждение|Передача|Сборка|Добавление|Упаковать|Пометить|Прикрепить|Открепить|"
    r"Задать|Применить|Сбросить|Скопировать|Перевести|Запустить|Остановить|Возобновить|"
    r"Приостановить|Сгенерировать штрихкод|Создать штрихкод|Проверить URL|Отрисовать)", re.I)
# Заказ отчёта/файла: создаёт файл, кабинет не меняет.
REPORT_PATH = re.compile(r"/report/.+/create$|/report/create$|/report/[^/]+/create$")
READ_SUMMARY = re.compile(
    r"^(Получить|Список|Информация|Узнать|Справочник|Дерево|Поиск|Количество|Статус|Лимиты|"
    r"История|Отч[её]т|Аналитика|Цена|Загруженность|Интервалы|Состав|Чек-лист|Реестр|Таможенные|"
    r"Оборачиваемость|Управление остатками|Возможные|Товары с|Финансовый|Позаказный|Проверить "
    r"доступность|Данные|Посмотреть|Рейтинг|Характеристики|Описание|Атрибуты|Остатки|Контент|"
    r"Лимит|Товары|Отзывы|Вопросы|Сведения)", re.I)
MUTATION_PATH = re.compile(
    r"(create|update|delete|import|/set$|/set/|set-|/add|remove|activate|deactivate|cancel|ship|"
    r"archive|unarchive|send|upload|approve|decline|move|confirm|edit|/start|/stop|split|pack|"
    r"apply|/bind|unbind|pause|unpause|/read$|generate|/reset|/change|/accept|/reject|restore)", re.I)
GETTER_TAIL = re.compile(r"/(status|info|list|get|check-status|summary|counter|history|tree|values|"
                         r"search|types|timetable|by-day|count|top-sku|json|file/\{[^}]+\})$")

# Явные поправки: путь → (класс, причина). Только там, где правила ошибаются.
OVERRIDES: dict[str, tuple[str, str]] = {
    "/v6/fbs/posting/product/exemplar/create-or-get": ("MUTATION", "создаёт экземпляры, если их нет"),
    "/v2/chat/read": ("MUTATION", "отмечает сообщения прочитанными"),
    "/v1/pricing-strategy/status": ("MUTATION", "«Изменить статус стратегии»"),
    "/v1/warehouse/rfbs/pause": ("MUTATION", "ставит склад на паузу"),
    "/v1/warehouse/rfbs/unpause": ("MUTATION", "снимает склад с паузы"),
    "/v1/notification/check": ("MUTATION", "отправляет тестовое уведомление на внешний URL"),
    "/v1/barcode/generate": ("MUTATION", "создаёт штрихкоды"),
    "/v2/products/stocks": ("MUTATION", "«Обновить количество товаров на складах»"),
    "/v1/roles": ("READ", "инспекция самого ключа"),
    "/v1/delivery/map": ("READ", "отрисовка точек на карте — только чтение справочника"),
    "/v1/delivery/check": ("READ", "проверка доступности доставки — без изменения кабинета"),
    # Разбор UNKNOWN спецификации 2026-09-28 (17 методов).
    "/v1/carriage/container/fill": ("MUTATION", "заполняет грузоместо"),
    "/v1/carriage/container/place-into": ("MUTATION", "кладёт в грузоместо"),
    "/v1/fbp/draft/direct/product/validate": ("READ", "проверка товаров черновика без записи"),
    "/v1/fbp/draft/drop-off/product/validate": ("READ", "проверка товаров черновика без записи"),
    "/v1/fbp/draft/pick-up/product/validate": ("READ", "проверка товаров черновика без записи"),
    "/v5/fbs/posting/product/exemplar/validate": ("READ", "проверка кодов маркировки без записи"),
    "/v1/posting/fbs/pick-up-code/verify": ("MUTATION", "подтверждение кода курьера — консервативно"),
    "/v1/return/giveout/barcode": ("READ", "получение штрихкода возвратной отгрузки"),
    "/v1/return/giveout/barcode-reset": ("MUTATION", "сбрасывает штрихкод"),
    "/v1/return/giveout/get-pdf": ("READ", "файл возвратной отгрузки"),
    "/v1/return/giveout/get-png": ("READ", "файл возвратной отгрузки"),
    "/v1/return/giveout/is-enabled": ("READ", "признак включения"),
    "/v1/warehouse/fbs/return-mile/check": ("READ", "проверка возвратной мили без записи"),
    "/v2/posting/fbs/act/get-barcode": ("READ", "штрихкод акта"),
    "/v2/posting/fbs/act/get-barcode/text": ("READ", "штрихкод акта текстом"),
    "/v2/posting/fbs/act/get-container-labels": ("READ", "этикетки грузомест"),
    "/v3/finance/transaction/totals": ("READ", "итоги транзакций"),
    # Ложные MUTATION по слову cancel в пути: это справочники и статусы.
    "/v1/cancel-reason/list": ("READ", "справочник причин отмены"),
    "/v1/posting/fbo/cancel-reason/list": ("READ", "справочник причин отмены FBO"),
    "/v2/posting/fbs/cancel-reason/list": ("READ", "справочник причин отмены FBS"),
    "/v1/posting/cancel/status": ("READ", "статус отмены — чтение"),
}


def classify(path: str, summary: str) -> tuple[str, str]:
    if path in OVERRIDES:
        return OVERRIDES[path]
    s = (summary or "").strip()
    if REPORT_PATH.search(path):
        return "REPORT", "заказ отчёта"
    if MUTATION_SUMMARY.search(s):
        return "MUTATION", "глагол изменения в описании"
    if GETTER_TAIL.search(path) and READ_SUMMARY.search(s):
        return "READ", "геттер с глаголом чтения"
    if MUTATION_PATH.search(path):
        return "MUTATION", "признак изменения в пути"
    if READ_SUMMARY.search(s):
        return "READ", "глагол чтения в описании"
    return "UNKNOWN", "нет однозначного признака"


def build(spec_path: Path, spec_date: str) -> dict:
    raw = spec_path.read_bytes()
    spec = _parse(raw)
    methods = {}
    for p, it in sorted(spec["paths"].items()):
        for m, op in sorted(it.items()):
            if m not in ("get", "post", "put", "delete", "patch"):
                continue
            cls, why = classify(p, op.get("summary") or "")
            methods[p] = {"class": cls, "reason": why, "http": m.upper(),
                          "deprecated": bool(op.get("deprecated"))}
    return {"schema_version": 1, "api": "ozon_seller",
            "spec_date": spec_date, "spec_sha256": hashlib.sha256(raw).hexdigest(),
            "approved_mutation_methods": [],
            "methods": methods}


def _parse(text):
    # Единый строгий разборщик: повтор ключа в политике мог бы спрятать метод изменения.
    from tools.tenancy.validation import parse_tenant_json
    return parse_tenant_json(text.decode("utf-8") if isinstance(text, bytes) else text)


def load_policy(path: Path = POLICY_FILE) -> dict:
    return _parse(path.read_text(encoding="utf-8"))


def check(policy: dict) -> list[str]:
    """Инварианты политики: всё классифицировано, опасные известные методы — MUTATION."""
    out = []
    m = policy["methods"]
    for p, v in m.items():
        if v["class"] not in ("READ", "REPORT", "MUTATION", "UNKNOWN"):
            out.append(f"{p}: неизвестный класс {v['class']}")
    for p in ("/v1/product/import/prices", "/v2/products/stocks", "/v3/product/import", "/v2/chat/read",
              "/v1/actions/products/activate", "/v2/posting/fbo/cancel" if "/v2/posting/fbo/cancel" in m else None):
        if p and p in m and m[p]["class"] != "MUTATION":
            out.append(f"{p}: обязан быть MUTATION")
    if policy.get("approved_mutation_methods"):
        out.append("approved_mutation_methods непуст — нужно отдельное решение владельца")
    return out


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "build":
        policy = build(Path(argv[1]), argv[2])
        POLICY_FILE.write_text(json.dumps(policy, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                               encoding="utf-8")
        counts = {}
        for v in policy["methods"].values():
            counts[v["class"]] = counts.get(v["class"], 0) + 1
        print(f"политика: {len(policy['methods'])} методов {counts}, spec sha256 {policy['spec_sha256'][:12]}")
        return 0
    if argv == ["check"]:
        f = check(load_policy())
        for x in f:
            print(f"FAIL {x}")
        return 1 if f else 0
    print(__doc__, file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
