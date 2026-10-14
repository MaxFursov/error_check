import pandas as pd

import rules


def make(*rows):
    """rows: (code, name, price, unit, group, available)"""
    df = pd.DataFrame(rows, columns=["code", "name", "price", "unit", "group", "available"])
    df["image"] = "http://img"
    df["qty"] = None
    df["archived"] = df["group"].str.startswith("Я!")
    return df


def found(df, rule):
    return [i for i in rules.run_all(df) if i.rule == rule]


def test_latin_c_in_syr_is_error_with_fix():
    # перша «C» — латинська
    df = make((1, "Cир плавлений 70 г/шт PRESIDENT", 80, "шт", "Г", "+"))
    [issue] = found(df, "mixed_alphabet")
    assert issue.severity == rules.ERROR
    assert issue.suggestion == "Cир → Сир"
    assert "C" in issue.suggestion.split(" → ")[0] and "С" in issue.suggestion.split(" → ")[1]


def test_cyrillic_o_inside_latin_brand_is_fixed_to_latin():
    df = make((1, "Сир Gudbrand 35% Synnоve", 300, "шт", "Г", "+"))  # кирилична «о»
    [issue] = found(df, "mixed_alphabet")
    assert issue.suggestion.endswith("Synnove")


def test_stylised_brands_are_whitelisted():
    df = make((1, "Снеки м'ясні DELICATE с/к ЇSTA", 70, "шт", "Г", "+"),
              (2, "Рулет Свинний в/к М'ЯСКОFF", 90, "кг", "Г", "+"))
    assert found(df, "mixed_alphabet") == []


def test_unfixable_mixed_word_is_warning_not_error():
    df = make((1, "Йогурт Deліссімо 6,3% DANONE", 30, "шт", "Г", "+"))
    [issue] = found(df, "mixed_alphabet")
    assert issue.severity == rules.WARNING


def test_young_cheese_age_flagged_as_brand_confusion():
    df = make((1, 'Сир "Gouda Holland Cheese" (Гауда) 48% YOUNG', 500, "кг", "Г", "+"))
    [issue] = found(df, "descriptor_as_brand")
    assert "YOUNG" in issue.message


def test_real_brand_with_premium_is_not_flagged():
    df = make((1, "Сир Gouda з кмином 48% Mill`s Premium", 500, "кг", "Г", "+"),
              (2, 'Ікра осетрова 100 г "OSETRA PREMIUM"', 3000, "шт", "Г", "+"))
    assert found(df, "descriptor_as_brand") == []


def test_age_in_months_then_descriptor():
    df = make((1, "Сир Edam 12 місяців OLD", 500, "кг", "Г", "+"))
    assert len(found(df, "descriptor_as_brand")) == 1


def test_broken_quotes_and_brackets():
    df = make((1, "Сир плавлений тостовий \"EDAM 130 г/шт Lactima", 50, "шт", "Г", "+"),
              (2, "ковбаса 'Баликова\" в/к", 50, "кг", "Г", "+"),
              (3, 'Тушонка "Свинина"скло) 500гр', 50, "шт", "Г", "+"),
              (4, "Ковбаса \"Дитяча\" вар. м'ясна", 50, "кг", "Г", "+"))
    codes = {i.code for i in found(df, "quotes_brackets")}
    assert codes == {1, 2, 3}  # звичайний апостроф у «м'ясна» — не помилка


def test_missing_price_severity_depends_on_stock():
    df = make((1, "Паштет Печінковий Вербена", None, "шт", "Г", "+"),
              (2, "Паштет Курячий Вербена", None, "шт", "Г", "-"))
    sev = {i.code: i.severity for i in found(df, "missing_price")}
    assert sev == {1: rules.ERROR, 2: rules.WARNING}


def test_short_name():
    df = make((1, "мас", 10, "шт", "Г", "-"), (2, "Ковбаса Київська с/к", 10, "кг", "Г", "+"))
    assert [i.code for i in found(df, "short_name")] == [1]


def test_duplicate_code_and_name_ignore_archive():
    df = make((1, "Ковбаса Київська с/к Довгошия", 380, "кг", "Довгошия", "+"),
              (2, "Ковбаса  «Київська» с/к Довгошия", 380, "кг", "Довгошия", "+"),
              (3, "Ковбаса Архівна Тест", 380, "кг", "Я!Архів", "-"),
              (3, "Ковбаса Архівна Тест", 380, "кг", "Я!Архів", "-"),
              (7, "Сир Один", 1, "кг", "Г", "+"), (7, "Сир Два", 1, "кг", "Г", "+"))
    assert {i.code for i in found(df, "duplicate_name")} == {1, 2}
    assert {i.code for i in found(df, "duplicate_code")} == {7}


def test_price_outlier_per_pack_in_kg_unit():
    rows = [(i, f"Ковбаса Тест{i} в/г", 800 + i, "кг", "Алан", "+") for i in range(10)]
    rows.append((100, "Ковбаса Нарізка 80 г/шт Алан", 107, "кг", "Алан", "+"))
    [issue] = found(make(*rows), "price_outlier")
    assert issue.code == 100 and "упаковку" in issue.message


def test_price_jump_against_previous_run():
    df = make((1, "Ковбаса Київська с/к", 200, "кг", "Г", "+"))
    iss = rules.run_all(df, prev_prices={1: 100.0})
    assert any(i.rule == "price_jump" for i in iss)
    assert not any(i.rule == "price_jump" for i in rules.run_all(df, prev_prices={1: 190.0}))


def test_whitespace_is_info_only():
    df = make((1, "Сир  Гауда 45% ", 100, "кг", "Г", "+"))
    [issue] = found(df, "whitespace")
    assert issue.severity == rules.INFO and issue.suggestion == "Сир Гауда 45%"
