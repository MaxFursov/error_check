import io
from types import SimpleNamespace

import pandas as pd

import ai_check
import descriptions
from descriptions import DescStore


def make_df(rows):
    """rows: (code, name, group, desc, available)."""
    df = pd.DataFrame(rows, columns=["code", "name", "group", "desc", "available"])
    df["uid"] = df["code"] + 1000
    df["weight"] = float("nan")
    df["archived"] = df["group"].str.startswith("Я!")
    return df


class FakeClient:
    def __init__(self, text, in_tok=2000, out_tok=500, stop_reason="end_turn"):
        self.calls, self.prompts = 0, []

        def create(**kw):
            self.calls += 1
            self.prompts.append(kw["messages"][0]["content"])
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason,
                                   usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok))
        self.messages = SimpleNamespace(create=create)


GOOD = ("1 | КОВБАСА «ДОМАШНЯ» СИРОКОПЧЕНА | Сирокопчена ковбаса вищого гатунку від ТМ Рикун. "
        "Підходить для нарізки та бутербродів.")


def test_heading_of_takes_first_line_and_skips_composition():
    assert descriptions.heading_of("<p><strong>САРДЕЛЬКИ ДОМАШНІ</strong><br />\nТУ У 10.1</p>") == "САРДЕЛЬКИ ДОМАШНІ"
    assert descriptions.heading_of("<p><strong>Ковбаса &quot;Х&quot; сирокопчена</strong></p>") == 'Ковбаса "Х" сирокопчена'
    assert descriptions.heading_of("<p><strong>Склад:</strong> м'ясо</p>") is None
    assert descriptions.heading_of("<p>ТУ У 10.1 - 2005402955-001</p>") is None


def test_rejects_invented_composition_numbers_and_ads():
    name = "Сир Гауда 48% 200 г Lyubar"
    ok = descriptions.reject_reason(name, None, "СИР «ГАУДА»", "Твердий сир жирністю 48%, фасування 200 г.")
    assert ok is None
    assert descriptions.reject_reason(name, None, "СИР", "Калорійність 350 ккал на 100 г, смачний сир.")
    assert descriptions.reject_reason(name, None, "СИР", "Твердий сир зі складом з молока та закваски.")
    assert "числа" in descriptions.reject_reason(name, None, "СИР", "Твердий сир жирністю 45%, фасування 200 г.")
    assert descriptions.reject_reason(name, None, "СИР", "Натуральний твердий сир від виробника Lyubar.")


def test_weight_column_numbers_are_allowed():
    assert descriptions.reject_reason("Ковбаса Х", 400.0, "КОВБАСА", "Ковбаса вагою 400 г у вакуумній упаковці.") is None


def test_pick_targets_skips_archive_filled_stored_and_puts_stock_first(tmp_path, monkeypatch):
    df = make_df([(1, "Сир А", "Г", "", "-"), (2, "Сир Б", "Г", "", "+"), (3, "Сир В", "Г", "<p>є</p>", "+"),
                  (4, "Сир Г", "Я!Г", "", "+"), (5, "Сир Д", "Г", "", "+")])
    store = DescStore(str(tmp_path / "d.json"))
    store.add(5, 1005, "Сир Д", "Г", "+", "<p>x</p>")
    got, left = descriptions.pick_targets(df, store, 10)
    assert list(got["code"]) == [2] and left == 1                       # лише в наявності
    monkeypatch.setenv("DESC_ONLY_IN_STOCK", "false")
    got, _ = descriptions.pick_targets(df, store, 10)
    assert list(got["code"]) == [2, 1]                                  # у наявності — першими


def test_store_renamed_product_is_regenerated_and_failures_capped(tmp_path):
    store = DescStore(str(tmp_path / "d.json"))
    store.add(1, 1001, "Сир А", "Г", "+", "<p>x</p>")
    store.save()
    store = DescStore(str(tmp_path / "d.json"))
    assert store.has_fresh(1, "Сир А") and not store.has_fresh(1, "Сир А (нова назва)")
    store.fail(2, "Сир Б")
    store.fail(2, "Сир Б")
    assert store.attempts(2, "Сир Б") == 2 and store.attempts(2, "Сир Б2") == 0


def test_generate_keeps_good_rejects_bad_and_ignores_invented_codes(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    reply = (GOOD + "\n"
             "2 | СИР | Сир зі складом з молока та закваски, 350 ккал.\n"
             "999 | СИР | Вигаданий код, якого немає у запиті.")
    client = FakeClient(reply)
    monkeypatch.setattr(ai_check, "_client", lambda: client)
    df = make_df([(1, 'Ковбаса "Домашня" с/к в/г Рикун', "Г", "", "+"), (2, "Сир Х", "Г", "", "+"),
                  (3, 'Ковбаса "Приклад" в/к', "Г", "<p><strong>КОВБАСА «ПРИКЛАД»</strong></p><p>Склад: м'ясо</p>", "+")])
    store = DescStore(str(tmp_path / "d.json"))
    targets, _ = descriptions.pick_targets(df, store, 10)
    budget = ai_check.Budget(5.0, max_out=descriptions.MAX_OUTPUT_TOKENS)
    made, rejected, out_of_budget = descriptions.generate(targets, descriptions.style_examples(df), budget, store)
    assert list(made) == [1] and rejected == 1 and not out_of_budget
    assert made[1].startswith("<p><strong>КОВБАСА «ДОМАШНЯ» СИРОКОПЧЕНА</strong></p>")
    assert store.has_fresh(1, df.name[0]) and store.attempts(2, "Сир Х") == 1
    assert "КОВБАСА «ПРИКЛАД»" in client.prompts[0]                      # зразок стилю потрапив у запит
    assert "Склад: м'ясо" not in client.prompts[0]                        # а склад із зразка — ні


def test_generate_stops_before_exceeding_budget(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = FakeClient("", in_tok=10_000, out_tok=2_000)               # 0.03 + 0.03 = $0.06 за порцію
    monkeypatch.setattr(ai_check, "_client", lambda: client)
    df = make_df([(i, f"Сир {i}", "Г", "", "+") for i in range(1, 101)])
    budget = ai_check.Budget(0.20, max_out=descriptions.MAX_OUTPUT_TOKENS)
    _, _, out_of_budget = descriptions.generate(df, [], budget, None)
    assert out_of_budget and client.calls < 10


def test_no_api_key_means_nothing_generated(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    df = make_df([(1, "Сир А", "Г", "", "+")])
    assert descriptions.generate(df, [], ai_check.Budget(1.0), None) == ({}, 0, False)


def test_xlsx_has_label_template_column():
    data = descriptions.to_xlsx([{"uid": 7, "code": 1, "name": "Сир А ", "group": "Г", "available": "+",
                                  "html": "<p>x</p>"}])
    back = pd.read_excel(io.BytesIO(data))
    assert list(back.columns) == descriptions.COLS
    assert back.iloc[0]["ID"] == 7 and "Склад:" in back.iloc[0][descriptions.COLS[-1]]


def test_run_sends_xlsx_and_marks_sent_only_after_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("DESC_STATE_PATH", str(tmp_path / "d.json"))
    monkeypatch.setattr(ai_check, "_client", lambda: FakeClient(GOOD))
    df = make_df([(1, 'Ковбаса "Домашня" с/к в/г Рикун', "Г", "", "+")])
    monkeypatch.setattr(descriptions.loader, "read_catalog", lambda data: df)
    src = tmp_path / "x.xlsx"
    src.write_bytes(b"x")
    sent = []
    result = {"ok": False}
    monkeypatch.setattr(descriptions.notifier, "send_report",
                        lambda text, files=None: sent.append((text, files)) or result["ok"])

    assert descriptions.run_descriptions(str(src)) == 1
    assert sent[0][1][0][0].startswith("descriptions_") and sent[0][1][0][0].endswith(".xlsx")
    assert DescStore(str(tmp_path / "d.json")).unsent()                   # Telegram не прийняв — лишається

    result["ok"] = True
    assert descriptions.run_descriptions(str(src)) == 1                   # той самий файл надсилається знову
    assert not DescStore(str(tmp_path / "d.json")).unsent()
    assert descriptions.run_descriptions(str(src)) == 0                   # далі нових немає
