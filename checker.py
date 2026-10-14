"""Один повний цикл перевірки: завантаження → правила → AI → звіт."""
import csv
import html
import io
import logging
import os
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime

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


def collect_issues(df: pd.DataFrame, state: State) -> tuple[list[Issue], str]:
    """Повертає (проблеми, підсумок про витрати AI для звіту)."""
    issues = rules.run_all(df, state.prices)

    active = df[~df["archived"]]
    rows = list(zip(active["code"], active["name"], active["group"]))
    current = {c: n for c, n, _ in rows}
    limit = int(os.getenv("AI_MAX_NAMES_PER_RUN", "3000"))
    todo = state.names_to_check(rows)[:limit]
    limit_usd = float(os.getenv("AI_MONTHLY_BUDGET_USD", "1.0"))
    budget = ai_check.Budget(limit_usd, state.ai_spent_this_month())
    new_ai, done, out_of_budget = ai_check.check_names(todo, budget)
    state.mark_checked(todo, done)
    state.remember_ai_issues([asdict(i) for i in new_ai])
    state.set_ai_spent(budget.spent)
    state.save_cache()  # оплачена робота не повинна пропасти, навіть у dry-run
    issues.extend(Issue(**d) for d in state.cached_ai_issues(current))

    footer = ""
    if os.getenv("ANTHROPIC_API_KEY"):
        footer = (f"🤖 Claude: цього запуску ≈${budget.used_now:.3f}, "
                  f"за місяць ≈${budget.spent:.3f} з ліміту ${limit_usd:.2f}")
        left = len(state.names_to_check(rows))
        if left:
            footer += (f"\n⏳ Ще не перевірено Claude: {left} назв"
                       + (" (ліміт вичерпано — продовжу з наступного місяця або після збільшення "
                          "AI_MONTHLY_BUDGET_USD)" if out_of_budget else " (продовжу наступного разу)"))
    return issues, footer


LINE = "━━━━━━━━━━━━━━━━━━━━"
TG_LIMIT = 3900  # одне повідомлення Telegram — до 4096 символів


def _example(i: Issue) -> str:
    detail = i.suggestion if i.suggestion else i.message
    return (f"• <code>{i.code}</code> {html.escape(i.name.strip()[:45])}\n"
            f"   ↳ <i>{html.escape(detail[:90])}</i>")


def format_report(issues: list[Issue], new_keys: set[str], total: int, footer: str = "",
                  when: datetime | None = None) -> str:
    """Один компактний звіт з чіткими межами. Кількість прикладів на категорію
    зменшується, доки все не влізе в одне повідомлення; повний список — у файлах."""
    when = when or datetime.now()
    new = [i for i in issues if i.key in new_keys]
    counts = Counter(i.severity for i in new)
    by_rule: dict[str, list[Issue]] = defaultdict(list)
    for i in new:
        by_rule[i.rule].append(i)
    ordered = sorted(by_rule.items(),
                     key=lambda kv: (min(SEV_ORDER[i.severity] for i in kv[1]), -len(kv[1])))

    def build(per_rule: int) -> str:
        lines = [
            LINE,
            f"🔍 <b>ЗВІТ ПРО ПОМИЛКИ В КАТАЛОЗІ</b>",
            f"📅 {when:%d.%m.%Y, %H:%M}",
            LINE,
            f"Позицій у файлі: <b>{total}</b>",
            f"Нових проблем: <b>{len(new)}</b>  (🔴 {counts[ERROR]} · 🟠 {counts[WARNING]} · ℹ️ {counts[INFO]})",
            f"Усього відкритих: {len(issues)}",
        ]
        for rule, items in ordered:
            sev = min(items, key=lambda i: SEV_ORDER[i.severity]).severity
            lines.append(f"\n{SEV_ICON[sev]} <b>{html.escape(RULE_TITLES.get(rule, rule))}</b> — {len(items)}")
            if sev == INFO or per_rule == 0:
                continue  # дрібниці й зайві приклади — лише у файлі
            lines.extend(_example(i) for i in items[:per_rule])
            if len(items) > per_rule:
                lines.append(f"   …і ще {len(items) - per_rule} — у файлі")
        if footer:
            lines.append("\n" + html.escape(footer))
        lines.append("\n📎 <b>Повний список — у файлах нижче</b>")
        lines.append(LINE)
        lines.append("<b>КІНЕЦЬ ЗВІТУ</b>")
        lines.append(LINE)
        return "\n".join(lines)

    for per_rule in (4, 3, 2, 1, 0):
        text = build(per_rule)
        if len(text) <= TG_LIMIT:
            break
    return text


def to_csv(issues: list[Issue], new_keys: set[str] | None = None) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Код", "Нова", "Важливість", "Правило", "Назва", "Проблема", "Пропозиція"])
    for i in sorted(issues, key=lambda i: (SEV_ORDER[i.severity], i.rule, i.code)):
        w.writerow([i.code, "так" if new_keys is None or i.key in new_keys else "ні", i.severity,
                    RULE_TITLES.get(i.rule, i.rule), i.name.strip(), i.message, i.suggestion])
    return buf.getvalue().encode("utf-8-sig")  # BOM, щоб Excel бачив кирилицю


def build_files(issues: list[Issue], new_keys: set[str], when: datetime | None = None):
    """Файли-вкладення: нові помилки та повний список відкритих."""
    day = (when or datetime.now()).strftime("%Y-%m-%d")
    new = [i for i in issues if i.key in new_keys]
    files = [(f"new_errors_{day}.csv", to_csv(new, new_keys), f"📎 Звіт від {day}: нові помилки ({len(new)})")]
    if len(new) != len(issues):
        files.append((f"all_open_{day}.csv", to_csv(issues, new_keys), f"Усі відкриті ({len(issues)})"))
    return files


def due(state: State, today: date | None = None) -> bool:
    """Чи пора запускати перевірку: минуло CHECK_EVERY_DAYS днів з останньої успішної."""
    every = int(os.getenv("CHECK_EVERY_DAYS", "2"))
    passed = state.days_since_last_run(today or date.today())
    return passed is None or passed >= every


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

    issues, footer = collect_issues(df, state)
    new_keys = state.new_issues([i.key for i in issues])
    n_new = sum(1 for i in issues if i.key in new_keys)
    log.info("Проблем: %d, нових: %d", len(issues), n_new)

    report = format_report(issues, new_keys, len(df), footer)
    if dry_run:
        print(html.unescape(report))
        return issues
    if n_new or os.getenv("NOTIFY_WHEN_CLEAN", "false").lower() == "true":
        sent = notifier.send_report(report, build_files(issues, new_keys))
        if not sent and notifier._configured():
            return issues  # не зберігаємо стан — повідомимо при наступному запуску
    prices = {int(r.code): float(r.price) for r in df[~df["archived"]].itertuples() if pd.notna(r.price)}
    state.save([i.key for i in issues], prices)
    return issues
