"""End-to-end verification of Lingua Web P2 changes."""
import sys
import os
from io import BytesIO

sys.path.insert(0, "/home/pompeo_z/workspace/lingua-web")
os.chdir("/home/pompeo_z/workspace/lingua-web")

from fastapi.testclient import TestClient
from app.main import app
from app.db import Base, engine, get_db
from sqlalchemy.orm import Session

client = TestClient(app)

results = []

# =============================================================================
# Test 1: Empty state shows upload form directly
# =============================================================================
print("=== Test 1: Empty state upload form ===")
r = client.get("/materials")
body = r.text
has_form = 'upload-area' in body and 'type="file"' in body
has_pdf = '.pdf' in body or 'PDF' in body
has_welcome = "欢迎使用 Lingua Web" in body
results.append(("Empty state shows upload form", has_form, "upload-area found"))
results.append(("Empty state mentions PDF", has_pdf, "PDF found in body"))
results.append(("Welcome text present", has_welcome, ""))

# =============================================================================
# Test 2: show_upload=1 still works
# =============================================================================
print("=== Test 2: show_upload=1 ===")
r2 = client.get("/materials", params={"show_upload": "1"})
results.append(("show_upload=1 renders form", 'upload-area' in r2.text, ""))

# =============================================================================
# Test 3: TXT upload works
# =============================================================================
print("=== Test 3: TXT upload ===")
r3 = client.post("/materials/upload", files={
    "file": ("test.txt", b"Japanese text for testing grammar points.", "text/plain")
})
results.append(("TXT upload responds", r3.status_code in (200, 303, 307), f"status={r3.status_code}"))
# 303 redirect to material detail page

# =============================================================================
# Test 4: PDF upload with embedded text
# =============================================================================
print("=== Test 4: PDF upload (embedded text) ===")
with open("/tmp/test_embedded_pdf.pdf", "rb") as f:
    pdf_content = f.read()
r4 = client.post("/materials/upload", files={
    "file": ("grammar_guide.pdf", pdf_content, "application/pdf")
})
results.append(("PDF upload responds", r4.status_code in (200, 303, 307), f"status={r4.status_code}"))

# =============================================================================
# Test 5: PDF upload with OCR fallback
# =============================================================================
print("=== Test 5: PDF upload (OCR) ===")
with open("/tmp/test_ocr_pdf.pdf", "rb") as f:
    pdf_content = f.read()
r5 = client.post("/materials/upload", files={
    "file": ("scan.pdf", pdf_content, "application/pdf")
})
results.append(("OCR PDF upload responds", r5.status_code in (200, 303, 307), f"status={r5.status_code}"))

# =============================================================================
# Test 6: Unsupported extension rejected
# =============================================================================
print("=== Test 6: Unsupported extension ===")
r6 = client.post("/materials/upload", files={
    "file": ("test.exe", b"fake", "application/octet-stream")
})
results.append(("Unsupported extension rejected", r6.status_code == 400, f"status={r6.status_code}"))

# =============================================================================
# Print summary
# =============================================================================
print("\n" + "=" * 60)
print("E2E VERIFICATION SUMMARY")
print("=" * 60)
all_pass = True
for name, ok, detail in results:
    status = "✅ PASS" if ok else "❌ FAIL"
    if not ok:
        all_pass = False
    print(f"  {status} | {name}")
    if detail:
        print(f"         {detail}")

print("=" * 60)
print(f"OVERALL: {'ALL PASSED' if all_pass else 'SOME FAILED'}")
sys.exit(0 if all_pass else 1)
