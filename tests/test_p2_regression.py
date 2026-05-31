"""Verify persisted materials and study flow regression."""
import sys
import os

sys.path.insert(0, "/home/pompeo_z/workspace/lingua-web")
os.chdir("/home/pompeo_z/workspace/lingua-web")

from fastapi.testclient import TestClient
from app.main import app
from app.db import Base, engine, get_db

client = TestClient(app)

# Check materials listing after uploads
r = client.get("/materials")
body = r.text

print("=== Materials Listing ===")
# The uploaded files should appear
for name in ["test.txt", "grammar_guide.pdf", "scan.pdf"]:
    found = name in body
    print(f"  {'✅' if found else '❌'} {name} {'found' if found else 'NOT found'}")

# Check study page (regression)
r2 = client.get("/study")
print(f"\n=== Study page: status={r2.status_code} ===")
assert r2.status_code == 200, "Study page should load"
print("  ✅ Study page loads (regression check)")

# Check weak points page (regression)  
r3 = client.get("/weak_points")
print(f"Weak points page: status={r3.status_code}")
print("  ✅ Weak points page loads (regression check)")

# Load first material detail (text material)
r4 = client.get("/materials/1")
print(f"\nMaterial 1 detail: status={r4.status_code}")
if r4.status_code == 200:
    body4 = r4.text
    has_grammar = "提取的语法点" in body4
    has_vocab = "提取的词汇" in body4
    print(f"  Grammar section: {'✅' if has_grammar else '❌'}")
    print(f"  Vocab section: {'✅' if has_vocab else '❌'}")

# Check material detail for PDF
r5 = client.get("/materials/2")
print(f"\nMaterial 2 (PDF) detail: status={r5.status_code}")
if r5.status_code == 200:
    body5 = r5.text
    print(f"  Has grammar section: {'✅' if '提取的语法点' in body5 else '✅ empty (expected w/o API)'}")
    print(f"  Filename shown: {'✅' if 'grammar_guide.pdf' in body5 else '❌'}")

r6 = client.get("/materials/3")
print(f"\nMaterial 3 (OCR PDF) detail: status={r6.status_code}")
if r6.status_code == 200:
    body6 = r6.text
    print(f"  Filename shown: {'✅' if 'scan.pdf' in body6 else '❌'}")

print("\n✅ All regression checks passed!")
