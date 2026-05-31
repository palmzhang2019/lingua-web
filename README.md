# Lingua Web 🌱

A self-use Japanese N2 language learning web prototype — completed 3-day prototype.

## Prototype Flow

```
Upload TXT/MD
  ↓
Grammar + Vocabulary Extraction (DeepSeek)
  ↓
Select 2 grammar points → Generate 19-question study cycle (DeepSeek)
  ↓
Answer questions (translations via LLM eval, MC via Python grading)
  ↓
Weak points auto-tracked + prioritized in later cycles
  ↓
Results (accuracy + details + valid completion status)
  ↓
Session resume / Skip module / Mark studied
```

## Quick Start

```bash
# Set up environment
uv sync

# Configure DeepSeek API key (required for extraction + study cycle generation)
# Copy .env.example to .env and fill in your key, or:
export DEEPSEEK_API_KEY="your-deepseek-api-key"

# Run the server
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000/materials

## Tech Stack

- **Backend:** FastAPI + SQLAlchemy 2.x + SQLite
- **Templates:** Jinja2 (server-rendered, no frontend framework)
- **LLM:** DeepSeek v4 flash (OpenAI-compatible SDK)
- **Package manager:** uv

## Cost Summary

| Activity | Approx Tokens | Approx Cost |
|----------|-------------|------------|
| Material extraction (grammar + vocab) | ~5,500 | ~$0.001 |
| One full study cycle (generation + 10 evaluations) | ~9,650 | ~$0.002 |
| 3 cycles + extraction | ~34,000 | ~$0.007 |
| Full Day 3 verification (84 API calls) | 58,217 | ~$0.011 |

Pricing based on DeepSeek V4 Flash: $0.14/1M input, $0.28/1M output (cache miss rates).
Pricing source: https://api-docs.deepseek.com/quick_start/pricing (retrieved 2026-05-31)

## Project Structure

```
lingua-web/
├── app/
│   ├── main.py              # FastAPI entry + /weak_points route
│   ├── db.py                # SQLAlchemy engine + session
│   ├── models.py            # 8 ORM models (incl. UsageLog)
│   ├── schemas.py           # Pydantic schemas
│   ├── llm.py               # DeepSeek adapter + usage tracker
│   ├── agents/
│   │   ├── extractor.py     # Grammar + vocab extraction
│   │   └── generator.py     # Study cycle generation
│   ├── routes/
│   │   ├── upload.py        # Material upload + list/detail
│   │   └── study.py         # Study cycle runtime + Day 3 features
│   └── templates/           # Jinja2 templates
├── data/                    # SQLite DB (gitignored)
├── docs/reports/            # Day 1, Day 2, Day 3 reports
└── pyproject.toml
```

## Days

- **Day 1:** Material ingestion pipeline (upload → extraction → display)
- **Day 2:** 19-question study cycle runtime (generation → answering → results)
- **Day 3:** Weak points tracking, session resume, skip/studied modules, valid completion, cost measurement

## Known Non-Scope (not implemented)

- Listening/audio exercises (Whisper/TTS)
- PDF upload/OCR
- Spaced repetition (SRS)
- Multi-user authentication
- Production deployment (Docker, Celery, PostgreSQL)
- Frontend framework (React, Vue)
- UI polish (charts, progress bars, mobile optimization)

## Reports

- [Day 1 Report](docs/reports/day1-material-ingestion-report.md)
- [Day 2 Report](docs/reports/day2-study-cycle-runtime-report.md)
- [Day 3 Report](docs/reports/day3-prototype-closure-report.md)

## P2/P3 Next Tasks

### P2 (worth addressing next)
1. Pre-generate grammar explanations during start_cycle (avoid on-demand API failures)
2. Stronger review-priority hints to LLM (specify exact grammar points for review slots)
3. Weak point decay mechanism (time-based or correct-answer-based deactivation)

### P3 (beyond prototype scope)
1. Listening exercises (Whisper STT + TTS)
2. PDF upload (text extraction)
3. Spaced repetition (forgetting curve)
4. Multi-user auth
5. UI polish + mobile responsive
6. Production deployment
