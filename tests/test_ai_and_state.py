from types import SimpleNamespace

import ai_check
from state import State


class FakeClient:
    def __init__(self, text, in_tok=3000, out_tok=200, stop_reason="end_turn"):
        self.calls = 0

        def create(**kw):
            self.calls += 1
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason,
                                   usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok))
        self.messages = SimpleNamespace(create=create)


def test_ai_response_parsed_and_invented_codes_dropped():
    reply = ("2680 | Camambert → Camembert | друкарська помилка\n"
             "5 | Camambert → Camembert | друкарська помилка\n"
             "999 | щось → інше | вигадано\n"
             "5 | сир → сир | нічого не змінює")
    issues, *_ = ai_check.check_batch(FakeClient(reply), "m", [(5, "Сир Camambert", "Г"), (2680, "Сир Camambert GRIL", "Г")])
    assert sorted((i.code, i.suggestion) for i in issues) == [
        (5, "Camambert → Camembert"), (2680, "Camambert → Camembert")]


def test_ai_none_and_prose_replies_give_no_issues():
    for reply in ("NONE", "все гаразд", "```json\n[]\n```"):
        assert ai_check.check_batch(FakeClient(reply), "m", [(1, "Сир", "Г")])[0] == []


def test_state_only_rechecks_new_or_changed_names(tmp_path):
    rows = [(1, "Сир А", "Г"), (2, "Сир Б", "Г")]
    st = State(str(tmp_path / "s.json"))
    assert st.first_run and st.names_to_check(rows) == rows
    st.mark_checked(rows, {1, 2})
    st.save(["k1"], {1: 10.0})
    st2 = State(str(tmp_path / "s.json"))
    assert not st2.first_run and st2.prices == {1: 10.0}
    assert st2.names_to_check([(1, "Сир А", "Г"), (2, "Сир Б (змінено)", "Г")]) == [(2, "Сир Б (змінено)", "Г")]


def test_fixed_issue_is_reported_again_if_it_returns(tmp_path):
    st = State(str(tmp_path / "s.json"))
    assert st.new_issues(["a"]) == {"a"}
    st.save(["a"], {})
    assert State(str(tmp_path / "s.json")).new_issues(["a"]) == set()
    st.save([], {})  # виправили
    assert State(str(tmp_path / "s.json")).new_issues(["a"]) == {"a"}


def test_budget_counts_real_usage_and_stops_before_exceeding(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = FakeClient("[]", in_tok=10_000, out_tok=1_000)   # 0.03 + 0.015 = $0.045 за порцію
    monkeypatch.setattr(ai_check, "_client", lambda: client)
    items = [(i, f"Сир Тест {i}", "Г") for i in range(ai_check.BATCH_SIZE * 10)]
    budget = ai_check.Budget(limit_usd=0.20)
    issues, done, out_of_budget = ai_check.check_names(items, budget)
    assert out_of_budget and client.calls < 10            # не пройшли всі 10 порцій
    assert budget.spent <= 0.20                            # ліміт ніколи не перевищено
    assert len(done) == client.calls * ai_check.BATCH_SIZE # недоперевірені не позначені як готові


def test_budget_already_spent_makes_no_calls(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = FakeClient("[]")
    monkeypatch.setattr(ai_check, "_client", lambda: client)
    _, done, out_of_budget = ai_check.check_names([(1, "Сир Тест", "Г")], ai_check.Budget(1.0, spent_usd=0.999))
    assert client.calls == 0 and out_of_budget and not done


def test_spend_is_tracked_per_month(tmp_path):
    from datetime import date
    st = State(str(tmp_path / "s.json"))
    st.set_ai_spent(0.42, today=date(2026, 10, 14))
    st.save_cache()
    st = State(str(tmp_path / "s.json"))
    assert st.ai_spent_this_month(date(2026, 10, 30)) == 0.42
    assert st.ai_spent_this_month(date(2026, 11, 1)) == 0.0   # новий місяць — новий ліміт


def test_truncated_response_is_not_marked_done_and_run_stops(monkeypatch):
    """Регресія: відповідь, обрізана по max_tokens, давала [] і назви вважались перевіреними,
    а гроші витрачались даремно."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = FakeClient("1 | Сир → Сир", in_tok=3000, out_tok=1200, stop_reason="max_tokens")
    monkeypatch.setattr(ai_check, "_client", lambda: client)
    items = [(i, f"Сир Тест {i}", "Г") for i in range(ai_check.BATCH_SIZE * 5)]
    budget = ai_check.Budget(limit_usd=10)
    issues, done, out_of_budget = ai_check.check_names(items, budget)
    assert done == set() and issues == []
    assert client.calls == 2                 # зупинились після двох обрізань поспіль
    assert budget.spent > 0                  # але витрати враховано


def test_thinking_is_disabled(monkeypatch):
    seen = {}
    client = FakeClient("[]")
    orig = client.messages.create
    client.messages.create = lambda **kw: (seen.update(kw), orig(**kw))[1]
    ai_check.check_batch(client, "m", [(1, "Сир Тест", "Г")])
    assert seen["thinking"] == {"type": "disabled"}
