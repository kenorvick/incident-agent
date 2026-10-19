"""Anti-fabrication check: does a value the agent cited actually appear in a tool result?

Everything here is deterministic and strict on purpose: the point is to catch made-up numbers,
not to forgive them. A cited value is *grounded* in a tool result's text if either rule holds.

Rule 1, text match (IDs, field values, strings, and numbers written exactly as shown):
  * Both the cited value and the tool result are normalized: Unicode NFKC, case-folded, curly
    quotes straightened, every quote character (`"`, `'`, backtick) removed, whitespace runs
    collapsed to one space, and any `:` or `=` (with optional surrounding spaces) rewritten as
    `=` so `status: ERROR`, `"status": "ERROR"` and `status=error` agree.
  * The normalized value must occur as a substring, but not inside a longer token: the
    characters next to it may not be letters, digits or `_`, and a number may not be glued to
    more digits by `.` or `,` (so `45` does not match `145`, `45.2` or `1,45`).

Rule 2, numeric match (only if the cited value parses as a plain number):
  Accepted forms: optional sign and currency symbol, digits with optional `,` thousands groups
  and decimals, then an optional unit from a fixed list (`%`, ms, s, us, ns, sec, min, h, b, kb,
  mb, gb, x, rps, ...). The unit is ignored when matching (it is not verified). Integers with a
  leading zero or 13+ digits are treated as IDs and only get rule 1.
  Numbers in the tool result are tokenized the same way (`1,234` is read as 1234, and also as 1
  and 234 since a list is ambiguous; a trailing `%` is remembered; tokens glued to letters,
  such as `p95`, `v1` or the digits inside an ID, are ignored unless the letters are a unit).
  A cited number matches a result number if:
    * they are numerically equal (`45.20` == `45.2`, `1,234` == `1234`), or
    * it is the result number rounded to the cited precision (half-up or half-even, never
      truncation): `45.2` for 45.2371, `1203` for 1203.4. Rounding is only accepted when the
      cited value has a decimal point or at least two significant digits, so `3` can never be
      "3.14 rounded", or
    * percent/fraction forms agree, only for ratios in [0, 1]: cited `45.2%` matches a result
      0.452 (and rounding of it, e.g. `45%`), and a cited fraction 0.452 matches a result
      `45.2%`.
  Anything else, including unit conversions (2.5s vs 2500ms), arithmetic on the data, or
  truncation, is NOT grounded. The prompt tells the agent to cite values exactly as shown.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from functools import cached_property

__all__ = ["Haystack", "ParsedNumber", "normalize_text", "parse_cited_number", "value_grounded"]

_QUOTE_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
_QUOTE_CHARS = re.compile(r"[\"'`]")
_WS = re.compile(r"\s+")
_SEPARATOR = re.compile(r"\s*[:=]\s*")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_QUOTE_MAP).casefold()
    text = _QUOTE_CHARS.sub("", text)
    text = _WS.sub(" ", text).strip()
    return _SEPARATOR.sub("=", text)


# --- numbers -----------------------------------------------------------------------------

_UNITS = frozenset(
    "% ms s us μs ns sec secs seconds min mins h hr hrs b kb mb gb tb kib mib gib x rps qps".split()
)
_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_CITED = re.compile(
    rf"^(?:[$€£])?(?P<sign>[+-])?(?:[$€£])?(?P<num>{_NUMBER})\s*(?P<unit>%|[a-zμ]+)?$"
)
_UNIT_ALT = "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True))
_TOKEN = re.compile(
    rf"(?<![a-z0-9_.])(?P<sign>-)?(?P<num>{_NUMBER})(?P<exp>e[+-]?\d+)?"
    rf"(?:\s?(?P<unit>{_UNIT_ALT}))?(?![a-z0-9_])"
)
_ONE = Decimal(1)


@dataclass(frozen=True)
class ParsedNumber:
    value: Decimal
    decimals: int
    sig_digits: int
    percent: bool = False


def _decimals(d: Decimal) -> int:
    exp = d.as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def parse_cited_number(normalized: str) -> ParsedNumber | None:
    """Parse an already-normalized cited value as a plain number, or None if it is not one."""
    m = _CITED.match(normalized)
    if not m:
        return None
    unit = m.group("unit")
    if unit is not None and unit not in _UNITS:
        return None
    digits = m.group("num").replace(",", "")
    if "." not in digits and (len(digits) > 12 or (len(digits) > 1 and digits[0] == "0")):
        return None  # looks like an ID (or a timestamp), not a quantity
    value = Decimal(digits)
    if m.group("sign") == "-":
        value = -value
    sig = len(digits.replace(".", "").lstrip("0")) or 1
    return ParsedNumber(value, _decimals(value), sig, percent=unit == "%")


def _tokenize(text: str) -> list[ParsedNumber]:
    out: list[ParsedNumber] = []
    for m in _TOKEN.finditer(text):
        raw = m.group("num")
        pct = m.group("unit") == "%"
        pieces = [raw.replace(",", "")]
        if "," in raw:  # `1,234` is 1234 as a number, but `12,345` may be a list of two
            pieces += raw.split(",")
        for piece in pieces:
            try:
                value = Decimal(piece + (m.group("exp") or ""))
            except InvalidOperation:
                continue
            if m.group("sign"):
                value = -value
            out.append(ParsedNumber(value, _decimals(value), 0, percent=pct))
    return out


def _rounds_to(result: Decimal, cited: ParsedNumber) -> bool:
    """`result` equals the cited number, or rounds to it at the cited precision."""
    if result == cited.value:
        return True
    if cited.decimals == 0 and cited.sig_digits < 2:
        return False
    quantum = _ONE.scaleb(-cited.decimals)
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            return cited.value in (
                result.quantize(quantum, rounding=ROUND_HALF_UP),
                result.quantize(quantum, rounding=ROUND_HALF_EVEN),
            )
    except InvalidOperation:
        return False


def _numeric_match(cited: ParsedNumber, tokens: list[ParsedNumber]) -> bool:
    for t in tokens:
        if t.value == cited.value or _rounds_to(t.value, cited):
            return True
        if cited.percent and 0 <= t.value <= 1 and _rounds_to(t.value * 100, cited):
            return True  # cited 45.2%, result 0.452
        if t.percent and 0 <= cited.value <= 1 and _rounds_to(t.value / 100, cited):
            return True  # cited 0.452, result 45.2%
    return False


# --- text --------------------------------------------------------------------------------


def _text_contains(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    pattern = re.escape(needle)
    if needle[0].isalnum() or needle[0] == "_":
        pattern = r"(?<![a-z0-9_])" + (r"(?<!\d[.,])" if needle[0].isdigit() else "") + pattern
    if needle[-1].isalnum() or needle[-1] == "_":
        pattern += r"(?![a-z0-9_])" + (r"(?![.,]\d)" if needle[-1].isdigit() else "")
    return re.search(pattern, haystack) is not None


@dataclass
class Haystack:
    """One tool result's text, normalized once and searched many times."""

    raw: str
    text: str = field(init=False)

    def __post_init__(self) -> None:
        self.text = normalize_text(self.raw)

    @cached_property
    def numbers(self) -> list[ParsedNumber]:
        return _tokenize(self.text)


def value_grounded(value: str, haystack: Haystack) -> bool:
    """True if `value` (as cited by the agent) is found in the tool result under the rules in
    the module docstring."""
    needle = normalize_text(value)
    if _text_contains(haystack.text, needle):
        return True
    parsed = parse_cited_number(needle)
    return parsed is not None and _numeric_match(parsed, haystack.numbers)
