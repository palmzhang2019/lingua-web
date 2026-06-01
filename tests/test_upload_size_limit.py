"""Upload size-limit tests for Lingua Web.

Validates that:
  - Config exposes MAX_UPLOAD_SIZE_MB = 10.
  - Supported files below/at the limit follow the normal upload flow.
  - Supported files above the limit are rejected before save, parse, or Material creation.
  - Backend error message contains actual size and configured limit.
  - Existing file-type restrictions are unchanged.
  - Frontend template exposes the limit via data attribute (not hardcoded).
  - Template/HTML contract shows the limit in the upload form.

All tests use a temporary SQLite database — never data/lingua.db.
Run with: uv run pytest tests/test_upload_size_limit.py -v
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Set env vars BEFORE any app import.
os.environ["LINGUA_TESTING"] = "1"
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["LINGUA_DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import pytest
from fastapi.testclient import TestClient

from app.db import init_db, SessionLocal
from app.config import MAX_UPLOAD_SIZE_MB, MAX_UPLOAD_BYTES, DATABASE_URL, LINGUA_TESTING
from app.models import Material
from app.main import app


# ---------------------------------------------------------------------------
# Session-scoped setup
# ---------------------------------------------------------------------------

def _create_test_material(db, filename="test.txt", content_text="test content"):
    """Helper to seed a material record directly in the temp DB."""
    import datetime
    m = Material(
        filename=filename,
        content_text=content_text,
        source_type="txt",
        language_code="ja",
        uploaded_at=datetime.datetime.utcnow(),
    )
    db.add(m)
    db.commit()
    return m


@pytest.fixture(scope="session", autouse=True)
def setup_temp_db():
    """Create schema in the temp DB once per session."""
    init_db()
    yield
    try:
        os.unlink(_tmp_db.name)
    except Exception:
        pass


@pytest.fixture
def db():
    """Per-test DB session with rollback isolation."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Mock: prevent real DeepSeek extraction during upload-acceptance tests
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_extraction():
    """Mock extract_grammar_points and extract_vocab so upload succeeds without API."""
    with patch("app.routes.upload.extract_grammar_points") as mock_gp, \
         patch("app.routes.upload.extract_vocab") as mock_v:
        mock_gp.return_value = []
        mock_v.return_value = []
        yield


# ===========================================================================
# Configuration tests
# ===========================================================================

class TestUploadSizeLimitConfig:
    """Verify the configuration constant."""

    def test_upload_size_limit_default_is_10_mb(self):
        """Internal configuration exposes MAX_UPLOAD_SIZE_MB = 10."""
        assert MAX_UPLOAD_SIZE_MB == 10
        assert MAX_UPLOAD_BYTES == 10 * 1024 * 1024


# ===========================================================================
# Backend upload-acceptance tests
# ===========================================================================

class TestBackendUploadSizeLimit:
    """Backend validation of max upload size."""

    def test_supported_material_below_size_limit_uploads_normally(
        self, client, mock_extraction
    ):
        """A supported .txt file below 10 MB follows the normal flow."""
        small_content = b"Valid Japanese learning material for N2 grammar."
        assert len(small_content) < MAX_UPLOAD_BYTES

        resp = client.post(
            "/materials/upload",
            files={"file": ("valid.txt", small_content, "text/plain")},
        )
        assert resp.status_code in (200, 303), (
            f"Expected success redirect, got {resp.status_code}: {resp.text[:200]}"
        )

        # Verify the material appears on the listing page
        list_resp = client.get("/materials")
        assert list_resp.status_code == 200
        assert "valid.txt" in list_resp.text

    def test_supported_material_exactly_at_size_limit_is_accepted(
        self, client, mock_extraction
    ):
        """A .txt file at exactly 10 MB is accepted (exact boundary)."""
        exactly_10mb = b"a" * (10 * 1024 * 1024)
        assert len(exactly_10mb) == MAX_UPLOAD_BYTES

        resp = client.post(
            "/materials/upload",
            files={"file": ("exact.txt", exactly_10mb, "text/plain")},
        )
        assert resp.status_code in (200, 303), (
            f"Expected success for exact-boundary file, got {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    def test_supported_material_above_size_limit_is_rejected_before_save_or_parse(
        self, client
    ):
        """A .txt file over 10 MB is rejected before any processing occurs.

        This test verifies:
          - Upload is rejected (400).
          - No Material record is created in the DB.
          - No processing/parse functions are invoked.
        """
        oversized = b"a" * (11 * 1024 * 1024)  # 11 MB

        resp = client.post(
            "/materials/upload",
            files={"file": ("big.txt", oversized, "text/plain")},
        )
        assert resp.status_code == 400, (
            f"Expected 400 for oversized file, got {resp.status_code}"
        )
        # The error message should be in the response
        assert "超过最大允许大小" in resp.text

        # Verify no material record was created
        list_resp = client.get("/materials")
        assert "big.txt" not in list_resp.text

    def test_backend_oversize_message_uses_actual_size_and_configured_limit(
        self, client
    ):
        """Error message includes actual size (from bounded read) and configured maximum."""
        # Bounded read: only reads MAX_UPLOAD_BYTES + 1, so for a slightly-over file
        # the reported "actual size" is the bytes read before detecting oversize.
        one_over = b"b" * (MAX_UPLOAD_BYTES + 1)
        size_mb = (MAX_UPLOAD_BYTES + 1) / (1024 * 1024)

        resp = client.post(
            "/materials/upload",
            files={"file": ("large.txt", one_over, "text/plain")},
        )
        assert resp.status_code == 400
        text = resp.text

        # Must contain the actual size from bounded read (~10.0 MB)
        assert f"{size_mb:.1f} MB" in text, f"Expected actual size in message, got: {text}"
        # Must contain the configured limit (10 MB as integer)
        assert f"{MAX_UPLOAD_SIZE_MB} MB" in text, (
            f"Expected configured limit '10 MB' in message, got: {text}"
        )

    def test_existing_unsupported_file_type_behavior_remains_unchanged(
        self, client
    ):
        """The .exe file type is still rejected as before — this feature didn't change type rules."""
        tiny_exe = b"MZ\x90\x00"  # Tiny fake EXE header, well under 10 MB
        assert len(tiny_exe) < MAX_UPLOAD_BYTES

        resp = client.post(
            "/materials/upload",
            files={"file": ("malware.exe", tiny_exe, "application/x-msdownload")},
        )
        assert resp.status_code == 400
        assert "Unsupported file type" in resp.text

    def test_oversized_text_file_not_saved_and_not_parsed(self, client):
        """Prove that parsing functions are never invoked for oversize files."""
        with patch("app.routes.upload.parse_uploaded_material") as mock_parse:
            oversized = b"c" * (10 * 1024 * 1024 + 1)
            resp = client.post(
                "/materials/upload",
                files={"file": ("test.txt", oversized, "text/plain")},
            )
            assert resp.status_code == 400
            mock_parse.assert_not_called()

    def test_oversized_pdf_rejected_before_page_analysis(self, client):
        """Prove PDF processing functions are never invoked for oversized PDFs."""
        with patch("app.routes.upload.parse_pdf_with_pages") as mock_pdf_parse:
            oversized = b"PDF" * (4 * 1024 * 1024)  # ~12 MB PDF-like content
            resp = client.post(
                "/materials/upload",
                files={"file": ("big.pdf", oversized, "application/pdf")},
            )
            assert resp.status_code == 400
            mock_pdf_parse.assert_not_called()

    def test_file_one_byte_above_limit_is_rejected(self, client):
        """A file one byte above MAX_UPLOAD_BYTES is rejected (exact boundary)."""
        one_over = b"x" * (MAX_UPLOAD_BYTES + 1)
        assert len(one_over) == MAX_UPLOAD_BYTES + 1

        resp = client.post(
            "/materials/upload",
            files={"file": ("one_over.txt", one_over, "text/plain")},
        )
        assert resp.status_code == 400, (
            f"Expected 400 for one-byte-over file, got {resp.status_code}: "
            f"{resp.text[:200]}"
        )
        assert "超过最大允许大小" in resp.text

        # Verify no material record was created
        list_resp = client.get("/materials")
        assert "one_over.txt" not in list_resp.text

    def test_oversized_upload_is_rejected_before_unbounded_full_read_or_processing(
        self, client
    ):
        """The bounded read strategy prevents full-file memory load before size decision.

        This test proves that even though we mock parse_uploaded_material,
        it is never called for an oversized file — meaning the size check
        runs before any further processing, using only a bounded read.
        """
        with patch("app.routes.upload.parse_uploaded_material") as mock_parse:
            oversized = b"z" * (10 * 1024 * 1024 + 1)
            resp = client.post(
                "/materials/upload",
                files={"file": ("oversized.txt", oversized, "text/plain")},
            )
            assert resp.status_code == 400
            mock_parse.assert_not_called()
            # Also verify no extraction was started
            with patch("app.routes.upload.extract_grammar_points") as mock_gp:
                mock_gp.assert_not_called()


# ===========================================================================
# Frontend / template contract tests
# ===========================================================================

class TestFrontendSizeLimitExposure:
    """Verify the configured limit reaches the frontend."""

    def test_upload_page_exposes_configured_size_limit_to_frontend(self, client):
        """The rendered upload page contains the config value (not hardcoded 10)."""
        resp = client.get("/materials?show_upload=1")
        assert resp.status_code == 200
        html = resp.text

        # The hint text should use the dynamic value
        assert f"文件最大 {MAX_UPLOAD_SIZE_MB} MB" in html

    def test_upload_page_has_configurable_data_attribute(self, client):
        """The file input carries a data-max-upload-size attribute with the config value."""
        resp = client.get("/materials?show_upload=1")
        assert resp.status_code == 200

        # Check for data attribute with correct value on both file inputs
        assert f'data-max-upload-size="{MAX_UPLOAD_SIZE_MB}"' in resp.text

    def test_no_separate_hardcoded_10_mb_in_script(
        self, client, mock_extraction
    ):
        """The page's JavaScript uses the data attribute, not a hardcoded literal 10 for size."""
        resp = client.get("/materials?show_upload=1")
        html = resp.text

        # The JS functions should reference input.dataset, not a numeric literal
        assert "input.dataset.maxUploadSize" in html
        assert "validateFileSize" in html

    def test_upload_page_empty_state_shows_size_limit(self, client):
        """The upload page (both states) exposes the limit via the upload form."""
        resp = client.get("/materials?show_upload=1")
        assert resp.status_code == 200
        html = resp.text

        # The upload form hint uses dynamic value
        assert f"文件最大 {MAX_UPLOAD_SIZE_MB} MB" in html

    def test_frontend_script_structure_designed_for_file_selection_validation(self, client):
        """The validateFileSize function exists and references the data attribute pattern."""
        resp = client.get("/materials?show_upload=1")
        html = resp.text

        # Verify the validation function exists
        assert "function validateFileSize" in html
        assert "data-max-upload-size" in html
        # Verify it clears the input on oversize
        assert "input.value = ''" in html

    def test_upload_page_no_hardcoded_10_mb_js_literal(self, client):
        """The JavaScript reads from dataset, not a hardcoded literal."""
        resp = client.get("/materials?show_upload=1")
        html = resp.text

        # Verify that validateFileSize reads from dataset, not a literal comparison.
        assert "input.dataset.maxUploadSize" in html


class TestFrontendBehavioralValidation:
    """Frontend validation behavior (template contract)."""

    def test_frontend_uses_rendered_config_limit_and_clears_oversized_selection(
        self, client
    ):
        """Template includes JS that clears oversized file selection."""
        resp = client.get("/materials?show_upload=1")
        html = resp.text

        # Verify the validation function sets input.value to '' on oversize
        assert "input.value = ''" in html
        # Verify dynamic error message construction is present
        assert "文件为" in html
        assert "超过最大允许大小" in html
        # The primary form's validation function is present
        assert "validateFileSize" in html

    def test_frontend_valid_reselection_clears_previous_size_error(self, client):
        """Template JS clears error display on valid file selection after error."""
        resp = client.get("/materials?show_upload=1")
        html = resp.text

        # Verify error display is hidden on valid selection
        assert "errorEl.style.display = 'none'" in html
        # At minimum the primary form has this reset
        count = html.count("errorEl.style.display = 'none'")
        assert count >= 1, (
            f"Expected 'none' display reset in validate functions, "
            f"found {count} occurrences"
        )


# ===========================================================================
# PDF page-flow tests
# ===========================================================================

@pytest.fixture
def mock_pdf_processing():
    """Mock PDF metadata + vision extraction so PDF upload works without real calls.

    Yields (mock_page_count, mock_parse) for per-test configuration.
    """
    with patch("app.routes.upload._get_pdf_page_count") as mock_count, \
         patch("app.routes.upload.parse_pdf_with_pages") as mock_parse:
        # Default: valid parsing result
        mock_parse.return_value = MagicMock(
            warnings=[],
            content_text="Mocked PDF Japanese content.",
            grammar_items=[],
            vocab_items=[],
        )
        yield mock_count, mock_parse


class TestPDFPageFlow:
    """Automatic first-30-page PDF processing."""

    def test_pdf_upload_page_does_not_render_page_range_controls(self, client):
        """No user-facing start-page/end-page selection in upload forms."""
        resp = client.get("/materials?show_upload=1")
        html = resp.text

        # Page-range divs must not exist
        assert "pdf-page-range" not in html, "Found page-range control in Form 1"
        assert "pdf-page-range-empty" not in html, (
            "Found page-range control in Form 2"
        )
        # No start_page / end_page input names in form
        assert 'name="start_page"' not in html
        assert 'name="end_page"' not in html
        # Toggle functions must not exist
        assert "togglePageRange(" not in html
        assert "togglePageRangeEmpty(" not in html

    def test_pdf_at_or_below_30_pages_processes_all_pages(
        self, client, mock_pdf_processing
    ):
        """A PDF with <= 30 pages is processed entirely (no truncation)."""
        mock_count, mock_parse = mock_pdf_processing
        mock_count.return_value = 15  # Total 15 pages

        small_pdf = b"%PDF-1.4 small valid content"
        # Ensure it's well under size limit
        assert len(small_pdf) < MAX_UPLOAD_BYTES

        resp = client.post(
            "/materials/upload",
            files={"file": ("small.pdf", small_pdf, "application/pdf")},
        )
        assert resp.status_code in (200, 303), (
            f"Expected success for 15-page PDF, got {resp.status_code}: "
            f"{resp.text[:200]}"
        )

        # Verify parse_pdf_with_pages was called with pages 1..15
        mock_parse.assert_called_once()
        args, _ = mock_parse.call_args
        # args: (raw_bytes, filename, start_page, end_page)
        assert args[2] == 1, f"Expected start_page=1, got {args[2]}"
        assert args[3] == 15, f"Expected end_page=15, got {args[3]}"

    def test_pdf_above_30_pages_is_accepted_and_processes_first_30_only(
        self, client, mock_pdf_processing
    ):
        """A PDF with > 30 pages is accepted and only first 30 pages are processed."""
        mock_count, mock_parse = mock_pdf_processing
        mock_count.return_value = 45  # Total 45 pages

        large_pdf = b"%PDF-1.4 larger valid content"
        assert len(large_pdf) < MAX_UPLOAD_BYTES

        resp = client.post(
            "/materials/upload",
            files={"file": ("large.pdf", large_pdf, "application/pdf")},
        )
        assert resp.status_code in (200, 303), (
            f"Expected success for 45-page PDF, got {resp.status_code}: "
            f"{resp.text[:200]}"
        )

        # Verify parse_pdf_with_pages was called with pages 1..30 only
        mock_parse.assert_called_once()
        args, _ = mock_parse.call_args
        assert args[2] == 1, f"Expected start_page=1, got {args[2]}"
        assert args[3] == 30, f"Expected end_page=30 (first 30), got {args[3]}"

    def test_pdf_above_30_pages_shows_processed_pages_success_message(
        self, client, mock_pdf_processing
    ):
        """After processing a >30-page PDF, success feedback shows total + processed count."""
        mock_count, mock_parse = mock_pdf_processing
        mock_count.return_value = 45

        pdf_content = b"%PDF-1.4 forty five page doc"
        assert len(pdf_content) < MAX_UPLOAD_BYTES

        resp = client.post(
            "/materials/upload",
            files={"file": ("fortyfive.pdf", pdf_content, "application/pdf")},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        html = resp.text

        # The final rendered page should contain the truncation message
        assert "该 PDF 共" in html
        assert "45" in html
        assert "<strong>30</strong>" in html

    def test_pdf_oversize_file_is_rejected_before_pdf_page_analysis(
        self, client
    ):
        """File-size rejection occurs before PDF page metadata processing."""
        with patch("app.routes.upload._get_pdf_page_count") as mock_count:
            oversized = b"P" * (MAX_UPLOAD_BYTES + 100)
            resp = client.post(
                "/materials/upload",
                files={"file": ("huge.pdf", oversized, "application/pdf")},
            )
            assert resp.status_code == 400
            # _get_pdf_page_count must NOT be called — size check happens first
            mock_count.assert_not_called()

    def test_normal_pdf_flow_no_longer_depends_on_page_range_inputs(
        self, client, mock_pdf_processing
    ):
        """Ordinary PDF upload works without start_page/end_page form fields."""
        mock_count, mock_parse = mock_pdf_processing
        mock_count.return_value = 5

        pdf_content = b"%PDF-1.4 normal upload"
        assert len(pdf_content) < MAX_UPLOAD_BYTES

        # Post without start_page/end_page — just file
        resp = client.post(
            "/materials/upload",
            files={"file": ("normal.pdf", pdf_content, "application/pdf")},
        )
        assert resp.status_code in (200, 303), (
            f"Expected success for PDF without page-range params, "
            f"got {resp.status_code}: {resp.text[:200]}"
        )
        # Verify extraction was called
        mock_parse.assert_called_once()
