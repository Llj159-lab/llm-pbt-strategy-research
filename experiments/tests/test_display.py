from eval.display import _fmt_optional_percentage


def test_format_optional_percentage_for_numeric_value():
    assert _fmt_optional_percentage(0.375) == "38%"


def test_format_optional_percentage_for_unavailable_value():
    assert _fmt_optional_percentage(None) == "-"
