"""Один повний цикл перевірки: завантаження → правила → AI → звіт."""
import csv
import html
import io
import logging
import os
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime

import pandas as pd

import ai_check
import loader
import notifier
import rules
from rules import ERROR, INFO, WARNING, Issue
from state import State

log = logging.getLogger(__name__)

DEFAULT_URL = "https://www.dilovakovbasa.ua/upload/category/small/prom_export.xlsx"
PER_RULE_LIMIT = 10

RULE_TITLES = {
    "mixed_alphabet": "Змішані кирилиця/латиниця в слові",
    "descriptor_as_brand": "Вік/тип продукту замість виробника (як «Young»)",
    "ai_review": "Знайдено Claude (орфографія, сенс)",
    "quotes_brackets": "Зламані лапки/дужки",
    "short_name": "Занадто коротка назва",
    "missing_price": "Немає ціни",
    "duplicate_code": "Дубль коду товару",
    "duplicate_name": "Дубль назви",
    "price_outlier": "Підозріла ціна",
    "price_jump": "Різка зміна ціни",
    "percent": "Некоректний відсоток",
    "archived_in_stock": "Архівний товар у наявності",
    "missing_image": "Немає фото",
    "whitespace": "Зайві пробіли",
    "glued_words": "Пропущені пробіли",
}
SEV_ICON = {ERROR: "🔴", WARNING: "🟠", INFO: "ℹ️"}
SEV_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


def collect_issues(df: pd.DataFrame, state: State) -> list[Issue]:
    issues = rules.run_all(df, state.prices)

    active = df[~df["archived"]]
    rows = list(zip(active["code"], active["name"], active["group"]))
    current = {c: n for c, n, _ in rows}
    limit = int(os.getenv("AI_MAX_NAMES_PER_RUN", "3000"))
    todo = state.names_to_check(rows)[:limit]
    new_ai, done = ai_check.check_names(todo)
    state.mark_checked(todo, done)
    state.remember_ai_issues([asdict(i) for i in new_ai])
    issues.extend(Issue(**d) for d in state.cached_ai_issues(current))
    return issues


def format_report(issues: list[Issue], new_keys: set[str], total: int) -> str:
    new = [i for i in issues if i.key in new_keys]
    counts = Counter(i.severity for i in new)
    lines = [
        f"<b>🔍 Перевірка каталогу Prom</b> · {datetime.now():%d.%m.%Y %H:%M}",
        f"Позицій: {total} · нових проблем: <b>{len(new)}</b> "
        f"(🔴 {counts[ERROR]} · 🟠 {counts[WARNING]} · ℹ️ {counts[INFO]})",
        f"Усього відкритих проблем: {len(issues)}",
    ]
    by_rule: dict[str, list[Issue]] = defaultdict(list)
    for i in new:
        by_rule[i.rule].append(i)
    ordered = sorted(by_rule.items(), key=lambda kv: (min(SEV_ORDER[i.severity] for i in kv[1]), -len(kv[1])))
    for rule, items in ordered:
        sev = min(items, key=lambda i: SEV_ORDER[i.severity]).severity
        lines.append(f"\n<b>{SEV_ICON[sev]} {html.escape(RULE_TITLES.get(rule, rule))}</b> — {len(items)}")
        if sev == INFO:
            continue  # дрібниці — тільки лічильник і файл
        for i in items[:PER_RULE_LIMIT]:
            tail = f" → <i>{html.escape(i.suggestion)}</i>" if i.suggestion else ""
            lines.append(f"• <code>{i.code}</code> {html.escape(i.name.strip()[:80])}\n   {html.escape(i.message)}{tail}")
        if len(items) > PER_RULE_LIMIT:
            lines.append(f"…і ще {len(items) - PER_RULE_LIMIT} (див. файл)")
    return "\n".join(lines)


def to_csv(issues: list[Issue]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Код", "Важливість", "Правило", "Назва", "Проблема", "Пропозиція"])
    for i in sorted(issues, key=lambda i: (SEV_ORDER[i.severity], i.rule, i.code)):
        w.writerow([i.code, i.severity, RULE_TITLES.get(i.rule, i.rule), i.name.strip(), i.message, i.suggestion])
    return buf.getvalue().encode("utf-8-sig")  # BOM, щоб Excel бачив кирилицю


def run_check(source: str | None = None, dry_run: bool = False) -> list[Issue]:
    """source — URL або шлях до локального xlsx. dry_run: нічого не шле і не зберігає."""
    state = State(os.getenv("STATE_PATH", "state.json"))
    try:
        if source and os.path.exists(source):
            df = loader.read_catalog(open(source, "rb").read())
        else:
            df = loader.load_catalog(source or os.getenv("PROM_EXPORT_URL", DEFAULT_URL))
    except Exception as e:
        log.exception("Не вдалось завантажити каталог")
        if not dry_run:
            notifier.send_alert(f"Не вдалось завантажити/прочитати prom_export.xlsx: {e}")
        return []

    issues = collect_issues(df, state)
    new_keys = state.new_issues([i.key for i in issues])
    n_new = sum(1 for i in issues if i.key in new_keys)
    log.info("Проблем: %d, нових: %d", len(issues), n_new)

    report = format_report(issues, new_keys, len(df))
    if dry_run:
        print(html.unescape(report))
        return issues
    if n_new or os.getenv("NOTIFY_WHEN_CLEAN", "false").lower() == "true":
        sent = notifier.send_report(report, to_csv(issues),
                                    f"errors_{datetime.now():%Y%m%d_%H%M}.csv")
        if not sent and notifier._configured():
            return issues  # не зберігаємо стан — повідомимо при наступному запуску
    prices = {int(r.code): float(r.price) for r in df[~df["archived"]].itertuples() if pd.notna(r.price)}
    state.save([i.key for i in issues], prices)
    return issues
