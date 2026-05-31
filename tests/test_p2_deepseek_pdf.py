"""
Full P2 verification: upload OCR PDF, trigger DeepSeek extraction, check results.
DeepSeek creds loaded from ~/.hermes/.env (not echoed).
"""
import os
import sys
sys.path.insert(0, "/home/pompeo_z/workspace/lingua-web")
os.chdir("/home/pompeo_z/workspace/lingua-web")

# Load DeepSeek env silently
env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

from fastapi.testclient import TestClient
from app.main import app
from app.llm import is_available
from app.db import SessionLocal, engine, Base

# Confirm DeepSeek is available
print(f"DEEPSEEK available: {is_available()}")
key_preview = os.environ.get("DEEPSEEK_API_KEY", "")
print(f"Key length: {len(key_preview)} chars")

# Upload OCR PDF via test client
client = TestClient(app)

with open("/tmp/test_ocr_n2_pdf.pdf", "rb") as f:
    pdf_data = f.read()

print("\n=== Uploading OCR PDF ===")
resp = client.post("/materials/upload", files={
    "file": ("ocr_n2_grammar.pdf", pdf_data, "application/pdf")
})
print(f"Upload status: {resp.status_code}")
print(f"Redirect URL: {resp.headers.get('location', 'N/A')}")

# Parse material ID from redirect
import re
loc = resp.headers.get("location", "")
m = re.search(r"/materials/(\d+)", loc)
if m:
    material_id = int(m.group(1))
    print(f"Material ID: {material_id}")
    
    # Check detail page
    detail = client.get(f"/materials/{material_id}")
    assert detail.status_code == 200
    body = detail.text
    
    # Count grammar and vocab points
    import re
    gp_count = body.count('<div class="card">')  # rough count of cards
    has_grammar = "提取的语法点" in body
    has_vocab = "提取的词汇" in body
    grammar_points_found = 0
    vocab_items_found = 0
    
    # More precise: count GrammarPoint and VocabItem rows
    import sqlite3
    db_path = "data/lingua.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM grammar_points WHERE material_id = ?", (material_id,))
    grammar_points_found = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM vocab_items WHERE material_id = ?", (material_id,))
    vocab_items_found = cur.fetchone()[0]
    conn.close()
    
    print(f"\nGrammar points extracted: {grammar_points_found}")
    print(f"Vocab items extracted: {vocab_items_found}")
    print(f"Has grammar section: {has_grammar}")
    print(f"Has vocab section: {has_vocab}")
    
    # Print grammar point names
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, point_name, difficulty_level FROM grammar_points WHERE material_id = ?", (material_id,))
    rows = cur.fetchall()
    print(f"\nGrammar points detail:")
    for row in rows:
        print(f"  {row[0]}. {row[1]} ({row[2]})")
    conn.close()
    
    if grammar_points_found >= 2:
        print("\n✅ PDF material has >= 2 grammar points — study-eligible")
    else:
        print(f"\n⚠️ PDF material has only {grammar_points_found} grammar points")
else:
    print(f"Could not parse material_id from redirect: {loc}")

print("\n=== Verification complete ===")
