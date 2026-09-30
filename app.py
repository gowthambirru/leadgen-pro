import os
import uuid
import json
import asyncio
import logging
import shutil
import html as html_module
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Form, File, UploadFile, Query
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from database import (
    init_db, create_job, update_job_status, update_job_progress, get_job,
    get_recent_jobs, insert_leads, get_leads, get_leads_with_email,
    create_campaign, update_campaign_status, get_campaign,
    mark_recipient_sent, mark_recipient_failed, get_pending_recipients
)
from scraper import JustdialScraper
from manual_scraper import ManualSearchScraper
from exporter import export_to_excel, export_to_csv
from ai_provider import ai_provider, generate_cold_email
from email_sender import (
    get_default_smtp_config,
    test_smtp_connection,
    send_campaign,
    render_template_tags,
    build_html_email
)

logger = logging.getLogger("leadgen")
logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
                    format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="LeadGen Pro", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
ATTACHMENTS_DIR = os.path.join(DOWNLOADS_DIR, "attachments")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

# Initialize database on startup
init_db()

# Backward compatibility proxy for legacy scripts/tests inspecting app.jobs
class _JobsDictProxy:
    def __getitem__(self, job_id):
        job = get_job(job_id)
        if not job:
            raise KeyError(job_id)
        leads = get_leads(job_id)
        job['results'] = leads
        return job

    def __setitem__(self, job_id, value):
        from database import get_db
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs (id, type, query, status, config, created_at, updated_at) VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                (job_id, value.get('type', 'manual'), value.get('query', ''), value.get('status', 'completed'), json.dumps(value.get('config', {})))
            )
            conn.commit()
        if 'results' in value and value['results']:
            insert_leads(job_id, value['results'])

    def get(self, job_id, default=None):
        job = get_job(job_id)
        if not job:
            return default
        job['results'] = get_leads(job_id)
        return job

    def __contains__(self, job_id):
        return get_job(job_id) is not None

jobs = _JobsDictProxy()

# SSE queues for real-time progress (transient, not persisted)
_sse_queues: Dict[str, asyncio.Queue] = {}

# Cancellation flags for long-running jobs
_cancel_flags: Dict[str, bool] = {}


# ============================
# Pydantic Request Models
# ============================

class SearchRequest(BaseModel):
    query: str
    num_websites: Optional[int] = 10

class JustdialRequest(BaseModel):
    url: str
    max_pages: Optional[int] = 5
    scrape_all: bool = False
    max_listings: Optional[int] = None
    enrich_details: bool = False

class ComposeEmailRequest(BaseModel):
    product_name: str
    product_description: str
    target_niche: Optional[str] = ""
    tone: Optional[str] = "conversational"
    video_link: Optional[str] = ""
    job_id: Optional[str] = ""
    gemini_api_key: Optional[str] = ""

class TestSmtpRequest(BaseModel):
    host: str
    port: int = 587
    user: str
    password: str
    from_email: str
    from_name: Optional[str] = "Outreach"
    test_recipient: str

class PreviewEmailRequest(BaseModel):
    subject: str
    body: str
    video_link: Optional[str] = ""
    lead_id: Optional[int] = None
    job_id: Optional[str] = ""


# ============================
# Helper: SSE Queue Management
# ============================

def _get_queue(job_id: str) -> asyncio.Queue:
    if job_id not in _sse_queues:
        _sse_queues[job_id] = asyncio.Queue()
    return _sse_queues[job_id]

def _push_update(job_id: str, update: dict, loop: asyncio.AbstractEventLoop):
    """Thread-safe push to SSE queue."""
    queue = _get_queue(job_id)
    try:
        asyncio.run_coroutine_threadsafe(queue.put(update), loop)
    except Exception:
        pass

def _cleanup_queue(job_id: str):
    """Remove SSE queue after job completes."""
    _sse_queues.pop(job_id, None)
    _cancel_flags.pop(job_id, None)


# ============================
# Secure File Handling
# ============================

def _safe_filename(filename: str) -> str:
    """Sanitize uploaded filename to prevent path traversal."""
    # Strip path components, keep only the basename
    name = os.path.basename(filename)
    # Remove any remaining path separators or suspicious chars
    name = name.replace('\\', '').replace('/', '').replace('\x00', '')
    if not name:
        name = 'upload'
    return name

MAX_UPLOAD_SIZE = 25 * 1024 * 1024  # 25MB

async def _save_upload(upload: UploadFile, dest_dir: str) -> Optional[str]:
    """Save an uploaded file safely, returning the path or None."""
    if not upload or not upload.filename:
        return None
    safe_name = _safe_filename(upload.filename)
    dest_path = os.path.join(dest_dir, safe_name)
    content = await upload.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail=f"File '{safe_name}' exceeds 25MB limit.")
    with open(dest_path, "wb") as f:
        f.write(content)
    return dest_path


# ============================
# Index Page
# ============================

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(TEMPLATES_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="Template not found")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ============================
# Search (Primary Entry Point)
# ============================

def _run_search_task(job_id: str, query: str, num_websites: int, loop: asyncio.AbstractEventLoop):
    """Background thread: web search across top websites."""
    scraper = ManualSearchScraper()

    def progress_callback(update: Dict[str, Any]):
        # Check cancellation
        if _cancel_flags.get(job_id):
            raise InterruptedError("Job cancelled by user")

        current = update.get("current_page", 0)
        total = update.get("total_pages", 1)
        status = update.get("status", "running")

        pct = 0
        if status == "completed":
            status = "finalizing"
            update = {**update, "status": "finalizing", "message": "Saving leads to database...", "percent": 99}
            pct = 99
        elif total > 0:
            pct = int((current / max(total, 1)) * 95)

        update["percent"] = min(pct, 99)
        update_job_progress(job_id, update)
        _push_update(job_id, update, loop)

    try:
        update_job_status(job_id, "running")
        results = scraper.scrape_query(
            query=query,
            num_websites=num_websites,
            progress_callback=progress_callback
        )

        # Persist leads to database
        lead_count = insert_leads(job_id, [
            {**r, 'source_url': r.get('justdial_url', ''), 'source_domain': ''}
            for r in results
        ])

        update_job_status(job_id, "completed")

        # Generate export files
        all_leads = get_leads(job_id)
        leads_dicts = [_lead_to_export_dict(l) for l in all_leads]
        _generate_exports(job_id, leads_dicts)

        phone_count = sum(1 for l in all_leads if l.get("phone"))
        email_count = sum(1 for l in all_leads if l.get("email"))

        final = {
            "status": "completed",
            "percent": 100,
            "count": len(all_leads),
            "phone_count": phone_count,
            "email_count": email_count,
            "message": f"Found {len(all_leads)} leads ({phone_count} with phone, {email_count} with email)"
        }
        update_job_progress(job_id, final)
        _push_update(job_id, final, loop)

    except InterruptedError:
        update_job_status(job_id, "cancelled")
        _push_update(job_id, {"status": "cancelled", "percent": 100, "message": "Job cancelled"}, loop)
    except Exception as e:
        logger.error(f"Search task error: {e}", exc_info=True)
        update_job_status(job_id, "failed", error=str(e))
        _push_update(job_id, {
            "status": "failed", "percent": 100,
            "message": f"Search error: {str(e)[:200]}"
        }, loop)
    finally:
        _cleanup_queue(job_id)


@app.post("/api/search")
async def start_search(req: SearchRequest, background_tasks: BackgroundTasks):
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Search query cannot be empty.")

    num_websites = max(1, min(req.num_websites or 10, 30))
    config = {"num_websites": num_websites}
    job_id = create_job("web_search", query, config)
    loop = asyncio.get_running_loop()
    _get_queue(job_id)  # Initialize queue

    background_tasks.add_task(_run_search_task, job_id, query, num_websites, loop)
    return {"job_id": job_id, "status": "started"}


# ============================
# Justdial (Secondary Entry)
# ============================

def _run_justdial_task(job_id: str, url: str, max_pages: int, scrape_all: bool,
                       max_listings: Optional[int], enrich_details: bool,
                       loop: asyncio.AbstractEventLoop):
    """Background thread: Justdial scraping."""
    scraper = JustdialScraper()

    def progress_callback(update: Dict[str, Any]):
        if _cancel_flags.get(job_id):
            raise InterruptedError("Job cancelled by user")

        current_page = update.get("current_page", 0)
        total_pages = update.get("total_pages", 1)
        status = update.get("status", "running")

        pct = 0
        if status == "completed":
            status = "finalizing"
            update = {**update, "status": "finalizing", "message": "Saving leads to database & generating files...", "percent": 99}
            pct = 99
        elif status == "enriching":
            enrich_idx = update.get("enrich_index", 0)
            enrich_tot = update.get("enrich_total", 1)
            pct = 60 + int((enrich_idx / max(enrich_tot, 1)) * 38)
        elif status == "scraping":
            pct = int((current_page / max(total_pages, 1)) * 60)

        update["percent"] = min(pct, 99)
        update_job_progress(job_id, update)
        _push_update(job_id, update, loop)

    try:
        update_job_status(job_id, "running")
        results = scraper.scrape(
            url=url, max_pages=max_pages, scrape_all=scrape_all,
            max_listings=max_listings, enrich_details=enrich_details,
            progress_callback=progress_callback
        )

        lead_count = insert_leads(job_id, [
            {**r, 'source_url': r.get('justdial_url', ''), 'source_domain': 'justdial.com'}
            for r in results
        ])

        update_job_status(job_id, "completed")

        all_leads = get_leads(job_id)
        leads_dicts = [_lead_to_export_dict(l) for l in all_leads]
        _generate_exports(job_id, leads_dicts)

        phone_count = sum(1 for l in all_leads if l.get("phone"))
        email_count = sum(1 for l in all_leads if l.get("email"))

        final = {
            "status": "completed", "percent": 100,
            "count": len(all_leads), "phone_count": phone_count,
            "email_count": email_count,
            "detected_total": update.get("detected_total") if 'update' in dir() else None,
            "message": f"Extracted {len(all_leads)} listings ({phone_count} with phone, {email_count} with email)"
        }
        update_job_progress(job_id, final)
        _push_update(job_id, final, loop)

    except InterruptedError:
        update_job_status(job_id, "cancelled")
        _push_update(job_id, {"status": "cancelled", "percent": 100, "message": "Job cancelled"}, loop)
    except Exception as e:
        logger.error(f"Justdial task error: {e}", exc_info=True)
        update_job_status(job_id, "failed", error=str(e))
        _push_update(job_id, {
            "status": "failed", "percent": 100,
            "message": f"Scraping error: {str(e)[:200]}"
        }, loop)
    finally:
        _cleanup_queue(job_id)


@app.post("/api/scrape")
async def start_scrape(req: JustdialRequest, background_tasks: BackgroundTasks):
    url = req.url.strip()
    if "justdial.com" not in url:
        raise HTTPException(status_code=400, detail="URL must be a Justdial link.")

    scrape_all = req.scrape_all
    max_listings = req.max_listings

    if scrape_all:
        pages = 100
    elif max_listings and max_listings > 0:
        pages = max(1, (max_listings + 9) // 10)
    else:
        pages = max(1, min(req.max_pages or 5, 120))

    config = {
        "max_pages": pages, "scrape_all": scrape_all,
        "max_listings": max_listings, "enrich_details": req.enrich_details
    }
    job_id = create_job("justdial", url, config)
    loop = asyncio.get_running_loop()
    _get_queue(job_id)

    background_tasks.add_task(
        _run_justdial_task, job_id, url, pages, scrape_all,
        max_listings, req.enrich_details, loop
    )
    return {"job_id": job_id, "status": "started"}


# ============================
# Backward Compat: manual-search -> search
# ============================

@app.post("/api/manual-search")
async def start_manual_search_compat(req: SearchRequest, background_tasks: BackgroundTasks):
    return await start_search(req, background_tasks)


# ============================
# SSE Progress Stream
# ============================

@app.get("/api/progress/{job_id}")
async def get_progress_stream(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    queue = _get_queue(job_id)

    async def event_generator():
        # Send current state first
        if job.get("progress"):
            yield f"data: {json.dumps(job['progress'])}\n\n"

        while True:
            try:
                update = await asyncio.wait_for(queue.get(), timeout=15.0)
                yield f"data: {json.dumps(update)}\n\n"
                if update.get("status") in ("completed", "failed", "cancelled"):
                    break
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                # Check if job finished outside SSE
                current = get_job(job_id)
                if current and current.get("status") in ("completed", "failed", "cancelled"):
                    if current.get("progress"):
                        yield f"data: {json.dumps(current['progress'])}\n\n"
                    break
            except Exception:
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    )


# ============================
# Job Control
# ============================

@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("pending", "running"):
        raise HTTPException(status_code=400, detail="Job is not running")
    _cancel_flags[job_id] = True
    return {"status": "cancelling", "message": "Cancellation requested"}


@app.get("/api/jobs/history")
async def get_job_history(limit: int = Query(default=20, le=100)):
    return get_recent_jobs(limit)


# ============================
# Results & Data
# ============================

def _lead_to_export_dict(lead: dict) -> dict:
    """Convert DB lead row to export-compatible dict."""
    return {
        "name": lead.get("name", ""),
        "phone": lead.get("phone", ""),
        "whatsapp": lead.get("whatsapp", ""),
        "email": lead.get("email", ""),
        "contact_person": lead.get("contact_person", ""),
        "full_address": lead.get("full_address", ""),
        "area": lead.get("area", ""),
        "city": lead.get("city", ""),
        "pincode": lead.get("pincode", ""),
        "rating": lead.get("rating", ""),
        "total_reviews": lead.get("total_reviews", ""),
        "website": lead.get("website", ""),
        "justdial_url": lead.get("source_url", ""),
    }

def _generate_exports(job_id: str, leads_dicts: list):
    """Generate Excel and CSV export files for a job."""
    prefix = f"leads_{job_id[:8]}"
    excel_path = os.path.join(DOWNLOADS_DIR, f"{prefix}.xlsx")
    csv_path = os.path.join(DOWNLOADS_DIR, f"{prefix}.csv")
    try:
        export_to_excel(leads_dicts, excel_path)
        export_to_csv(leads_dicts, csv_path)
    except Exception as e:
        logger.warning(f"Export generation error: {e}")


@app.get("/api/results/{job_id}")
async def get_results(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    leads = get_leads(job_id)
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "count": len(leads),
        "results": leads
    }


@app.get("/api/download/{job_id}")
async def download_excel(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    prefix = f"leads_{job_id[:8]}"
    excel_path = os.path.join(DOWNLOADS_DIR, f"{prefix}.xlsx")

    if not os.path.exists(excel_path):
        leads = get_leads(job_id)
        if not leads:
            raise HTTPException(status_code=400, detail="No results to download.")
        leads_dicts = [_lead_to_export_dict(l) for l in leads]
        export_to_excel(leads_dicts, excel_path)

    return FileResponse(
        path=excel_path,
        filename=f"Leads_{job_id[:8]}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@app.get("/api/download-csv/{job_id}")
async def download_csv(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    prefix = f"leads_{job_id[:8]}"
    csv_path = os.path.join(DOWNLOADS_DIR, f"{prefix}.csv")

    if not os.path.exists(csv_path):
        leads = get_leads(job_id)
        if not leads:
            raise HTTPException(status_code=400, detail="No results to download.")
        leads_dicts = [_lead_to_export_dict(l) for l in leads]
        export_to_csv(leads_dicts, csv_path)

    return FileResponse(
        path=csv_path,
        filename=f"Leads_{job_id[:8]}.csv",
        media_type="text/csv"
    )


@app.get("/download-apk")
@app.get("/api/download-apk")
async def download_apk():
    apk_path = os.path.join(BASE_DIR, "LeadGenPro-debug.apk")
    if not os.path.exists(apk_path):
        apk_path = os.path.join(BASE_DIR, "android", "app", "build", "outputs", "apk", "debug", "app-debug.apk")
    if not os.path.exists(apk_path):
        raise HTTPException(status_code=404, detail="APK not found.")
    return FileResponse(
        path=apk_path,
        filename="LeadGenPro-debug.apk",
        media_type="application/vnd.android.package-archive"
    )


# ============================
# Email Marketing & Gemini AI
# ============================

@app.get("/api/email/settings")
async def get_email_settings():
    smtp_cfg = get_default_smtp_config()
    return {
        "gemini_configured": settings.is_gemini_configured,
        "gemini_key_count": len(settings.get_gemini_keys()),
        "gemini_key_status": ai_provider.get_key_status(),
        "smtp_host": smtp_cfg.get("host", ""),
        "smtp_port": smtp_cfg.get("port", 587),
        "smtp_user": smtp_cfg.get("user", ""),
        "smtp_from_email": smtp_cfg.get("from_email", ""),
        "smtp_from_name": smtp_cfg.get("from_name", ""),
        "smtp_configured": settings.is_smtp_configured,
    }


@app.post("/api/ai/compose-email")
async def ai_compose_email(req: ComposeEmailRequest):
    sample_leads = []
    if req.job_id:
        sample_leads = get_leads(req.job_id)[:10]

    try:
        composed = await generate_cold_email(
            product_name=req.product_name,
            product_description=req.product_description,
            target_niche=req.target_niche,
            tone=req.tone or "conversational",
            video_link=req.video_link or "",
            sample_leads=sample_leads,
            api_key_override=req.gemini_api_key if req.gemini_api_key else None
        )
        return composed
    except Exception as e:
        logger.error(f"Email compose error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to generate email: {str(e)}")


@app.post("/api/email/preview")
async def preview_email(req: PreviewEmailRequest):
    """Render a preview of how an email will look for a specific lead."""
    lead_data = {"name": "Sample Business", "contact_person": "John", "city": "Mumbai",
                 "phone": "9876543210", "area": "Bandra"}

    if req.lead_id and req.job_id:
        leads = get_leads(req.job_id)
        for l in leads:
            if l.get("id") == req.lead_id:
                lead_data = l
                break

    rendered_subject = render_template_tags(req.subject, lead_data, video_link=req.video_link or "")
    rendered_body = render_template_tags(req.body, lead_data, video_link=req.video_link or "")
    rendered_html = build_html_email(rendered_body, video_link=req.video_link or "")

    return {
        "subject": rendered_subject,
        "body_text": rendered_body,
        "body_html": rendered_html,
        "lead": {
            "name": lead_data.get("name", ""),
            "email": lead_data.get("email", ""),
            "city": lead_data.get("city", "")
        }
    }


@app.post("/api/email/test-smtp")
async def test_smtp(req: TestSmtpRequest):
    cfg = {
        "host": req.host, "port": req.port,
        "user": req.user, "password": req.password,
        "from_email": req.from_email, "from_name": req.from_name
    }
    result = test_smtp_connection(cfg, req.test_recipient)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "SMTP test failed."))
    return result


def _run_email_campaign_task(
    campaign_id: str, job_id: str, leads: List[Dict[str, Any]],
    subject: str, body: str, smtp_config: Dict[str, Any],
    video_link: str, pdf_path: Optional[str], img_path: Optional[str],
    video_path: Optional[str], loop: asyncio.AbstractEventLoop
):
    """Background thread: email campaign dispatch."""
    def progress_callback(update: Dict[str, Any]):
        if _cancel_flags.get(campaign_id):
            raise InterruptedError("Campaign cancelled")
        update_campaign_status(
            campaign_id, update.get("status", "running"),
            sent=update.get("sent"), failed=update.get("failed")
        )
        _push_update(campaign_id, update, loop)

    try:
        update_campaign_status(campaign_id, "running")
        result = send_campaign(
            leads=leads,
            subject_template=subject,
            body_template=body,
            smtp_config=smtp_config,
            video_link=video_link,
            pdf_attachment_path=pdf_path,
            img_attachment_path=img_path,
            video_attachment_path=video_path,
            progress_callback=progress_callback,
            campaign_id=campaign_id,
        )
        update_campaign_status(
            campaign_id, result.get("status", "completed"),
            sent=result.get("sent", 0), failed=result.get("failed", 0)
        )
        final = {**result, "percent": 100}
        _push_update(campaign_id, final, loop)
    except InterruptedError:
        update_campaign_status(campaign_id, "cancelled")
        _push_update(campaign_id, {"status": "cancelled", "percent": 100, "message": "Campaign cancelled"}, loop)
    except Exception as e:
        logger.error(f"Campaign error: {e}", exc_info=True)
        update_campaign_status(campaign_id, "failed")
        _push_update(campaign_id, {
            "status": "failed", "percent": 100, "message": f"Campaign error: {str(e)[:200]}"
        }, loop)
    finally:
        _cleanup_queue(campaign_id)


@app.post("/api/email/send-campaign")
async def send_email_campaign(
    background_tasks: BackgroundTasks,
    job_id: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    lead_ids: str = Form(""),  # Comma-separated lead IDs, empty = all with email
    video_link: Optional[str] = Form(""),
    smtp_host: Optional[str] = Form(""),
    smtp_port: Optional[int] = Form(587),
    smtp_user: Optional[str] = Form(""),
    smtp_password: Optional[str] = Form(""),
    smtp_from_email: Optional[str] = Form(""),
    smtp_from_name: Optional[str] = Form(""),
    pdf_file: Optional[UploadFile] = File(None),
    img_file: Optional[UploadFile] = File(None),
    video_file: Optional[UploadFile] = File(None)
):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found. Run a search first.")

    # Get eligible leads
    if lead_ids and lead_ids.strip():
        selected_ids = [int(x.strip()) for x in lead_ids.split(",") if x.strip().isdigit()]
        all_leads = get_leads(job_id)
        eligible = [l for l in all_leads if l.get("id") in selected_ids and l.get("email") and "@" in l.get("email", "")]
    else:
        eligible = get_leads_with_email(job_id)

    if not eligible:
        raise HTTPException(status_code=400, detail="No leads with email addresses found.")

    # Build SMTP config
    default_cfg = get_default_smtp_config()
    final_smtp = {
        "host": (smtp_host or "").strip() or default_cfg.get("host", "smtp.gmail.com"),
        "port": int(smtp_port or 0) or default_cfg.get("port", 587),
        "user": (smtp_user or "").strip() or default_cfg.get("user", ""),
        "password": (smtp_password or "").strip() or default_cfg.get("password", ""),
        "from_email": (smtp_from_email or "").strip() or default_cfg.get("from_email", ""),
        "from_name": (smtp_from_name or "").strip() or default_cfg.get("from_name", "Outreach"),
    }

    if not final_smtp["user"] or not final_smtp["password"]:
        raise HTTPException(status_code=400, detail="SMTP credentials required.")

    # Create campaign in database
    lead_id_list = [l["id"] for l in eligible]
    campaign_id = create_campaign(job_id, subject, body, video_link or "", lead_id_list)

    # Save attachments securely
    camp_dir = os.path.join(ATTACHMENTS_DIR, campaign_id)
    os.makedirs(camp_dir, exist_ok=True)

    pdf_path = await _save_upload(pdf_file, camp_dir) if pdf_file else None
    img_path = await _save_upload(img_file, camp_dir) if img_file else None
    video_path = await _save_upload(video_file, camp_dir) if video_file else None

    loop = asyncio.get_running_loop()
    _get_queue(campaign_id)

    background_tasks.add_task(
        _run_email_campaign_task,
        campaign_id, job_id, eligible, subject, body,
        final_smtp, video_link or "", pdf_path, img_path, video_path, loop
    )

    return {
        "campaign_id": campaign_id,
        "total_recipients": len(eligible),
        "message": f"Campaign started for {len(eligible)} recipients."
    }


@app.post("/api/campaigns/{campaign_id}/cancel")
async def cancel_campaign(campaign_id: str):
    campaign = get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign["status"] not in ("pending", "running"):
        raise HTTPException(status_code=400, detail="Campaign is not running")
    _cancel_flags[campaign_id] = True
    return {"status": "cancelling"}


@app.get("/api/email/progress/{campaign_id}")
async def email_campaign_progress(campaign_id: str):
    campaign = get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    queue = _get_queue(campaign_id)

    async def event_generator():
        yield f"data: {json.dumps({'status': campaign['status'], 'sent': campaign.get('sent_count', 0), 'failed': campaign.get('failed_count', 0), 'total': campaign.get('total_recipients', 0)})}\n\n"

        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=25.0)
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("status") in ("completed", "failed", "cancelled"):
                    break
            except asyncio.TimeoutError:
                current = get_campaign(campaign_id)
                if current and current.get("status") in ("completed", "failed", "cancelled"):
                    yield f"data: {json.dumps({'status': current['status'], 'sent': current.get('sent_count', 0), 'failed': current.get('failed_count', 0), 'percent': 100})}\n\n"
                    break
                yield f": heartbeat\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    )


# ============================
# System
# ============================

@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "gemini_configured": settings.is_gemini_configured,
        "smtp_configured": settings.is_smtp_configured,
        "ai_keys": ai_provider.get_key_status(),
    }


# ============================
# Static Assets for Web UI
# ============================

@app.get("/{filename:path}")
async def serve_static_assets(filename: str):
    safe_path = os.path.normpath(os.path.join(TEMPLATES_DIR, filename))
    if not safe_path.startswith(TEMPLATES_DIR):
        raise HTTPException(status_code=403, detail="Forbidden")
    if os.path.isfile(safe_path):
        media_type = None
        if safe_path.endswith(".js"):
            media_type = "application/javascript"
        elif safe_path.endswith(".css"):
            media_type = "text/css"
        elif safe_path.endswith(".json"):
            media_type = "application/json"
        elif safe_path.endswith(".png"):
            media_type = "image/png"
        elif safe_path.endswith(".svg"):
            media_type = "image/svg+xml"
        return FileResponse(safe_path, media_type=media_type)
    raise HTTPException(status_code=404, detail="File not found")

