from mcp_helpers import approx_tokens


def test_happy_path(tiny, call):
    text, err = call(tiny, "describe_dataset")
    assert not err
    assert "44 events" in text and "2 services" in text
    assert "checkout 24" in text and "payment 20" in text
    assert "<telemetry_data>" in text and "Untrusted" in text
    assert "payment.provider" in text and "values: stripe, adyen" in text
    assert "duration_ms" in text and "p95" in text  # numeric scale shown
    assert approx_tokens(text) < 1000


def test_truncation_says_how_to_see_more(tiny, call):
    text, err = call(tiny, "describe_dataset", max_fields=5)
    assert not err
    assert "5 of " in text and "more fields not shown" in text and "name_contains" in text


def test_name_contains_searches_fields(tiny, call):
    text, _ = call(tiny, "describe_dataset", name_contains="USER")
    assert "user_id" in text and "service.name" not in text.split("</telemetry_data>")[0]


def test_empty_result_suggests_next_step(tiny, call):
    text, err = call(tiny, "describe_dataset", name_contains="zzz")
    assert not err and "No field name contains that text" in text


def test_bad_type_is_a_fixable_error(tiny, call):
    text, err = call(tiny, "describe_dataset", max_fields="lots")
    assert err and "max_fields" in text and "Fix them" in text


def test_unknown_argument_is_reported(tiny, call):
    text, err = call(tiny, "describe_dataset", fields=["x"])
    assert err and "fields" in text


def test_id_like_fields_are_flagged(big, call):
    text, _ = call(big, "describe_dataset", name_contains="user")
    assert "high-cardinality id" in text
