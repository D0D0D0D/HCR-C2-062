"""HCR-C2-062 — record_parser unit tests (docs/02_design.md Design 入場条件 #2)."""

from __future__ import annotations

from src.services.record_parser import (
    parse_capa_row,
    parse_deviation_row,
    parse_docx_text_heuristic,
    parse_effectiveness_row,
)

_MAPPING = {
    "source_record_id": "Deviation Number",
    "occurred_on": "Date Occurred",
    "event_summary": "Event Description",
}


class TestParseDeviationRow:
    def test_minimum_required_fields_accepted(self):
        row = {"Deviation Number": "DEV-001", "Date Occurred": "2026-01-01", "Event Description": "test event"}
        result = parse_deviation_row(row, _MAPPING)
        assert result.accepted
        assert result.fields["source_record_id"] == "DEV-001"

    def test_missing_required_field_rejected(self):
        row = {"Deviation Number": "DEV-001"}  # missing occurred_on, event_summary
        result = parse_deviation_row(row, _MAPPING)
        assert not result.accepted
        assert "source_record_id" not in (result.reject_reason or "") or "occurred_on" in result.reject_reason

    def test_missing_recommended_fields_flagged_not_rejected(self):
        row = {"Deviation Number": "DEV-001", "Date Occurred": "2026-01-01", "Event Description": "test"}
        result = parse_deviation_row(row, _MAPPING)
        assert result.accepted
        assert result.fields["deviation_category"] == "unclassified"
        assert "deviation_category" in result.missing_data_flags

    def test_canonical_column_passthrough(self):
        """QMS export already using canonical field names passes through."""
        row = {"source_record_id": "DEV-002", "occurred_on": "2026-01-02", "event_summary": "another event"}
        result = parse_deviation_row(row, {})
        assert result.accepted
        assert result.fields["source_record_id"] == "DEV-002"


class TestParseCapaRow:
    def test_minimum_required_accepted(self):
        row = {"CAPA Number": "CAPA-1", "Related Deviation Number": "DEV-001", "Action Description": "fixed"}
        mapping = {
            "capa_id": "CAPA Number",
            "deviation_id": "Related Deviation Number",
            "action_summary": "Action Description",
        }
        result = parse_capa_row(row, mapping)
        assert result.accepted

    def test_missing_required_rejected(self):
        result = parse_capa_row({}, {})
        assert not result.accepted


class TestParseEffectivenessRow:
    def test_minimum_required_accepted(self):
        row = {"CAPA Number": "CAPA-1", "Verification Date": "2026-02-01"}
        mapping = {"capa_id": "CAPA Number", "checked_on": "Verification Date"}
        result = parse_effectiveness_row(row, mapping)
        assert result.accepted

    def test_missing_required_rejected(self):
        result = parse_effectiveness_row({"CAPA Number": "CAPA-1"}, {"capa_id": "CAPA Number"})
        assert not result.accepted


class TestParseDocxTextHeuristic:
    def test_labeled_lines_extracted(self):
        text = "逸脱番号: DEV-100\n発生日: 2026-03-01\n事象: 何かが起きた\n"
        result = parse_docx_text_heuristic(text)
        assert result.accepted
        assert result.fields["source_record_id"] == "DEV-100"
        assert result.fields["occurred_on"] == "2026-03-01"

    def test_missing_required_rejected(self):
        text = "工程: 充填\n"  # no 逸脱番号/発生日/事象
        result = parse_docx_text_heuristic(text)
        assert not result.accepted
