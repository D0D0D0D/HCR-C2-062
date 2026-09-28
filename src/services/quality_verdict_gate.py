"""AgentCore Platform v1.0 — HCR-C2-062: S-3 second layer (quality-verdict gate)

docs/02_design.md S-3 設計 第2層（品質断定ガード）:
  Layer 1 (structural) is enforced by NEVER defining a quality-verdict /
  shippability field in the output schema (schema test, not this module).
  Layer 2 (lexicon) is this module: assertive statements ("品質への影響な
  し", "出荷可能", "規格適合" etc.) in free text are detected and REPLACED
  (not merely flagged) with a non-assertive template sentence, sentence by
  sentence, after NFKC normalize + casefold (full/half-width & case bypass
  prevention — SubcontractorQuotationDisputeHistory legal_output_gate pattern, adapted for the GMP
  quality-verdict lexicon rather than legal-assertion lexicon).

  If EVERY sentence in the text is flagged, no factual content survives
  neutralisation -> caller must fail closed (AgentStatus.ERROR.value).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_LEXICON_PATH = Path(__file__).resolve().parents[2] / "config" / "quality_verdict_lexicon.yaml"

# Japanese sentence boundary split, keeping the terminator with the
# preceding sentence so each sentence can be independently judged.
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？])")


@dataclass(frozen=True)
class QualityVerdictLexicon:
    schema_owner: str
    last_reviewed_at: str
    version: int
    categories: dict[str, list[str]]
    non_assertive_replacement_template: str
    disclaimer: str


def _normalize(text: str) -> str:
    """NFKC normalize + casefold — full/half-width & case bypass prevention."""
    return unicodedata.normalize("NFKC", text).casefold()


def load_lexicon(path: Path | str = _DEFAULT_LEXICON_PATH) -> QualityVerdictLexicon:
    with open(path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)
    categories = {
        cat_name: [_normalize(p) for p in cat["patterns"]] for cat_name, cat in raw.get("categories", {}).items()
    }
    return QualityVerdictLexicon(
        schema_owner=raw.get("schema_owner", ""),
        last_reviewed_at=raw.get("last_reviewed_at", ""),
        version=raw.get("version", 0),
        categories=categories,
        non_assertive_replacement_template=raw.get("non_assertive_replacement_template", ""),
        disclaimer=raw.get("disclaimer", ""),
    )


def _sentence_is_flagged(sentence: str, lexicon: QualityVerdictLexicon) -> list[str]:
    normalized = _normalize(sentence)
    detected: list[str] = []
    for cat_name, patterns in lexicon.categories.items():
        if any(p in normalized for p in patterns):
            detected.append(cat_name)
    return detected


def scan_and_sanitize(text: str, lexicon: QualityVerdictLexicon) -> tuple[str, bool, list[str]]:
    """Scan text for prohibited quality-verdict assertions and sanitize.

    Splits `text` into sentences, flags each sentence independently, and
    REPLACES every flagged sentence with the non-assertive template — the
    prohibited wording itself never survives into the returned string.
    Unflagged sentences pass through unchanged.

    Returns (sanitized_text, is_safe, detected_categories). is_safe=False
    means every sentence was flagged (no factual content survives) and the
    caller must fail closed (AgentStatus.ERROR.value) rather than emit an
    all-boilerplate string.
    """
    stripped = text.strip()
    if not stripped:
        return text, True, []

    sentences = [s for s in _SENTENCE_SPLIT.split(stripped) if s.strip()]
    if not sentences:
        sentences = [stripped]

    detected_all: list[str] = []
    output_sentences: list[str] = []
    flagged_count = 0

    for sentence in sentences:
        cats = _sentence_is_flagged(sentence, lexicon)
        if cats:
            detected_all.extend(c for c in cats if c not in detected_all)
            flagged_count += 1
            output_sentences.append(lexicon.non_assertive_replacement_template)
        else:
            output_sentences.append(sentence)

    if not detected_all:
        return text, True, []

    if flagged_count == len(sentences):
        return text, False, detected_all

    sanitized = "".join(output_sentences)
    return sanitized, True, detected_all


def scan_and_sanitize_fields(fields: dict[str, Any], lexicon: QualityVerdictLexicon) -> tuple[dict[str, Any], bool, list[str]]:
    """Apply scan_and_sanitize() to every string leaf in a nested dict/list
    structure (report sections). Returns (sanitized_fields, all_safe,
    all_detected_categories)."""
    all_detected: list[str] = []
    all_safe = True

    def _walk(value: Any) -> Any:
        nonlocal all_safe
        if isinstance(value, str):
            sanitized, safe, detected = scan_and_sanitize(value, lexicon)
            all_detected.extend(c for c in detected if c not in all_detected)
            if not safe:
                all_safe = False
            return sanitized
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    sanitized_fields = _walk(fields)
    return sanitized_fields, all_safe, all_detected
