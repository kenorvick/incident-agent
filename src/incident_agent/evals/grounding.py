"""Re-export of the citation matcher, which lives in `incident_agent.grounding` so the agent can
use it too. Rules and normalization are documented there."""

from incident_agent.grounding import (
    Haystack,
    ParsedNumber,
    normalize_citation,
    normalize_text,
    parse_cited_number,
    value_grounded,
)

__all__ = [
    "Haystack",
    "ParsedNumber",
    "normalize_citation",
    "normalize_text",
    "parse_cited_number",
    "value_grounded",
]
