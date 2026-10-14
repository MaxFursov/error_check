from datetime import date

from checker import due
from state import State


def test_runs_every_two_days(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECK_EVERY_DAYS", "2")
    st = State(str(tmp_path / "s.json"))
    assert due(st, date(2026, 10, 14))                 # першого разу — одразу
    st.save([], {}, today=date(2026, 10, 14))
    st = State(str(tmp_path / "s.json"))
    assert not due(st, date(2026, 10, 15))             # наступного дня — рано
    assert due(st, date(2026, 10, 16))                 # через 2 дні — пора
    assert due(st, date(2026, 11, 3))                  # і після довгої перерви


def test_interval_survives_month_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECK_EVERY_DAYS", "2")
    st = State(str(tmp_path / "s.json"))
    st.save([], {}, today=date(2026, 10, 31))
    st = State(str(tmp_path / "s.json"))
    assert not due(st, date(2026, 11, 1))
    assert due(st, date(2026, 11, 2))
