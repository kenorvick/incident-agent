"""TelemetryBackend protocol plus the Pydantic types and errors every backend shares.

Design notes for callers (the MCP tools):

- Fields use the **canonical wide-event names** of the fixture format (`trace_id`, `service.name`,
  `duration_ms`, `error`, ...). Backends translate to their native names internally.
- Time windows are relative to the **dataset's own end time** (the newest event), not wall-clock.
- Every list-returning call reports how much was cut (`total_*` / `truncated`).
- Every bad input raises a `BackendError` subclass whose message is safe to show the model.
- Values that came from telemetry are untrusted. Backends never interpolate them into SQL or
  query text; they are always bound parameters.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

Scalar = str | int | float | bool | None

MAX_GROUP_LIMIT = 1000
MAX_BUCKETS = 1000


# --------------------------------------------------------------------------- errors


class BackendError(Exception):
    """Base class. `str(e)` is a message that is safe and useful to show the model."""


class UnknownFieldError(BackendError):
    """A field name is not in the dataset schema. Carries close matches."""

    def __init__(self, field: str, suggestions: Sequence[str] = ()):
        self.field = field
        self.suggestions = list(suggestions)
        hint = (
            f"; did you mean {', '.join(f'`{s}`' for s in self.suggestions)}?"
            if self.suggestions
            else ""
        )
        super().__init__(f"field `{field}` not found{hint}")


class FieldTypeError(BackendError):
    """An operation or calculation is not valid for the field's type (e.g. p95 of a string)."""

    def __init__(self, field: str, field_type: "FieldType", operation: str, allowed: str):
        self.field = field
        self.field_type = field_type
        super().__init__(
            f"cannot apply {operation} to `{field}` (type {field_type.value}); "
            f"it needs {allowed} field"
        )


class InvalidQueryError(BackendError):
    """The request is malformed (bad value, bad window, too many buckets, ...)."""


class UnsupportedOperationError(BackendError):
    """The backend cannot do this (documented limitation), as opposed to a bad request."""


class BackendUnavailableError(BackendError):
    """The backend could not be reached or rejected our credentials/permissions."""


# --------------------------------------------------------------------------- enums


class FieldType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    TIMESTAMP = "timestamp"

    @property
    def is_numeric(self) -> bool:
        return self in (FieldType.INTEGER, FieldType.FLOAT)


class Calc(StrEnum):
    COUNT = "COUNT"
    COUNT_DISTINCT = "COUNT_DISTINCT"
    AVG = "AVG"
    SUM = "SUM"
    MIN = "MIN"
    MAX = "MAX"
    P50 = "P50"
    P90 = "P90"
    P95 = "P95"
    P99 = "P99"
    ERROR_RATE = "ERROR_RATE"  # fraction of rows with `error = true`, in [0, 1]


NO_FIELD_CALCS = frozenset({Calc.COUNT, Calc.ERROR_RATE})
NUMERIC_CALCS = frozenset(
    {Calc.AVG, Calc.SUM, Calc.MIN, Calc.MAX, Calc.P50, Calc.P90, Calc.P95, Calc.P99}
)
PERCENTILES: dict[Calc, float] = {Calc.P50: 0.5, Calc.P90: 0.9, Calc.P95: 0.95, Calc.P99: 0.99}


class FilterOp(StrEnum):
    EQ = "="
    NE = "!="
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    IN = "in"
    NOT_IN = "not_in"
    EXISTS = "exists"
    DOES_NOT_EXIST = "does_not_exist"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"


class SampleOrder(StrEnum):
    NEWEST = "newest"
    OLDEST = "oldest"
    SLOWEST = "slowest"  # by duration_ms descending


# --------------------------------------------------------------------------- inputs


class Filter(BaseModel):
    """`field op value`. `exists`/`does_not_exist` take no value; `in`/`not_in` take a list.

    Semantics: `!=` and `not_in` do not match rows where the field is absent (use
    `does_not_exist` for that). `contains` is a case-insensitive substring match on strings.
    """

    field: str
    op: FilterOp = FilterOp.EQ
    value: Scalar | list[str | int | float | bool] = None

    @model_validator(mode="after")
    def _check_value_shape(self) -> "Filter":
        if self.op in (FilterOp.EXISTS, FilterOp.DOES_NOT_EXIST):
            if self.value is not None:
                raise ValueError(f"op `{self.op.value}` takes no value")
        elif self.op in (FilterOp.IN, FilterOp.NOT_IN):
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"op `{self.op.value}` needs a non-empty list value")
        elif self.value is None or isinstance(self.value, list):
            raise ValueError(f"op `{self.op.value}` needs a single scalar value")
        return self


class Calculation(BaseModel):
    """One aggregate. COUNT and ERROR_RATE take no field; every other calc requires one."""

    op: Calc
    field: str | None = None

    @model_validator(mode="after")
    def _check_field(self) -> "Calculation":
        if self.op in NO_FIELD_CALCS and self.field is not None:
            raise ValueError(f"{self.op.value} takes no field")
        if self.op not in NO_FIELD_CALCS and not self.field:
            raise ValueError(f"{self.op.value} needs a field")
        return self

    @property
    def key(self) -> str:
        """Stable result key, e.g. `count`, `p95(duration_ms)`, `error_rate`."""
        name = self.op.value.lower()
        return f"{name}({self.field})" if self.field else name


class OrderBy(BaseModel):
    """Order groups by a calculation key (`Calculation.key`) or by a group_by field."""

    key: str
    descending: bool = True


class TimeWindow(BaseModel):
    """Either `last_seconds` (relative to the dataset's end time) or absolute `start`/`end`.

    Absolute bounds are ISO-8601 (naive values are treated as UTC); `start` is inclusive and
    `end` exclusive. A missing absolute bound defaults to the dataset's start/end.
    """

    last_seconds: int | None = Field(default=None, gt=0)
    start: datetime | None = None
    end: datetime | None = None

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)

    @model_validator(mode="after")
    def _one_mode(self) -> "TimeWindow":
        absolute = self.start is not None or self.end is not None
        if self.last_seconds is not None and absolute:
            raise ValueError("use either last_seconds or start/end, not both")
        if self.last_seconds is None and not absolute:
            raise ValueError("give last_seconds or at least one of start/end")
        if self.start and self.end and self.start >= self.end:
            raise ValueError("start must be before end")
        return self


# --------------------------------------------------------------------------- outputs


class FieldInfo(BaseModel):
    name: str
    type: FieldType
    cardinality: int | None = Field(default=None, description="Distinct non-null values.")
    fraction_present: float | None = Field(
        default=None, description="Fraction of rows where the field is non-null."
    )
    mixed_types: bool = Field(
        default=False, description="Values had conflicting types across rows; stored as strings."
    )


class ServiceInfo(BaseModel):
    name: str
    rows: int | None = None


class DatasetSchema(BaseModel):
    fields: list[FieldInfo]
    row_count: int | None = None
    time_start: datetime
    time_end: datetime = Field(description="The dataset's end time; relative windows end here.")
    services: list[ServiceInfo]
    notes: list[str] = Field(default_factory=list, description="Caveats from loading.")

    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]


class ResolvedWindow(BaseModel):
    """A concrete window: `start` inclusive, `end` exclusive."""

    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


class Bucket(BaseModel):
    start: datetime
    values: dict[str, float | int | None]


class Group(BaseModel):
    key: dict[str, Scalar] = Field(description="group_by field -> value; empty without group_by.")
    values: dict[str, float | int | None] = Field(description="Calculation key -> value.")
    series: list[Bucket] | None = Field(
        default=None, description="Dense time buckets (zero-filled) when granularity was given."
    )


class AggregateResult(BaseModel):
    calcs: list[str]
    group_by: list[str]
    groups: list[Group]
    total_groups: int = Field(description="Groups before the limit was applied.")
    truncated: bool
    matched_rows: int | None = Field(
        description="Rows matching filters + window (None if unknown)."
    )
    window: ResolvedWindow
    granularity_seconds: int | None = None


class ValueCount(BaseModel):
    value: Scalar
    count: int


class DistinctValues(BaseModel):
    field: str
    values: list[ValueCount]
    total_distinct: int
    truncated: bool
    matched_rows: int | None = None
    rows_with_field: int | None = None
    window: ResolvedWindow


class SampleResult(BaseModel):
    events: list[dict[str, Scalar]] = Field(description="Absent (null) attributes are omitted.")
    total_matching: int
    truncated: bool
    window: ResolvedWindow


class Span(BaseModel):
    span_id: str
    parent_span_id: str | None = None
    name: str | None = None
    service: str | None = None
    kind: str | None = None
    start: datetime | None = Field(default=None, description="None if the backend lacks it.")
    duration_ms: float | None = None
    error: bool = False
    status_message: str | None = None
    attributes: dict[str, Scalar] = Field(
        default_factory=dict, description="Remaining non-null fields (empty on Honeycomb)."
    )


class Trace(BaseModel):
    trace_id: str
    spans: list[Span]
    span_count: int = Field(description="Spans in the trace, before max_spans truncation.")
    truncated: bool


class NumericStats(BaseModel):
    count: int | None = Field(default=None, description="Rows with the field (None on Honeycomb).")
    mean: float | None = None
    p50: float | None = None
    p90: float | None = None
    p95: float | None = None
    max: float | None = None


class ValueShare(BaseModel):
    """One value (or numeric bucket) of a field in selection vs baseline.

    Fractions are of *all* rows in that set, so the absent bucket (`value=None`,
    `absent=True`) makes missing-field differences visible.
    """

    value: Scalar
    absent: bool = False
    low: float | None = Field(default=None, description="Numeric bucket lower bound (exclusive).")
    high: float | None = Field(default=None, description="Numeric bucket upper bound (inclusive).")
    selection_count: int
    baseline_count: int
    selection_fraction: float
    baseline_fraction: float
    delta: float = Field(description="selection_fraction - baseline_fraction")


class FieldComparison(BaseModel):
    field: str
    type: FieldType
    kind: str = Field(description="`categorical` or `numeric` (quantile buckets + stats).")
    score: float = Field(
        description="0..1, higher = selection differs more. See backends/_common.py."
    )
    score_method: str
    selection_present_fraction: float | None = None
    baseline_present_fraction: float | None = None
    total_values: int | None = Field(default=None, description="Distinct non-null values/buckets.")
    values: list[ValueShare] = Field(description="Top values by |delta|, at most max_values.")
    selection_stats: NumericStats | None = None
    baseline_stats: NumericStats | None = None


class DistributionComparison(BaseModel):
    selection_rows: int
    baseline_rows: int
    selection_window: ResolvedWindow
    baseline_window: ResolvedWindow
    fields: list[FieldComparison] = Field(description="Ranked by score, descending.")
    skipped_fields: dict[str, str] = Field(
        default_factory=dict, description="field -> why it was not compared."
    )


# --------------------------------------------------------------------------- protocol


@runtime_checkable
class TelemetryBackend(Protocol):
    """What the MCP tools sit on. All methods are read-only and idempotent."""

    def schema(self) -> DatasetSchema:
        """Fields (type, cardinality, presence), row count, time range, services."""
        ...

    def aggregate(
        self,
        calcs: Sequence[Calculation],
        *,
        filters: Sequence[Filter] = (),
        group_by: Sequence[str] = (),
        time_window: TimeWindow | None = None,
        order_by: OrderBy | None = None,
        limit: int = 10,
        granularity_seconds: int | None = None,
    ) -> AggregateResult:
        """Aggregate over the window, optionally grouped and/or bucketed over time.

        Rows missing any group_by field are excluded. Default order: first calc, descending.
        With `granularity_seconds`, each group (top `limit` only) gets a dense `series`.
        """
        ...

    def distinct_values(
        self,
        field: str,
        *,
        filters: Sequence[Filter] = (),
        time_window: TimeWindow | None = None,
        limit: int = 20,
    ) -> DistinctValues:
        """Most common values of a field with counts, plus the total number of distinct values."""
        ...

    def sample_events(
        self,
        *,
        filters: Sequence[Filter] = (),
        fields: Sequence[str] | None = None,
        time_window: TimeWindow | None = None,
        limit: int = 5,
        order: SampleOrder = SampleOrder.NEWEST,
    ) -> SampleResult:
        """A few raw rows. Not supported by every backend (Honeycomb raises)."""
        ...

    def get_trace(self, trace_id: str, *, max_spans: int = 2000) -> Trace:
        """All spans of a trace (empty `spans` if not found). Summarising is the caller's job."""
        ...

    def compare_distributions(
        self,
        *,
        selection_filters: Sequence[Filter] = (),
        baseline_filters: Sequence[Filter] | None = None,
        fields: Sequence[str] | None = None,
        time_window: TimeWindow | None = None,
        baseline_window: TimeWindow | None = None,
        max_values_per_field: int = 5,
    ) -> DistributionComparison:
        """BubbleUp primitive: per field, value distribution in selection vs baseline.

        Selection = `selection_filters` within `time_window`. Baseline is, in priority order:
        1. `baseline_filters` (within `baseline_window`, defaulting to `time_window`);
        2. if only `baseline_window` is given: the *same* filters in that other window
           (before/after comparison);
        3. otherwise the complement of the selection within `time_window` (BubbleUp default).

        Result fields are ranked by score. Scoring and numeric handling are documented in
        `incident_agent.backends._common`.
        """
        ...

    def close(self) -> None: ...


def resolve_window(
    window: TimeWindow | None, data_start: datetime, data_end: datetime
) -> ResolvedWindow:
    """Turn a TimeWindow into concrete bounds against the dataset's own time range.

    `data_end` is the newest event; the exclusive end is 1 microsecond later so it is included.
    """
    end_default = data_end + timedelta(microseconds=1)
    if window is None:
        return ResolvedWindow(start=data_start, end=end_default)
    if window.last_seconds is not None:
        return ResolvedWindow(
            start=data_end - timedelta(seconds=window.last_seconds), end=end_default
        )
    return ResolvedWindow(start=window.start or data_start, end=window.end or end_default)
