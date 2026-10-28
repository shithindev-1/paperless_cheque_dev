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
│   │   ├── blob_storage.py         # Azure Blob read / write (connection string)
│   │   ├── image_processor.py      # OpenCV crop + rotate (pure functions)
│   │   ├── cheque_imaging.py       # When to process, and where to save
│   │   └── gemini/                 # OCR model client and generation config
│   └── utils/
│       ├── dates.py                # Lenient date parsing (any common format)
│       └── files.py                # File type detection & validation
├── config/
│   ├── settings.json               # Shared defaults (committed)
│   └── settings.example.json       # Template for settings.<APP_ENV>.json (not committed)
├── tests/                          # Unit tests (no model calls)
├── postman/                        # Postman collection
├── .env.example                    # Secrets template (copy to .env)
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
copy .env.example .env                                   # secrets
copy config\settings.example.json config\settings.local.json   # per-environment values
python -m uvicorn app.main:app --reload --port 8080
```

- Health check: `GET http://localhost:8080/api/v1/health`
- Swagger UI: `http://localhost:8080/docs` (when `docs_enabled` is true) — click **Authorize** and enter the API key

### Configuration

Settings are layered; later sources override earlier ones:

| Order | Source | Holds | Committed |
|---|---|---|---|
| 1 | `config/settings.json` | Shared defaults: limits, sampling, retries, image processing | Yes |
| 2 | `config/settings.<APP_ENV>.json` | Per environment: model name, blob base URL, CORS, docs, local folder | **No** |
| 3 | `.env` | Secrets: `API_KEYS`, `GEMINI_API_KEY`, `AZURE_STORAGE_CONNECTION_STRING`, plus `APP_ENV` | **No** |
| 4 | Environment variables | Any setting, by upper-case name (e.g. `MAX_FILE_SIZE_MB`) | — |

Config files are grouped in sections; keys are the setting names. Unknown keys, and secrets
placed in a config file, stop the app at startup. Moving between environments means
changing `APP_ENV`, the matching `settings.<env>.json` and the secrets in `.env` — no code changes.

| Setting | Purpose |
|---|---|
| `gemini_model` | OCR model name (required; per-environment file) |
| `azure_blob_base_url` | Blob container for cheque files (originals read, processed images written) |
| `azure_sas_expiry_hours` | Add a read-only SAS token to returned URLs (0 = plain URL) |
| `cors_origins`, `docs_enabled`, `log_level` | Per-environment app behaviour |
| `local_files_dir` | Local testing only; ignored unless `APP_ENV=local` |
| `max_files_per_request`, `max_file_size_mb`, `max_concurrent_requests` | Limits |
| `allowed_file_hosts` | Hosts `/match` may download from (default `*.blob.core.windows.net`) |
| `gemini_temperature`, `gemini_top_p`, `gemini_top_k`, `gemini_thinking_*` | Sampling / reasoning |
| `image_processing_enabled`, `crop_padding_percent`, `pdf_render_dpi` | Crop / rotation |

### Authentication

`/cheques/extract` and `/cheques/match` require an **`X-API-Key`** header matching one of
`API_KEYS` in `.env` (comma-separated, so keys can be rotated without downtime).
A missing or wrong key returns `401 {"error": "Invalid or missing API key."}`.
`API_KEYS` is required when `APP_ENV` is `uat` or `prod`; `/health` stays open.

Generate a key:
```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## API

All errors return `{"error": "<message>"}` with status 400 / 401 / 422 / 500.
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
| `MatchedOcrFileURL` | Original URL of the matching file | Original URL of the closest cheque's file (the one quoted in `OcrResponse`); `null` if there is none |
| `CroppedOCRFileURL` | Cropped / rotated PDF of that cheque; `null` if no processing was needed | Always `null` (unmatched cheques are not processed) |
| `OcrResponse` | `"Matched"` | Short reason with the value read from the cheque |

| Case | `OcrResponse` |
|---|---|
| Date differs | `Date doesn't match. Cheque date is 01-10-2026.` |
| Amount differs | `Amount doesn't match. Cheque amount is 5,000.` |
| Both differ | `Amount and date don't match. Cheque shows 5,000 dated 01-10-2026.` |

An empty `payments` or `file_urls` list returns `400`.

#### Cropping and rotation

For each file the OCR model also reports where every cheque is (bounding box), which
page it is on, how far it must be rotated, and whether the image needs cropping.
Only **matched** cheques are processed. A processed image is created when **a file holds several cheques**, or the model reports
the cheque **needs cropping or rotation**. OpenCV then crops and straightens each cheque
and saves it as a one-page PDF next to the original:

```
One cheque in the file:     .../OCR/Temp/<guid>.jpeg  ->  .../OCR/Temp/<guid>_ocr.pdf
Several cheques in the file: .../OCR/Temp/<guid>.pdf   ->  .../OCR/Temp/<guid>_ocr_1.pdf, <guid>_ocr_2.pdf, ...
```

`MatchedOcrFileURL` always returns the original file URL. `CroppedOCRFileURL` returns the
processed image of that cheque, or `null` when no processing was needed (or it failed). Files under `azure_blob_base_url` are read and
written with `AZURE_STORAGE_CONNECTION_STRING`; other URLs are only read. In local
testing, processed images are written into `local_files_dir`.

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
