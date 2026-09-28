"""HCR-C2-062 — shared test fixtures.

Fakes injected via the constructor seams (docs/02_design.md "LLM 注入シーム" /
KBService(db=...)) — no real DB/LLM connection in unit/integration tests
(real connectivity is verified in staging).
"""

from __future__ import annotations

import pytest

from shared.services.database.base_db import BaseDatabase
from shared.services.llm.base_llm import BaseLLM
from src.graph.graph import GmpDeviationCapaGraph
from src.services.kb_service import KBService, TransientPayloadStore


class FakeDatabase(BaseDatabase):
    """In-memory fake conforming to BaseDatabase — records every execute()
    call and lets tests seed fetch_many() responses per query substring."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict | None]] = []
        self._fetch_many_responses: list[tuple[str, list[dict]]] = []
        self._fetch_one_response: dict | None = None
        # A real DB driver reports how many rows a statement touched, and
        # KBService.update_effectiveness() fails closed on 0 (a feedback UPDATE
        # that matched nothing must never be reported as "recorded" — it would
        # put a false entry in the GMP audit trail). A fake that always returned
        # None would let that guard rot untested, so it models rowcount too:
        # writes affect 1 row by default; set_rowcount(0) simulates
        # unknown-capa_id / other-tenant.
        self._rowcount = 1

    def fetch_one(self, query: str, params: dict | None = None) -> dict | None:
        return self._fetch_one_response

    def fetch_many(self, query: str, params: dict | None = None) -> list[dict]:
        for substring, rows in self._fetch_many_responses:
            if substring in query:
                return rows
        return []

    def execute(self, query: str, params: dict | None = None) -> int:
        self.executed.append((query, params))
        return self._rowcount

    def set_fetch_many(self, query_substring: str, rows: list[dict]) -> None:
        self._fetch_many_responses.append((query_substring, rows))

    def set_rowcount(self, rowcount: int) -> None:
        """Model a statement that matched `rowcount` rows (0 = no such row in
        this tenant)."""
        self._rowcount = rowcount


class FakeLLM(BaseLLM):
    """Deterministic fake — returns a fixed content string so Stage 3/4 tests
    are reproducible without a real model call."""

    def __init__(self, content: str = "テスト事象記述: 充填工程で異常が発生した。") -> None:
        self._content = content

    def complete(self, messages: list) -> dict:
        return {"content": self._content, "tool_calls": [], "model": "fake-llm", "usage": {}}

    def stream(self, messages: list):
        yield self._content

    def bind_tools(self, tools: list) -> "FakeLLM":
        return self


@pytest.fixture
def fake_db() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


def base_state(**overrides) -> dict:
    """Minimal state dict satisfying InvocationContext.from_state()'s
    required keys (correlation_id/session_id/thread_id/trace_id) — shared
    across node unit tests so every test file does not need to rediscover
    the required-field set by trial and error."""
    from framework.schemas.trust_level import TrustLevel

    state = {
        "user_input": "",
        "input_context": {},
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "caller_id": "",
        "correlation_id": "test-corr",
        "session_id": "test-session",
        "thread_id": "test-thread",
        "trace_id": "test-trace",
        "hitl_allowed": True,
        "node_history": [],
        "error_log": [],
        "status": "pending",
    }
    state.update(overrides)
    return state


def _seed_live_kb(fake_db: FakeDatabase) -> KBService:
    kb = KBService(db=fake_db)
    kb.insert_deviation_case(
        "ACME:SITE1",
        {
            "source_record_id": "DEV-9",
            "occurred_on": "2026-01-01",
            "event_summary": "x",
            "deviation_category": "process",
        },
    )
    fake_db.set_fetch_many(
        "FROM deviation_cases",
        [
            {
                "source_id": "dev_seed",
                "deviation_category": "process",
                "process_type": "cleaning",
                "root_cause_class": "method",
                "severity_class": "major",
                "recurrence_of": None,
            }
        ],
    )
    fake_db.set_fetch_many(
        "FROM capa_records",
        [
            {
                "source_id": "capa_seed",
                "deviation_id": "dev_seed",
                "capa_type": "corrective",
                "action_class": "procedure_revision",
                "effective": True,
                "recurrence_observed": False,
            }
        ],
    )
    return kb


@pytest.fixture
def agent_config(monkeypatch) -> dict:
    fake_db = FakeDatabase()
    kb = _seed_live_kb(fake_db)
    original_invoke = GmpDeviationCapaGraph.invoke

    def invoke_without_hitl(self, user_input, *args, **kwargs):
        ctx = kwargs.get("ctx")
        if ctx is not None:
            ctx.hitl_allowed = False
        return original_invoke(self, user_input, *args, **kwargs)

    monkeypatch.setattr(GmpDeviationCapaGraph, "invoke", invoke_without_hitl)
    return {
        "memory_enabled": False,
        "hitl": {"enabled": False, "max_hitl": 12},
        "payload_store": TransientPayloadStore(),
        "kb_service": kb,
        "top_k_similar_deviations": 8,
    }


@pytest.fixture
def case_payload() -> str:
    return "incident"


@pytest.fixture
def live_input_context() -> dict:
    return {
        "mode": "invoke",
        "company_id": "ACME",
        "site_id": "SITE1",
        "free_text": "process anomaly",
    }
