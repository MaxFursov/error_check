"""Завантаження та читання експорту Проми (prom_export.xlsx)."""
import io
import logging
import zipfile

import pandas as pd
import requests

log = logging.getLogger(__name__)

# Колонки файлу -> внутрішні назви
COLUMNS = {
    "Код_товару": "code",
    "Назва_позиції": "name",
    "Ціна": "price",
    "Одиниця_виміру": "unit",
    "Посилання_зображення": "image",
    "Наявність": "available",
    "Кількість": "qty",
    "Назва_групи": "group",
}
REQUIRED = ("code", "name", "price", "unit", "group")


class CatalogFormatError(Exception):
    """Файл не схожий на експорт Проми (змінилась структура)."""


def fetch(url: str, timeout: int = 60) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _fix_zip_case(data: bytes) -> bytes:
    """Експорт Проми містить `xl/SharedStrings.xml` з великої літери.
    Excel у Windows це ковтає, а openpyxl на Linux (Railway) — ні.
    Перепаковуємо архів, приводячи ім'я до стандартного."""
    src = zipfile.ZipFile(io.BytesIO(data))
    canonical = {"xl/sharedstrings.xml": "xl/sharedStrings.xml"}
    if all(n == canonical.get(n.lower(), n) for n in src.namelist()):
        return data
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            dst.writestr(canonical.get(name.lower(), name), src.read(name))
    return out.getvalue()


def read_catalog(data: bytes) -> pd.DataFrame:
    """Повертає DataFrame з колонками code, name, price, unit, image,
    available, qty, group + archived (bool)."""
    raw = pd.read_excel(io.BytesIO(_fix_zip_case(data)), engine="openpyxl")
    missing = [c for c in COLUMNS if c in ("Код_товару", "Назва_позиції", "Ціна",
                                           "Одиниця_виміру", "Назва_групи")
               and c not in raw.columns]
    if missing:
        raise CatalogFormatError(f"У файлі немає колонок: {', '.join(missing)}")

    df = raw[[c for c in COLUMNS if c in raw.columns]].rename(columns=COLUMNS)
    for col in COLUMNS.values():
        if col not in df.columns:
            df[col] = None
    df = df.dropna(subset=["code"]).copy()
    df["code"] = df["code"].astype(int)
    df["name"] = df["name"].fillna("").astype(str)
    df["group"] = df["group"].fillna("").astype(str)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["archived"] = (df["group"].str.strip().str.startswith("Я!")
                      | df["name"].str.startswith("Я!"))
    return df.reset_index(drop=True)


def load_catalog(url: str) -> pd.DataFrame:
    log.info("Завантажую %s", url)
    df = read_catalog(fetch(url))
    log.info("Прочитано %d позицій (%d в архіві)", len(df), int(df["archived"].sum()))
    return df
