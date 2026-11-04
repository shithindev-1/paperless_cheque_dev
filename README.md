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
│   │           └── matching.py     # POST /api/v1/cheques/match and /match/upload
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
| `processed_folder` | Folder for cropped cheques (`<folder>/<request id>/`); empty = next to the original |

### Authentication

`/cheques/extract`, `/cheques/match` and `/cheques/match/upload` require an **`X-API-Key`** header matching one of
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
     "ServiceDescription": "Property Rent", "Amount": 8400, "DueDate": "2026-11-05T00:00:00",
     "cheque_info": {"ChequepayeeName": "ABC LLC", "ChequeHolderName": "Omar Ali",
                     "ChequeHolderNameAr": null, "ChequeHolderIBAN": "AE07 0331 2345 6789 0123 456"}}
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

Optional per payment: a `cheque_info` object with `ChequepayeeName`, `ChequeHolderName`,
`ChequeHolderNameAr` (holder name in Arabic), `ChequeHolderIBAN` (the object and every key may be
null or left out; it is returned unchanged). The same keys sent directly on the payment (earlier
format) are still accepted and moved into `cheque_info`. When a value
is sent it must also match the cheque (names: case, spaces and punctuation ignored, every letter must match;
IBAN: spaces ignored). Empty or missing values are not checked.
Names may be sent in English or Arabic (`ChequeHolderNameAr` is the holder name in Arabic). Each is
compared with the name on the cheque in the same script when the cheque has one (every letter must
match); the holder may be printed in both. Only when the cheque has the name in the other script
are they compared by sound (vowels, doubled letters and a final h ignored: `Mohammed Aly` =
`محمد علي` = `Mohamed Ali`). A mismatch shows the cheque's name, with its English spelling for an
Arabic name: `Holder name doesn't match. Cheque shows محمد علي (Mohamed Ali).`
Arabic-Indic digits (١٠/١١/٢٠٢٦, ٦٬٢٥٠) are accepted in cheque dates, amounts, IBANs and DueDate.

| Case | `OcrResponse` (added to any amount / date reason) |
|---|---|
| Name differs | `Payee name doesn't match. Cheque shows ABC LLC.` |
| Not on the cheque | `IBAN not found on the cheque.` |

#### Cheque checks

Every cheque is also checked on its own; any problem makes the payment unmatched and is
listed in `OcrResponse` (and in the cheque's `error` / `issues` on `/extract`).
SIGNATURE_MISSING, AMOUNT_MISMATCH and DAMAGED are always listed first, before any
amount / date / name reason:

| Code | `OcrResponse` text |
|---|---|
| ALTERATION | `Overwriting or strike-through found on the <field>. UAE banks do not accept altered cheques. Please provide a new cheque.` — any strike-through, overwritten character or correction, even if initialled |
| AMOUNT_MISMATCH | `Amount in words (Five Thousand) does not match figures (8,000).` — decided by the backend from both values |
| AMOUNT_MISSING | `Amount in words or figures is missing.` |
| DATE_ISSUE | `Cheque date is not valid (date missing).` — only when there is no readable date; a readable date (old, wrong) is judged by the DueDate comparison |
| PAYEE_ISSUE | `Payee name is missing.` — only when `ChequepayeeName` is not sent (otherwise the comparison reports it); overwriting is ALTERATION |
| SIGNATURE_MISSING | `Signature missing.` |
| MIXED_LANGUAGE | `Cheque is filled in a mix of English and Arabic.` |
| DAMAGED | `Cheque appears damaged near the MICR line.` (location from the OCR) |
| ILLEGIBLE | `Handwriting is not clear.` |
| IMAGE_INCOMPLETE | `Cheque image is incomplete or blurred.` |

Fields that are present but unreadable are never guessed or compared:
`Payee name is not clearly visible.` Unclear fields, ILLEGIBLE, IMAGE_INCOMPLETE and DAMAGED
add `Please upload a clear copy.` Holder name / IBAN are only considered when sent.
A field is treated as unclear if even one character or digit is in doubt (e.g. 4 vs 6). For the
date the message names the doubtful part: `Year of the cheque date is not clearly visible.`
(also day / month); the whole date is then dropped, never guessed.

Messages stay short: a cut-off, blurred or illegible cheque (IMAGE_INCOMPLETE / ILLEGIBLE,
or a page below `min_image_sharpness`) returns only
`Cheque image is incomplete or unclear. Please upload a clear, full copy.` — nothing is
compared or derived from it. Model details longer than 45 characters are left out.

An empty `payments` or `file_urls` list returns `400`.

### `POST /api/v1/cheques/match/upload`

Same checks and response as `/cheques/match`, for cheque files **uploaded in the request**
(useful for testing files from any folder, locally or on UAT). `multipart/form-data`:

| Field | Value |
|---|---|
| `files` | One or more cheque files (repeat the field; same limits as `/extract`) |
| `payments` | The payment list as a JSON string, same keys as `/cheques/match`, e.g. `[{"RentPaymentId": 1, "Amount": 9333, "DueDate": "2026-01-01", "cheque_info": {"ChequepayeeName": "ABC LLC"}}]` |

`MatchedOcrFileURL` is the uploaded file name. Cropped cheques are saved under
`<processed_folder>/<request-id>/` (blob storage on UAT, `local_files_dir` locally).
An invalid `payments` value returns `422`.

#### Cropping and rotation

For each file the OCR model also reports where every cheque is (bounding box), which
page it is on, how far it must be rotated, and whether the image needs cropping.
Only **matched** cheques are processed. A processed image is created when **a file holds several cheques**, or the model reports
the cheque **needs cropping or rotation**. OpenCV then crops and straightens each cheque
and saves it as a one-page PDF. With `processed_folder` set, every request gets its own
folder (named by the request id, so nothing is overwritten):

```
One cheque in the file:      .../<processed_folder>/<request-id>/<name>_ocr.pdf
Several cheques in the file: .../<processed_folder>/<request-id>/<name>_ocr_1.pdf, <name>_ocr_2.pdf, ...
```

With `processed_folder` empty, the PDF is saved next to the original (`.../<name>_ocr.pdf`).

`MatchedOcrFileURL` always returns the original file URL. `CroppedOCRFileURL` returns the
processed image of that cheque, or `null` when no processing was needed (or it failed). Files under `azure_blob_base_url` are read and
written with `AZURE_STORAGE_CONNECTION_STRING`; other URLs are only read. In local
testing, processed images are written into `local_files_dir`.

---

## Logging

| File | Content |
|---|---|
| `logs/app/YYYY-MM-DD.log` | Application log; every line has UAE and IST time, per-file token usage and timings, per-payment match results |
| `logs/results/YYYY-MM-DD.jsonl` | **One JSON line per API hit** (the request trace, below) |

Every `/cheques/*` call gets a `request_id`, returned to the caller as the `X-Request-ID` header.
The app log ends each hit with `[<request_id>] SUMMARY {...}`; the full record is one line in the
results log:

| Key | Content |
|---|---|
| `request_id`, `endpoint`, `type` | Identify the hit (`match` / `extract`) |
| `started_at`, `finished_at` | UAE and IST timestamps (ms) |
| `total_ms`, `status_code`, `outcome` | Duration and result (`success` / `client_error` / `server_error`) |
| `client` | IP, user agent, `api_key_id` (fingerprint of the key used, never the key) |
| `app` | Version, environment, model |
| `counts` | Files, files ok / failed, payments, cheques found, cheques cropped, matched / unmatched |
| `stages_ms` | `load`, `extraction`, `matching`, `cropping` (match); `validation`, `extraction` (extract) |
| `files[]` | Per file: source, size, type, `load_ms`, `queue_wait_ms`, `model_ms`, `quality_check_ms`, `sharpness`, tokens, status / error, cheque summary (date, amount, orientation, issues — no names or IBANs) |
| `cheques_processed[]` | Per cropped cheque: quarter turn, measured / applied tilt, output size, `process_ms`, `save_ms`, output file |
| `payments[]` | Per payment: id, amount, due date, which name/IBAN checks were sent (not the values), matched cheque, files, `OcrResponse` |
| `issues_summary` | Count per issue code (e.g. `{"SIGNATURE_MISSING": 1}`) |
| `usage` | Total tokens |
| `events[]` | Model retries, with reason and wasted tokens |
| `errors[]` | Load / validation / auth / model / unexpected errors |

Files roll over at UAE midnight. URL query strings (e.g. SAS tokens) are removed before logging.
`logs/` is excluded from version control.

---

## Tests

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

Unit tests cover date parsing, matching rules and file loading; they make no model or network calls.
