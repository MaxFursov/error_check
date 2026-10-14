"""Детерміновані перевірки каталогу (без API, безкоштовні й швидкі)."""
import re
import statistics
from dataclasses import dataclass
from typing import Callable, Iterable

import pandas as pd

ERROR, WARNING, INFO = "error", "warning", "info"

CYR = "АБВГҐДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯабвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
LAT = "A-Za-z"
LETTERS = f"[{CYR}{LAT}]"
WORD_RE = re.compile(f"{LETTERS}+(?:['’ʼ]{LETTERS}+)*")

# Латинські літери, візуально ідентичні кириличним (і навпаки)
LAT_TO_CYR = {
    "A": "А", "a": "а", "B": "В", "C": "С", "c": "с", "E": "Е", "e": "е",
    "H": "Н", "I": "І", "i": "і", "K": "К", "M": "М", "O": "О", "o": "о",
    "P": "Р", "p": "р", "T": "Т", "X": "Х", "x": "х", "Y": "У", "y": "у",
}
CYR_TO_LAT = {v: k for k, v in LAT_TO_CYR.items()}

# Бренди, у яких змішаний алфавіт — навмисний стиль, а не помилка
MIXED_ALPHABET_WHITELIST = {"ЇSTA", "М'ЯСКОFF", "М’ЯСКОFF"}

# Слова, що описують вік/тип сиру чи ковбаси, а не виробника.
# Класична помилка: "YOUNG" (молодий сир) записали як торгову марку.
DESCRIPTOR_WORDS = {
    "young", "янг", "old", "олд", "mature", "матюр", "aged", "extra", "екстра",
    "premium", "преміум", "classic", "класік", "light", "лайт", "vecchio",
    "stravecchio", "reserve", "резерва", "gold", "голд", "original", "оригінал",
    "fresco", "bio", "біо", "mild", "strong", "medium",
}


@dataclass
class Issue:
    code: int
    name: str
    rule: str
    severity: str
    message: str
    suggestion: str = ""

    @property
    def key(self) -> str:
        """Стабільний ключ, щоб не повідомляти про одну й ту саму помилку двічі."""
        return f"{self.code}|{self.rule}|{self.message}"


def _active(df: pd.DataFrame) -> pd.DataFrame:
    return df[~df["archived"]]


# --------------------------------------------------------------------------
# Окремі правила. Кожне приймає DataFrame і повертає список Issue.
# --------------------------------------------------------------------------

def check_missing_price(df: pd.DataFrame) -> list[Issue]:
    out = []
    for r in _active(df).itertuples():
        if pd.isna(r.price) or r.price <= 0:
            sev = ERROR if r.available == "+" else WARNING
            out.append(Issue(r.code, r.name, "missing_price", sev,
                             "Не вказано ціну" + (" (товар у наявності!)" if sev == ERROR else "")))
    return out


def check_short_name(df: pd.DataFrame) -> list[Issue]:
    return [Issue(r.code, r.name, "short_name", ERROR,
                  "Назва занадто коротка або порожня")
            for r in _active(df).itertuples()
            if len(r.name.strip()) < 8 or len(r.name.split()) < 2]


def check_duplicate_code(df: pd.DataFrame) -> list[Issue]:
    act = _active(df)
    dup = act[act["code"].duplicated(keep=False)]
    return [Issue(r.code, r.name, "duplicate_code", ERROR, "Код товару повторюється")
            for r in dup.itertuples()]


def _norm_name(name: str) -> str:
    return re.sub(r"[\s\"'«»“”]+", " ", name).strip().casefold()


def check_duplicate_name(df: pd.DataFrame) -> list[Issue]:
    act = _active(df).assign(_n=lambda d: d["name"].map(_norm_name))
    out = []
    for _, grp in act.groupby("_n"):
        codes = grp["code"].unique()
        if len(codes) < 2:
            continue
        for r in grp.itertuples():
            others = ", ".join(str(c) for c in codes if c != r.code)
            out.append(Issue(r.code, r.name, "duplicate_name", WARNING,
                             f"Така сама назва в іншого товару (код {others})"))
    return out


def _is_lat(ch: str) -> bool:
    return ch.isascii() and ch.isalpha()


def _fix_word(word: str) -> str | None:
    """Пропонує виправлення слова зі змішаним алфавітом або None, якщо
    не всі «чужі» літери мають візуальний двійник (тоді потрібна людина)."""
    cyr = sum(ch in CYR for ch in word)
    lat = sum(_is_lat(ch) for ch in word)
    if cyr >= lat:
        mapping, is_minor = LAT_TO_CYR, _is_lat
    else:
        mapping, is_minor = CYR_TO_LAT, lambda ch: ch in CYR
    if any(is_minor(ch) and ch not in mapping for ch in word):
        return None
    return "".join(mapping[ch] if is_minor(ch) else ch for ch in word)


def check_mixed_alphabet(df: pd.DataFrame) -> list[Issue]:
    """Слово з літер двох алфавітів: 'Cир' (латинська C), 'Synnоve' (кирилична о)."""
    out = []
    for r in _active(df).itertuples():
        for word in WORD_RE.findall(r.name):
            has_cyr = any(ch in CYR for ch in word)
            has_lat = any(_is_lat(ch) for ch in word)
            if not (has_cyr and has_lat) or word.upper() in MIXED_ALPHABET_WHITELIST:
                continue
            fix = _fix_word(word)
            if fix:
                out.append(Issue(r.code, r.name, "mixed_alphabet", ERROR,
                                 f"У слові «{word}» змішано кирилицю й латиницю — "
                                 f"на око непомітно, але пошук такого слова не знайде",
                                 f"{word} → {fix}"))
            else:
                out.append(Issue(r.code, r.name, "mixed_alphabet", WARNING,
                                 f"У слові «{word}» змішано кирилицю й латиницю — перевірте"))
    return out


def check_quotes_brackets(df: pd.DataFrame) -> list[Issue]:
    out = []
    stray_apostrophe = re.compile(f"(?<!{LETTERS})['’]|['’](?!{LETTERS})")
    for r in _active(df).itertuples():
        n = r.name
        problems = []
        if n.count('"') % 2:
            problems.append("непарні лапки \"")
        if stray_apostrophe.search(n):
            problems.append("апостроф замість лапки")
        if n.count("(") != n.count(")"):
            problems.append("незакрита дужка")
        if problems:
            out.append(Issue(r.code, r.name, "quotes_brackets", ERROR,
                             "Пошкоджена розмітка назви: " + ", ".join(problems)))
    return out


def check_percent(df: pd.DataFrame) -> list[Issue]:
    out = []
    for r in _active(df).itertuples():
        for m in re.findall(r"(\d+(?:[.,]\d+)?)\s*%", r.name):
            if float(m.replace(",", ".")) > 100:
                out.append(Issue(r.code, r.name, "percent", ERROR,
                                 f"Жирність/відсоток {m}% більший за 100"))
    return out


AGE_CONTEXT_RE = re.compile(
    r"(?:\d+\s*%|\d+\s*(?:міс\w*|рок\w*|років|years?|months?|days?|днів))\s*[,(]?\s*$",
    re.IGNORECASE)


def check_descriptor_as_brand(df: pd.DataFrame) -> list[Issue]:
    """Після відсотка/віку стоїть слово YOUNG/OLD/MATURE… там, де має бути
    виробник. Так вік сиру ('Young') помилково сприймали як торгову марку.
    Справжні бренди на кшталт 'Mill`s Premium' не чіпаємо (перед словом — не вік)."""
    out = []
    for r in _active(df).itertuples():
        m = list(WORD_RE.finditer(r.name))
        if not m or m[-1].group().casefold() not in DESCRIPTOR_WORDS:
            continue
        last = m[-1]
        if AGE_CONTEXT_RE.search(r.name[:last.start()]):
            out.append(Issue(
                r.code, r.name, "descriptor_as_brand", WARNING,
                f"Наприкінці назви «{last.group()}» — це вік/тип продукту, а не "
                f"виробник; ТМ у назві не вказана"))
    return out


def check_archive_state(df: pd.DataFrame) -> list[Issue]:
    arch = df[df["archived"] & (df["available"] == "+")]
    return [Issue(r.code, r.name, "archived_in_stock", WARNING,
                  "Товар у групі-архіві, але позначений як «в наявності»")
            for r in arch.itertuples()]


def check_missing_image(df: pd.DataFrame) -> list[Issue]:
    act = _active(df)
    return [Issue(r.code, r.name, "missing_image", INFO, "Немає фото")
            for r in act[act["image"].isna() & (act["available"] == "+")].itertuples()]


def check_whitespace(df: pd.DataFrame) -> list[Issue]:
    return [Issue(r.code, r.name, "whitespace", INFO, "Зайві пробіли в назві",
                  re.sub(r"\s+", " ", r.name).strip())
            for r in _active(df).itertuples()
            if r.name != r.name.strip() or "  " in r.name]


GLUED_RE = re.compile(f'{LETTERS}"{LETTERS}|\\d(?:кг|г|гр){LETTERS}')


def check_glued_words(df: pd.DataFrame) -> list[Issue]:
    """Пропущений пробіл: '"Gudbrand"традиційний', '0,08кгТМ'."""
    return [Issue(r.code, r.name, "glued_words", INFO, "Пропущено пробіл у назві")
            for r in _active(df).itertuples() if GLUED_RE.search(r.name)]


PACK_RE = re.compile(r"\d+(?:[.,]\d+)?\s*г(?:р)?\b(?!\s*/\s*кг)", re.IGNORECASE)


def check_price_outliers(df: pd.DataFrame) -> list[Issue]:
    """Ціна за кг у 3+ рази відрізняється від типової в групі постачальника
    (по логарифму, стійко до викидів). Часто це ціна пачки замість ціни за кг."""
    import math
    out = []
    act = _active(df)
    act = act[(act["price"] > 0) & (act["unit"] == "кг")]
    for group, grp in act.groupby("group"):
        if len(grp) < 8:
            continue
        logs = [math.log(p) for p in grp["price"]]
        med = statistics.median(logs)
        mad = statistics.median(abs(x - med) for x in logs) or 0.05
        for r, lg in zip(grp.itertuples(), logs):
            z = 0.6745 * (lg - med) / max(mad, 0.05)
            if abs(z) > 4 and abs(lg - med) > math.log(3):
                hint = (" У назві є фасування в грамах — можливо, вказано ціну "
                        "за упаковку, а не за кг." if PACK_RE.search(r.name) else "")
                out.append(Issue(
                    r.code, r.name, "price_outlier", WARNING,
                    f"Ціна {r.price:g} грн/кг дуже відрізняється від типової в групі "
                    f"«{group.strip()}» (≈{math.exp(med):.0f}).{hint}"))
    return out


RULES: list[Callable[[pd.DataFrame], list[Issue]]] = [
    check_missing_price, check_short_name, check_duplicate_code, check_duplicate_name,
    check_mixed_alphabet, check_quotes_brackets, check_percent,
    check_descriptor_as_brand, check_archive_state, check_missing_image,
    check_whitespace, check_glued_words, check_price_outliers,
]


def check_price_jumps(df: pd.DataFrame, prev_prices: dict[int, float],
                      threshold: float = 0.4) -> list[Issue]:
    """Порівняння з минулим запуском: ціна змінилась більше ніж на threshold."""
    out = []
    for r in _active(df).itertuples():
        old = prev_prices.get(r.code)
        if old and not pd.isna(r.price) and old > 0:
            change = (r.price - old) / old
            if abs(change) >= threshold:
                out.append(Issue(r.code, r.name, "price_jump", WARNING,
                                 f"Ціна змінилась на {change:+.0%}: {old:g} → {r.price:g} грн"))
    return out


def run_all(df: pd.DataFrame, prev_prices: dict[int, float] | None = None) -> list[Issue]:
    issues: list[Issue] = []
    for rule in RULES:
        issues.extend(rule(df))
    if prev_prices:
        issues.extend(check_price_jumps(df, prev_prices))
    return issues
