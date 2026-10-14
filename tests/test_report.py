from datetime import datetime

import checker
import notifier
from rules import ERROR, INFO, WARNING, Issue


def make_issues(n_per_rule=200):
    out = []
    for rule, sev in (("mixed_alphabet", ERROR), ("ai_review", ERROR), ("missing_price", ERROR),
                      ("price_outlier", WARNING), ("quotes_brackets", ERROR), ("whitespace", INFO),
                      ("missing_image", INFO), ("glued_words", INFO)):
        for k in range(n_per_rule):
            out.append(Issue(k, f"Сир плавлений дуже довга назва товару номер {k} PRESIDENT", rule, sev,
                             "Дуже довге пояснення проблеми, яке займає багато місця " * 3, "Cир → Сир"))
    return out


def test_big_report_fits_into_single_telegram_message_with_clear_borders():
    issues = make_issues()
    keys = {i.key for i in issues}
    text = checker.format_report(issues, keys, 2577, footer="🤖 Claude: ≈$0.7",
                                 when=datetime(2026, 10, 14, 8, 0))
    assert len(text) <= 4096
    assert len(notifier.split_message(text)) == 1            # одне повідомлення
    assert text.startswith(checker.LINE) and "ЗВІТ ПРО ПОМИЛКИ" in text
    assert text.rstrip().endswith(checker.LINE) and "КІНЕЦЬ ЗВІТУ" in text
    assert "у файлах нижче" in text


def test_small_report_lists_examples_per_category():
    issues = [Issue(50, "Cир плавлений", "mixed_alphabet", ERROR, "змішано", "Cир → Сир")]
    text = checker.format_report(issues, {issues[0].key}, 100)
    assert "<code>50</code>" in text and "Cир → Сир" in text


def test_files_are_new_errors_plus_all_open():
    old = Issue(1, "Старий", "short_name", ERROR, "x")
    new = Issue(2, "Новий", "short_name", ERROR, "y")
    files = checker.build_files([old, new], {new.key}, datetime(2026, 10, 14))
    assert [f[0] for f in files] == ["new_errors_2026-10-14.csv", "all_open_2026-10-14.csv"]
    all_open = files[1][1].decode("utf-8-sig")
    assert "Старий" in all_open and "Новий" in all_open
    assert "Старий" not in files[0][1].decode("utf-8-sig")


def test_single_file_when_everything_is_new():
    i = Issue(1, "Назва", "short_name", ERROR, "x")
    assert len(checker.build_files([i], {i.key})) == 1
