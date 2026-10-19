from mcp_helpers import approx_tokens


def test_happy_path_counts_and_rates(tiny, call):
    text, err = call(
        tiny,
        "run_query",
        calculations=[{"op": "COUNT"}, {"op": "P95", "field": "duration_ms"}, {"op": "ERROR_RATE"}],
        group_by=["service.name"],
    )
    assert not err
    assert "44 events matched" in text
    rows = {
        ln.split()[0]: ln.split()
        for ln in text.splitlines()
        if ln.startswith(("checkout", "payment"))
    }
    assert rows["checkout"][1] == "24" and rows["payment"][1] == "20"
    assert "All 2 group(s) shown." in text
    assert "<telemetry_data>" in text


def test_defaults_to_count_without_group(tiny, call):
    text, err = call(tiny, "run_query")
    assert not err and "(all)" in text and "44" in text


def test_lowercase_ops_and_aliases_are_accepted(tiny, call):
    text, err = call(
        tiny,
        "run_query",
        calculations=[{"op": "median", "field": "duration_ms"}],
        filters=[{"field": "service.name", "op": "==", "value": "payment"}],
    )
    assert not err and "p50(duration_ms)" in text


def test_truncation_note_says_how_to_narrow(tiny, call):
    text, err = call(tiny, "run_query", group_by=["name"], limit=2)
    assert not err
    assert "Truncated: showing 2 of 5 groups" in text and "filters" in text and "limit" in text


def test_unknown_field_error_suggests_and_points_to_describe(tiny, call):
    text, err = call(tiny, "run_query", group_by=["service.nam"])
    assert err and "`service.name`" in text and "describe_dataset" in text


def test_unknown_filter_value_suggests_close_values(tiny, call):
    text, err = call(tiny, "run_query", filters=[{"field": "service.name", "value": "chekout"}])
    assert not err  # an empty result is an answer, not a failure
    assert "No events matched" in text and 'did you mean "checkout"' in text


def test_empty_window_is_explained(tiny, call):
    text, err = call(tiny, "run_query", start="2027-01-01T00:00:00Z")
    assert err and "outside the dataset's time range" in text


def test_empty_result_names_the_dead_end_filter(tiny, call):
    text, _ = call(
        tiny,
        "run_query",
        filters=[
            {"field": "service.name", "value": "payment"},
            {"field": "duration_ms", "op": ">", "value": 99999},
        ],
    )
    assert "alone matches 0 events (dead end)" in text


def test_bad_type_lists_valid_fields(tiny, call):
    text, err = call(tiny, "run_query", calculations=[{"op": "P95", "field": "service.name"}])
    assert err and "numeric" in text and "duration_ms" in text


def test_validation_errors_are_short_and_actionable(tiny, call):
    text, err = call(tiny, "run_query", limit=500, calculations=[{"op": "BOGUS"}])
    assert err and "limit" in text and "calculations[0].op" in text
    assert "pydantic" not in text.lower() and "errors.pydantic.dev" not in text


def test_count_with_field_is_rejected(tiny, call):
    text, err = call(tiny, "run_query", calculations=[{"op": "COUNT", "field": "duration_ms"}])
    assert err and "takes no field" in text


def test_time_series(big, call):
    text, err = call(
        big,
        "run_query",
        calculations=[{"op": "ERROR_RATE"}],
        group_by=["service.name"],
        filters=[{"field": "service.name", "op": "in", "value": ["checkout", "payment"]}],
        granularity_seconds=60,
    )
    assert not err and "per 60s bucket" in text
    first = next(ln for ln in text.splitlines() if ln.startswith("21:00:00"))
    last_fault = next(ln for ln in text.splitlines() if ln.startswith("21:06:00"))
    assert "0%" in first and "0%" not in last_fault.split(None, 1)[1].split()


def test_series_limits_are_explained(big, call):
    text, err = call(big, "run_query", granularity_seconds=1)
    assert err and "granularity_seconds >=" in text
    text, err = call(
        big,
        "run_query",
        calculations=[{"op": "COUNT"}, {"op": "ERROR_RATE"}],
        granularity_seconds=60,
    )
    assert err and "exactly one calculation" in text


def test_group_by_identifier_warns(big, call):
    text, err = call(big, "run_query", group_by=["trace_id"], limit=3)
    assert not err and "Warning" in text and "find_outliers" in text
    assert "Truncated" in text


def test_response_is_small(big, call):
    text, _ = call(big, "run_query", group_by=["service.name", "name"], limit=50)
    assert approx_tokens(text) < 1500
