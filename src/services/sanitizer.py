"""AgentCore Platform v1.0 — HCR-C2-062: input-channel sanitization (S-2)

docs/02_design.md S-2:
  PreProcessNode._extra_security_gate_input() must NOT branch on `mode` —
  `mode` is written by execute(), so at S-2 gate time (which runs BEFORE
  execute()) it does not yet exist in state; branching on it always
  evaluates False and the check is silently skipped (fail-open — confirmed
  real bug class, SoilInvestigationNeighbouringCaseReferenceAgent).

  Channel/format is instead determined from the CONTENT of the input
  (magic-byte / lightweight MIME sniffing), never from client-declared
  Content-Type or file extension.

This module is a pure library (no framework/shared imports beyond typing) —
independently unit-testable, called from PreProcessNode.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

# ── magic-byte / content sniffing ───────────────────────────────────────

_XLSX_MAGIC = b"PK\x03\x04"  # zip container (xlsx/docx/xlsm all share this)
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")

_KNOWN_CHANNELS = ("csv", "xlsx", "docx")


@dataclass
class SanitizeResult:
    channel: str | None = None
    accepted: bool = True
    reject_reason: str | None = None
    sanitized_text: str | None = None  # decoded/neutralised textual content
    sanitized_rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def sniff_channel(raw_bytes: bytes, declared_name: str = "") -> str | None:
    """Determine the channel from CONTENT, never from extension/Content-Type.

    Returns one of _KNOWN_CHANNELS or None (unknown -> allowlist reject).
    """
    if not raw_bytes:
        return None

    if raw_bytes.startswith(_XLSX_MAGIC):
        # Zip container — disambiguate xlsx vs xlsm vs docx by inspecting
        # the internal manifest, never trusting the filename.
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                names = set(zf.namelist())
                if any(n.startswith("word/") for n in names):
                    return "docx"
                if "xl/workbook.xml" in names:
                    # Macro-enabled workbooks declare vbaProject.bin — reject
                    # channel resolves to a distinct sentinel so the caller
                    # can reject explicitly rather than silently treating it
                    # as a plain xlsx.
                    if "xl/vbaProject.bin" in names:
                        return "xlsm"
                    return "xlsx"
        except zipfile.BadZipFile:
            return None
        return None

    # Heuristic: printable text with a comma/semicolon-delimited first line
    # is treated as CSV. PDFs (%PDF-) and any other binary format are NOT
    # in the allowlist and fall through to None (reject).
    if raw_bytes.startswith(b"%PDF-"):
        return None  # v1 scope: PDF is out of scope (Design Decision Record)

    try:
        text_head = raw_bytes[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return None

    first_line = text_head.splitlines()[0] if text_head.splitlines() else ""
    if "," in first_line or ";" in first_line:
        return "csv"

    return None


def neutralize_csv_formula_cell(value: str) -> str:
    """Prefix a leading '=' '+' '-' '@' cell with a single-quote to prevent
    formula injection when the exported CSV is later opened in spreadsheet
    software."""
    if value and value[0] in _CSV_FORMULA_PREFIXES:
        return "'" + value
    return value


def sanitize_csv_bytes(raw_bytes: bytes) -> SanitizeResult:
    import csv as csv_module

    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return SanitizeResult(channel="csv", accepted=False, reject_reason="csv: non-utf8 payload")

    reader = csv_module.reader(io.StringIO(text))
    rows: list[list[str]] = []
    for row in reader:
        rows.append([neutralize_csv_formula_cell(cell) for cell in row])

    if not rows:
        return SanitizeResult(channel="csv", accepted=False, reject_reason="csv: empty payload")

    header = [h.strip() for h in rows[0]]
    records = [dict(zip(header, row)) for row in rows[1:]]
    return SanitizeResult(channel="csv", accepted=True, sanitized_rows=records)


def sanitize_xlsx_bytes(raw_bytes: bytes) -> SanitizeResult:
    """openpyxl, read_only, formulas are NEVER evaluated (values only)."""
    try:
        import openpyxl
    except ImportError:
        return SanitizeResult(channel="xlsx", accepted=False, reject_reason="xlsx: openpyxl unavailable")

    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=False)
    except Exception:  # noqa: BLE001 — any parse failure is a reject
        return SanitizeResult(channel="xlsx", accepted=False, reject_reason="xlsx: parse error")

    ws = wb[wb.sheetnames[0]]
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = [str(c) if c is not None else "" for c in next(rows_iter)]
    except StopIteration:
        return SanitizeResult(channel="xlsx", accepted=False, reject_reason="xlsx: empty sheet")

    records = []
    for row in rows_iter:
        values = []
        for cell in row:
            if isinstance(cell, str):
                values.append(neutralize_csv_formula_cell(cell))
            else:
                values.append(cell)
        records.append(dict(zip(header, values)))

    return SanitizeResult(channel="xlsx", accepted=True, sanitized_rows=records)


_DOCX_FORBIDDEN_TAGS = re.compile(rb"<[^>]*:(oleObject|control)[^>]*>", re.IGNORECASE)


def sanitize_docx_bytes(raw_bytes: bytes) -> SanitizeResult:
    """ZIP + word/document.xml text extraction via defusedxml.

    forbid_entities=True / forbid_external=True — XXE hardened. Macros and
    embedded objects are never read (we only ever look at document.xml's
    text runs).
    """
    try:
        from defusedxml import ElementTree as SafeET
    except ImportError:
        return SanitizeResult(channel="docx", accepted=False, reject_reason="docx: defusedxml unavailable")

    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            if "word/document.xml" not in zf.namelist():
                return SanitizeResult(channel="docx", accepted=False, reject_reason="docx: missing document.xml")
            xml_bytes = zf.read("word/document.xml")
    except zipfile.BadZipFile:
        return SanitizeResult(channel="docx", accepted=False, reject_reason="docx: not a valid zip container")

    try:
        # defusedxml.ElementTree.fromstring rejects external entities /
        # DTDs by default; forbid_entities/forbid_external kwargs are
        # accepted for explicitness on supporting versions.
        root = SafeET.fromstring(xml_bytes, forbid_entities=True, forbid_external=True)
    except TypeError:
        # Older defusedxml releases: fromstring() takes no such kwargs but
        # is hardened by default — call without them.
        try:
            root = SafeET.fromstring(xml_bytes)
        except Exception:  # noqa: BLE001
            return SanitizeResult(channel="docx", accepted=False, reject_reason="docx: XML parse error")
    except Exception:  # noqa: BLE001 — includes EntitiesForbidden etc.
        return SanitizeResult(channel="docx", accepted=False, reject_reason="docx: rejected unsafe XML")

    text_runs = [
        node.text or "" for node in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
    ]
    text = "".join(text_runs)
    if not text.strip():
        text_runs = [node.text or "" for node in root.iter() if node.tag.endswith("}t")]
        text = "".join(text_runs)

    return SanitizeResult(channel="docx", accepted=True, sanitized_text=text)


def sanitize_input(raw_bytes: bytes, max_file_bytes: int = 10_485_760) -> SanitizeResult:
    """Top-level S-2 entry point: sniff channel, dispatch, enforce allowlist.

    Unknown channel -> reject (allowlist, fail closed). Oversized payload
    -> reject (DoS / checkpoint-bloat guard).
    """
    if len(raw_bytes) > max_file_bytes:
        return SanitizeResult(accepted=False, reject_reason=f"payload exceeds max_file_bytes={max_file_bytes}")

    channel = sniff_channel(raw_bytes)
    if channel is None:
        return SanitizeResult(accepted=False, reject_reason="unknown or unsupported input format (allowlist reject)")

    if channel == "xlsm":
        return SanitizeResult(channel="xlsm", accepted=False, reject_reason="macro-enabled workbook (.xlsm) rejected")

    if channel == "csv":
        return sanitize_csv_bytes(raw_bytes)
    if channel == "xlsx":
        return sanitize_xlsx_bytes(raw_bytes)
    if channel == "docx":
        return sanitize_docx_bytes(raw_bytes)

    return SanitizeResult(accepted=False, reject_reason=f"unhandled channel: {channel}")


def truncate_free_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Cap free-text field length (prompt-injection surface reduction)."""
    if text is None:
        return "", False
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True
