from mcp_helpers import approx_tokens

ERROR = {"field": "error", "op": "=", "value": True}


def ranked_fields(text: str) -> list[str]:
    return [ln.split(". ", 1)[1].split("  (")[0] for ln in text.splitlines() if ln[:1].isdigit()]


def test_tiny_error_selection(tiny, call):
    text, err = call(tiny, "find_outliers", selection=[ERROR])
    assert not err
    assert "selection `error = true` -> 9 events" in text
    assert "payment.provider" in text and "adyen" in text
    assert "Example selected events" in text and "trace_id=" in text
    assert "<telemetry_data>" in text and "Untrusted" in text


def test_fault_attribute_with_high_cardinality_ranks_first(big, call):
    text, err = call(big, "find_outliers", selection=[ERROR])
    assert not err
    fields = ranked_fields(text)
    assert fields[0] == "app.tenant.id"
    entry = text.split("1. app.tenant.id")[1].split("\n2. ")[0]
    assert "tenant-042" in entry and "selection 99" in entry and "baseline 7" in entry


def test_scoped_selection_gets_a_scoped_baseline(big, call):
    text, err = call(
        big,
        "find_outliers",
        selection=[{"field": "service.name", "value": "payment"}, ERROR],
    )
    assert not err
    # healthy payment spans, not every other service
    assert "baseline (auto, same scope)" in text and "`error = false`" in text
    assert "CardProcessorTimeout" in text
    assert "app.tenant.id" in text and "tenant-042" in text


def test_perfect_separators_are_collapsed_and_definitional_fields_dropped(big, call):
    text, _ = call(
        big,
        "find_outliers",
        selection=[{"field": "service.name", "value": "payment"}, ERROR],
    )
    assert "Perfect separators" in text
    perfect = text.split("Perfect separators")[1].split("Ranked attributes")[0]
    assert "exception.type=CardProcessorTimeout" in perfect
    assert "status_code" not in text  # restates `error = true`


def test_same_split_attributes_are_merged(big, call):
    text, _ = call(big, "find_outliers", selection=[ERROR], max_attributes=20)
    assert "same split:" in text
    merged = text.split("same split:")[1].splitlines()[0]
    assert "exception.type" in merged or "span_event_count" in merged


def test_selection_spanning_services_gets_a_where_line(big, call):
    text, _ = call(big, "find_outliers", selection=[ERROR])
    assert "Selected events by service.name:" in text and "add a service.name condition" in text


def test_numeric_fields_show_stats(big, call):
    text, _ = call(
        big,
        "find_outliers",
        selection=[{"field": "service.name", "value": "payment"}, ERROR],
        max_attributes=20,
    )
    # duration_ms is compared although it is near-unique per event (backend gap, worked around)
    entry = text.split("duration_ms  (float")[1].split("\n")[0:3]
    assert any("p50" in ln and "p95" in ln and "max" in ln for ln in entry)


def test_before_after_mode(big, call):
    text, err = call(
        big,
        "find_outliers",
        selection=[
            {"field": "app.tenant.id", "value": "tenant-042"},
            {"field": "name", "value": "POST /charge"},
        ],
        start="21:06:00",
        baseline_start="21:00:00",
        baseline_end="21:04:00",
    )
    assert not err and "same selection in" in text
    assert "exception.type=CardProcessorTimeout" in text


def test_nothing_stands_out_on_a_healthy_selection(big, call):
    text, err = call(
        big,
        "find_outliers",
        selection=[{"field": "service.name", "value": "cart"}],
        start="21:08:00",
        baseline_start="21:00:00",
        baseline_end="21:02:00",
    )
    assert not err
    assert "No attribute separates the selection from the baseline" in text
    assert "normal background" in text


def test_truncation_states_how_to_narrow(big, call):
    text, _ = call(big, "find_outliers", selection=[ERROR], max_attributes=2)
    assert "Truncated: showing 2 of" in text and "max_attributes" in text and "`fields`" in text
    assert len(ranked_fields(text)) == 2


def test_max_values_caps_values(big, call):
    text, _ = call(big, "find_outliers", selection=[ERROR], max_values=1, max_attributes=3)
    entry = text.split("1. ")[1].split("\n2. ")[0]
    assert len([ln for ln in entry.splitlines() if "selection" in ln and "%" in ln]) <= 1


def test_unknown_field_error_suggests(tiny, call):
    text, err = call(tiny, "find_outliers", selection=[{"field": "payment.provder", "value": "x"}])
    assert err and "payment.provider" in text and "describe_dataset" in text


def test_unknown_field_in_fields_argument(tiny, call):
    text, err = call(tiny, "find_outliers", selection=[ERROR], fields=["usr_id"])
    assert err and "user_id" in text


def test_empty_selection_explains(tiny, call):
    text, err = call(
        tiny,
        "find_outliers",
        selection=[
            {"field": "service.name", "value": "checkout"},
            {"field": "duration_ms", "op": ">", "value": 99999},
        ],
    )
    assert not err and "matches no events" in text and "dead end" in text


def test_bad_type_error(tiny, call):
    text, err = call(
        tiny, "find_outliers", selection=[{"field": "service.name", "op": ">", "value": 3}]
    )
    assert err and "numeric or timestamp" in text
    text, err = call(tiny, "find_outliers", selection=[])
    assert err and "selection" in text


def test_empty_baseline_is_explained(tiny, call):
    text, err = call(tiny, "find_outliers", selection=[{"field": "service.name", "op": "exists"}])
    assert not err and "baseline is empty" in text


def test_small_selection_is_flagged(tiny, call):
    text, _ = call(tiny, "find_outliers", selection=[ERROR])
    assert "only 9 selected events" in text


def test_response_is_small(big, call):
    text, _ = call(big, "find_outliers", selection=[ERROR], max_attributes=20, max_values=8)
    assert approx_tokens(text) < 2000
