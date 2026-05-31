# Lingua Web 🌱

A self-use Japanese N2 language learning web prototype.

## Day 1: Material Ingestion Pipeline ✅

Upload Japanese N2 TXT/MD materials → DeepSeek extracts grammar points + vocabulary → display results.

### Quick Start

```bash
# Set up environment
uv sync

# Set DeepSeek API credentials (optional — extraction works without, but skips LLM calls)
export DEEPSEEK_API_KEY="your-deepseek-api-key"

# Run server
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open http://localhost:8000/materials

### Tech Stack

- **Backend:** FastAPI + SQLAlchemy 2.x + SQLite
- **Templates:** Jinja2 + HTMX-ready
- **LLM:** DeepSeek v4 flash (OpenAI-compatible)
- **Package manager:** uv

### Project Structure

```
lingua-web/
├── app/
│   ├── main.py              # FastAPI entry
│   ├── db.py                # SQLAlchemy engine
│   ├── models.py            # 7 ORM models
│   ├── schemas.py           # Pydantic schemas
│   ├── llm.py               # DeepSeek adapter
│   ├── agents/
│   │   ├── extractor.py     # Grammar + vocab extraction
│   │   └── generator.py     # (Day 2)
│   ├── routes/
│   │   ├── upload.py        # Material routes
│   │   └── study.py         # (Day 2)
│   └── templates/           # Jinja2 templates
├── data/                    # SQLite DB (gitignored)
├── docs/reports/            # Implementation reports
└── pyproject.toml
```

### Day 1 Report

See [docs/reports/day1-material-ingestion-report.md](docs/reports/day1-material-ingestion-report.md)
