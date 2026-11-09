from datetime import date, datetime

import pandas as pd

import weight_report as wr
from state import State


def make(rows):
    """rows: (uid, code, name, group, unit, available, weight, archived)"""
    df = pd.DataFrame(rows, columns=["uid", "code", "name", "group", "unit", "available", "weight", "archived"])
    df["uid"] = df["uid"].astype("Int64")
    return df


ROWS = [
    (101, 1, "Ковбаса Київська с/к", "Алан", "кг", "+", 0.5, False),         # вага є
    (102, 2, "Сир Гауда 48%", "Гурмандіс", "кг", "+", None, False),           # немає, в наявності
    (103, 3, "Сир Едам 45%", "Гурмандіс", "кг", "-", None, False),            # немає, не в наявності
    (104, 4, "Шинка Прошуто", "Гурмандіс", "шт", "+", 0.0, False),            # нуль = немає
    (105, 5, "_ВИВЕДЕНО Старий товар", "Алан", "кг", "+", None, True),        # виведений — не рахуємо
    (None, 6, "Позиція без ID", "Алан", "шт", "+", None, False),              # без uid -> код
]


def test_finds_missing_weight_only_among_active_and_treats_zero_as_missing():
    m = wr.find_missing(make(ROWS))
    assert set(m["ident"]) == {102, 103, 104, 6}
    assert 105 not in set(m["ident"]) and 101 not in set(m["ident"])
    assert list(m["in_stock"])[:3] == [True, True, True]          # спочатку ті, що в наявності


def test_report_is_single_framed_message_with_counts_and_diff():
    df = make(ROWS)
    m = wr.find_missing(df)
    text = wr.format_report(m, int((~df["archived"]).sum()), prev_ids={102, 999},
                            when=datetime(2026, 11, 9, 8, 15))
    assert text.startswith("━") and "КІНЕЦЬ ЗВІТУ" in text and text.rstrip().endswith("━━━━━━━━━━━━━━━━━━━━")
    assert "Без ваги: <b>4</b>" in text and "у наявності: <b>3</b>" in text
    assert "➕ 3 нових" in text and "✅ 1 виправлено" in text
    assert "<code>102</code>" in text


def test_big_report_fits_into_one_telegram_message():
    rows = [(i, i, f"Сир дуже довга назва товару номер {i} PRESIDENT", f"Група {i % 40} з довгою назвою",
             "кг", "+", None, False) for i in range(1, 900)]
    df = make(rows)
    text = wr.format_report(wr.find_missing(df), len(df))
    assert len(text) <= 4096


def test_csv_file_has_id_and_original_code():
    name, data, caption = wr.build_file(wr.find_missing(make(ROWS)), datetime(2026, 11, 9))
    assert name == "no_weight_2026-11-09.csv"
    lines = data.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("ID,Назва")
    ids = [l.split(",")[0] for l in lines[1:]]
    assert set(ids) == {"102", "103", "104", "6"}              # для позиції без ID — Код_товару
    assert ids.index("103") == len(ids) - 1                    # «немає в наявності» — в кінці списку
    assert "Позиція без ID" in data.decode("utf-8-sig") and "4" in caption


def test_due_every_three_days_and_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("WEIGHT_REPORT_EVERY_DAYS", "3")
    st = State(str(tmp_path / "s.json"))
    assert wr.due(st, date(2026, 11, 9))                    # першого разу — одразу
    st.mark_weight_sent([102, 103], today=date(2026, 11, 9))
    st = State(str(tmp_path / "s.json"))
    assert st.data["weight_prev"] == [102, 103]
    assert not wr.due(st, date(2026, 11, 11))
    assert wr.due(st, date(2026, 11, 12))


def test_mark_weight_sent_does_not_overwrite_error_check_state(tmp_path):
    a = State(str(tmp_path / "s.json"))
    b = State(str(tmp_path / "s.json"))                      # друга задача завантажила стан раніше
    a.save(["k1"], {1: 10.0}, today=date(2026, 11, 9))       # перевірка помилок зберегла свій стан
    b.mark_weight_sent([1], today=date(2026, 11, 9))         # звіт про вагу не має його затерти
    final = State(str(tmp_path / "s.json"))
    assert final.data["reported"] == ["k1"] and final.data["weight_prev"] == [1]
