"""HCR-C2-062 — S-3 layer-2 quality-verdict lexicon gate unit tests.

docs/02_design.md S-3 第2層: NFKC + casefold 正規化 -> 文単位分割 -> 該当文を
実置換。全文該当なら fail closed (is_safe=False)。
"""

from __future__ import annotations

import pytest

from src.services.quality_verdict_gate import load_lexicon, scan_and_sanitize, scan_and_sanitize_fields


class TestScanAndSanitize:
    def setup_method(self):
        self.lexicon = load_lexicon()

    def test_lone_assertive_sentence_fails_closed(self):
        """A single sentence that is entirely assertive has no factual
        content left after neutralization — must fail closed, not emit an
        all-boilerplate string (docs/02_design.md S-3 第2層)."""
        text = "本ロットは出荷可能である。"
        sanitized, is_safe, detected = scan_and_sanitize(text, self.lexicon)
        assert not is_safe
        assert "shippability_verdict" in detected

    def test_assertive_sentence_among_others_replaced(self):
        text = "類似逸脱DEV-001が過去に記録されている。本ロットは出荷可能である。"
        sanitized, is_safe, detected = scan_and_sanitize(text, self.lexicon)
        assert is_safe
        assert "出荷可能" not in sanitized
        assert "shippability_verdict" in detected

    def test_non_assertive_sentence_unchanged(self):
        text = "類似逸脱DEV-001が過去に記録されている。"
        sanitized, is_safe, detected = scan_and_sanitize(text, self.lexicon)
        assert sanitized == text
        assert is_safe
        assert detected == []

    def test_mixed_sentences_only_flagged_replaced(self):
        text = "類似逸脱DEV-001が過去に記録されている。品質への影響なしと判断する。"
        sanitized, is_safe, detected = scan_and_sanitize(text, self.lexicon)
        assert is_safe
        assert "類似逸脱DEV-001が過去に記録されている。" in sanitized
        assert "品質への影響なし" not in sanitized

    def test_entire_text_flagged_fails_closed(self):
        text = "出荷可能である。"
        sanitized, is_safe, detected = scan_and_sanitize(text, self.lexicon)
        assert not is_safe

    def test_fullwidth_halfwidth_bypass_prevented(self):
        # Fullwidth variant of ASCII-adjacent chars should still normalize the
        # same via NFKC — verifying the normalization pass actually runs.
        text = "規格に適合している。"
        _sanitized, _is_safe, detected = scan_and_sanitize(text, self.lexicon)
        assert "spec_conformance_verdict" in detected

    def test_casefold_variant_detected(self):
        # Lexicon patterns are pre-normalized at load time; verify a
        # non-normalized-looking input sentence still matches.
        text = "合格と判断する。"
        _sanitized, _is_safe, detected = scan_and_sanitize(text, self.lexicon)
        assert "pass_fail_verdict" in detected

    def test_empty_text_passthrough(self):
        sanitized, is_safe, detected = scan_and_sanitize("", self.lexicon)
        assert sanitized == ""
        assert is_safe
        assert detected == []


class TestScanAndSanitizeFields:
    def setup_method(self):
        self.lexicon = load_lexicon()

    def test_nested_dict_and_list_walked(self):
        fields = {
            "section": {
                "impact_assessment": "類似逸脱の記録がある。本ロットは出荷可能と判断する。",
                "notes": ["過去記録を参照。品質への影響なしと考える。", "根拠あり"],
            }
        }
        sanitized, all_safe, detected = scan_and_sanitize_fields(fields, self.lexicon)
        assert all_safe
        assert "出荷可能" not in sanitized["section"]["impact_assessment"]
        assert "品質への影響なし" not in sanitized["section"]["notes"][0]
        assert detected  # at least one category detected

    def test_lone_assertive_leaf_marks_all_safe_false(self):
        fields = {"impact_assessment": "出荷可能と判断する。"}
        _sanitized, all_safe, _detected = scan_and_sanitize_fields(fields, self.lexicon)
        assert not all_safe

    def test_non_string_values_passthrough(self):
        fields = {"count": 5, "flag": True, "value": None}
        sanitized, all_safe, _detected = scan_and_sanitize_fields(fields, self.lexicon)
        assert sanitized == fields
        assert all_safe


class TestGateLexiconLanguageMatchesTheLanguageThePromptRequests:
    """🔴 実LLM回帰（2026-08-20）: S-3 第2層の品質断定ガードは **日本語 lexicon**
    （config/quality_verdict_lexicon.yaml — 全 20 パターン日本語）だが、Stage 3 の
    プロンプトは英語で書かれ **出力言語を一切縛っていなかった**。そのため実 LLM は
    英語で回答し、英語の品質断定は lexicon に照合されないまま GMP 調査報告書へ
    流れる経路になっていた。単体/統合テストは FakeLLM が日本語を返すため日本語
    経路しか通っておらず、緑のまま潜伏していた。

    lexicon は schema_owner=customer_quality_assurance の版管理された統制文書なので
    こちらで英語パターンを足すのは不可（入力であって改変対象ではない）。よって
    プロンプト側で出力言語を日本語に固定した。

    ⚠️ ただしプロンプト指示は **LLM が従う限り**しか効かない。決定論的な塞ぎでは
    ないため、英語出力の fail-open 自体は未閉塞のまま残る（下の xfail 参照）。
    """

    def test_stage3_prompt_pins_the_output_language_to_the_guarded_language(self):
        """プロンプトが日本語出力を明示的に要求している（本修正の仕様テスト）。

        これが無いと実 LLM は（プロンプトが英語なので）英語で答え、S-3 第2層の
        品質断定ガードが素通りする。
        """
        from src.services.investigation_pipeline import _build_stage3_prompt

        prompt = _build_stage3_prompt(
            [{"source_id": "inc_1", "kind": "event_summary", "value": "充填工程で異常"}],
            [],
        )
        assert "日本語" in prompt, (
            "Stage 3 プロンプトが出力言語を日本語に固定していない — "
            "実 LLM が英語で答えると S-3 第2層の品質断定ガードが機能しない"
        )

    def test_the_guard_neutralizes_a_verdict_in_the_guarded_language(self):
        """要求している言語（日本語）の断定表現が実際に中和されること。

        「言語を固定した」だけでは不十分で、その言語でガードが動くことまで
        示して初めてこの経路の対処が意味を持つ。
        """
        lexicon = load_lexicon()
        sanitized, is_safe, detected = scan_and_sanitize(
            "類似逸脱DEV-001が過去に記録されている。本ロットは出荷可能である。", lexicon
        )
        assert "shippability_verdict" in detected
        assert "出荷可能" not in sanitized

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "未閉塞の既知欠陥: 英語の品質断定は日本語 lexicon に照合されず素通りする。"
            "出力言語の固定はプロンプト指示にすぎず、LLM が従わなければ効かない。"
            "決定論的に塞ぐには (a) LLM 応答後・lexicon 適用前に言語 admission gate を置き"
            "日本語以外を fail-closed にする か (b) QA の変更管理のもとで英語 lexicon を"
            "追加する、のいずれかが要る（どちらも統制文書のオーナー判断が必要）。"
            "この xfail は strict なので、塞がれた時点で『予期せず成功』として落ち、"
            "マーカーの除去と閉塞の記録を強制する。"
        ),
    )
    def test_an_english_quality_verdict_should_also_be_neutralized(self):
        """あるべき姿を assert する（現状は落ちるので xfail）。

        🔴 「英語は検知されない」を成功条件にすると **欠陥を仕様として固定** して
        しまい、QA が正規に英語パターンを追加した際に CI が落ちる。望ましい状態を
        書いて xfail(strict) で留めるのが正しい記録の仕方（レビュー指摘）。
        """
        lexicon = load_lexicon()
        sanitized, is_safe, detected = scan_and_sanitize(
            "類似逸脱DEV-001が過去に記録されている。" "This batch can be released and has no quality impact.",
            lexicon,
        )
        assert detected, "英語の品質断定が検知されていない"
        assert "can be released" not in sanitized
