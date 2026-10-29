"""Описи сирів за даними з інтернету.

Claude (або сесія Claude) шукає в інтернеті розповідь про сам сир — що це, витримка, смак, особливості — і, якщо
вдасться, дані з етикетки САМЕ цього продукту; повертає JSON. Код нічого не вигадує сам: перевіряє числа на
правдоподібність, відсіює рекламу й медичні твердження, вимагає джерела (у режимі API — лише ті, що справді були у
видачі пошуку) і складає простий текст: заголовок, абзаци «про сир», далі Склад / Поживна цінність / Енергетична
цінність / Умови зберігання. Чого не знайдено — того в описі немає: воно потрапляє в колонку «Не знайдено»."""
import json
import logging
import os
import re
from dataclasses import dataclass, field

import pandas as pd

import ai_check
import descriptions

log = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 4000
MAX_CONTINUATIONS = 3                     # pause_turn: серверний цикл пошуку впирається в ліміт ітерацій
SEARCH_TOOL = "web_search_20260209"       # динамічна фільтрація; потрібні Sonnet 5.5 / Opus 5.5 та новіші
ASSUMED_INPUT_TOKENS = 30_000             # результати пошуку роздувають вхід — рахуємо із запасом

SYSTEM = """Ти — редактор каталогу гуртового постачальника сирів. Для одного товару знайди в інтернеті цікаву, але
ТОЧНУ інформацію про сам сир (і, якщо вдасться, дані з етикетки) та поверни її в JSON.

ЯК ШУКАТИ: 2–4 пошуки (бренд/виробник + назва; можна англійською чи мовою країни виробника), потім відкрий 1–3 найкращі
сторінки. Найкращі джерела: сайт виробника, сайти магазинів і дистриб'юторів, сирні каталоги й огляди.
НЕ ПІДХОДЯТЬ: бази, які наповнюють користувачі (FatSecret, MyFitnessPal, Open Food Facts, health-diet.ru, Fitatu),
форуми, соцмережі, сторінки інших брендів чи схожих сирів.

РОЗПОВІДЬ ПРО СИР ("about"): 2–5 коротких абзаців, кожен 1–3 речення, українською, діловим, живим тоном.
Приблизний порядок (лише те, що є в джерелах):
  1) Що це за сир: тип, країна/регіон, молоко, виробник.
  2) Витримка, жирність, вага голови чи фасування.
  3) Смак і текстура — як описує виробник чи надійний огляд.
  4) Особливості: кристали, колір, скоринка, як подавати чи зберігати.
Приклад тону (про сир Oude Meije): «Витриманий нідерландський сир типу Gouda з коров'ячого пастеризованого молока.
Витримка — щонайменше 100 тижнів, жирність 48+. За смаком насичений, гоструватий, концентрований; характерна
особливість — хрусткі кристали витримки.»

ЖОРСТКІ ПРАВИЛА:
1. Кожне твердження — зі сторінки, яку ти справді відкрив. Нічого з пам'яті, нічого «зазвичай».
2. Числа про цей продукт (витримка, жирність, вага голови) — лише зі сторінок САМЕ цього продукту. Якщо джерела
   розходяться (наприклад, вага голови 9 і 10 кг) — цього числа не пиши зовсім.
3. Загальні відомості про тип сиру (що таке Гауда) допустимі лише якщо вони є на відкритій сторінці і не суперечать назві.
4. Переказуй СВОЇМИ словами; не копіюй речення джерел і не бери в лапки.
5. Без реклами («найкращий», «ідеальний», «унікальний»), без медичних тверджень і без цін.
6. Склад і алергени — українською дослівно, нічого не додавай. Числа поживної цінності — на 100 г, у грамах, точно
   як на сторінці; немає для цього продукту — null (типові значення для виду сиру заборонені).
7. "sources" — адреси сторінок, з яких ти реально взяв дані.
8. "match": "exact" — знайшов сторінку цього продукту; "type" — лише відомості про тип сиру/бренд без сторінки
   самого продукту (тоді без чисел про цей продукт і без етикеткових даних); "none" — нічого придатного.

ЗАГОЛОВОК ("heading"): назва продукту ВЕЛИКИМИ літерами, лапки «ялинки», скорочення розшифровані, без виробника, ваги
й фасування. Приклади:
  Сир "MOZZARELLA" м'який з в'яленими томатами 45% 165 г/шт LYUBAR Milk → СИР «MOZZARELLA» М'ЯКИЙ З В'ЯЛЕНИМИ ТОМАТАМИ
  Сир "Сметанковий" твердий 50% брус 1,9 кг → СИР «СМЕТАНКОВИЙ» ТВЕРДИЙ
"summary" — 1–2 нейтральні речення лише з назви (на випадок, коли нічого не знайдено), без чисел, яких немає в назві.

ВІДПОВІДЬ — один JSON-об'єкт без markdown і без тексту навколо, рівно такі ключі:
{"match": "exact"|"type"|"none", "heading": str, "summary": str, "about": [str],
 "composition": str|null, "allergens": str|null,
 "fat_g": number|null, "saturated_g": number|null, "carbs_g": number|null, "sugars_g": number|null,
 "protein_g": number|null, "salt_g": number|null, "kj": number|null, "kcal": number|null,
 "storage": str|null, "sources": [str], "notes": str}
"notes" — одним реченням: що знайдено і що ні (напр. «розповідь є, етикетки на сторінці немає»)."""

NUTRITION = [  # (ключ, підпис, роздільник у прикладах)
    ("fat_g", "жири", ","), ("saturated_g", "з них насичені", ";"), ("carbs_g", "вуглеводи", ","),
    ("sugars_g", "з них цукри", ";"), ("protein_g", "білки", ";"), ("salt_g", "сіль", "."),
]
FACT_KEYS = ["composition", "allergens", "storage", "kj", "kcal"] + [k for k, *_ in NUTRITION]


class ResearchBudget(ai_check.Budget):
    """Бюджет із урахуванням платних пошуків (AI_PRICE_PER_SEARCH, USD за один пошук)."""

    def __init__(self, limit_usd: float, spent_usd: float, max_searches: int):
        super().__init__(limit_usd, spent_usd, max_out=MAX_OUTPUT_TOKENS)
        self.price_search = float(os.getenv("AI_PRICE_PER_SEARCH", "0.01"))
        self.max_searches = max_searches

    def worst_case(self, prompt_chars: int = 0) -> float:
        return self.cost(ASSUMED_INPUT_TOKENS, self.max_out) + self.max_searches * self.price_search

    def add_searches(self, n: int) -> None:
        self.spent += n * self.price_search


@dataclass
class Result:
    ok: bool
    text: str = ""
    status: str = ""
    missing: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    notes: str = ""
    reason: str = ""


# ---------- розбір відповіді ----------

def parse_json(text: str) -> dict | None:
    """Бере найбільший {...} з тексту: модель може обгорнути JSON словами чи ```."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _num(v):
    """Число або None. Приймає «27,5» і «27.5»; логічні й від'ємні значення — відсіює."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, str):
        v = v.strip().replace(",", ".")
        v = re.sub(r"\s*(?:г|g|kcal|ккал|kj|кдж)$", "", v, flags=re.I)
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x >= 0 else None


def _text(v, limit: int):
    if not isinstance(v, str):
        return None
    v = re.sub(r"\s+", " ", v).strip()
    return v if v and len(v) <= limit and v.casefold() not in ("null", "none", "n/a", "-", "—") else None


# бази, які наповнюють користувачі, і агрегатори калорій: їхні цифри не є даними з етикетки
BAD_SOURCE_RE = re.compile(r"fatsecret|myfitnesspal|openfoodfacts|health-diet|fitatu|calorizator|calorie|kalorie|"
                           r"yazio|nutritionix|edamam|facebook|instagram|reddit|youtube", re.I)


def _norm_url(u: str) -> str:
    return re.sub(r"^https?://(?:www\.)?", "", u.strip().lower()).rstrip("/")


QUOTED_RE = descriptions.QUOTED_RE
GENERIC_WORDS = {"сир", "сири", "твердий", "м'який", "напівтвердий", "плавлений", "копчений", "нарізка", "брус", "шт"}
# реклама, медичні твердження, етикеткові слова й ціни в розповіді про сир
ABOUT_FORBIDDEN_RE = re.compile(
    r"найкращ|ідеальн|унікальн|неперевершен|чудов|смачніш|корисн|лікув|користь для|без гмо|без консервант|"
    r"ккал|кдж|калорій|ціна|грн|\$|€", re.I)


def _shared_words(name: str, heading: str) -> int:
    """Скільки значущих слів (від 4 літер, не службових) з назви є в заголовку."""
    words = {w for w in re.findall(r"[\w'’]{4,}", name.casefold()) if w not in GENERIC_WORDS}
    return sum(1 for w in words if w in heading.casefold())


def build_result(data: dict, name: str, weight, allowed_urls: set | None) -> Result:
    """Перевіряє відповідь і складає HTML. allowed_urls — адреси зі справжньої видачі пошуку
    (None — не перевіряти, для ручного режиму)."""
    heading = _text(data.get("heading"), 140)
    if not heading or not descriptions._mostly_upper(heading):
        return Result(False, reason="заголовок відсутній або не великими літерами")
    quoted = QUOTED_RE.search(name)
    if quoted and quoted.group(1).casefold() not in heading.casefold() and _shared_words(name, heading) < 2:
        return Result(False, reason=f"в заголовку немає назви «{quoted.group(1)}»")

    notes = _text(data.get("notes"), 400) or ""
    f = {k: _num(data.get(k)) for k in ("fat_g", "saturated_g", "carbs_g", "sugars_g", "protein_g", "salt_g",
                                         "kj", "kcal")}
    f["composition"] = _text(data.get("composition"), 900)
    f["allergens"] = _text(data.get("allergens"), 300)
    f["storage"] = _text(data.get("storage"), 300)

    # джерела: лише ті, що справді були у видачі пошуку
    raw = data.get("sources") if isinstance(data.get("sources"), list) else []
    sources = [s.strip() for s in raw if isinstance(s, str) and re.match(r"https?://", s.strip())
               and not BAD_SOURCE_RE.search(s)]
    if allowed_urls is not None:
        allowed = {_norm_url(u) for u in allowed_urls}
        sources = [s for s in sources if _norm_url(s) in allowed]
    sources = list(dict.fromkeys(sources))[:5]

    problems = []
    match = data.get("match")
    if match != "exact":
        f = {k: None for k in f}                  # етикеткові дані — лише зі сторінки самого продукту
    elif any(v is not None for v in f.values()) and not sources:
        problems.append("факти без підтверджених джерел — відкинуто")
        f = {k: None for k in f}

    # розповідь про сир: потрібні джерела; без реклами й медичних тверджень
    about = []
    if match in ("exact", "type") and isinstance(data.get("about"), list):
        for para in data["about"][:5]:
            para = _text(para, 600)
            if not para or len(para) < 25:
                continue
            bad = ABOUT_FORBIDDEN_RE.search(para)
            if bad and not ABOUT_FORBIDDEN_RE.search(name):
                problems.append(f"абзац відкинуто («{bad.group(0)}»)")
                continue
            about.append(para)
    if about and not sources:
        problems.append("розповідь без підтверджених джерел — відкинуто")
        about = []

    # правдоподібність чисел
    for k in ("fat_g", "saturated_g", "carbs_g", "sugars_g", "protein_g", "salt_g"):
        if f[k] is not None and f[k] > 100:
            problems.append(f"{k}>100")
            f[k] = None
    if f["saturated_g"] is not None and f["fat_g"] is not None and f["saturated_g"] > f["fat_g"] + 0.05:
        problems.append("насичені > жири")
        f["saturated_g"] = None
    if f["sugars_g"] is not None and f["carbs_g"] is not None and f["sugars_g"] > f["carbs_g"] + 0.05:
        problems.append("цукри > вуглеводи")
        f["sugars_g"] = None
    macro = sum(f[k] or 0 for k in ("fat_g", "protein_g", "carbs_g", "salt_g"))
    if macro > 100.5:
        problems.append("сума жири+білки+вуглеводи+сіль > 100 г")
        for k in ("fat_g", "protein_g", "carbs_g", "salt_g", "saturated_g", "sugars_g"):
            f[k] = None
    if f["kcal"] is not None and not 0 < f["kcal"] <= 900:
        problems.append("ккал поза межами")
        f["kcal"] = None
    if f["kj"] is not None and not 0 < f["kj"] <= 3800:
        problems.append("кДж поза межами")
        f["kj"] = None
    if f["kj"] and f["kcal"] and abs(f["kj"] - f["kcal"] * 4.184) > 0.08 * f["kj"]:
        problems.append("кДж і ккал не збігаються")
        f["kj"] = f["kcal"] = None

    summary = _text(data.get("summary"), 400)
    has_facts = any(v is not None for v in f.values())
    if summary and (has_facts or about or descriptions.reject_reason(name, weight, heading, summary)):
        summary = None                              # коротке summary — лише коли більше нічого немає

    missing = []
    if not f["composition"]:
        missing.append("Склад")
    if not f["allergens"]:
        missing.append("Алергени")
    if not any(f[k] is not None for k, *_ in NUTRITION):
        missing.append("Поживна цінність")
    if f["kj"] is None and f["kcal"] is None:
        missing.append("Енергетична цінність")
    if not f["storage"]:
        missing.append("Умови зберігання")
    core_missing = [m for m in missing if m != "Алергени"]   # алергенів на сторінці може й не бути
    if not about:
        missing.insert(0, "Розповідь про сир")
    if about and has_facts and not core_missing:
        status = "повний"
    elif about:
        status = "розповідь"
    elif has_facts:
        status = "частковий"
    else:
        status = "лише заголовок"
    if problems:
        notes = (notes + " " if notes else "") + "Перевірка: " + "; ".join(problems) + "."
    return Result(True, render_text(heading, summary, about, f), status, missing,
                  sources if (has_facts or about) else [], notes)


# ---------- простий текст: 1С сама перетворює абзаци й переноси на HTML для Проми ----------

def _fmt(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def render_text(heading: str, summary: str | None, about: list, f: dict) -> str:
    """Заголовок, абзаци «про сир», далі структура як у наявних описах (Склад, Поживна цінність, Енергетична цінність,
    Умови зберігання), але без тегів: абзаци розділені порожнім рядком, рядки поживної цінності — переносами."""
    blocks = [heading]
    if summary:
        blocks.append(summary)
    blocks.extend(about)
    if f.get("composition"):
        blocks.append(f"Склад: {f['composition']}")
    if f.get("allergens"):
        blocks.append(f["allergens"])
    rows = [f"{label} — {_fmt(f[key])} г{sep}" for key, label, sep in NUTRITION if f.get(key) is not None]
    if rows:
        rows[-1] = rows[-1][:-1] + "."
        blocks.append("Поживна цінність на 100 г продукту:\n" + "\n".join(rows))
    energy = " / ".join(x for x in (f"{_fmt(f['kj'], 0)} кДж" if f.get("kj") else "",
                                    f"{_fmt(f['kcal'], 0)} ккал" if f.get("kcal") else "") if x)
    if energy:
        blocks.append(f"Енергетична цінність (калорійність):\n{energy}")
    if f.get("storage"):
        blocks.append(f"Умови зберігання:\n{f['storage']}")
    return "\n\n".join(blocks)


# ---------- виклик Claude з веб-пошуком ----------

def search_urls(content) -> set:
    """Адреси зі справжньої видачі: блоки web_search_tool_result (у разі помилки content — об'єкт, не список)."""
    urls = set()
    for b in content:
        if getattr(b, "type", None) == "web_search_tool_result" and isinstance(getattr(b, "content", None), list):
            urls.update(getattr(r, "url", "") for r in b.content if getattr(r, "url", ""))
    return urls


def research_one(client, model: str, row, budget: ResearchBudget, effort: str = "medium") -> Result:
    weight = "" if pd.isna(row.weight) else f"\nВага: {int(row.weight)} г"
    first = {"role": "user", "content": f"Код: {row.code}\nНазва: {row.name.strip()}\nГрупа: {row.group.strip()}{weight}"}
    messages = [first]
    urls: set = set()
    resp = None
    for _ in range(MAX_CONTINUATIONS + 1):
        resp = client.messages.create(
            model=model, max_tokens=MAX_OUTPUT_TOKENS, system=SYSTEM, messages=messages,
            tools=[{"type": SEARCH_TOOL, "name": "web_search", "max_uses": budget.max_searches}],
            output_config={"effort": effort})
        usage = getattr(resp, "usage", None)
        budget.add(getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)
        budget.add_searches(getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0)
        urls |= search_urls(resp.content)
        if getattr(resp, "stop_reason", None) != "pause_turn":
            break
        messages = [first, {"role": "assistant", "content": resp.content}]   # сервер продовжить сам
    texts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    data = parse_json(texts[-1]) if texts else None
    if data is None:
        return Result(False, reason="відповідь не JSON")
    return build_result(data, row.name, row.weight, urls)
