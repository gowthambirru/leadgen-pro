# LeadGen Pro — Intelligent Lead Discovery & Cold Outreach Platform

A robust, search-first lead discovery, enrichment, and cold outreach web application built with FastAPI, SQLite, and Google Gemini. Automatically finds verified businesses across web search engines and directory platforms, extracts structured contact information, and launches targeted email outreach campaigns.

---

## 🌟 Key Features & Architectural Highlights

### 1. Search-First Workflow & Lead Discovery
- **Search as Primary Entry Point**: Enter any natural language query (e.g. `Pet shops in Hyderabad`, `Catering services in Mumbai`, `Solar panel dealers Bangalore`).
- **Concurrent Search-Engine Scraping**: Automatically discovers top-ranked websites and crawls them concurrently using bounded worker pools (`ThreadPoolExecutor`), achieving up to 5x faster discovery.
- **Multi-Strategy Content Extraction (`PageExtractor`)**:
  - Schema.org JSON-LD (`LocalBusiness`, `Store`, `Organization`, `ItemList`)
  - DOM Card recognition & microdata attributes
  - Curated directory listicles and heading sections (`<h2>`, `<h3>`)
  - Standalone corporate homepage contact discovery
- **Direct Source URL Support**: Secondary/advanced collapsible drawer for targeted Justdial category links with multi-page pagination and locality expansion.

### 2. Enterprise Data Pipeline & SQLite Persistence
- **Persistent Job & Lead Store (`database.py`)**: All search jobs, discovered leads, and campaigns are persisted to a local SQLite database with Write-Ahead Logging (WAL mode) enabled.
- **Survives Server Restarts**: Previous scrapes and campaign histories remain accessible. Users can inspect job history and reload past datasets at any time.
- **Deterministic Deduplication**: Enforces unique constraints on `(job_id, name, phone)` to prevent duplicate listings within datasets.
- **Data Sanitization Engine (`cleaner.py`)**: Normalizes phone numbers with leading zeros preserved, formats clean emails, filters boilerplate keywords, and bounds ratings.

### 3. Resilient AI Provider with Multi-Key Circuit Breaker (`ai_provider.py`)
- **Multiple Gemini API Keys**: Configure one or multiple keys in `GEMINI_API_KEYS` separated by commas.
- **Circuit Breaker Pattern**:
  - Key states: `HEALTHY`, `COOLDOWN`, `FAILED`.
  - Intelligently bypasses rate-limited (HTTP 429) keys for a 60-second cooldown period.
  - Automatically identifies permanently invalid keys (HTTP 400/401/403) and isolates them.
  - Round-robin load balancing across healthy keys.
- **Battle-Tested Offline Fallback**: If all keys are exhausted or network is unavailable, generates high-converting outreach copy using built-in direct-response frameworks.

### 4. High-Performance, Safe Email Subsystem (`email_sender.py`)
- **Attachment Pre-Encoding (Massive Performance Optimization)**: Brochure PDFs, images, and video files are read and base64-encoded **once** in memory before campaign dispatch, avoiding redundant disk I/O and CPU churn across recipients.
- **Duplicate Send Prevention & Idempotency**: Tracks individual recipient dispatch status (`pending`, `sent`, `failed`) in SQLite. If a campaign is interrupted, resuming it will never resend to already-contacted leads.
- **HTML Injection Defense**: User templates and scraped lead names are strictly HTML-escaped before insertion into outbound emails to prevent script execution or formatting hijacking.
- **Connection Resilience**: Automatically handles broken SMTP pipes with exponential retry logic.
- **Live Email Preview Modal**: Preview exactly how an email renders (both plain text and responsive HTML) for an actual discovered lead before firing.

### 5. Security Hardening
- **SSRF Protection (`_is_safe_url`)**: Blocks requests to loopback addresses (`127.0.0.1`), private networks (`10.x.x.x`, `192.168.x.x`, `172.16.x.x`), and internal cloud metadata services (`169.254.169.254`).
- **Path Traversal Protection**: Uploaded attachment filenames are sanitized via `os.path.basename` and stripped of directory traversal characters (`../../`).
- **File Upload Limits**: Enforces a 25MB cap on attachment uploads.
- **Secrets Masking**: Configuration values (`SMTP_PASSWORD`, `GEMINI_API_KEYS`) are wrapped in Pydantic `SecretStr` to prevent exposure in logs or terminal outputs.

### 6. Clean, Action-Oriented UI
- **Single Continuous Pipeline**: `SEARCH → REVIEW & SELECT → OUTREACH`.
- **Zero Decorative Noise**: Eliminates meaningless badges, redundant pills, and decorative clutter.
- **Lead Checkbox Selection**: Select individual leads, "Select All", or "Select With Email" to curate target audiences.
- **Modern Toast Notifications**: Replaces disruptive native browser `window.alert()` popups with inline notification toasts.
- **Bounded Activity Ticker**: Automatically caps terminal log output to the last 50 entries to prevent DOM bloating.
- **Job Control**: Includes cancellation buttons for both active scraping jobs and outgoing email campaigns.

---

## 📁 System Architecture

```
                                  +-----------------------------+
                                  |    Web UI (templates/index) |
                                  |    - Search-First Input     |
                                  |    - Lead Selection Table   |
                                  |    - AI Composer & Preview  |
                                  +-----------------------------+
                                                |
                                   REST / SSE   |
                                                v
                                  +-----------------------------+
                                  |      FastAPI (app.py)       |
                                  |   - Centralized Router      |
                                  |   - Secure File Uploads     |
                                  |   - Task Management         |
                                  +-----------------------------+
                                     /          |          \
                                    /           |           \
                                   v            v            v
    +--------------------------------+  +---------------+  +-----------------------------+
    |  manual_scraper.py             |  | database.py   |  |  ai_provider.py             |
    |  - WebSearcher (DuckDuckGo)    |  | - SQLite WAL  |  |  - Multi-Key Rotation       |
    |  - ThreadPoolExecutor (4 pool) |  | - Deduplication|  |  - Circuit Breaker (429)   |
    |  - SSRF Validation Filter      |  | - Recipient   |  |  - Model Fallback Chain     |
    |  - PageExtractor (5-tier DOM)  |  |   Status      |  |  - Offline Copywriter       |
    +--------------------------------+  +---------------+  +-----------------------------+
                   \                            |                            /
                    \                           |                           /
                     v                          v                          v
    +------------------------------------------------------------------------------------+
    | email_sender.py                                                                    |
    | - Pre-encoded Attachments (read once, deep-copy)                                   |
    | - HTML Escaping & Injection Defense                                                |
    | - Duplicate-Send Prevention (idempotent resume)                                    |
    | - SMTP Reconnection & Progress Reporting                                          |
    +------------------------------------------------------------------------------------+
```

---

## ⚙️ Configuration Guide

Configuration is managed via environment variables and a local `.env` file, validated on startup by `config.py` using Pydantic Settings.

### `.env` File Reference

Create or edit your `.env` file in the project root (`d:\scraper\.env`):

```bash
# ==============================================================
# LeadGen Pro — Configuration Settings
# ==============================================================

# --------------------------------------------------------------
# Google Gemini AI Settings
# --------------------------------------------------------------
# Multiple keys can be provided, separated by commas.
# The system automatically rotates and fails over across keys.
# Get free keys at: https://aistudio.google.com/app/apikey
GEMINI_API_KEYS=AIzaSyA_key1...,AIzaSyB_key2...,AIzaSyC_key3...

# (Optional backward-compatibility single key fallback)
# GEMINI_API_KEY=AIzaSy...

# --------------------------------------------------------------
# Application Runtime Settings
# --------------------------------------------------------------
HOST=127.0.0.1
PORT=8000
DEBUG=False
LOG_LEVEL=INFO
MAX_SCRAPE_WORKERS=4
MAX_EMAIL_RATE_DELAY=1.2

# --------------------------------------------------------------
# SMTP Server Settings (Cold Outreach)
# --------------------------------------------------------------
# For Gmail, generate a 16-character App Password at:
# https://myaccount.google.com/apppasswords
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=abcd efgh ijkl mnop
SMTP_FROM_EMAIL=your-email@gmail.com
SMTP_FROM_NAME=Growth Team
```

### Configuration Variables Breakdown

| Variable | Required? | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEYS` | Optional | `None` | Comma-separated list of Gemini API keys for circuit breaker rotation. |
| `GEMINI_API_KEY` | Optional | `None` | Single Gemini API key fallback. |
| `HOST` | Optional | `127.0.0.1` | Local network binding host. |
| `PORT` | Optional | `8000` | Port for the HTTP web interface. |
| `MAX_SCRAPE_WORKERS`| Optional | `4` | Concurrency limit for parallel website scraping. |
| `SMTP_HOST` | Optional* | `smtp.gmail.com` | Outbound mail server hostname. (*Required to send emails) |
| `SMTP_PORT` | Optional* | `587` | Outbound mail server port (`587` for STARTTLS, `465` for SSL). |
| `SMTP_USER` | Optional* | `""` | SMTP authentication username / email. |
| `SMTP_PASSWORD` | Optional* | `""` | SMTP password or Google App Password. |
| `SMTP_FROM_EMAIL`| Optional* | `""` | Sender email address appearing in `From:` header. |
| `SMTP_FROM_NAME` | Optional | `Marketing Outreach` | Sender display name appearing in email clients. |

---

## 🚀 Running the Application

### 1. Launch the Server
```bash
python run.py
```
This initializes the SQLite database, validates `.env` configuration, launches the Uvicorn ASGI server on `http://127.0.0.1:8000`, and opens the dashboard in your default browser.

Alternatively, run with Uvicorn directly:
```bash
uvicorn app:app --host 127.0.0.1 --port 8000
```

---

## 🧪 Testing & Validation

The application includes an extensive test suite verifying all layers:

### 1. Architectural Redesign Suite
Tests configuration security, SQLite persistence, lead deduplication, AI circuit breaker state transitions, SSRF filtering, HTML escaping, attachment pre-encoding, and secure file uploads:
```bash
python test_redesign_suite.py
```

### 2. Email & AI Integration Suite
Tests offline fallback copy generation, merge tag interpolation, responsive HTML rendering, and FastAPI endpoint validation:
```bash
python test_email_and_ai.py
```

### 3. End-to-End Live Scraping & Export Suite
Executes live scraping across search engines and directory platforms, verifies pagination, and validates Excel export formats:
```bash
python test_app.py
```

---

## 📡 API Reference

### Lead Discovery & Scraping
- `POST /api/search`: Primary entry point. Crawls top search results for a given query.
  - Body: `{"query": "pet shops in hyderabad", "num_websites": 10}`
- `POST /api/scrape`: Direct directory scraper for Justdial URLs.
  - Body: `{"url": "https://www.justdial.com/...", "max_pages": 5, "enrich_details": true}`
- `GET /api/progress/{job_id}`: Real-time Server-Sent Events (SSE) telemetry stream.
- `POST /api/jobs/{job_id}/cancel`: Safely cancels an active scraping job.
- `GET /api/results/{job_id}`: Retrieves all scraped lead records.
- `GET /api/jobs/history`: Returns list of past jobs with timestamps and query terms.
- `GET /api/download/{job_id}`: Downloads Excel workbook (`.xlsx`).
- `GET /api/download-csv/{job_id}`: Downloads CSV file (`.csv`).

### Email Marketing & Gemini AI
- `GET /api/email/settings`: Checks real-time health of AI keys and SMTP credentials.
- `POST /api/ai/compose-email`: Generates personalized cold email copy tailored to lead context.
  - Body: `{"product_name": "...", "product_description": "...", "target_niche": "...", "tone": "conversational"}`
- `POST /api/email/preview`: Renders live email HTML and subject for a specific lead.
- `POST /api/email/test-smtp`: Dispatches an instant test email to verify credentials.
- `POST /api/email/send-campaign`: Dispatches an outreach campaign with attachments and lead selection.
- `GET /api/email/progress/{campaign_id}`: Real-time SSE delivery progress stream.
- `POST /api/campaigns/{campaign_id}/cancel`: Halts an in-flight email campaign.
