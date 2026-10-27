import io
import json
from types import SimpleNamespace

import pandas as pd

import ai_check
import cheese_research as cr
import descriptions
from descriptions import DescStore

NAME = 'Сир "GOUDA" нарізка (слайси) 150 г/шт 48% Arla'
URL = "https://www.arla.com/products/gouda-slices/"

GOOD = {
    "match": "exact", "heading": "СИР «GOUDA» НАРІЗКА СЛАЙСАМИ", "summary": "Сир «Gouda» у нарізці слайсами.",
    "composition": "молоко пастеризоване, сіль, закваска, сичужний фермент", "allergens": "Містить молоко.",
    "fat_g": 27, "saturated_g": 17.5, "carbs_g": 0, "sugars_g": 0, "protein_g": "25,0", "salt_g": 1.8,
    "kj": 1451, "kcal": 350, "storage": "Зберігати при температурі від +2°C до +6°C.",
    "sources": [URL], "notes": "Усе знайдено на сайті виробника.",
}


def web_block(*urls):
    return SimpleNamespace(type="web_search_tool_result",
                           content=[SimpleNamespace(type="web_search_result", url=u, title="t") for u in urls])


def resp(content, stop="end_turn", searches=1):
    return SimpleNamespace(content=content, stop_reason=stop, usage=SimpleNamespace(
        input_tokens=12000, output_tokens=700, server_tool_use=SimpleNamespace(web_search_requests=searches)))


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def text(d):
    return SimpleNamespace(type="text", text="Ось результат:\n```json\n" + json.dumps(d, ensure_ascii=False) + "\n```")


def test_full_result_is_rendered_as_plain_text_in_the_layout_of_existing_descriptions():
    res = cr.build_result(dict(GOOD), NAME, 150.0, {URL})
    assert res.ok and res.status == "повний" and res.sources == [URL]
    assert "<" not in res.text and "&" not in res.text                  # жодних тегів і сутностей: HTML зробить 1С
    assert res.text == (
        "СИР «GOUDA» НАРІЗКА СЛАЙСАМИ\n\n"
        "Склад: молоко пастеризоване, сіль, закваска, сичужний фермент\n\n"
        "Містить молоко.\n\n"
        "Поживна цінність на 100 г продукту:\n"
        "жири — 27,0 г,\nз них насичені — 17,5 г;\n"
        "вуглеводи — 0,0 г,\nз них цукри — 0,0 г;\n"
        "білки — 25,0 г;\nсіль — 1,8 г.\n\n"
        "Енергетична цінність (калорійність):\n1451 кДж / 350 ккал\n\n"
        "Умови зберігання:\nЗберігати при температурі від +2°C до +6°C.")


def test_nothing_found_gives_heading_summary_and_lists_missing():
    res = cr.build_result({"match": "none", "heading": "СИР «GOUDA» НАРІЗКА", "summary": "Сир «Gouda» у нарізці слайсами.",
                           "composition": "вигадано", "fat_g": 30, "sources": [URL]}, NAME, 150.0, {URL})
    assert res.ok and res.status == "лише заголовок" and res.sources == []
    assert "Склад" not in res.text and "вигадано" not in res.text and "Сир «Gouda» у нарізці слайсами." in res.text
    assert res.missing[:2] == ["Склад", "Алергени"]


def test_facts_without_real_sources_are_dropped():
    data = dict(GOOD, sources=["https://invented.example/page"])
    res = cr.build_result(data, NAME, 150.0, {URL})        # цієї адреси не було у видачі пошуку
    assert res.status == "лише заголовок" and "Склад" not in res.text and "джерел" in res.notes
    assert cr.build_result(dict(GOOD, sources=[]), NAME, 150.0, {URL}).status == "лише заголовок"


def test_implausible_numbers_are_dropped():
    r = cr.build_result(dict(GOOD, kj=900), NAME, 150.0, {URL})          # 900 кДж ≠ 350 ккал
    assert "ккал" not in r.text and "кДж" not in r.text and "не збігаються" in r.notes
    r = cr.build_result(dict(GOOD, saturated_g=40), NAME, 150.0, {URL})  # насичені більше за жири
    assert "насичені" not in r.text and "жири — 27,0" in r.text
    r = cr.build_result(dict(GOOD, fat_g=60, protein_g=50), NAME, 150.0, {URL})   # сума > 100 г
    assert "Поживна цінність" not in r.text


def test_bad_heading_is_rejected():
    assert not cr.build_result(dict(GOOD, heading="Сир Gouda"), NAME, 150.0, {URL}).ok
    assert not cr.build_result(dict(GOOD, heading="СИР «EDAM»"), NAME, 150.0, {URL}).ok   # немає назви з лапок
    assert not cr.build_result({"match": "exact"}, NAME, 150.0, {URL}).ok


def test_parse_json_tolerates_wrapping_and_garbage():
    assert cr.parse_json('Ось:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert cr.parse_json("без json") is None and cr.parse_json("{зламаний") is None


def test_research_one_resumes_pause_turn_and_collects_search_urls(monkeypatch):
    monkeypatch.setenv("AI_PRICE_PER_SEARCH", "0.01")
    first = resp([SimpleNamespace(type="server_tool_use", name="web_search", input={"query": "arla gouda"}),
                  web_block(URL)], stop="pause_turn", searches=2)
    second = resp([text(GOOD)], searches=1)
    client = FakeClient(first, second)
    budget = cr.ResearchBudget(10.0, 0.0, max_searches=3)
    row = SimpleNamespace(code=1499, name=NAME, group="Азимут", weight=150.0)
    res = cr.research_one(client, "claude-sonnet-5-5", row, budget)
    assert res.ok and res.status == "повний"
    assert client.calls[0]["tools"][0]["type"] == "web_search_20260209" and client.calls[0]["tools"][0]["max_uses"] == 3
    assert "thinking" not in client.calls[0]                              # Sonnet 5.5 відхиляє {"type": "disabled"}
    assert client.calls[1]["messages"][1]["role"] == "assistant" and len(client.calls[1]["messages"]) == 2
    assert abs(budget.spent - (2 * 12000 * 2.0 / 1e6 * 1.5 + 2 * 700 * 10.0 / 1e6 * 1.5 + 3 * 0.01)) < 0.2  # порядок величини
    assert budget.spent > 0.03                                            # платні пошуки враховано


def test_search_error_block_content_is_not_a_list():
    err = SimpleNamespace(type="web_search_tool_result", content=SimpleNamespace(type="web_search_tool_result_error",
                                                                                  error_code="max_uses_exceeded"))
    assert cr.search_urls([err, web_block(URL)]) == {URL}


def test_research_many_stores_results_and_stops_on_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    df = pd.DataFrame([[i, 1000 + i, NAME, "Азимут", "+", "", 150.0, False] for i in range(1, 6)],
                      columns=["code", "uid", "name", "group", "available", "desc", "weight", "archived"])
    client = FakeClient(*[resp([web_block(URL), text(GOOD)]) for _ in range(5)])
    monkeypatch.setattr(ai_check, "_client", lambda: client)
    store = DescStore(str(tmp_path / "d.json"))
    budget = cr.ResearchBudget(0.30, 0.0, max_searches=3)    # запас на товар ≈ $0.18, реально ≈ $0.057 → зупинка після 2-3 товарів
    made, rejected, out_of_budget = descriptions.research_many(df, budget, store)
    assert out_of_budget and 0 < len(made) < 5 and rejected == 0
    first = next(iter(made))
    assert made[first]["status"] == "повний" and store.data["items"][str(first)]["sources"] == [URL]


def test_pick_targets_defaults_to_cheese_only(monkeypatch, tmp_path):
    monkeypatch.delenv("DESC_ONLY_KINDS")
    df = pd.DataFrame([[1, 'Сир "А"', "Г", "", "+", float("nan"), False], [2, "Сирники", "Г", "", "+", float("nan"), False],
                       [3, "Ковбаса Х", "Г", "", "+", float("nan"), False], [4, "сир Б", "Г", "", "+", float("nan"), False]],
                      columns=["code", "name", "group", "desc", "available", "weight", "archived"])
    got, left = descriptions.pick_targets(df, DescStore(str(tmp_path / "d.json")), 10)
    assert sorted(got["code"]) == [1, 4] and left == 2          # «Сирники» і ковбаса — не сири


def test_xlsx_research_columns():
    data = descriptions.to_xlsx([{"uid": 7, "code": 1, "name": NAME, "group": "Г", "available": "+", "text": "Текст",
                                  "status": "частковий", "missing": ["Склад", "Умови зберігання"], "sources": [URL, URL + "2"],
                                  "notes": "ок"}])
    back = pd.read_excel(io.BytesIO(data))
    assert list(back.columns) == descriptions.COLS_RESEARCH
    assert back.iloc[0]["Статус"] == "частковий" and back.iloc[0]["Не знайдено — дописати вручну"] == "Склад, Умови зберігання"
    assert URL in back.iloc[0]["Джерела (перевірте перед публікацією)"]


def test_user_maintained_databases_are_never_accepted_as_sources():
    data = dict(GOOD, sources=["https://www.fatsecret.pl/kalorie/ryki/maasdam/100g", "https://world.openfoodfacts.org/product/1"])
    res = cr.build_result(data, NAME, 150.0, None)
    assert res.status == "лише заголовок" and res.sources == [] and "Склад" not in res.text
    ok = cr.build_result(dict(GOOD, sources=["https://www.fatsecret.pl/x", URL]), NAME, 150.0, None)
    assert ok.status == "повний" and ok.sources == [URL]


def test_prepare_research_lists_cheeses_with_instructions(tmp_path, monkeypatch):
    monkeypatch.setenv("DESC_STATE_PATH", str(tmp_path / "d.json"))
    df = pd.DataFrame([[1, 1001, NAME, "Азимут", "+", "", 150.0, False]],
                      columns=["code", "uid", "name", "group", "available", "desc", "weight", "archived"])
    monkeypatch.setattr(descriptions, "_load", lambda source, dry_run: df)
    out = tmp_path / "req.txt"
    assert descriptions.prepare(None, str(out), research=True) == 1
    text = out.read_text(encoding="utf-8")
    assert "НЕ ПІДХОДЯТЬ як джерела" in text and "1 | Сир \"GOUDA\"" in text and "вага: 150 г" in text


# ---------- нагадування про чернетки, які так і не внесли в Пром ----------

from datetime import date


def reminder_df(rows):
    """rows: (code, name, desc_in_prom, available)."""
    df = pd.DataFrame([[c, 1000 + c, n, "Г", a, d, float("nan"), False] for c, n, d, a in rows],
                      columns=["code", "uid", "name", "group", "available", "desc", "weight", "archived"])
    return df


def stored(tmp_path, items):
    """items: (code, name, status, last_sent, sent)."""
    store = DescStore(str(tmp_path / "d.json"))
    for code, name, status, last, sent in items:
        store.add(code, 1000 + code, name, "Г", "+", f"Текст {code}", status=status, missing=[], sources=[], notes="")
        store.data["items"][str(code)].update({"sent": sent, "last_sent": last, "first_sent": last, "times_sent": 1 if sent else 0})
    return store


def test_reminders_skip_filled_in_prom_recent_and_unsent_and_order_by_status_then_age(tmp_path):
    today = date(2026, 11, 20)
    df = reminder_df([(1, "Сир 1", "", "+"), (2, "Сир 2", "<p>вже є</p>", "+"), (3, "Сир 3", "", "+"),
                      (4, "Сир 4", "", "+"), (5, "Сир 5", "", "+"), (6, "Сир 6", "", "-"), (7, "Сир 7", "", "+")])
    store = stored(tmp_path, [
        (1, "Сир 1", "лише заголовок", "2026-10-01", True),   # старий, але «лише заголовок» — в кінець
        (2, "Сир 2", "повний", "2026-10-01", True),           # опис уже в Промі — пропустити
        (3, "Сир 3", "повний", "2026-11-15", True),           # надсилали 5 днів тому — рано
        (4, "Сир 4", "повний", "2026-10-20", True),
        (5, "Сир 5", "частковий", "2026-10-10", True),
        (6, "Сир 6", "повний", "2026-10-01", True),           # немає в наявності
        (7, "Сир 7", "повний", "2026-10-25", False),          # ще не надсилали — це «новий», не нагадування
    ])
    got = descriptions.pick_reminders(df, store, 10, today)
    assert [r["code"] for r in got] == [4, 5, 1]
    assert all(r["reminder"] for r in got)
    assert [r["code"] for r in descriptions.pick_reminders(df, store, 2, today)] == [4, 5]
    assert descriptions.pick_reminders(df, store, 0, today) == []


def test_renamed_product_is_not_reminded(tmp_path):
    df = reminder_df([(1, "Сир 1 (нова назва)", "", "+")])
    store = stored(tmp_path, [(1, "Сир 1", "повний", "2026-10-01", True)])
    assert descriptions.pick_reminders(df, store, 5, date(2026, 11, 20)) == []


def test_empty_day_sends_reminders_and_updates_last_sent_only_after_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("DESC_STATE_PATH", str(tmp_path / "d.json"))
    monkeypatch.setenv("DESC_PER_RUN", "2")
    monkeypatch.setenv("DESC_ONLY_KINDS", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")               # Telegram налаштовано: збій відправки не вважається доставкою
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    df = reminder_df([(1, "Сир 1", "", "+"), (2, "Сир 2", "", "+"), (3, "Сир 3", "", "+")])
    monkeypatch.setattr(descriptions, "_load", lambda source, dry_run: df)
    store = stored(tmp_path, [(1, "Сир 1", "повний", "2026-01-01", True), (2, "Сир 2", "повний", "2026-01-02", True),
                              (3, "Сир 3", "повний", "2026-01-03", True)])
    store.save()
    sent, ok = [], {"v": False}
    monkeypatch.setattr(descriptions.notifier, "send_report", lambda text, files=None: sent.append((text, files)) or ok["v"])
    research = tmp_path / "r.json"
    research.write_text("{}", encoding="utf-8")                 # нових сирів немає — лише нагадування

    assert descriptions.ingest_research(str(research)) == 2
    xlsx = pd.read_excel(io.BytesIO(sent[0][1][0][1]))
    assert list(xlsx["Код_товару"]) == [1, 2] and all("нагадування" in x for x in xlsx["Нагадування"])
    assert "нагадувань 2" in sent[0][0]
    assert DescStore(str(tmp_path / "d.json")).data["items"]["1"]["last_sent"] == "2026-01-01"   # не доставлено — не змінюємо

    ok["v"] = True
    descriptions.ingest_research(str(research))
    after = DescStore(str(tmp_path / "d.json")).data["items"]
    assert after["1"]["last_sent"] == after["2"]["last_sent"] == date.today().isoformat() and after["1"]["times_sent"] == 2
    assert after["3"]["last_sent"] == "2026-01-03"
    assert descriptions.ingest_research(str(research)) == 1     # 1 і 2 надіслано сьогодні — лишився тільки сир 3
