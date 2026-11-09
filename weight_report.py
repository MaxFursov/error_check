"""Звіт «позиції без середньої ваги» (колонка «Вага» в експорті Проми).
Надсилається раз на WEIGHT_REPORT_EVERY_DAYS днів окремим повідомленням + файл CSV."""
import csv
import html
import io
import logging
import os
from collections import Counter
from datetime import date, datetime

import pandas as pd

import loader
import notifier
from checker import DEFAULT_URL, LINE
from state import State

log = logging.getLogger(__name__)
TG_LIMIT = 3900


def find_missing(df: pd.DataFrame) -> pd.DataFrame:
    """Активні (не виведені) позиції, де вага не вказана або не додатна."""
    act = df[~df["archived"]]
    miss = act[act["weight"].isna() | (act["weight"] <= 0)].copy()
    miss["in_stock"] = miss["available"] == "+"
    ident = miss["uid"].where(miss["uid"].notna(), miss["code"])
    miss["ident"] = ident.astype(int)
    return miss.sort_values(["in_stock", "group", "name"], ascending=[False, True, True])


def format_report(missing: pd.DataFrame, total_active: int, prev_ids: set[int] | None = None,
                  when: datetime | None = None) -> str:
    when = when or datetime.now()
    n = len(missing)
    stock = int(missing["in_stock"].sum())
    ids = set(missing["ident"])
    lines_head = [
        LINE, "⚖️ <b>ПОЗИЦІЇ БЕЗ СЕРЕДНЬОЇ ВАГИ</b>", f"📅 {when:%d.%m.%Y, %H:%M}", LINE,
        f"Активних позицій: <b>{total_active}</b>",
        f"Без ваги: <b>{n}</b> ({n / total_active:.1%})" if total_active else f"Без ваги: <b>{n}</b>",
        f"   • у наявності: <b>{stock}</b>",
        f"   • немає в наявності: {n - stock}",
        f"   • у кг: {int((missing['unit'] == 'кг').sum())} · у шт: {int((missing['unit'] == 'шт').sum())}",
    ]
    if prev_ids is not None:
        lines_head.append(f"З минулого звіту: ➕ {len(ids - prev_ids)} нових · ✅ {len(prev_ids - ids)} виправлено")

    groups = Counter(g.strip() for g in missing["group"])
    group_lines = ["\n<b>Найбільше без ваги — за групами:</b>"] + [
        f"• {html.escape(g[:40])} — {c}" for g, c in groups.most_common(8)]

    ex = missing[missing["in_stock"]]

    def build(k: int) -> str:
        lines = list(lines_head) + group_lines
        if k and len(ex):
            lines.append("\n<b>У наявності, приклади:</b>")
            for r in ex.head(k).itertuples():
                lines.append(f"• <code>{r.ident}</code> {html.escape(r.name.strip()[:55])}")
            if len(ex) > k:
                lines.append(f"   …і ще {len(ex) - k} — у файлі")
        lines += ["\n📎 <b>Повний список — у файлі нижче</b>", LINE, "<b>КІНЕЦЬ ЗВІТУ</b>", LINE]
        return "\n".join(lines)

    for k in (15, 10, 5, 0):
        text = build(k)
        if len(text) <= TG_LIMIT:
            break
    return text


def build_file(missing: pd.DataFrame, when: datetime | None = None) -> tuple[str, bytes, str]:
    day = (when or datetime.now()).strftime("%Y-%m-%d")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Назва", "Група", "Одиниця", "В наявності", "Код_товару"])
    for r in missing.itertuples():
        w.writerow([r.ident, r.name.strip(), r.group.strip(), r.unit,
                    "так" if r.in_stock else "ні", r.code])
    return (f"no_weight_{day}.csv", buf.getvalue().encode("utf-8-sig"),
            f"📎 Позиції без середньої ваги: {len(missing)}")


def due(state: State, today: date | None = None) -> bool:
    every = int(os.getenv("WEIGHT_REPORT_EVERY_DAYS", "3"))
    passed = state.weight_days_since(today or date.today())
    return passed is None or passed >= every


def run_weight_report(source: str | None = None, dry_run: bool = False) -> bool:
    """source — URL або локальний xlsx. dry_run: лише показати, нічого не слати й не зберігати."""
    state = State(os.getenv("STATE_PATH", "state.json"))
    try:
        if source and os.path.exists(source):
            df = loader.read_catalog(open(source, "rb").read())
        else:
            df = loader.load_catalog(source or os.getenv("PROM_EXPORT_URL", DEFAULT_URL))
    except Exception as e:
        log.exception("Не вдалось завантажити каталог для звіту про вагу")
        if not dry_run:
            notifier.send_alert(f"Звіт про вагу: не вдалось завантажити prom_export.xlsx: {e}")
        return False

    if not df.attrs.get("has_weight_column", False):
        msg = "У prom_export.xlsx немає колонки «Вага» — звіт про середню вагу неможливий."
        log.error(msg)
        if not dry_run:
            notifier.send_alert(msg)
        return False

    missing = find_missing(df)
    total = int((~df["archived"]).sum())
    prev = set(state.data.get("weight_prev") or []) if state.data.get("weight_last_run") else None
    text = format_report(missing, total, prev)
    log.info("Без ваги: %d з %d активних", len(missing), total)
    if dry_run:
        print(html.unescape(text))
        return True
    if not notifier.send_report(text, [build_file(missing)]):
        return False   # не зберігаємо — повторимо при наступному запуску
    state.mark_weight_sent(list(missing["ident"]))
    return True
