"""AgentCore Platform v1.0 — HCR-C2-062: deviation/CAPA KB adapter.

docs/02_design.md Design 入場条件 #1 / #5 — KB スキーマ / データ統治契約:
  This is the ONLY layer allowed to touch entity data (event_summary,
  rca_summary, action_summary free text). Inner graph nodes receive back
  ONLY sanitized structured attributes + reference IDs — raw narrative
  never crosses back into State/checkpoint (SubcontractorQuotationDisputeHistory KBService pattern).

  Tenant fail-closed (docs/02_design.md テナント統制): every method requires
  a non-empty tenant_id ("{company_id}:{site_id}") and filters all rows by
  it. An empty/missing tenant_id returns an empty result — never "all
  tenants", never a cross-tenant fallback.

  retrieved_records (Stage 2 output) is the S-3 Provenance gate's single
  source of truth — it must carry ONLY sanitized match attributes
  (source_id / deviation_category / process_type / root_cause_class /
  action_class / effective / recurrence_observed, ...), never raw
  narrative text (State-bloat + sensitive-data-exposure avoidance).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from shared.services.database.base_db import BaseDatabase


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# Upper bound on ids expanded into a SQL `IN (...)` list. Each id becomes one
# bind parameter and drivers cap those (SQLite 999, PostgreSQL 65535); the
# normal caller passes `retrieval.top_k_similar_deviations` ids (default 8).
_MAX_IN_CLAUSE_IDS = 200


class CapaNotFoundError(LookupError):
    """Raised when a feedback UPDATE matches no row in this tenant.

    The UPDATE is tenant-scoped in its WHERE clause, so an unknown capa_id and
    another tenant's capa_id both simply match zero rows. Reporting success in
    that case would put "effectiveness recorded" into the audit trail while
    nothing was written — fail closed instead.
    """


class TransientPayloadStore:
    """TTL-bounded, in-memory transient store for raw deviation-record /
    report bodies (SubcontractorQuotationDisputeHistory pattern). The raw text NEVER enters LangGraph
    State — the API adapter deposits it here before graph invocation and
    only the opaque `payload_ref` travels through State/checkpoint. Inner
    nodes resolve it transiently inside execute() via get().

    Two parallel entry kinds share the same ref-keyed map:
      - raw bytes  (put_bytes / get_bytes)   — deposited by the API adapter
        before the S-2 gate runs; this is what PreProcessNode's
        _extra_security_gate_input() sniffs/sanitizes.
      - sanitized result (put_sanitized / get_sanitized) — the S-2 gate
        overwrites the SAME ref with the sanitizer.SanitizeResult so that
        every downstream reader (execute(), the inner workflow graph) only
        ever observes the post-sanitization version, never the raw bytes.
    """

    def __init__(self, ttl_seconds: int = 600):
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, Any]] = {}

    def put(self, raw_payload: Any) -> str:
        self._evict_expired()
        ref = _new_id("payload_ref")
        self._entries[ref] = (time.monotonic(), raw_payload)
        return ref

    def get(self, payload_ref: str | None) -> Any | None:
        self._evict_expired()
        if not payload_ref:
            return None
        entry = self._entries.get(payload_ref)
        return entry[1] if entry else None

    # ── raw-bytes channel (pre-S-2) ─────────────────────────────────────

    def put_bytes(self, raw_bytes: bytes) -> str:
        """Deposit a raw file payload before graph invocation. Returns the
        opaque ref that travels through State as `payload_ref`."""
        return self.put(raw_bytes)

    def get_bytes(self, payload_ref: str | None) -> bytes | None:
        """Read back the raw bytes for `payload_ref`, if the entry has not
        yet been overwritten by put_sanitized() and has not expired.
        Returns None if the ref is empty/unknown/expired, or if the entry
        was already replaced by a sanitized result (bytes, not the
        SanitizeResult, is the only valid return here)."""
        value = self.get(payload_ref)
        return value if isinstance(value, (bytes, bytearray)) else None

    # ── sanitized-result channel (post-S-2) ─────────────────────────────

    def put_sanitized(self, payload_ref: str, sanitized: Any) -> None:
        """Overwrite the entry at `payload_ref` with the S-2 gate's
        SanitizeResult, replacing the raw bytes so nothing downstream can
        ever observe pre-sanitization content again. Refreshes the TTL
        clock on write (treated as a new access)."""
        self._evict_expired()
        if not payload_ref:
            raise ValueError("TransientPayloadStore.put_sanitized: payload_ref is required")
        self._entries[payload_ref] = (time.monotonic(), sanitized)

    def get_sanitized(self, payload_ref: str | None) -> Any | None:
        """Read back the sanitized result for `payload_ref` (None if the
        entry is still raw bytes, unknown, or expired)."""
        value = self.get(payload_ref)
        if value is None or isinstance(value, (bytes, bytearray)):
            return None
        return value

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [ref for ref, (t, _) in self._entries.items() if now - t > self._ttl]
        for ref in expired:
            del self._entries[ref]


class KBService:
    """Adapter over deviation_cases / capa_records / effectiveness_checks.

    All methods are tenant fail-closed: an empty tenant_id raises (writes)
    or returns an empty result (reads) — never a cross-tenant fallback.

    `db=None` is a valid, supported configuration (no DB DSN secret wired
    yet, e.g. local dev / cold-start STG) — search methods return empty
    results (graceful degradation, docs/02_design.md "KB がゼロでも...
    グレースフルデグレード動作する"), and write methods raise explicitly
    (a write with nowhere to persist is a real server misconfiguration,
    not something to silently degrade)."""

    def __init__(self, db: BaseDatabase | None = None):
        self._db = db

    # ── ingest ───────────────────────────────────────────────────────────

    def insert_deviation_case(self, tenant_id: str, fields: dict[str, Any]) -> str:
        if not tenant_id:
            raise ValueError("KBService.insert_deviation_case: tenant_id is required (fail-closed)")
        if self._db is None:
            raise RuntimeError("KBService.insert_deviation_case: no database wired (server misconfiguration)")
        deviation_id = _new_id("dev")
        self._db.execute(
            "INSERT INTO deviation_cases "
            "(deviation_id, tenant_id, source_record_id, occurred_on, deviation_category, "
            "process_type, root_cause_class, severity_class, equipment_class, product_class, "
            "lot_ref, event_summary, rca_summary, recurrence_of, ingested_at) "
            "VALUES (:deviation_id, :tenant_id, :source_record_id, :occurred_on, :deviation_category, "
            ":process_type, :root_cause_class, :severity_class, :equipment_class, :product_class, "
            ":lot_ref, :event_summary, :rca_summary, :recurrence_of, :ingested_at)",
            {
                "deviation_id": deviation_id,
                "tenant_id": tenant_id,
                "source_record_id": fields.get("source_record_id"),
                "occurred_on": fields.get("occurred_on"),
                "deviation_category": fields.get("deviation_category", "unclassified"),
                "process_type": fields.get("process_type", "unclassified"),
                "root_cause_class": fields.get("root_cause_class", "unclassified"),
                "severity_class": fields.get("severity_class", "unclassified"),
                "equipment_class": fields.get("equipment_class"),
                "product_class": fields.get("product_class"),
                # lot_ref default: NOT stored unless the customer governance
                # contract explicitly enables it (docs/02_design.md §5).
                "lot_ref": fields.get("lot_ref") if fields.get("lot_ref_governance_enabled") else None,
                "event_summary": fields.get("event_summary"),  # already sanitized upstream
                "rca_summary": fields.get("rca_summary"),
                "recurrence_of": fields.get("recurrence_of"),
                "ingested_at": fields.get("ingested_at"),
            },
        )
        return deviation_id

    def insert_capa_record(self, tenant_id: str, fields: dict[str, Any]) -> str:
        if not tenant_id:
            raise ValueError("KBService.insert_capa_record: tenant_id is required (fail-closed)")
        if self._db is None:
            raise RuntimeError("KBService.insert_capa_record: no database wired (server misconfiguration)")
        capa_id = _new_id("capa")
        self._db.execute(
            "INSERT INTO capa_records "
            "(capa_id, tenant_id, deviation_id, capa_type, action_class, action_summary, "
            "owner_role, implemented_on) "
            "VALUES (:capa_id, :tenant_id, :deviation_id, :capa_type, :action_class, :action_summary, "
            ":owner_role, :implemented_on)",
            {
                "capa_id": capa_id,
                "tenant_id": tenant_id,
                "deviation_id": fields.get("deviation_id"),
                "capa_type": fields.get("capa_type", "unclassified"),
                "action_class": fields.get("action_class", "unclassified"),
                "action_summary": fields.get("action_summary"),  # sanitized upstream
                "owner_role": fields.get("owner_role"),  # role, never a name
                "implemented_on": fields.get("implemented_on"),
            },
        )
        return capa_id

    def insert_effectiveness_check(self, tenant_id: str, fields: dict[str, Any]) -> str:
        if not tenant_id:
            raise ValueError("KBService.insert_effectiveness_check: tenant_id is required (fail-closed)")
        if self._db is None:
            raise RuntimeError("KBService.insert_effectiveness_check: no database wired (server misconfiguration)")
        check_id = _new_id("chk")
        self._db.execute(
            "INSERT INTO effectiveness_checks "
            "(check_id, tenant_id, capa_id, checked_on, effective, recurrence_observed, "
            "evidence_note, source, operator_edited) "
            "VALUES (:check_id, :tenant_id, :capa_id, :checked_on, :effective, :recurrence_observed, "
            ":evidence_note, :source, :operator_edited)",
            {
                "check_id": check_id,
                "tenant_id": tenant_id,
                "capa_id": fields.get("capa_id"),
                "checked_on": fields.get("checked_on"),
                "effective": fields.get("effective"),
                "recurrence_observed": fields.get("recurrence_observed"),
                "evidence_note": fields.get("evidence_note"),
                "source": fields.get("source", "ingest"),
                "operator_edited": fields.get("operator_edited", False),
            },
        )
        return check_id

    # ── invoke: Stage 2 KB search (tenant-scoped, fail-closed) ─────────────

    def search_similar_deviations(
        self,
        tenant_id: str,
        deviation_category: str | None,
        process_type: str | None,
        root_cause_class: str | None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Returns the S-3 Provenance gate's source-of-truth records:
        sanitized match attributes ONLY, never raw event_summary/rca_summary
        text (docs/02_design.md retrieved_records contract).

        Tenant fail-closed: empty tenant_id -> empty result, never cross-tenant.
        `db=None` (no DB wired) -> empty result (graceful degradation, not
        a crash — KB-empty and KB-not-wired are indistinguishable from the
        caller's perspective).
        """
        if not tenant_id or self._db is None:
            return []
        rows = self._db.fetch_many(
            "SELECT deviation_id AS source_id, deviation_category, process_type, "
            "root_cause_class, severity_class, recurrence_of "
            "FROM deviation_cases WHERE tenant_id = :tenant_id "
            "AND (:deviation_category IS NULL OR deviation_category = :deviation_category) "
            "AND (:process_type IS NULL OR process_type = :process_type) "
            "AND (:root_cause_class IS NULL OR root_cause_class = :root_cause_class) "
            "ORDER BY occurred_on DESC LIMIT :limit",
            {
                "tenant_id": tenant_id,
                "deviation_category": deviation_category,
                "process_type": process_type,
                "root_cause_class": root_cause_class,
                "limit": limit,
            },
        )
        return rows or []

    def search_capa_effectiveness(
        self,
        tenant_id: str,
        deviation_ids: list[str],
        limit: int = 16,
    ) -> list[dict[str, Any]]:
        """CAPA + effectiveness records for the given deviation ids —
        sanitized attributes only (action_class / effective /
        recurrence_observed), never action_summary free text.

        🔴 SQL `IN` cannot bind a sequence to a single named parameter — a
        driver receives `IN :deviation_ids` as literal SQL and raises a
        syntax error (`near ":deviation_ids"`, reproduced on a real sqlite3
        engine; PostgreSQL behaves the same). The placeholder list MUST be
        expanded to one named parameter per id. A FakeDatabase that ignores
        the query string cannot catch this, so the real-DB proof-of-boundary
        test (tests/proof_of_boundary/test_pb3_real_database.py) is what
        keeps this honest.
        """
        if not tenant_id or not deviation_ids or self._db is None:
            return []
        # De-duplicate before expanding into placeholders (every id becomes one
        # bind parameter, and drivers cap those — SQLite 999, PostgreSQL 65535).
        unique_ids = list(dict.fromkeys(deviation_ids))
        # Over the cap, raise rather than truncate. Silently dropping ids would
        # return a PARTIAL CAPA-effectiveness recall that the draft then presents
        # as the site's complete history — "no prior CAPA was effective here"
        # when in fact the evidence was simply cut off. A wrong-but-confident
        # recall is worse than a failed call in a GMP investigation.
        # The normal caller passes `retrieval.top_k_similar_deviations` ids
        # (config/config.yaml, default 8), so this only fires on misconfiguration
        # or a direct caller.
        if len(unique_ids) > _MAX_IN_CLAUSE_IDS:
            raise ValueError(
                f"KBService.search_capa_effectiveness: {len(unique_ids)} deviation_ids exceeds the "
                f"{_MAX_IN_CLAUSE_IDS} cap — refusing to return a partial (silently truncated) recall"
            )
        # One placeholder per id — never interpolate the ids themselves
        # (parameter binding is what prevents SQL injection here).
        id_params = {f"dev_id_{i}": did for i, did in enumerate(unique_ids)}
        in_clause = ", ".join(f":{name}" for name in id_params)
        rows = self._db.fetch_many(
            "SELECT c.capa_id AS source_id, c.deviation_id, c.capa_type, c.action_class, "
            "e.effective, e.recurrence_observed "
            "FROM capa_records c "
            "LEFT JOIN effectiveness_checks e ON e.capa_id = c.capa_id AND e.tenant_id = c.tenant_id "
            f"WHERE c.tenant_id = :tenant_id AND c.deviation_id IN ({in_clause}) "
            "ORDER BY c.implemented_on DESC LIMIT :limit",
            {"tenant_id": tenant_id, **id_params, "limit": limit},
        )
        return rows or []

    # ── feedback ─────────────────────────────────────────────────────────

    def update_effectiveness(
        self,
        tenant_id: str,
        capa_id: str,
        effective: bool | None,
        recurrence_observed: bool | None,
        evidence_note: str | None,
    ) -> dict[str, Any]:
        if not tenant_id:
            raise ValueError("KBService.update_effectiveness: tenant_id is required (fail-closed)")
        if self._db is None:
            raise RuntimeError("KBService.update_effectiveness: no database wired (server misconfiguration)")
        # 🔴 The UPDATE is tenant-scoped in the WHERE clause, so a capa_id that
        # does not exist — or belongs to another tenant — simply matches no
        # rows. Reporting {"recorded": True} regardless would tell the QA
        # operator (and the audit trail) that an effectiveness outcome was
        # recorded when nothing was written. CAPA effectiveness IS the KB's
        # value and part of the GMP audit record, so a silent no-op is not
        # acceptable: fail closed on rowcount 0.
        rowcount = self._db.execute(
            "UPDATE effectiveness_checks SET effective = :effective, "
            "recurrence_observed = :recurrence_observed, evidence_note = :evidence_note, "
            "operator_edited = TRUE, source = 'feedback' "
            "WHERE capa_id = :capa_id AND tenant_id = :tenant_id",
            {
                "effective": effective,
                "recurrence_observed": recurrence_observed,
                "evidence_note": evidence_note,
                "capa_id": capa_id,
                "tenant_id": tenant_id,
            },
        )
        # A BaseDatabase adapter that cannot report a rowcount returns None —
        # treat "unknown" as "not proven written" rather than assuming success.
        # `bool` is a subclass of `int`, so an adapter that returns True as a
        # generic "ok" would sail through an isinstance(rowcount, int) check and
        # be read as "1 row updated" — exactly the false-success this guard
        # exists to prevent. Require a real int.
        if type(rowcount) is not int or rowcount < 1:  # noqa: E721 — bool must NOT pass
            raise CapaNotFoundError(
                f"KBService.update_effectiveness: no effectiveness_checks row for capa_id="
                f"{capa_id!r} in this tenant (nothing was recorded)"
            )
        return {"capa_id": capa_id, "recorded": True, "rows_updated": rowcount}
