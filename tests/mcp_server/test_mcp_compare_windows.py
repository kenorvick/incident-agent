def row(text: str, label: str) -> str:
    return next(ln for ln in text.splitlines() if ln.startswith(label))


def test_at_splits_into_equal_windows(tiny, call):
    text, err = call(tiny, "compare_windows", at="21:04:30")
    assert not err
    assert "before 2026-10-19T21:00:00Z .. 2026-10-19T21:04:30Z (270s)" in text
    assert "after 2026-10-19T21:04:30Z .. 2026-10-19T21:09:00Z (270s)" in text
    all_row = row(text, "(all)")
    assert "0% -> " in all_row and "pp)" in all_row  # error rate jumped from zero


def test_group_by_ranks_biggest_mover_first(big, call):
    text, err = call(big, "compare_windows", at="21:05:00", group_by="service.name")
    assert not err
    body = text.split("<telemetry_data>")[1].split("</telemetry_data>")[0]
    lines = [ln for ln in body.splitlines() if "->" in ln]
    names = [ln.split()[0] for ln in lines]
    assert names[0] == "(all)" and names[1] == "checkout"  # biggest error-rate change
    assert "All 6 group(s) shown" in text
    assert "+9.73pp" in row(text, "checkout") or "pp)" in row(text, "checkout")


def test_truncation_note(big, call):
    text, _ = call(big, "compare_windows", at="21:05:00", group_by="service.name", limit=2)
    assert "Truncated: showing the 2 biggest movers of 6" in text and "limit" in text


def test_explicit_windows_and_custom_metric(tiny, call):
    text, err = call(
        tiny,
        "compare_windows",
        before={"start": "21:00:00", "end": "21:03:00"},
        after={"start": "21:07:00", "end": "21:09:00"},
        calculations=[{"op": "P95", "field": "duration_ms"}],
        group_by="service.name",
    )
    assert not err and "explicit windows" in text and "p95(duration_ms)" in text
    assert "payment" in text


def test_small_windows_are_flagged(tiny, call):
    text, _ = call(tiny, "compare_windows", at="21:04:30", group_by="service.name")
    assert "* fewer than 10 events" in text


def test_unknown_field_error(tiny, call):
    text, err = call(tiny, "compare_windows", at="21:04:30", group_by="service.nam")
    assert err and "service.name" in text and "describe_dataset" in text


def test_bad_type_error(tiny, call):
    text, err = call(
        tiny,
        "compare_windows",
        at="21:04:30",
        calculations=[{"op": "AVG", "field": "payment.provider"}],
    )
    assert err and "numeric" in text


def test_missing_time_and_bad_time_errors_say_what_to_give(tiny, call):
    text, err = call(tiny, "compare_windows")
    assert err and "give `at`" in text
    text, err = call(tiny, "compare_windows", at="yesterday-ish")
    assert err and "Cannot parse" in text
    text, err = call(tiny, "compare_windows", at="2026-10-19T23:00:00Z")
    assert err and "leaves no data on one side" in text


def test_empty_side_is_explained(tiny, call):
    text, err = call(
        tiny,
        "compare_windows",
        at="21:04:30",
        filters=[{"field": "payment.provider", "value": "adyen"}],
    )
    assert not err and "No events match in the before window" in text
