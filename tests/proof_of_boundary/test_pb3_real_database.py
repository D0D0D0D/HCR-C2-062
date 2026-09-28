# tests/proof_of_boundary/test_pb3_real_database.py
#
# PB-3 — L1 → External service (real database connection).
#
# WHY THIS FILE EXISTS
# --------------------
# The rest of the suite injects a FakeDatabase that ignores the SQL string and
# returns canned rows. That makes every SQL-level defect invisible: a query can
# be syntactically impossible and the unit tests still go green.
#
# This actually happened. `search_capa_effectiveness()` shipped with
#
#     WHERE c.deviation_id IN :deviation_ids          -- bound to a tuple
#
# which no driver accepts — SQL `IN` cannot bind a sequence to one named
# parameter. On a real engine it raises `OperationalError: near
# ":deviation_ids": syntax error`. It passed 191 unit tests and two
# independent reviews, because nothing ever handed the SQL to a database.
# `search_capa_effectiveness()` is the CAPA-effectiveness recall — the core of
# this template's value — so it would have failed on the very first real
# invocation.
#
# These tests therefore run KBService's real SQL against a real (sqlite3)
# engine, via a real BaseDatabase implementation. They cover the same ground the
# STG first-invoke is meant to cover (DDL agreement, tenant isolation through
# the retrieval stage, the ingest -> invoke -> feedback round trip), but here it
# is enforced on every push instead of being deferred.
#
# The DDL below is the executable statement of docs/02_design.md's KB schema.
# If a column is renamed in the service layer without updating the design, the
# INSERT fails here.

from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

import pytest

from shared.services.database.base_db import BaseDatabase
from src.services.kb_service import CapaNotFoundError, KBService

# docs/02_design.md — Design 入場条件 #1 (KB schema). Sanitized structured data
# only: no raw source file, no worker names, lot_ref nullable/not populated by
# default.
DDL = """
CREATE TABLE deviation_cases (
    deviation_id       TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    source_record_id   TEXT NOT NULL,
    occurred_on        TEXT,
    deviation_category TEXT,
    process_type       TEXT,
    root_cause_class   TEXT,
    severity_class     TEXT,
    equipment_class    TEXT,
    product_class      TEXT,
    lot_ref            TEXT,
    event_summary      TEXT,
    rca_summary        TEXT,
    recurrence_of      TEXT,
    ingested_at        TEXT
);
CREATE TABLE capa_records (
    capa_id        TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    deviation_id   TEXT NOT NULL,
    capa_type      TEXT,
    action_class   TEXT,
    action_summary TEXT,
    owner_role     TEXT,
    implemented_on TEXT
);
CREATE TABLE effectiveness_checks (
    check_id            TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    capa_id             TEXT NOT NULL,
    checked_on          TEXT,
    effective           INTEGER,
    recurrence_observed INTEGER,
    evidence_note       TEXT,
    source              TEXT,
    operator_edited     INTEGER
);
"""

TENANT_A = "ACME:SITE1"
TENANT_B = "OTHER:SITE9"


class SqliteDatabase(BaseDatabase):
    """A real BaseDatabase implementation (sqlite3, `:name` paramstyle).

    This is a *real* database — the SQL is parsed, planned and executed. It is
    not a stand-in that pattern-matches on the query string.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    def execute(self, query: str, params: Optional[dict] = None) -> Any:
        cur = self._conn.execute(query, params or {})
        self._conn.commit()
        return cur.rowcount

    def fetch_one(self, query: str, params: Optional[dict] = None) -> Optional[dict]:
        row = self._conn.execute(query, params or {}).fetchone()
        return dict(row) if row else None

    def fetch_many(self, query: str, params: Optional[dict] = None) -> list[dict]:
        return [dict(r) for r in self._conn.execute(query, params or {}).fetchall()]


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(DDL)
    return c


@pytest.fixture()
def kb(conn: sqlite3.Connection) -> KBService:
    return KBService(db=SqliteDatabase(conn))


def _seed_case(kb: KBService, tenant: str, source_record_id: str = "QMS-2024-001") -> str:
    return kb.insert_deviation_case(
        tenant,
        {
            "source_record_id": source_record_id,
            "occurred_on": "2024-05-01",
            "deviation_category": "process",
            "process_type": "compression_filling",
            "root_cause_class": "method",
            "severity_class": "major",
            "equipment_class": "filler-A",
            "product_class": "tablet",
            # already sanitized upstream by PreProcessNode's PII gate —
            # role notation, never a worker's name
            "event_summary": "充填工程担当者Aが温度逸脱を検知",
            "rca_summary": "洗浄手順の不備",
        },
    )


def _seed_capa_with_effectiveness(kb: KBService, tenant: str, deviation_id: str) -> str:
    capa_id = kb.insert_capa_record(
        tenant,
        {
            "deviation_id": deviation_id,
            "capa_type": "corrective",
            "action_class": "procedure_revision",
            "action_summary": "洗浄手順書を改訂",
            "owner_role": "充填工程責任者",
            "implemented_on": "2024-05-20",
        },
    )
    kb.insert_effectiveness_check(
        tenant,
        {
            "capa_id": capa_id,
            "checked_on": "2024-08-01",
            "effective": True,
            "recurrence_observed": False,
            "evidence_note": "3ヶ月再発なし",
            "source": "ingest",
        },
    )
    return capa_id


class TestPb3IngestWrites:
    """PB-3: the ingest path's INSERTs execute against a real engine."""

    def test_insert_deviation_case_executes(self, kb: KBService, conn: sqlite3.Connection) -> None:
        deviation_id = _seed_case(kb, TENANT_A)
        row = conn.execute(
            "SELECT tenant_id, source_record_id FROM deviation_cases WHERE deviation_id = ?",
            (deviation_id,),
        ).fetchone()
        assert row is not None
        assert row["tenant_id"] == TENANT_A
        assert row["source_record_id"] == "QMS-2024-001"

    def test_insert_capa_and_effectiveness_execute(self, kb: KBService, conn: sqlite3.Connection) -> None:
        deviation_id = _seed_case(kb, TENANT_A)
        capa_id = _seed_capa_with_effectiveness(kb, TENANT_A, deviation_id)
        assert conn.execute("SELECT 1 FROM capa_records WHERE capa_id = ?", (capa_id,)).fetchone() is not None
        assert conn.execute("SELECT 1 FROM effectiveness_checks WHERE capa_id = ?", (capa_id,)).fetchone() is not None


class TestPb3RetrievalQueries:
    """PB-3: Stage 2's SELECTs execute — the regression that a FakeDatabase hides."""

    def test_search_similar_deviations_executes(self, kb: KBService) -> None:
        _seed_case(kb, TENANT_A)
        rows = kb.search_similar_deviations(
            tenant_id=TENANT_A,
            deviation_category="process",
            process_type="compression_filling",
            root_cause_class="method",
            limit=8,
        )
        assert rows, "same-tenant similar deviation must be retrieved"
        assert rows[0]["source_id"], "retrieved_records need a source_id for the S-3 gate"

    def test_search_capa_effectiveness_executes_with_multiple_ids(self, kb: KBService) -> None:
        """🔴 REGRESSION GUARD — `IN :deviation_ids` bound to a tuple raised
        `OperationalError: near ":deviation_ids": syntax error` on a real engine.
        The IN list must expand to one placeholder per id. Multiple ids are used
        here because a single-element list would still pass a naive one-
        placeholder implementation.
        """
        dev_1 = _seed_case(kb, TENANT_A, "QMS-2024-001")
        dev_2 = _seed_case(kb, TENANT_A, "QMS-2024-002")
        _seed_capa_with_effectiveness(kb, TENANT_A, dev_1)
        _seed_capa_with_effectiveness(kb, TENANT_A, dev_2)

        rows = kb.search_capa_effectiveness(tenant_id=TENANT_A, deviation_ids=[dev_1, dev_2], limit=16)

        assert len(rows) == 2, "both deviations' CAPA effectiveness records must come back"
        assert {r["deviation_id"] for r in rows} == {dev_1, dev_2}
        # sanitized attributes only — never the free-text action_summary
        assert "action_summary" not in rows[0]

    def test_capa_ids_are_bound_not_interpolated(self, kb: KBService) -> None:
        """A malicious id must not terminate the SQL statement — the ids are
        bound as parameters, never spliced into the query text."""
        dev_1 = _seed_case(kb, TENANT_A)
        _seed_capa_with_effectiveness(kb, TENANT_A, dev_1)

        rows = kb.search_capa_effectiveness(
            tenant_id=TENANT_A,
            deviation_ids=[dev_1, "x'); DROP TABLE capa_records; --"],
            limit=16,
        )

        assert len(rows) == 1  # the injection string simply matches nothing
        # the table is still there
        assert kb.search_capa_effectiveness(tenant_id=TENANT_A, deviation_ids=[dev_1]) != []


class TestPb3TenantIsolation:
    """PB-3 + docs/02_design.md: tenant scoping is enforced *in the SQL*, not
    only in the service layer's Python guard."""

    def test_other_tenant_cannot_see_deviations(self, kb: KBService) -> None:
        _seed_case(kb, TENANT_A)
        rows = kb.search_similar_deviations(
            tenant_id=TENANT_B,
            deviation_category="process",
            process_type="compression_filling",
            root_cause_class="method",
            limit=8,
        )
        assert rows == [], "cross-tenant leakage: tenant B must never see tenant A's deviations"

    def test_other_tenant_cannot_see_capa_effectiveness(self, kb: KBService) -> None:
        dev_1 = _seed_case(kb, TENANT_A)
        _seed_capa_with_effectiveness(kb, TENANT_A, dev_1)
        rows = kb.search_capa_effectiveness(tenant_id=TENANT_B, deviation_ids=[dev_1], limit=16)
        assert rows == [], "cross-tenant leakage on the CAPA-effectiveness path"

    def test_other_tenant_cannot_update_effectiveness(self, kb: KBService, conn: sqlite3.Connection) -> None:
        """Tenant B must neither overwrite tenant A's row NOR be told the write
        succeeded. The tenant-scoped WHERE means the UPDATE simply matches zero
        rows — reporting success there would write "effectiveness recorded" into
        the GMP audit trail while nothing was persisted, so it fails closed."""
        dev_1 = _seed_case(kb, TENANT_A)
        capa_id = _seed_capa_with_effectiveness(kb, TENANT_A, dev_1)

        with pytest.raises(CapaNotFoundError):
            kb.update_effectiveness(
                tenant_id=TENANT_B,
                capa_id=capa_id,
                effective=False,
                recurrence_observed=True,
                evidence_note="attacker",
            )

        row = conn.execute(
            "SELECT effective, evidence_note FROM effectiveness_checks WHERE capa_id = ?",
            (capa_id,),
        ).fetchone()
        assert row["effective"] == 1, "tenant B must not be able to overwrite tenant A's record"
        assert row["evidence_note"] == "3ヶ月再発なし"

    def test_unknown_capa_id_is_not_reported_as_recorded(self, kb: KBService) -> None:
        """An unknown capa_id matches no row — it must raise, not silently
        return {"recorded": True} (review High, found via the real-DB probe)."""
        with pytest.raises(CapaNotFoundError):
            kb.update_effectiveness(
                tenant_id=TENANT_A,
                capa_id="capa_does_not_exist",
                effective=True,
                recurrence_observed=False,
                evidence_note="note",
            )


class TestPb3FeedbackRoundTrip:
    """PB-3: ingest -> retrieval -> feedback closes the loop on a real engine."""

    def test_feedback_updates_effectiveness_and_marks_operator_edited(
        self, kb: KBService, conn: sqlite3.Connection
    ) -> None:
        dev_1 = _seed_case(kb, TENANT_A)
        capa_id = _seed_capa_with_effectiveness(kb, TENANT_A, dev_1)

        kb.update_effectiveness(
            tenant_id=TENANT_A,
            capa_id=capa_id,
            effective=False,
            recurrence_observed=True,
            evidence_note="再発を確認",
        )

        row = conn.execute(
            "SELECT effective, recurrence_observed, operator_edited, source "
            "FROM effectiveness_checks WHERE capa_id = ?",
            (capa_id,),
        ).fetchone()
        assert row["effective"] == 0
        assert row["recurrence_observed"] == 1
        assert row["operator_edited"], "feedback must set the operator_edited improvement signal"
        assert row["source"] == "feedback"

    def test_feedback_result_is_visible_to_the_next_retrieval(self, kb: KBService) -> None:
        """The moat: what the operator recorded must come back on the next
        invoke's CAPA-effectiveness recall."""
        dev_1 = _seed_case(kb, TENANT_A)
        capa_id = _seed_capa_with_effectiveness(kb, TENANT_A, dev_1)
        kb.update_effectiveness(
            tenant_id=TENANT_A,
            capa_id=capa_id,
            effective=False,
            recurrence_observed=True,
            evidence_note="再発を確認",
        )

        rows = kb.search_capa_effectiveness(tenant_id=TENANT_A, deviation_ids=[dev_1], limit=16)

        assert rows, "the CAPA record must still be retrievable after feedback"
        assert rows[0]["effective"] == 0, "the recorded ineffectiveness must be recalled"
        assert rows[0]["recurrence_observed"] == 1


class TestPb3KbHasNoPiiResidue:
    """docs/02_design.md データ統治契約: the KB holds sanitized structured data
    only — no worker names, no employee numbers, no raw source file."""

    def test_no_pii_pattern_in_any_kb_row(self, kb: KBService, conn: sqlite3.Connection) -> None:
        dev_1 = _seed_case(kb, TENANT_A)
        _seed_capa_with_effectiveness(kb, TENANT_A, dev_1)

        pii = re.compile(r"[一-鿿]{2,4}\s*(さん|氏)|従業員番号|[A-Z]{2}\d{5,}")
        for table in ("deviation_cases", "capa_records", "effectiveness_checks"):
            for row in conn.execute(f"SELECT * FROM {table}"):  # noqa: S608 — fixed table names
                for value in dict(row).values():
                    if isinstance(value, str):
                        assert not pii.search(value), f"PII residue in {table}: {value!r}"


class TestPb3FailClosedContract:
    """The two fail-closed guards on the feedback / recall paths, both found by
    running the real engine (a FakeDatabase that ignores SQL and returns None
    hid them both)."""

    def test_bool_rowcount_is_not_accepted_as_success(self) -> None:
        """`bool` is a subclass of `int`. An adapter returning True as a generic
        "ok" would pass `isinstance(rowcount, int)` and be read as "1 row
        updated" — the exact false-success this guard exists to prevent."""

        class BoolRowcountDatabase(BaseDatabase):
            def execute(self, query: str, params: Optional[dict] = None) -> Any:
                return True  # "ok", not a rowcount

            def fetch_one(self, query: str, params: Optional[dict] = None) -> Optional[dict]:
                return None

            def fetch_many(self, query: str, params: Optional[dict] = None) -> list[dict]:
                return []

        kb = KBService(db=BoolRowcountDatabase())
        with pytest.raises(CapaNotFoundError):
            kb.update_effectiveness(TENANT_A, "capa_1", True, False, "note")

    def test_none_rowcount_is_not_accepted_as_success(self) -> None:
        """An adapter that cannot report a rowcount returns None — "unknown"
        must be treated as "not proven written"."""

        class NoRowcountDatabase(BaseDatabase):
            def execute(self, query: str, params: Optional[dict] = None) -> Any:
                return None

            def fetch_one(self, query: str, params: Optional[dict] = None) -> Optional[dict]:
                return None

            def fetch_many(self, query: str, params: Optional[dict] = None) -> list[dict]:
                return []

        kb = KBService(db=NoRowcountDatabase())
        with pytest.raises(CapaNotFoundError):
            kb.update_effectiveness(TENANT_A, "capa_1", True, False, "note")

    def test_oversized_id_list_raises_instead_of_truncating(self, kb: KBService) -> None:
        """Silently truncating the id list would hand the draft a PARTIAL
        CAPA-effectiveness recall that reads as the site's complete history."""
        with pytest.raises(ValueError, match="partial"):
            kb.search_capa_effectiveness(
                tenant_id=TENANT_A,
                deviation_ids=[f"dev_{i}" for i in range(500)],
                limit=16,
            )

    def test_duplicate_ids_are_deduplicated(self, kb: KBService) -> None:
        dev_1 = _seed_case(kb, TENANT_A)
        _seed_capa_with_effectiveness(kb, TENANT_A, dev_1)
        rows = kb.search_capa_effectiveness(tenant_id=TENANT_A, deviation_ids=[dev_1, dev_1, dev_1], limit=16)
        assert len(rows) == 1, "duplicate ids must not duplicate the recalled record"
