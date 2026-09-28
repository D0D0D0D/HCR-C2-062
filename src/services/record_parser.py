"""AgentCore Platform v1.0 — HCR-C2-062: QMS export record parsing.

docs/02_design.md Design 入場条件 #2 — 最小データ要件 / パース方式:
  - CSV: config/ingest_mapping.yaml で列名を正規フィールド名へマッピング
    (customer QMS export header names vary: TrackWise / Veeva Vault QMS /
    MasterControl each use different column names for the same concept).
  - Excel(.xlsx): same mapping, rows already sniffed+sanitized by
    src/services/sanitizer.py before this module ever sees them.
  - Word(.docx): text already extracted by sanitizer.sanitize_docx_bytes();
    this module's docx path does light heuristic field extraction only —
    full docx structure varies too much for column mapping.

Minimum data requirement enforcement (fields required for a KB row to be
constructible) happens here, NOT in sanitizer.py (sanitizer.py only judges
input-channel safety, never business-record completeness).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Canonical field names used across all three record kinds.
_DEVIATION_REQUIRED = ("source_record_id", "occurred_on", "event_summary")
_CAPA_REQUIRED = ("capa_id", "deviation_id", "action_summary")
_EFFECTIVENESS_REQUIRED = ("capa_id", "checked_on")


@dataclass
class ParsedRecord:
    record_kind: str  # "deviation" / "capa" / "effectiveness"
    accepted: bool
    fields: dict[str, Any] = field(default_factory=dict)
    missing_data_flags: list[str] = field(default_factory=list)
    reject_reason: str | None = None


def _apply_mapping(raw_row: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """Translate customer QMS column names -> canonical field names.

    `mapping` is {canonical_field: customer_column_name}. Unmapped canonical
    fields are simply absent from the result (caller applies
    required-field / unclassified handling).
    """
    out: dict[str, Any] = {}
    for canonical, customer_col in mapping.items():
        if customer_col in raw_row:
            out[canonical] = raw_row[customer_col]
    # Pass through any column that already matches a canonical name exactly
    # (customer export already uses our vocabulary — common for pilot
    # customers who adopted the taxonomy directly).
    for k, v in raw_row.items():
        if k not in out and k not in mapping.values():
            out.setdefault(k, v)
    return out


def parse_deviation_row(raw_row: dict[str, Any], mapping: dict[str, str]) -> ParsedRecord:
    mapped = _apply_mapping(raw_row, mapping)
    missing_required = [f for f in _DEVIATION_REQUIRED if not mapped.get(f)]
    if missing_required:
        return ParsedRecord(
            record_kind="deviation",
            accepted=False,
            reject_reason=f"deviation record missing required fields: {missing_required}",
        )

    recommended = ("deviation_category", "process_type", "root_cause_class", "severity_class")
    missing_flags = []
    for f in recommended:
        if not mapped.get(f):
            mapped[f] = "unclassified"
            missing_flags.append(f)

    return ParsedRecord(record_kind="deviation", accepted=True, fields=mapped, missing_data_flags=missing_flags)


def parse_capa_row(raw_row: dict[str, Any], mapping: dict[str, str]) -> ParsedRecord:
    mapped = _apply_mapping(raw_row, mapping)
    missing_required = [f for f in _CAPA_REQUIRED if not mapped.get(f)]
    if missing_required:
        return ParsedRecord(
            record_kind="capa",
            accepted=False,
            reject_reason=f"capa record missing required fields: {missing_required}",
        )
    if not mapped.get("capa_type"):
        mapped["capa_type"] = "unclassified"
    return ParsedRecord(record_kind="capa", accepted=True, fields=mapped)


def parse_effectiveness_row(raw_row: dict[str, Any], mapping: dict[str, str]) -> ParsedRecord:
    mapped = _apply_mapping(raw_row, mapping)
    missing_required = [f for f in _EFFECTIVENESS_REQUIRED if not mapped.get(f)]
    if missing_required:
        return ParsedRecord(
            record_kind="effectiveness",
            accepted=False,
            reject_reason=f"effectiveness record missing required fields: {missing_required}",
        )
    return ParsedRecord(record_kind="effectiveness", accepted=True, fields=mapped)


def parse_rows(
    rows: list[dict[str, Any]],
    record_kind: str,
    mapping: dict[str, str],
) -> list[ParsedRecord]:
    """Parse a batch of already-sanitized rows (from sanitizer.sanitize_input)."""
    parser = {
        "deviation": parse_deviation_row,
        "capa": parse_capa_row,
        "effectiveness": parse_effectiveness_row,
    }.get(record_kind)
    if parser is None:
        raise ValueError(f"unknown record_kind: {record_kind}")
    return [parser(row, mapping) for row in rows]


def parse_docx_text_heuristic(text: str) -> ParsedRecord:
    """Light heuristic extraction for narrative-form .docx investigation
    reports — looks for "逸脱番号:"/"発生日:"/label-style lines. Anything not
    matched is left for LLM-assisted classification downstream (Stage 3),
    never invented here."""
    fields: dict[str, Any] = {}
    label_map = {
        "逸脱番号": "source_record_id",
        "発生日": "occurred_on",
        "事象": "event_summary",
        "工程": "process_type",
    }
    for line in text.splitlines():
        line = line.strip()
        if not line or ("：" not in line and ":" not in line):
            continue
        sep = "：" if "：" in line else ":"
        label, _, value = line.partition(sep)
        label = label.strip()
        value = value.strip()
        canonical = label_map.get(label)
        if canonical and value:
            fields[canonical] = value

    missing_required = [f for f in _DEVIATION_REQUIRED if not fields.get(f)]
    if missing_required:
        return ParsedRecord(
            record_kind="deviation",
            accepted=False,
            reject_reason=f"docx narrative missing required fields: {missing_required}",
        )
    return ParsedRecord(record_kind="deviation", accepted=True, fields=fields)
