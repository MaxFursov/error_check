"""Чернетки описів (колонка «Опис_h2_uk») для товарів, у яких її ще не заповнено.

Прома не чіпаємо: експорт перезаписується з 1С. Бот читає експорт, генерує чернетки, зберігає їх
у власному файлі (DESC_STATE_PATH) і надсилає в Telegram окремим xlsx.

Claude бачить лише назву, групу й вагу, тому пише тільки те, що з них випливає: заголовок у стилі
наявних описів і 2–3 нейтральні речення. Склад, калорійність, умови зберігання, ТУ — дані з етикетки
виробника, їх вигадувати не можна: для них у файлі окрема колонка з порожнім шаблоном."""
import html
import io
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

import ai_check
import loader
import notifier
from state import name_hash

log = logging.getLogger(__name__)

BATCH_SIZE = 10
MAX_OUTPUT_TOKENS = 2500
DEFAULT_MODEL = "claude-sonnet-5-5"   # текст бачать клієнти — беремо сильнішу модель, ніж для перевірки назв
MAX_ATTEMPTS = 2                      # скільки разів пробуємо товар, чия відповідь не пройшла перевірку

LABEL_BLOCK = (
    "<p><strong>Склад:</strong> </p>\n\n"
    "<p><strong>Поживна цінність на 100 г продукту:</strong> </p>\n\n"
    "<p><strong>Енергетична цінність (калорійність):</strong> </p>\n\n"
    "<p><strong>Умови зберігання:</strong> </p>")

SYSTEM = f"""Ти — копірайтер каталогу гуртового постачальника харчових продуктів.
Для кожного товару з нумерованого списку напиши ЗАГОЛОВОК і КОРОТКИЙ ОПИС для картки товару.
Клієнти — магазини, ринки, HoReCa: тон діловий, спокійний, без реклами.

{ai_check.CONTEXT}

ЗАГОЛОВОК — назва продукту ВЕЛИКИМИ літерами в лапках-«ялинках», скорочення розшифровані
(с/к → СИРОКОПЧЕНА, в/к → ВАРЕНО-КОПЧЕНА, н/к → НАПІВКОПЧЕНА, с/в → СИРОВ'ЯЛЕНА, в/г → ВИЩОГО СОРТУ).
Без виробника/ТМ, ваги й фасування. Приклади «назва → заголовок» з нашого каталогу дано в запиті.

ОПИС — 2–3 речення суцільним текстом. Використовуй ЛИШЕ факти з назви, групи й ваги: вид продукту,
спосіб обробки (з розшифрованих скорочень), жирність, вагу/фасування, торгову марку чи виробника.
Якщо про товар майже нічого сказати — достатньо одного речення.

СУВОРО ЗАБОРОНЕНО (дані з етикетки, яких ти не знаєш):
- склад, інгредієнти, добавки, консерванти, алергени;
- калорійність, білки/жири/вуглеводи, будь-які числа, яких немає в назві чи вазі;
- термін і умови зберігання, ТУ/ДСТУ, країна походження (якщо її немає в назві);
- «натуральний», «без ГМО», «без консервантів», смак, аромат, рецептура, нагороди, «найкращий»;
- виробника, якого немає в назві.
Не вигадуй. Краще коротко, ніж неправда.

ФОРМАТ ВІДПОВІДІ — суворо один рядок на товар, без markdown і вступів:
код | ЗАГОЛОВОК | текст опису (в одному рядку, без символу «|»)
Відповідай по кожному товару зі списку, у тому самому порядку."""

TAG_RE = re.compile(r"<[^>]+>")
LINE_RE = re.compile(r"^\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*$")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
# слова, які означають, що модель полізла в дані з етикетки або в рекламу
FORBIDDEN_RE = re.compile(
    r"\bсклад(?:у|і|ом|ники|ник)?\b|ккал|кдж|калорій|\bгмо\b|консервант|дсту|\bту\b|алерген|"
    r"термін\w* (?:придатності|зберігання)|умов\w* зберігання|натуральн|без добав|найкращ", re.I)


# ---------- зразки стилю з наявних описів ----------

def heading_of(desc_html: str) -> str | None:
    """Перший рядок наявного опису (заголовок): текст до першого <br> / </p>."""
    first = re.split(r"<br|</p>", desc_html.strip(), maxsplit=1)[0]
    text = re.sub(r"\s+", " ", html.unescape(TAG_RE.sub("", first))).strip()
    if not text or len(text) > 90 or re.match(r"склад", text, re.I) or re.search(r"\bТУ\b", text):
        return None
    return text


# Еталонні зразки: у каталозі майже немає сирів, а наявні заголовки неоднорідні.
STATIC_EXAMPLES = [
    ("", 'Ковбаса "Імператорська" с/к в/г Рикун', "КОВБАСА «ІМПЕРАТОРСЬКА» СИРОКОПЧЕНА ВИЩОГО СОРТУ"),
    ("", 'Ковбаса "Сервелат" в/к в/г (накат) Богодухівський МК', "КОВБАСА «СЕРВЕЛАТ» ВАРЕНО-КОПЧЕНА ВИЩОГО СОРТУ"),
    ("", 'Сир "MOZZARELLA" м\'який з в\'яленими томатами 45% 165 г/шт LYUBAR Milk', "СИР «MOZZARELLA» М'ЯКИЙ З В'ЯЛЕНИМИ ТОМАТАМИ"),
    ("", 'Сир "Сметанковий" твердий 50% брус 1,9 кг', "СИР «СМЕТАНКОВИЙ» ТВЕРДИЙ"),
    ("", 'Снеки м\'ясні "DELICATE" з відбірною курятиною с/к ЇSTA', "СНЕКИ М'ЯСНІ «DELICATE» З ВІДБІРНОЮ КУРЯТИНОЮ СИРОКОПЧЕНІ"),
]
QUOTED_RE = re.compile(r'["«“](.+?)["»”]')


def _mostly_upper(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) >= 0.7


def style_examples(df: pd.DataFrame) -> list[tuple[str, str, str]]:
    """[(група, назва, заголовок)] із товарів, у яких опис уже є, + еталонні зразки. Із каталогу беремо лише
    заголовки ВЕЛИКИМИ літерами, відмінні від назви й такі, що містять назву продукту в лапках:
    у каталозі трапляються копії назви, обрізані й надто загальні заголовки — це поганий зразок."""
    out = []
    for r in df[(df["desc"].str.strip() != "") & ~df["archived"]].itertuples():
        h = heading_of(r.desc)
        if not h or not _mostly_upper(h) or h.casefold() == r.name.strip().casefold():
            continue
        quoted = QUOTED_RE.search(r.name)
        if quoted and quoted.group(1).casefold() not in h.casefold():
            continue
        out.append((r.group, r.name.strip(), h))
    return out + STATIC_EXAMPLES


def pick_examples(batch_groups: list[str], batch_names: list[str], pool, limit: int = 8):
    """Для порції беремо зразки з тих самих груп і з тим самим першим словом назви (Сир/Ковбаса/…)."""
    firsts = {n.split()[0].casefold() for n in batch_names if n.split()}
    groups = set(batch_groups)
    scored = sorted(pool, key=lambda e: (e[0] not in groups, e[1].split()[0].casefold() not in firsts))
    return scored[:limit]


# ---------- перевірка відповіді моделі ----------

def _numbers(text: str) -> set[str]:
    """Числа з тексту в одному вигляді: «1,50» і «1.5» — те саме."""
    out = set()
    for n in NUM_RE.findall(text):
        n = n.replace(",", ".")
        out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return out


def reject_reason(name: str, weight, heading: str, text: str) -> str | None:
    """Чому відповідь не можна віддавати людині як є (None — все гаразд)."""
    both = f"{heading} {text}"
    m = FORBIDDEN_RE.search(both)
    if m and not FORBIDDEN_RE.search(name):
        return f"заборонене «{m.group(0)}»"
    known = _numbers(name)
    if weight is not None and not pd.isna(weight):
        known |= _numbers(str(int(weight)))
    extra = _numbers(both) - known
    if extra:
        return f"числа поза назвою: {', '.join(sorted(extra))}"
    if len(text) < 20 or len(text) > 700:
        return "дивна довжина опису"
    return None


def to_html(heading: str, text: str) -> str:
    return (f"<p><strong>{html.escape(heading, quote=False)}</strong></p>\n\n"
            f"<p>{html.escape(text, quote=False)}</p>")


# ---------- сховище ----------

class DescStore:
    """Згенеровані чернетки: {код: {...}}. Живе поруч зі state.json (на Volume)."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.data = {"items": {}, "failed": {}, "spend": {"month": "", "usd": 0.0}}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass

    def spent_this_month(self, today: date | None = None) -> float:
        month = (today or date.today()).strftime("%Y-%m")
        sp = self.data["spend"]
        return sp["usd"] if sp["month"] == month else 0.0

    def set_spent(self, usd: float, today: date | None = None) -> None:
        self.data["spend"] = {"month": (today or date.today()).strftime("%Y-%m"), "usd": round(usd, 6)}

    def has_fresh(self, code: int, name: str) -> bool:
        it = self.data["items"].get(str(code))
        return bool(it) and it["name_hash"] == name_hash(name)

    def attempts(self, code: int, name: str) -> int:
        f = self.data["failed"].get(str(code))
        return f["n"] if f and f["name_hash"] == name_hash(name) else 0

    def add(self, code: int, uid, name: str, group: str, available: str, html_text: str) -> None:
        self.data["items"][str(code)] = {
            "uid": None if uid is None or pd.isna(uid) else int(uid), "name": name, "group": group,
            "available": available, "name_hash": name_hash(name), "html": html_text,
            "generated": date.today().isoformat(), "sent": False}
        self.data["failed"].pop(str(code), None)

    def fail(self, code: int, name: str) -> None:
        self.data["failed"][str(code)] = {"name_hash": name_hash(name), "n": self.attempts(code, name) + 1}

    def unsent(self) -> dict[int, dict]:
        return {int(c): it for c, it in self.data["items"].items() if not it["sent"]}

    def mark_sent(self, codes) -> None:
        for c in codes:
            self.data["items"][str(c)]["sent"] = True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


def store_path() -> str:
    explicit = os.getenv("DESC_STATE_PATH")
    return explicit or str(Path(os.getenv("STATE_PATH", "state.json")).with_name("descriptions.json"))


# ---------- відбір товарів ----------

def pick_targets(df: pd.DataFrame, store: DescStore, limit: int) -> tuple[pd.DataFrame, int]:
    """Товари без опису (не в архіві), яких ще немає в сховищі. Спершу ті, що в наявності.
    Повертає (порція, скільки всього залишилось)."""
    todo = df[~df["archived"] & (df["desc"].str.strip() == "")]
    if os.getenv("DESC_ONLY_IN_STOCK", "true").lower() == "true":
        todo = todo[todo["available"].astype(str).str.strip() == "+"]
    todo = todo[[not store.has_fresh(int(r.code), r.name) and store.attempts(int(r.code), r.name) < MAX_ATTEMPTS
                 for r in todo.itertuples()]]
    todo = todo.assign(_stock=(todo["available"].astype(str).str.strip() != "+")) \
               .sort_values(["_stock", "group", "code"])
    return todo.head(limit), len(todo)


# ---------- виклик Claude ----------

@dataclass
class Batch:
    ok: dict          # код -> (заголовок, текст)
    rejected: dict    # код -> причина
    in_tok: int
    out_tok: int


def build_prompt(items, examples) -> str:
    ex = "\n".join(f"{n}  →  {h}" for _, n, h in examples)
    listing = "\n".join(
        f"{r.code} | {r.name.strip()} | група: {r.group.strip()}"
        + (f" | вага: {int(r.weight)} г" if not pd.isna(r.weight) else "") for r in items)
    return f"ПРИКЛАДИ «назва → заголовок»:\n{ex}\n\nТОВАРИ:\n{listing}"


def generate_batch(client, model: str, items: list, examples) -> Batch:
    resp = client.messages.create(
        model=model, max_tokens=MAX_OUTPUT_TOKENS, system=SYSTEM,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": build_prompt(items, examples)}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    parsed = [(int(m.group(1)), m.group(2), m.group(3))
              for m in map(LINE_RE.match, text.splitlines()) if m]
    if getattr(resp, "stop_reason", None) == "max_tokens" and parsed:
        parsed = parsed[:-1]              # останній рядок міг обірватись
    by_code = {int(r.code): r for r in items}
    ok, rejected = {}, {}
    for code, heading, body in parsed:
        r = by_code.get(code)
        if r is None or code in ok or code in rejected:
            continue                      # вигаданий або повторений код
        why = reject_reason(r.name, r.weight, heading, body)
        if why:
            rejected[code] = why
        else:
            ok[code] = (heading, body)
    usage = getattr(resp, "usage", None)
    return Batch(ok, rejected, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)


def generate(items: pd.DataFrame, pool, budget: ai_check.Budget, store: DescStore | None):
    """Повертає ({код: html}, кількість відхилених, чи зупинились через бюджет)."""
    if items.empty:
        return {}, 0, False
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.info("ANTHROPIC_API_KEY не задано — генерацію описів пропущено")
        return {}, 0, False
    model = os.getenv("DESC_MODEL", DEFAULT_MODEL)
    client = ai_check._client()
    rows = list(items.itertuples())
    made: dict[int, str] = {}
    rejected = 0
    out_of_budget = False
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        examples = pick_examples([r.group for r in batch], [r.name for r in batch], pool)
        chars = len(SYSTEM) + len(build_prompt(batch, examples))
        if not budget.can_afford(chars):
            out_of_budget = True
            log.warning("Описи: ліміт $%.2f майже вичерпано (витрачено $%.4f) — зупиняюсь", budget.limit, budget.spent)
            break
        try:
            res = generate_batch(client, model, batch, examples)
        except Exception as e:             # мережа, ліміти, модель недоступна
            log.error("Описи: порція %d–%d не вдалась: %s", i, i + len(batch), e)
            continue
        budget.add(res.in_tok, res.out_tok)
        by_code = {int(r.code): r for r in batch}
        for code, (heading, body) in res.ok.items():
            made[code] = to_html(heading, body)
            if store:
                r = by_code[code]
                store.add(code, r.uid, r.name, r.group, r.available, made[code])
        for code, why in res.rejected.items():
            rejected += 1
            log.warning("Описи: код %s відхилено — %s", code, why)
            if store:
                store.fail(code, by_code[code].name)
    return made, rejected, out_of_budget


# ---------- файл і звіт ----------

COLS = ["ID", "Код_товару", "Назва", "Група", "Наявність", "Опис_h2_uk (готово до вставки)",
        "Склад і харчова цінність (заповнити з етикетки)"]


def to_xlsx(rows: list[dict]) -> bytes:
    df = pd.DataFrame([[r["uid"], r["code"], r["name"].strip(), r["group"], r["available"], r["html"], LABEL_BLOCK]
                       for r in rows], columns=COLS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Описи")
        ws = w.sheets["Описи"]
        from openpyxl.styles import Alignment
        for col, width in zip("ABCDEFG", (10, 12, 45, 24, 11, 70, 55)):
            ws.column_dimensions[col].width = width
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A2"
    return buf.getvalue()


def format_message(n: int, left: int, footer: str, when: date | None = None) -> str:
    when = when or date.today()
    lines = [f"📝 <b>Чернетки описів</b> — {when:%d.%m.%Y}",
             f"У файлі: <b>{n}</b> товарів без опису в «Опис_h2_uk».",
             "",
             "Колонка «Опис_h2_uk» містить лише заголовок і короткий текст за назвою — її можна вставляти в Пром.",
             "⚠️ <b>Склад, калорійність і умови зберігання бот не вигадує</b> — їх треба взяти з етикетки "
             "виробника (шаблон у крайній колонці).",
             f"Ще без опису: {left}"]
    if footer:
        lines += ["", html.escape(footer)]
    return "\n".join(lines)


def run_descriptions(source: str | None = None, dry_run: bool = False, limit: int | None = None) -> int:
    """Генерує чернетки для нових товарів і надсилає їх у Telegram. dry_run: нічого не шле й не зберігає,
    лише пише reports/descriptions_preview.xlsx. Повертає кількість товарів у файлі."""
    per_run = limit if limit is not None else int(os.getenv("DESC_PER_RUN", "30"))
    store = DescStore(store_path())
    try:
        if source and os.path.exists(source):
            df = loader.read_catalog(open(source, "rb").read())
        else:
            df = loader.load_catalog(source or os.getenv("PROM_EXPORT_URL", "https://www.dilovakovbasa.ua/"
                                                         "upload/category/small/prom_export.xlsx"))
    except Exception as e:
        log.exception("Не вдалось завантажити каталог")
        if not dry_run:
            notifier.send_alert(f"Описи: не вдалось завантажити prom_export.xlsx: {e}")
        return 0

    limit_usd = float(os.getenv("DESC_MONTHLY_BUDGET_USD", "3.0"))
    budget = ai_check.Budget(limit_usd, store.spent_this_month(), max_out=MAX_OUTPUT_TOKENS)
    targets, _ = pick_targets(df, store, per_run)
    made, rejected, out_of_budget = generate(targets, style_examples(df), budget, None if dry_run else store)
    if not dry_run:
        store.set_spent(budget.spent)
        store.save()                       # оплачена робота не повинна пропасти

    # у файл — нові + ті, що не дійшли минулого разу і досі актуальні
    current = {int(r.code): r for r in df[~df["archived"] & (df["desc"].str.strip() == "")].itertuples()}
    rows = []
    for code, it in ({} if dry_run else store.unsent()).items():
        if code in current:
            rows.append({**it, "code": code})
        else:
            store.mark_sent([code])        # опис уже внесли в Пром — нагадувати нема сенсу
            store.save()
    if dry_run:
        for code, h in made.items():
            r = current[code]
            rows.append({"uid": None if pd.isna(r.uid) else int(r.uid), "code": code, "name": r.name,
                         "group": r.group, "available": r.available, "html": h})
    if not rows:
        log.info("Нових описів немає")
        return 0

    _, left = pick_targets(df, store, 0)
    footer = ""
    if os.getenv("ANTHROPIC_API_KEY"):
        footer = (f"🤖 Claude: цього запуску ≈${budget.used_now:.3f}, за місяць ≈${budget.spent:.3f} "
                  f"з ліміту ${limit_usd:.2f}")
        if out_of_budget:
            footer += "\n⏳ Ліміт вичерпано — продовжу наступного місяця або після збільшення DESC_MONTHLY_BUDGET_USD"
        if rejected:
            footer += f"\n🚫 Відхилено перевіркою (вигадані факти): {rejected}"
    xlsx = to_xlsx(rows)
    day = date.today().isoformat()
    if dry_run:
        out = Path("reports")
        out.mkdir(exist_ok=True)
        (out / "descriptions_preview.xlsx").write_bytes(xlsx)
        for r in rows[:5]:
            print(f"\n[{r['code']}] {r['name']}\n{r['html']}")
        print(f"\nУ файлі {len(rows)} описів → reports/descriptions_preview.xlsx\n"
              + html.unescape(format_message(len(rows), left, footer)))
        return len(rows)
    sent = notifier.send_report(format_message(len(rows), left, footer),
                                [(f"descriptions_{day}.xlsx", xlsx, f"📎 Чернетки описів ({len(rows)})")])
    if sent:                               # інакше лишаються «не надісланими» й підуть наступного разу
        store.mark_sent([r["code"] for r in rows])
        store.save()
    return len(rows)
