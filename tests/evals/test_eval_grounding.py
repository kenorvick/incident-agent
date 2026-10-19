"""The anti-fabrication matcher: strict on made-up numbers, lenient on formatting."""

from __future__ import annotations

import pytest

from incident_agent.evals.grounding import Haystack, normalize_text, value_grounded

CONTENT = """
{"service.name": "Checkout", "p95_ms": 1203.4567, "error_rate": 0.452, "spans": 1234,
 "latency": "2.5s", "trace_id": "4BF92F3577B34DA6A3CE929D0E0E4736", "status": "ERROR",
 "http.response.status_code": 500, "share": "45.2%", "count": 145, "pi": 3.14159,
 "sizes": [12, 7], "big": "12,345", "app.product.id": "OLJCESPC7Z"}
status_message: "Payment failed: card declined"
"""


def grounded(value: str) -> bool:
    return value_grounded(value, Haystack(CONTENT))


@pytest.mark.parametrize(
    "value",
    [
        "Checkout",
        "checkout",  # case-insensitive
        "4bf92f3577b34da6a3ce929d0e0e4736",  # IDs are case-insensitive
        "OLJCESPC7Z",
        "app.product.id=OLJCESPC7Z",  # separator style differs from the JSON
        '"ERROR"',  # quotes ignored
        "status_message=Payment failed: card declined",
        "payment   failed",  # whitespace collapsed
    ],
)
def test_grounded_text(value):
    assert grounded(value)


@pytest.mark.parametrize(
    "value",
    [
        "1203.4567",  # exact
        "1203.46",  # rounded to the shown precision
        "1203.5",
        "1203",
        "1,234",  # thousands separator in the citation
        "1234",
        "45.2%",  # literal
        "45.20%",  # trailing zero
        "45",  # 45.2% rounded
        "45%",  # percent of the 0.452 ratio, rounded
        "0.452",
        "0.45",  # ratio rounded to two decimals
        "500",
        "2.5s",
        "12345",  # `12,345` in the result
        "12,345",
        "12345.0",  # numerically equal
        "7",
        "500 ms",  # the unit is ignored; the number is checked
    ],
)
def test_grounded_numbers_and_percent_forms(value):
    assert grounded(value)


@pytest.mark.parametrize(
    "value",
    [
        "1203.47",  # 1203.4567 rounds to 1203.46
        "1204",
        "1234.5",  # more precision than the result shows
        "46.2%",
        "0.453",
        "98%",
        "501",
        "3",  # one significant digit is never "3.14159 rounded"
        "2",
        "234",  # inside 12,345 / 1234 but not a token of its own
        "OLJCESPC7",  # prefix of an ID
        "OLJCESPC7Z9",
        "payment succeeded",
        "2500ms",  # unit conversion is not accepted
        "9999",
        "-1203.4567",  # sign matters
        "5xx",
        "14",  # 145 must not match inside a longer number
    ],
)
def test_ungrounded_values(value):
    assert not grounded(value)


def test_rounding_needs_two_significant_digits_or_a_decimal_point():
    h = Haystack("pi is 3.14159 and e is 2.71828")
    assert not value_grounded("3", h)
    assert value_grounded("3.1", h)
    assert value_grounded("3.14", h)
    assert not value_grounded("3.2", h)
    assert value_grounded("2.7", h)


def test_numbers_glued_to_letters_are_not_tokens():
    assert not value_grounded("95", Haystack("p95 latency"))
    assert not value_grounded("2", Haystack("v2 api"))
    assert not value_grounded("95", Haystack("95th percentile"))
    assert value_grounded("p95", Haystack("p95 latency"))
    assert value_grounded("320", Haystack("duration 320ms"))  # a unit suffix is fine


def test_dates_and_ids_are_text_matches():
    h = Haystack("timestamp 2026-10-19T21:00:00.123456Z trace 007abc")
    assert value_grounded("2026-10-19T21:00:00.123456Z", h)
    assert not value_grounded("2026-10-20", h)
    assert not value_grounded("2026-10-19", h)  # glued to the `T`: not a whole token
    assert not value_grounded("007", Haystack("value 7"))  # leading-zero ints are IDs


def test_fraction_percent_conversion_only_for_ratios():
    assert not value_grounded("200%", Haystack("count 2"))
    assert value_grounded("0.452", Haystack("rate 45.2%"))
    assert not value_grounded("4.52", Haystack("rate 45.2%"))


def test_normalize_text():
    assert normalize_text('  "Status":  ERROR\n') == "status=error"
    assert normalize_text("“Hello” ‘x’") == "hello x"
