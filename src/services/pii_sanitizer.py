"""AgentCore Platform v1.0 — HCR-C2-062: worker PII -> role-token sanitizer.

docs/02_design.md S-2 従業員 PII サニタイズ:
  Deviation records/reports carry worker names and employee numbers. These
  must NOT reach the KB. Default behaviour is REPLACEMENT with a role token
  ("充填工程担当者A"), not blanket redaction — the same person referenced
  twice in one record gets the SAME token so the narrative stays internally
  consistent. Unresolvable input fails closed (reject), per design.

KNOWN GAP in `shared.security.pii_detector.detect_pii` (CorporateGroupTripPlannerAgent finding,
confirmed again in this session's design review):
  1. No Japanese personal-name detection at all (Latin Title-Case heuristic
     only — never matches e.g. "田中太郎").
  2. Patterns ending in a trailing `\\b` (email, phone_jp, ...) fail to
     match when immediately followed by Japanese text with no delimiter
     (Unicode `\\b` treats the last ASCII char and the following Japanese
     char as adjacent word characters -> no boundary -> match fails).
`shared.security.pii_detector` is vendored `shared/` and MUST NOT be edited
here — this module runs it first, then a supplementary Japanese-name pass,
applied at all 4 mandated points: ① S-2 input ② KB write ③ S-3 output
rescan ④ HITL resume response (scrub-first).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from shared.security.pii_detector import detect_pii

# Honorific-suffixed Japanese personal name — high precision (the honorific
# is what marks this as a person, not a place/equipment/process noun).
#
# {2,4} (not {2,10}): real Japanese full names are 2-4 kanji characters
# (family + given name). A wide quantifier lets the class greedily backfill
# across an unrelated preceding clause when the same person is named twice
# in one sentence (e.g. in "山田太郎さんが操作し、その後山田太郎さんが確認し
# た.", a {2,10} cap lets finditer's SECOND match start at "その後" and
# consume it as if it were part of the name, since "そ/の/後" are also in the
# kanji/hiragana class — this produces a DIFFERENT matched span for the same
# person's second mention, breaking the same-person -> same-token
# invariant). Capping at {2,4} for the kanji-run keeps the match tight to an
# actual name-length span; kana-only given names (rarer in formal QMS
# records) are still covered by _EMPLOYEE_NO / _NAME_FULL for the common
# "family kana given" spacing pattern.
_NAME_HONORIFIC = re.compile(r"[一-鿿]{2,4}(?:さん|様|氏|くん|君|ちゃん)")
# "山田 太郎" style full-name with a mid-name space, followed by a role/verb
# boundary (作業員/担当/が/は/の) — narrower to avoid over-matching process
# terminology composed of two kanji compounds.
_NAME_FULL = re.compile(
    r"[一-鿿]{1,4}[ 　][一-鿿]{1,4}(?=(作業員|担当|オペレータ|技術者|検査員|は|が|の|さん|氏))"
)
# Employee number: 社員番号-style alnum code, 4-10 chars, optionally prefixed.
_EMPLOYEE_NO = re.compile(r"(?:社員番号|従業員番号|職員番号)[:：]?\s*[A-Za-z0-9\-]{3,12}")
# Supplementary email/phone without a trailing \b (fixes the Japanese-text
# adjacency gap in shared.security.pii_detector).
_EMAIL_SUPPL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_JP_SUPPL = re.compile(r"(?<!\d)(?:\+81[-\s]?\d{1,4}[-\s]\d{1,4}[-\s]\d{4}|0\d{1,4}[-\s]\d{1,4}[-\s]\d{4})")

_DEFAULT_ROLE_PREFIX = "工程担当者"


@dataclass
class RoleTokenAssigner:
    """Assigns a stable role token to each distinct redacted name within one
    record — narrative consistency ("山田太郎さん" -> "充填工程担当者A" every
    time it recurs in the same event_summary)."""

    role_prefix: str = _DEFAULT_ROLE_PREFIX
    _assigned: dict[str, str] = field(default_factory=dict)
    _next_index: int = 0

    def token_for(self, raw_span: str) -> str:
        if raw_span in self._assigned:
            return self._assigned[raw_span]
        letter = chr(ord("A") + self._next_index) if self._next_index < 26 else str(self._next_index)
        token = f"{self.role_prefix}{letter}"
        self._assigned[raw_span] = token
        self._next_index += 1
        return token


def _find_name_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in (_NAME_HONORIFIC, _NAME_FULL):
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _find_contact_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for f in detect_pii(text):
        spans.append((f["start"], f["end"]))
    for pattern in (_EMPLOYEE_NO, _EMAIL_SUPPL, _PHONE_JP_SUPPL):
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _merge_spans(spans: list[tuple[int, int]]) -> list[list[int]]:
    if not spans:
        return []
    spans = sorted(spans)
    merged: list[list[int]] = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def sanitize_worker_pii(text: str, role_prefix: str = _DEFAULT_ROLE_PREFIX) -> tuple[str, int, bool]:
    """Replace worker names with stable role tokens; strip contact PII.

    Returns (sanitized_text, replacement_count, resolvable).
    `resolvable=False` means the text could not be safely sanitized (should
    not normally happen — name/contact matching always yields a replacement
    span — but callers must fail-closed/reject if it is ever False).
    """
    if not text:
        return "", 0, True

    name_spans = _find_name_spans(text)
    contact_spans = _find_contact_spans(text)

    assigner = RoleTokenAssigner(role_prefix=role_prefix)
    replacements: list[tuple[int, int, str]] = []

    for start, end in _merge_spans(name_spans):
        raw = text[start:end]
        # Strip a trailing honorific from the token key so "山田太郎さん" and
        # a bare "山田太郎" (if it recurred) still map to one token, but keep
        # matching narrow — token key is the matched span itself, which is
        # deterministic per-record.
        token = assigner.token_for(raw)
        replacements.append((start, end, token))

    for start, end in _merge_spans(contact_spans):
        replacements.append((start, end, "[連絡先情報除去済み]"))

    if not replacements:
        return text, 0, True

    # Merge name + contact replacement spans, sort, apply back-to-front so
    # earlier offsets stay valid.
    replacements.sort(key=lambda r: r[0])
    merged: list[tuple[int, int, str]] = []
    for start, end, token in replacements:
        if merged and start < merged[-1][1]:
            # Overlap — keep the earlier (already chosen) replacement.
            continue
        merged.append((start, end, token))

    sanitized = text
    for start, end, token in reversed(merged):
        sanitized = sanitized[:start] + token + sanitized[end:]

    return sanitized, len(merged), True
