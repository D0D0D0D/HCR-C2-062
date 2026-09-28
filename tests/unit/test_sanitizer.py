"""HCR-C2-062 — S-2 channel sanitizer unit tests (src/services/sanitizer.py)."""

from __future__ import annotations

from src.services.sanitizer import (
    neutralize_csv_formula_cell,
    sanitize_csv_bytes,
    sanitize_docx_bytes,
    sanitize_xlsx_bytes,
    sanitize_input,
    sniff_channel,
    truncate_free_text,
)


class TestSniffChannel:
    def test_pdf_rejected_v1_scope(self):
        assert sniff_channel(b"%PDF-1.4 fake") is None

    def test_unknown_binary_rejected(self):
        assert sniff_channel(b"\x00\x01\x02\x03binarygarbage") is None

    def test_csv_detected(self):
        assert sniff_channel(b"a,b,c\n1,2,3\n") == "csv"

    def test_empty_bytes_rejected(self):
        assert sniff_channel(b"") is None


class TestCsvFormulaNeutralization:
    def test_equals_prefix_neutralized(self):
        assert neutralize_csv_formula_cell("=SUM(A1:A2)") == "'=SUM(A1:A2)"

    def test_plain_value_unchanged(self):
        assert neutralize_csv_formula_cell("normal text") == "normal text"

    def test_plus_minus_at_prefixes_neutralized(self):
        for prefix in ("+", "-", "@"):
            assert neutralize_csv_formula_cell(f"{prefix}cmd").startswith("'")


class TestSanitizeCsvBytes:
    def test_valid_csv_parsed(self):
        raw = "Deviation Number,Date Occurred,Event Description\nDEV-001,2026-01-01,test\n".encode()
        result = sanitize_csv_bytes(raw)
        assert result.accepted
        assert result.sanitized_rows == [
            {"Deviation Number": "DEV-001", "Date Occurred": "2026-01-01", "Event Description": "test"}
        ]

    def test_formula_injection_neutralized(self):
        raw = "col1,col2\n=cmd|calc,safe\n".encode()
        result = sanitize_csv_bytes(raw)
        assert result.accepted
        assert result.sanitized_rows[0]["col1"] == "'=cmd|calc"

    def test_empty_csv_rejected(self):
        result = sanitize_csv_bytes(b"")
        assert not result.accepted


class TestSanitizeDocxBytes:
    def test_missing_document_xml_rejected(self):
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("not_a_word_doc.txt", "hello")
        result = sanitize_docx_bytes(buf.getvalue())
        assert not result.accepted

    def test_not_a_zip_rejected(self):
        result = sanitize_docx_bytes(b"not a zip file at all")
        assert not result.accepted


class TestSanitizeInputTopLevel:
    def test_oversized_payload_rejected(self):
        raw = b"a,b\n1,2\n" * 1000
        result = sanitize_input(raw, max_file_bytes=10)
        assert not result.accepted
        assert "max_file_bytes" in result.reject_reason

    def test_unknown_format_rejected_allowlist(self):
        result = sanitize_input(b"%PDF-1.4 whatever")
        assert not result.accepted

    def test_xlsm_macro_workbook_rejected(self):
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("xl/workbook.xml", "<workbook/>")
            zf.writestr("xl/vbaProject.bin", b"\x00\x01")
        result = sanitize_input(buf.getvalue())
        assert not result.accepted
        assert "macro" in result.reject_reason.lower()

    def test_valid_csv_accepted(self):
        raw = "a,b\n1,2\n".encode()
        result = sanitize_input(raw)
        assert result.accepted
        assert result.channel == "csv"


class TestTruncateFreeText:
    def test_short_text_unchanged(self):
        text, truncated = truncate_free_text("short", 100)
        assert text == "short"
        assert not truncated

    def test_long_text_truncated(self):
        text, truncated = truncate_free_text("a" * 200, 50)
        assert len(text) == 50
        assert truncated

    def test_none_text_returns_empty(self):
        text, truncated = truncate_free_text(None, 50)
        assert text == ""
        assert not truncated


class TestRealFileParsing:
    """The S-2 channel sanitizers must survive REAL files, not just crafted byte
    strings. This mirrors the PB-3 lesson: a code path that is only ever fed a
    hand-made stand-in can be broken in a way no test notices — the real
    `openpyxl` workbook / real `.docx` container is what proves the parser.
    """

    def test_real_xlsx_workbook_is_parsed(self):
        """A genuine openpyxl-written .xlsx must be accepted and its rows
        extracted (v1 accepts CSV / xlsx / docx — docs/02_design.md)."""
        import io

        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["source_record_id", "occurred_on", "event_summary"])
        ws.append(["QMS-2024-001", "2024-05-01", "充填工程で温度逸脱"])
        # a formula-injection cell must be neutralized, not evaluated
        ws.append(["QMS-2024-002", "2024-05-02", "=cmd|'/c calc'!A1"])
        buf = io.BytesIO()
        wb.save(buf)

        result = sanitize_xlsx_bytes(buf.getvalue())

        assert result.accepted, f"real .xlsx must parse, got: {result.reject_reason}"
        assert result.channel == "xlsx"
        assert len(result.sanitized_rows) == 2
        assert result.sanitized_rows[0]["source_record_id"] == "QMS-2024-001"
        # formula neutralized (prefixed), never left as a live formula
        assert not result.sanitized_rows[1]["event_summary"].startswith("=")

    def test_real_docx_document_text_is_extracted(self):
        """A genuine .docx container (ZIP + word/document.xml with WordprocessingML
        text runs) must be accepted and its text extracted."""
        import io
        import zipfile

        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            "<w:p><w:r><w:t>逸脱番号: QMS-2024-001</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>事象: 充填工程で温度逸脱を検知</w:t></w:r></w:p>"
            "</w:body></w:document>"
        ).encode()

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("word/document.xml", document_xml)

        result = sanitize_docx_bytes(buf.getvalue())

        assert result.accepted, f"real .docx must parse, got: {result.reject_reason}"
        assert result.channel == "docx"
        text = result.sanitized_text or ""
        assert "QMS-2024-001" in text
        assert "充填工程で温度逸脱を検知" in text
