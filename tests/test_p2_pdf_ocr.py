"""P2 verification script for Lingua Web PDF OCR implementation."""
import sys
import os

sys.path.insert(0, "/home/pompeo_z/workspace/lingua-web")
os.chdir("/home/pompeo_z/workspace/lingua-web")

from app.services.material_parser import parse_uploaded_material

results = []

# Test 1: TXT regression
parsed = parse_uploaded_material("hello.txt", b"Hello world")
ok = parsed.source_type == "txt" and parsed.content_text == "Hello world"
results.append(("TXT parsing regression", ok, f"source={parsed.source_type}"))

# Test 2: MD regression
parsed = parse_uploaded_material("hello.md", b"# Hello")
results.append(("MD parsing regression", parsed.source_type == "md", f"source={parsed.source_type}"))

# Test 3: Embedded text PDF
with open("/tmp/test_embedded_pdf.pdf", "rb") as f:
    content = f.read()
parsed = parse_uploaded_material("test.pdf", content)
results.append(("Embedded text PDF", 
    parsed.source_type == "pdf" and parsed.parse_method == "pdf_text" and len(parsed.content_text) > 0,
    f"method={parsed.parse_method}, text_len={len(parsed.content_text)}"))

# Test 4: OCR fallback PDF
with open("/tmp/test_ocr_pdf.pdf", "rb") as f:
    content = f.read()
parsed = parse_uploaded_material("ocr.pdf", content)
ocr_used = parsed.parse_method == "pdf_ocr"
results.append(("OCR fallback PDF",
    ocr_used and len(parsed.content_text) > 0,
    f"method={parsed.parse_method}, text_len={len(parsed.content_text)}"))

# Test 5: Failed PDF
parsed = parse_uploaded_material("bad.pdf", b"garbage")
results.append(("Failed PDF rejection",
    len(parsed.content_text) == 0,
    f"text_len={len(parsed.content_text)}, warnings={len(parsed.warnings)}"))

print("=" * 60)
print("P2 VERIFICATION RESULTS")
print("=" * 60)
all_pass = True
for name, ok, detail in results:
    status = "✅ PASS" if ok else "❌ FAIL"
    if not ok:
        all_pass = False
    print(f"  {status} | {name}")
    print(f"         {detail}")

print("=" * 60)
print(f"OVERALL: {'ALL PASSED' if all_pass else 'SOME FAILED'}")
sys.exit(0 if all_pass else 1)
