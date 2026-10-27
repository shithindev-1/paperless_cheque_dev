# Paperless Cheque OCR API

REST API that reads cheque images / PDFs and

1. **extracts** cheque date, amount, installment number and service, and
2. **matches** cheques against a list of expected rent payments (by amount + due date).

Built with FastAPI. OCR is performed by a hosted vision model configured through environment variables.

---

## Project structure

```
.
├── app/
│   ├── main.py                     # App factory: CORS, routers, error handlers, docs toggle
│   ├── api/
│   │   ├── deps.py                 # Dependency injection
│   │   ├── errors.py               # Global error handlers -> {"error": "..."}
│   │   └── v1/
│   │       ├── router.py
│   │       └── endpoints/          # Thin HTTP layer, no business logic
│   │           ├── health.py       # GET  /api/v1/health
│   │           ├── extraction.py   # POST /api/v1/cheques/extract
│   │           └── matching.py     # POST /api/v1/cheques/match
│   ├── core/
│   │   ├── config.py               # Settings (environment only, no secrets in code)
│   │   ├── exceptions.py           # App exceptions with client-safe messages
│   │   ├── logging.py              # Daily app + results logs (UAE & IST timestamps)
│   │   └── timezones.py
│   ├── prompts/
│   │   └── cheque_extraction.py    # OCR instructions
│   ├── schemas/                    # Pydantic request / response models
│   │   ├── cheque.py
│   │   ├── matching.py
│   │   └── response.py
│   ├── services/
│   │   ├── extraction_service.py   # Use case: extract uploads
│   │   ├── matching_service.py     # Use case: load files -> extract -> match -> log
│   │   ├── cheque_extractor.py     # OCR one/many files (concurrency, retries)
│   │   ├── payment_matcher.py      # Pure matching rules + OcrResponse messages
│   │   ├── file_loader.py          # Azure URL download / local test files
│   │   └── gemini/                 # OCR model client and generation config
│   └── utils/
│       ├── dates.py                # Lenient date parsing (any common format)
│       └── files.py                # File type detection & validation
├── tests/                          # Unit tests (no model calls)
├── postman/                        # Postman collection
├── .env.example                    # Configuration template (copy to .env)
├── requirements.txt
├── requirements-dev.txt
└── pyproject.toml
```

---

## Setup

Requires Python 3.12.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
copy .env.example .env        # then fill in the values
python -m uvicorn app.main:app --reload --port 8080
```

- Health check: `GET http://localhost:8080/api/v1/health`
- Swagger UI: `http://localhost:8080/docs` (when `DOCS_ENABLED=true`)

### Configuration

All configuration comes from environment variables / `.env` (see `.env.example`).
The API key and model name are **required** and have no defaults in code.

| Variable | Purpose |
|---|---|
| `APP_ENV` | `local`, `uat` or `prod` |
| `DOCS_ENABLED` | Expose `/docs`, `/redoc`, `/openapi.json` |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | OCR model credentials and model name (required) |
| `GEMINI_TEMPERATURE`, `GEMINI_TOP_P`, `GEMINI_TOP_K` | Sampling |
| `GEMINI_THINKING_BUDGET` / `GEMINI_THINKING_LEVEL` | Reasoning effort (set one) |
| `GEMINI_MAX_RETRIES`, `GEMINI_TIMEOUT_SECONDS` | Reliability |
| `MAX_FILES_PER_REQUEST`, `MAX_FILE_SIZE_MB`, `MAX_CONCURRENT_REQUESTS` | Limits |
| `ALLOWED_FILE_HOSTS` | Hosts `/match` may download from (default `*.blob.core.windows.net`) |
| `LOCAL_FILES_DIR` | Local testing only; ignored unless `APP_ENV=local` |

---

## API

All errors return `{"error": "<message>"}` with status 400 / 422 / 500.
Internal details (model errors, token usage, timings) are written to logs only.

### `GET /api/v1/health`

```json
{"status": "ok", "version": "1.0.0"}
```

### `POST /api/v1/cheques/extract`

`multipart/form-data` with one or more `files` (JPG, PNG, WEBP, HEIC, PDF; max 15 MB each).

```json
{
  "results": [
    {
      "file_name": "cheque1.jpg",
      "status": "success",
      "cheques": [
        {
          "cheque_date": "01-11-2026",
          "cheque_amount": 25000.0,
          "installment_number": 1,
          "service": "Property Rent",
          "error": null
        }
      ]
    }
  ]
}
```

- `cheque_date` is always `DD-MM-YYYY`.
- `installment_number` is read from the cheque; if absent, it is filled from date order
  (cheques of the same service in the same request).
- `error` on a cheque is set when it is overlapped / partly covered; visible fields are
  still returned and covered fields are `null`.

### `POST /api/v1/cheques/match`

Matches each payment to a cheque with the **same amount and same date**. Each cheque
is used at most once. Dates may be in any common format
(`2026-11-05T00:00:00`, `05/11/2026`, `5 Nov 2026`, ...); ambiguous numeric dates are
read day-first.

Request:
```json
{
  "file_urls": ["https://<account>.blob.core.windows.net/<container>/cheque1.pdf?<sas>"],
  "payments": [
    {"RentPaymentId": 94814, "Installment": 1, "ServiceId": 1,
     "ServiceDescription": "Property Rent", "Amount": 8400, "DueDate": "2026-11-05T00:00:00"}
  ]
}
```

Response — the payment list with three keys filled (other keys are returned unchanged):

| Key | Matched | Not matched |
|---|---|---|
| `MatchedWithOcr` | `true` | `false` |
| `MatchedOcrFile` | URL of the matching file | URL of the closest cheque's file (the one quoted in `OcrResponse`); `null` if there is none |
| `OcrResponse` | `"Matched"` | Short reason with the value read from the cheque |

| Case | `OcrResponse` |
|---|---|
| Date differs | `Date doesn't match. Cheque date is 01-10-2026.` |
| Amount differs | `Amount doesn't match. Cheque amount is 5,000.` |
| Both differ | `Amount and date don't match. Cheque shows 5,000 dated 01-10-2026.` |

An empty `payments` or `file_urls` list returns `400`.

---

## Logging

| File | Content |
|---|---|
| `logs/app/YYYY-MM-DD.log` | Application log; every line has UAE and IST time, per-file token usage and timings, per-payment match results |
| `logs/results/YYYY-MM-DD.jsonl` | One JSON record per request: UAE/IST timestamps, totals, token usage, full results |

Files roll over at UAE midnight. URL query strings (e.g. SAS tokens) are removed before logging.
`logs/` is excluded from version control.

---

## Tests

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

Unit tests cover date parsing, matching rules and file loading; they make no model or network calls.
