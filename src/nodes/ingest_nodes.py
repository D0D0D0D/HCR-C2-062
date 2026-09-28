"""AgentCore Platform v1.0 — HCR-C2-062: inner ingest-branch nodes.

docs/02_design.md Data Flow — inner workflow graph, ingest branch:
    parse → sanitize → taxonomy_tag → kb_upsert

Each node is a plain FunctionNode operating on the INNER
DeviationWorkflowState (src/schemas/state.py), not the outer State. The
raw payload is resolved transiently from TransientPayloadStore.get_sanitized()
(the S-2 gate already sanitized channel-level threats before this graph
ever runs; this module handles record-level parsing/PII/taxonomy).
"""

from __future__ import annotations

from typing import Any, ClassVar

import yaml

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.kb_service import KBService, TransientPayloadStore
from src.services.pii_sanitizer import sanitize_worker_pii
from src.services.record_parser import parse_rows

_INGEST_MAPPING_PATH = "config/ingest_mapping.yaml"


def _load_mapping(path: str = _INGEST_MAPPING_PATH) -> dict[str, dict[str, str]]:
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    return {k: v for k, v in raw.items() if k in ("deviation", "capa", "effectiveness")}


class IngestParseNode(FunctionNode):
    """Parse the sanitized rows/text into record dicts (no KB write yet)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, payload_store: TransientPayloadStore | None = None) -> None:
        super().__init__()
        self._payload_store = payload_store or TransientPayloadStore()

    def execute(self, state: AgentState) -> dict:
        payload_ref = state.get("payload_ref")
        sanitized = self._payload_store.get_sanitized(payload_ref) if payload_ref else None

        if sanitized is None:
            emit_trace_event("ingest_parse_no_payload", {"payload_ref": bool(payload_ref)}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["IngestParseNode: no sanitized payload found for payload_ref"],
            }

        mapping = _load_mapping()
        record_kind = state.get("_ingest_record_kind", "deviation")
        rows = getattr(sanitized, "sanitized_rows", None) or []
        text = getattr(sanitized, "sanitized_text", None)

        parsed_records: list[dict[str, Any]] = []
        reject_reasons: list[str] = []

        if rows:
            parsed = parse_rows(rows, record_kind, mapping.get(record_kind, {}))
            for p in parsed:
                if p.accepted:
                    parsed_records.append({"fields": p.fields, "missing_data_flags": p.missing_data_flags})
                else:
                    reject_reasons.append(p.reject_reason or "unknown reject reason")
        elif text:
            from src.services.record_parser import parse_docx_text_heuristic

            p = parse_docx_text_heuristic(text)
            if p.accepted:
                parsed_records.append({"fields": p.fields, "missing_data_flags": p.missing_data_flags})
            else:
                reject_reasons.append(p.reject_reason or "unknown reject reason")

        emit_trace_event(
            "ingest_parsed",
            {"record_kind": record_kind, "accepted": len(parsed_records), "rejected": len(reject_reasons)},
            state,
        )

        if not parsed_records:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"IngestParseNode: no records could be parsed ({'; '.join(reject_reasons)})"],
            }

        return {
            "_parsed_records": parsed_records,
            "_ingest_record_kind": record_kind,
            "missing_data_flags": [flag for r in parsed_records for flag in r.get("missing_data_flags", [])],
            "status": AgentStatus.SUCCESS.value,
        }


class IngestSanitizeNode(FunctionNode):
    """Worker-PII sanitization on free-text fields (event_summary /
    rca_summary / action_summary / evidence_note) — MUST run before
    taxonomy_tag/kb_upsert (docs/02_design.md 従業員 PII サニタイズ, applied
    point ② KB書込直前)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    _FREE_TEXT_FIELDS = ("event_summary", "rca_summary", "action_summary", "evidence_note")

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        records = state.get("_parsed_records", [])
        sanitized_records = []
        unresolvable = 0
        for rec in records:
            fields = dict(rec.get("fields", {}))
            for field_name in self._FREE_TEXT_FIELDS:
                if field_name in fields and fields[field_name]:
                    sanitized_text, _count, resolvable = sanitize_worker_pii(str(fields[field_name]))
                    if not resolvable:
                        unresolvable += 1
                        continue
                    fields[field_name] = sanitized_text
            sanitized_records.append({**rec, "fields": fields})

        emit_trace_event("ingest_pii_sanitized", {"records": len(sanitized_records), "unresolvable": unresolvable}, state)

        if unresolvable:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"IngestSanitizeNode: {unresolvable} record(s) had unresolvable PII (fail closed)"],
            }

        return {"_parsed_records": sanitized_records, "status": AgentStatus.SUCCESS.value}


class TaxonomyTagNode(FunctionNode):
    """Apply the deviation_category/process_type/root_cause_class/severity_class
    taxonomy (config/taxonomy.yaml) — unknown values fall to 'unclassified' +
    missing_data_flags (docs/02_design.md 逸脱分類体系, never guessed)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    _TAXONOMY_FIELDS = ("deviation_category", "process_type", "root_cause_class", "severity_class", "capa_type")

    def __init__(self, taxonomy_path: str = "config/taxonomy.yaml") -> None:
        super().__init__()
        self._taxonomy_path = taxonomy_path

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        try:
            with open(self._taxonomy_path, encoding="utf-8") as f:
                taxonomy = yaml.safe_load(f) or {}
        except FileNotFoundError:
            taxonomy = {}

        records = state.get("_parsed_records", [])
        tagged_records = []
        new_missing_flags: list[str] = []
        for rec in records:
            fields = dict(rec.get("fields", {}))
            for field_name in self._TAXONOMY_FIELDS:
                allowed = taxonomy.get(field_name, [])
                value = fields.get(field_name)
                # Skip values already normalized to "unclassified" upstream
                # (record_parser.py's own required/recommended-field check) —
                # re-flagging here would double-count the same gap in
                # missing_data_flags (cosmetic duplication, not a correctness
                # bug, but avoided for a clean audit trail).
                if value and value != "unclassified" and allowed and value not in allowed:
                    fields[field_name] = "unclassified"
                    new_missing_flags.append(field_name)
            tagged_records.append({**rec, "fields": fields})

        emit_trace_event("ingest_taxonomy_tagged", {"records": len(tagged_records)}, state)

        return {
            "_parsed_records": tagged_records,
            "missing_data_flags": list(state.get("missing_data_flags", [])) + new_missing_flags,
            "status": AgentStatus.SUCCESS.value,
        }


class KbUpsertNode(FunctionNode):
    """INSERT the sanitized/tagged records into the KB (tenant fail-closed).
    Only sanitized structured attributes are ever written — no raw
    narrative, no payload_ref resolution beyond this point."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, kb_service: KBService | None = None) -> None:
        super().__init__()
        self._kb_service = kb_service

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        tenant_id = state.get("tenant_id", "")
        if not tenant_id:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["KbUpsertNode: tenant_id missing (fail closed)"],
            }
        if self._kb_service is None:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["KbUpsertNode: KBService not wired (server startup misconfiguration)"],
            }

        record_kind = state.get("_ingest_record_kind", "deviation")
        records = state.get("_parsed_records", [])
        inserted_ids: list[str] = []

        for rec in records:
            fields = rec.get("fields", {})
            if record_kind == "deviation":
                inserted_ids.append(self._kb_service.insert_deviation_case(tenant_id, fields))
            elif record_kind == "capa":
                inserted_ids.append(self._kb_service.insert_capa_record(tenant_id, fields))
            elif record_kind == "effectiveness":
                inserted_ids.append(self._kb_service.insert_effectiveness_check(tenant_id, fields))

        emit_trace_event(
            "kb_upsert_complete",
            {"tenant_id": tenant_id, "record_kind": record_kind, "inserted": len(inserted_ids)},
            state,
        )

        return {
            "ingest_summary": {
                "record_kind": record_kind,
                "inserted_ids": inserted_ids,
                "count": len(inserted_ids),
                "missing_data_flags": state.get("missing_data_flags", []),
            },
            "gate_kind": "ingest",  # S-3 Provenance gate path declaration
            "status": AgentStatus.SUCCESS.value,
        }
