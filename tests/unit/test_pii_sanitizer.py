"""HCR-C2-062 — worker-PII sanitizer unit tests (src/services/pii_sanitizer.py).

Covers docs/02_design.md 従業員 PII サニタイズ: name -> stable role token
replacement (same person -> same token within one record), employee-number
detection, and the SDK pii_detector Japanese-adjacency gap workaround.
"""

from __future__ import annotations

from src.services.pii_sanitizer import RoleTokenAssigner, sanitize_worker_pii


class TestRoleTokenAssigner:
    def test_same_name_gets_same_token(self):
        assigner = RoleTokenAssigner()
        t1 = assigner.token_for("山田太郎さん")
        t2 = assigner.token_for("山田太郎さん")
        assert t1 == t2

    def test_different_names_get_different_tokens(self):
        assigner = RoleTokenAssigner()
        t1 = assigner.token_for("山田太郎さん")
        t2 = assigner.token_for("鈴木花子さん")
        assert t1 != t2


class TestSanitizeWorkerPii:
    def test_honorific_name_replaced(self):
        text = "山田太郎さんが確認しました。"
        sanitized, count, resolvable = sanitize_worker_pii(text)
        assert resolvable
        assert count >= 1
        assert "山田太郎" not in sanitized
        assert "工程担当者" in sanitized

    def test_same_person_consistent_token_within_record(self):
        text = "山田太郎さんが操作し、その後山田太郎さんが確認した。"
        sanitized, _count, resolvable = sanitize_worker_pii(text)
        assert resolvable
        # Both occurrences should map to the identical role token.
        assert sanitized.count("工程担当者A") == 2

    def test_employee_number_removed(self):
        text = "社員番号: EMP12345 が担当した。"
        sanitized, count, resolvable = sanitize_worker_pii(text)
        assert resolvable
        assert count >= 1
        assert "EMP12345" not in sanitized

    def test_empty_text_returns_empty(self):
        sanitized, count, resolvable = sanitize_worker_pii("")
        assert sanitized == ""
        assert count == 0
        assert resolvable

    def test_no_pii_text_unchanged(self):
        text = "充填工程で温度異常を検知した。"
        sanitized, count, resolvable = sanitize_worker_pii(text)
        assert sanitized == text
        assert count == 0
        assert resolvable

    def test_email_supplementary_pattern_removed(self):
        text = "連絡先はtest.user@example.comです。"
        sanitized, count, resolvable = sanitize_worker_pii(text)
        assert resolvable
        assert "test.user@example.com" not in sanitized
