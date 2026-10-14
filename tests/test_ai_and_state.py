from types import SimpleNamespace

import ai_check
from state import State


class FakeClient:
    def __init__(self, text):
        self.messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)]))


def test_ai_response_parsed_and_invented_codes_dropped():
    reply = ('Ось результат:\n[{"code": 5, "problem": "Друкарська помилка: Camambert", '
             '"suggestion": "Camembert", "severity": "error"},'
             ' {"code": 999, "problem": "вигадано", "severity": "error"}]')
    issues = ai_check.check_batch(FakeClient(reply), "m", [(5, "Сир Camambert", "Г")])
    assert [(i.code, i.severity, i.suggestion) for i in issues] == [(5, "error", "Camembert")]


def test_ai_garbage_reply_is_ignored():
    assert ai_check.check_batch(FakeClient("все гаразд"), "m", [(1, "Сир", "Г")]) == []


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
