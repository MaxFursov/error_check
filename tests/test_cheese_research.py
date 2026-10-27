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


def test_full_result_is_rendered_like_existing_descriptions():
    res = cr.build_result(dict(GOOD), NAME, 150.0, {URL})
    assert res.ok and res.status == "повний" and res.sources == [URL]
    h = res.html
    assert h.startswith("<p><strong>СИР «GOUDA» НАРІЗКА СЛАЙСАМИ</strong></p>\n\n<p><strong>Склад:</strong> молоко")
    assert ("<p><strong>Поживна цінність на 100 г продукту:</strong><br />\n"
            "жири &mdash; 27,0 г,<br />\nз них насичені &mdash; 17,5 г;<br />\n"
            "вуглеводи &mdash; 0,0 г,<br />\nз них цукри &mdash; 0,0 г;<br />\n"
            "білки &mdash; 25,0 г;<br />\nсіль &mdash; 1,8 г.</p>") in h
    assert "<p><strong>Енергетична цінність (калорійність):</strong><br />\n1451 кДж / 350 ккал</p>" in h
    assert "від +2&deg;C до +6&deg;C" in h
    assert "Сир «Gouda» у нарізці" not in h          # коротке summary додається лише коли фактів немає


def test_nothing_found_gives_heading_summary_and_lists_missing():
    res = cr.build_result({"match": "none", "heading": "СИР «GOUDA» НАРІЗКА", "summary": "Сир «Gouda» у нарізці слайсами.",
                           "composition": "вигадано", "fat_g": 30, "sources": [URL]}, NAME, 150.0, {URL})
    assert res.ok and res.status == "лише заголовок" and res.sources == []
    assert "Склад" not in res.html and "вигадано" not in res.html and "Сир «Gouda» у нарізці слайсами." in res.html
    assert res.missing[:2] == ["Склад", "Алергени"]


def test_facts_without_real_sources_are_dropped():
    data = dict(GOOD, sources=["https://invented.example/page"])
    res = cr.build_result(data, NAME, 150.0, {URL})        # цієї адреси не було у видачі пошуку
    assert res.status == "лише заголовок" and "Склад" not in res.html and "джерел" in res.notes
    assert cr.build_result(dict(GOOD, sources=[]), NAME, 150.0, {URL}).status == "лише заголовок"


def test_implausible_numbers_are_dropped():
    r = cr.build_result(dict(GOOD, kj=900), NAME, 150.0, {URL})          # 900 кДж ≠ 350 ккал
    assert "ккал" not in r.html and "кДж" not in r.html and "не збігаються" in r.notes
    r = cr.build_result(dict(GOOD, saturated_g=40), NAME, 150.0, {URL})  # насичені більше за жири
    assert "насичені" not in r.html and "жири &mdash; 27,0" in r.html
    r = cr.build_result(dict(GOOD, fat_g=60, protein_g=50), NAME, 150.0, {URL})   # сума > 100 г
    assert "Поживна цінність" not in r.html


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
    data = descriptions.to_xlsx([{"uid": 7, "code": 1, "name": NAME, "group": "Г", "available": "+", "html": "<p>x</p>",
                                  "status": "частковий", "missing": ["Склад", "Умови зберігання"], "sources": [URL, URL + "2"],
                                  "notes": "ок"}])
    back = pd.read_excel(io.BytesIO(data))
    assert list(back.columns) == descriptions.COLS_RESEARCH
    assert back.iloc[0]["Статус"] == "частковий" and back.iloc[0]["Не знайдено — дописати вручну"] == "Склад, Умови зберігання"
    assert URL in back.iloc[0]["Джерела (перевірте перед публікацією)"]
