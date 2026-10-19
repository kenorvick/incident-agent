"""Shared helpers: schema-based validation, value coercion, and BubbleUp scoring.

DESIGN: how `compare_distributions` scores a field
--------------------------------------------------
For each field we build the distribution of its values in the selection and in the baseline.
Fractions are of *all* rows in each set, and "field absent" is its own bucket, so a field that
exists only on the selection's rows (e.g. `exception.type` on failing spans) is a strong signal.

score = max over values v of  max(0, |p_sel(v) - p_base(v)| - z * se_v)

i.e. the largest single-value fraction difference, shrunk by a lower confidence bound (z = 1.64,
Agresti-Coull standard error). Why not total-variation distance over the whole distribution: for
high-cardinality fields (`user_id`, `cart_id`) a small selection lands on mostly-unique values,
so TVD is ~1 for pure noise. The max single-value difference is insensitive to cardinality and
the confidence bound suppresses small-sample flukes.

Numeric fields with many distinct values (floats like `duration_ms`, or integers with more than
`NUMERIC_BUCKET_MIN_DISTINCT` distinct values) are not grouped by raw value. Instead they are cut
into `NUMERIC_BUCKETS` quantile buckets whose edges are the deciles of the combined
selection+baseline rows (so resolution is spent where the data is), and each bucket is scored like
a categorical value; the field's score is the larger of that and a KS-style score (largest gap
between the two cumulative bucket distributions, same confidence bound), so a selection that
sits in the upper tail scores ~1 however many buckets it spans. Summary stats
(count/mean/p50/p90/p95/max) for both sets are returned as well, so the caller can say
"p95 2300 ms vs 120 ms". Low-cardinality integers (status codes, counts) are treated as
categorical.
"""

import difflib
import math
import re
from collections.abc import Collection, Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from incident_agent.backends.base import (
    MAX_BUCKETS,
    MAX_GROUP_LIMIT,
    NO_FIELD_CALCS,
    NUMERIC_CALCS,
    Calculation,
    DatasetSchema,
    FieldInfo,
    FieldType,
    FieldTypeError,
    Filter,
    FilterOp,
    InvalidQueryError,
    OrderBy,
    ResolvedWindow,
    Scalar,
    UnknownFieldError,
)

NUMERIC_BUCKETS = 10
NUMERIC_BUCKET_MIN_DISTINCT = 20
SCORE_Z = 1.64
# Never useful for distribution comparison (unique per row / the time axis itself).
NEVER_COMPARE = frozenset({"timestamp", "trace_id", "span_id", "parent_span_id"})
NEAR_UNIQUE_RATIO = 0.9
NEAR_UNIQUE_MIN_ROWS = 100


def naive_utc(dt: datetime) -> datetime:
    """DuckDB TIMESTAMP columns are naive UTC; bind naive UTC to avoid session-tz surprises."""
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def suggest_fields(name: str, known: Sequence[str], n: int = 3) -> list[str]:
    """Close matches for a misspelled/abbreviated field name (difflib + loose matching)."""
    out = difflib.get_close_matches(name, known, n=n, cutoff=0.6)
    target = _norm(name)
    if target:
        for k in known:
            if k in out:
                continue
            last = _norm(k.rsplit(".", 1)[-1])
            if _norm(k) == target or last == target or (len(target) >= 4 and target in _norm(k)):
                out.append(k)
    return out[:n]


@dataclass(frozen=True)
class CheckedFilter:
    field: str
    op: FilterOp
    value: Scalar | list[Scalar]


def coerce_value(value: object, ftype: FieldType, field: str) -> Scalar:
    """Coerce a model-supplied value to the field's type ('500' -> 500 for an integer field)."""

    def bad() -> InvalidQueryError:
        return InvalidQueryError(f"value {value!r} is not valid for `{field}` (type {ftype.value})")

    if ftype is FieldType.BOOLEAN:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise bad()
    if ftype.is_numeric:
        if isinstance(value, bool):
            raise bad()
        try:
            num = float(value) if isinstance(value, (int, float, str)) else None  # type: ignore[arg-type]
        except ValueError:
            num = None
        if num is None or math.isnan(num):
            raise bad()
        if ftype is FieldType.INTEGER and num.is_integer():
            return int(num)
        return num
    if ftype is FieldType.TIMESTAMP:
        if not isinstance(value, str):
            raise bad()
        try:
            return naive_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            raise bad() from None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class FieldCatalog:
    """Validates field names, calcs and filters against a DatasetSchema."""

    def __init__(self, schema: DatasetSchema):
        self._fields = {f.name: f for f in schema.fields}
        self.names = list(self._fields)

    def __contains__(self, name: str) -> bool:
        return name in self._fields

    def get(self, name: str) -> FieldInfo:
        info = self._fields.get(name)
        if info is None:
            raise UnknownFieldError(name, suggest_fields(name, self.names))
        return info

    def check_group_by(self, group_by: Sequence[str]) -> None:
        for name in group_by:
            if self.get(name).type is FieldType.TIMESTAMP:
                raise InvalidQueryError(
                    f"cannot group by `{name}`; use granularity_seconds for time buckets"
                )

    def check_calc(self, calc: Calculation) -> None:
        if calc.op in NO_FIELD_CALCS:
            if calc.op.value == "ERROR_RATE":
                err = self.get("error")
                if err.type is not FieldType.BOOLEAN:
                    raise FieldTypeError("error", err.type, "ERROR_RATE", "a boolean")
            return
        assert calc.field is not None
        info = self.get(calc.field)
        if calc.op in NUMERIC_CALCS and not info.type.is_numeric:
            raise FieldTypeError(calc.field, info.type, calc.op.value, "a numeric (integer/float)")

    def check_filter(self, flt: Filter) -> CheckedFilter:
        info = self.get(flt.field)
        t = info.type
        if flt.op in (FilterOp.EXISTS, FilterOp.DOES_NOT_EXIST):
            return CheckedFilter(flt.field, flt.op, None)
        if flt.op in (FilterOp.CONTAINS, FilterOp.STARTS_WITH):
            if t is not FieldType.STRING or not isinstance(flt.value, str):
                raise FieldTypeError(flt.field, t, f"`{flt.op.value}`", "a string")
            return CheckedFilter(flt.field, flt.op, flt.value)
        if flt.op in (FilterOp.GT, FilterOp.GTE, FilterOp.LT, FilterOp.LTE) and not (
            t.is_numeric or t is FieldType.TIMESTAMP
        ):
            raise FieldTypeError(flt.field, t, f"`{flt.op.value}`", "a numeric or timestamp")
        if isinstance(flt.value, list):
            return CheckedFilter(
                flt.field, flt.op, [coerce_value(v, t, flt.field) for v in flt.value]
            )
        return CheckedFilter(flt.field, flt.op, coerce_value(flt.value, t, flt.field))

    def comparable_fields(
        self,
        schema_rows: int | None,
        requested: Sequence[str] | None,
        filtered: Collection[str] = (),
    ) -> tuple[list[str], dict[str, str]]:
        """Fields to compare plus {skipped field: reason} (explicit requests skip only time)."""
        if requested is not None:
            for name in requested:
                self.get(name)
            kept = [n for n in dict.fromkeys(requested) if n != "timestamp"]
            skipped = (
                {"timestamp": "the time axis; use granularity_seconds"}
                if ("timestamp" in requested)
                else {}
            )
            return kept, skipped
        kept, skipped = [], {}
        for name, info in self._fields.items():
            if name in filtered:
                skipped[name] = "used in the selection filters (tautological)"
            elif name in NEVER_COMPARE:
                skipped[name] = "identifier / time axis (unique per row)"
            elif is_near_unique(info, schema_rows):
                skipped[name] = (
                    "near-unique per row (no distribution signal); pass it in `fields` to force"
                )
            else:
                kept.append(name)
        return kept, skipped


def is_near_unique(info: FieldInfo, rows: int | None) -> bool:
    if not rows or info.cardinality is None or info.fraction_present is None:
        return False
    present = info.fraction_present * rows
    return present >= NEAR_UNIQUE_MIN_ROWS and info.cardinality >= NEAR_UNIQUE_RATIO * present


def is_continuous_numeric(info: FieldInfo) -> bool:
    if info.type is FieldType.FLOAT:
        return True
    return (
        info.type is FieldType.INTEGER
        and info.cardinality is not None
        and info.cardinality > NUMERIC_BUCKET_MIN_DISTINCT
    )


@dataclass(frozen=True)
class ScoredValue:
    key: Hashable
    p_sel: float
    p_base: float
    delta: float
    lcb: float


def score_distribution(
    sel: Mapping[Hashable, int], base: Mapping[Hashable, int], n_sel: int, n_base: int
) -> tuple[float, list[ScoredValue]]:
    """Score a field (see module docstring). Counts must include the absent bucket.

    Returns (score in [0, 1], every value with its fractions, sorted by |delta| descending).
    """
    scored: list[ScoredValue] = []
    for key in sel.keys() | base.keys():
        s, b = sel.get(key, 0), base.get(key, 0)
        ps, pb = (s / n_sel if n_sel else 0.0), (b / n_base if n_base else 0.0)
        scored.append(ScoredValue(key, ps, pb, ps - pb, _lcb(s, n_sel, b, n_base)))
    scored.sort(key=lambda v: (-abs(v.delta), str(v.key)))
    return (max((v.lcb for v in scored), default=0.0), scored)


def _lcb(s: int, n_s: int, b: int, n_b: int) -> float:
    """Lower confidence bound of |s/n_s - b/n_b| (Agresti-Coull standard error)."""
    if not n_s or not n_b:
        return 0.0
    qs, qb = (s + 2) / (n_s + 4), (b + 2) / (n_b + 4)
    se = math.sqrt(qs * (1 - qs) / (n_s + 4) + qb * (1 - qb) / (n_b + 4))
    return max(0.0, abs(s / n_s - b / n_b) - SCORE_Z * se)


def score_ordered(
    sel: Mapping[int, int], base: Mapping[int, int], n_sel: int, n_base: int
) -> float:
    """KS-style score over ordered numeric buckets: max cumulative-fraction gap (lower bound).

    Bucket keys are ints in value order; the absent bucket (-1) is excluded from the CDF. A
    selection that sits entirely in the upper tail scores ~1 however many buckets it spans.
    """
    cs = cb = 0
    best = 0.0
    for k in sorted(sel.keys() | base.keys()):
        if k < 0:
            continue
        cs += sel.get(k, 0)
        cb += base.get(k, 0)
        best = max(best, _lcb(cs, n_sel, cb, n_base))
    return best


def validate_aggregate(
    catalog: FieldCatalog,
    calcs: Sequence[Calculation],
    group_by: Sequence[str],
    order_by: OrderBy | None,
    limit: int,
) -> tuple[list[str], list[str]]:
    """Shared argument validation for `aggregate`. Returns (calc keys, de-duplicated group_by)."""
    if not calcs:
        raise InvalidQueryError("give at least one calculation")
    keys = [c.key for c in calcs]
    if len(set(keys)) != len(keys):
        raise InvalidQueryError("duplicate calculations")
    if not 1 <= limit <= MAX_GROUP_LIMIT:
        raise InvalidQueryError(f"limit must be between 1 and {MAX_GROUP_LIMIT}")
    groups = list(dict.fromkeys(group_by))
    for c in calcs:
        catalog.check_calc(c)
    catalog.check_group_by(groups)
    if order_by is not None and order_by.key not in keys and order_by.key not in groups:
        raise InvalidQueryError(f"cannot order by `{order_by.key}`; valid keys: {keys + groups}")
    return keys, groups


def check_granularity(g: int, win: ResolvedWindow) -> None:
    if g < 1:
        raise InvalidQueryError("granularity_seconds must be >= 1")
    n = math.ceil(win.seconds / g)
    if n > MAX_BUCKETS:
        raise InvalidQueryError(
            f"granularity {g}s over a {win.seconds:.0f}s window gives {n} buckets "
            f"(max {MAX_BUCKETS}); use granularity_seconds >= "
            f"{math.ceil(win.seconds / MAX_BUCKETS)}"
        )


def densify(
    found: Mapping[datetime, dict], calcs: Sequence[Calculation], win: ResolvedWindow, g: int
) -> list[tuple[datetime, dict]]:
    """All bucket starts in the window; empty buckets get 0 for counts and None otherwise."""
    first = int(win.start.timestamp() // g * g)
    empty = {c.key: (0 if c.op.value.startswith("COUNT") else None) for c in calcs}
    out = []
    for t in range(first, math.ceil(win.end.timestamp()), g):
        start = datetime.fromtimestamp(t, UTC)
        out.append((start, found.get(start, dict(empty))))
    return out
