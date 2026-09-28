"""HCR-C2-062 — KBService / TransientPayloadStore unit tests.

Covers docs/02_design.md テナント統制 (fail-closed) and the KB スキーマ
data-governance contract (lot_ref default non-storage, tenant filtering).
"""

from __future__ import annotations

import pytest

from src.services.kb_service import CapaNotFoundError, KBService, TransientPayloadStore


class TestTransientPayloadStore:
    def test_put_bytes_get_bytes_roundtrip(self):
        store = TransientPayloadStore()
        ref = store.put_bytes(b"raw file content")
        assert store.get_bytes(ref) == b"raw file content"

    def test_put_sanitized_replaces_raw_bytes(self):
        store = TransientPayloadStore()
        ref = store.put_bytes(b"raw content")
        store.put_sanitized(ref, {"sanitized": True})
        assert store.get_bytes(ref) is None  # raw bytes no longer readable
        assert store.get_sanitized(ref) == {"sanitized": True}

    def test_unknown_ref_returns_none(self):
        store = TransientPayloadStore()
        assert store.get_bytes("payload_ref_doesnotexist") is None
        assert store.get_sanitized("payload_ref_doesnotexist") is None

    def test_empty_ref_returns_none(self):
        store = TransientPayloadStore()
        assert store.get_bytes("") is None
        assert store.get_bytes(None) is None

    def test_put_sanitized_requires_ref(self):
        store = TransientPayloadStore()
        with pytest.raises(ValueError):
            store.put_sanitized("", {"x": 1})

    def test_ttl_expiry(self):
        store = TransientPayloadStore(ttl_seconds=0)
        ref = store.put_bytes(b"expires immediately")
        import time

        time.sleep(0.01)
        assert store.get_bytes(ref) is None


class TestKBServiceTenantFailClosed:
    def test_insert_deviation_case_requires_tenant(self, fake_db):
        kb = KBService(db=fake_db)
        with pytest.raises(ValueError):
            kb.insert_deviation_case("", {"source_record_id": "DEV-1"})

    def test_insert_without_db_raises(self):
        kb = KBService(db=None)
        with pytest.raises(RuntimeError):
            kb.insert_deviation_case("ACME:SITE1", {"source_record_id": "DEV-1"})

    def test_search_similar_deviations_no_db_returns_empty(self):
        kb = KBService(db=None)
        result = kb.search_similar_deviations("ACME:SITE1", None, None, None)
        assert result == []

    def test_search_similar_deviations_empty_tenant_returns_empty(self, fake_db):
        kb = KBService(db=fake_db)
        result = kb.search_similar_deviations("", None, None, None)
        assert result == []

    def test_search_capa_effectiveness_empty_ids_returns_empty(self, fake_db):
        kb = KBService(db=fake_db)
        result = kb.search_capa_effectiveness("ACME:SITE1", [])
        assert result == []

    def test_update_effectiveness_requires_tenant(self, fake_db):
        kb = KBService(db=fake_db)
        with pytest.raises(ValueError):
            kb.update_effectiveness("", "capa_1", True, False, "note")


class TestKBServiceLotRefGovernance:
    def test_lot_ref_not_stored_by_default(self, fake_db):
        kb = KBService(db=fake_db)
        kb.insert_deviation_case(
            "ACME:SITE1",
            {"source_record_id": "DEV-1", "lot_ref": "LOT-999"},  # no governance flag
        )
        _query, params = fake_db.executed[0]
        assert params["lot_ref"] is None

    def test_lot_ref_stored_when_governance_enabled(self, fake_db):
        kb = KBService(db=fake_db)
        kb.insert_deviation_case(
            "ACME:SITE1",
            {"source_record_id": "DEV-1", "lot_ref": "LOT-999", "lot_ref_governance_enabled": True},
        )
        _query, params = fake_db.executed[0]
        assert params["lot_ref"] == "LOT-999"


class TestKBServiceInsertsAndSearches:
    def test_insert_deviation_case_records_query(self, fake_db):
        kb = KBService(db=fake_db)
        deviation_id = kb.insert_deviation_case("ACME:SITE1", {"source_record_id": "DEV-1"})
        assert deviation_id.startswith("dev_")
        assert len(fake_db.executed) == 1

    def test_search_similar_deviations_returns_seeded_rows(self, fake_db):
        fake_db.set_fetch_many(
            "FROM deviation_cases",
            [{"source_id": "dev_1", "deviation_category": "process", "process_type": "cleaning"}],
        )
        kb = KBService(db=fake_db)
        result = kb.search_similar_deviations("ACME:SITE1", "process", None, None)
        assert result == [{"source_id": "dev_1", "deviation_category": "process", "process_type": "cleaning"}]

    def test_update_effectiveness_returns_recorded(self, fake_db):
        kb = KBService(db=fake_db)
        result = kb.update_effectiveness("ACME:SITE1", "capa_1", True, False, "note")
        assert result["capa_id"] == "capa_1"
        assert result["recorded"] is True
        assert result["rows_updated"] == 1

    def test_update_effectiveness_fails_closed_when_no_row_matched(self, fake_db):
        """rowcount 0 = unknown capa_id, or one belonging to another tenant
        (the WHERE clause is tenant-scoped). Reporting success there would put
        "effectiveness recorded" into the GMP audit trail while nothing was
        written — so it raises instead (review High, found via the real-DB probe).
        """
        fake_db.set_rowcount(0)
        kb = KBService(db=fake_db)
        with pytest.raises(CapaNotFoundError):
            kb.update_effectiveness("ACME:SITE1", "capa_unknown", True, False, "note")
