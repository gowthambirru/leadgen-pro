import os
import io
import time
import json
import html
from fastapi.testclient import TestClient

from app import app
from config import settings
from database import (
    init_db, create_job, update_job_status, get_job,
    insert_leads, get_leads, get_leads_with_email,
    create_campaign, get_campaign, mark_recipient_sent,
    is_lead_already_sent, get_recent_jobs
)
from ai_provider import AIProvider, KeyState, KeyInfo, generate_template_fallback
from manual_scraper import ManualSearchScraper
from email_sender import build_html_email, pre_encode_attachment, render_template_tags

client = TestClient(app)

def test_config():
    print("\n[TEST 1] Centralized Configuration & Secrets Masking...")
    assert hasattr(settings, 'HOST')
    assert hasattr(settings, 'PORT')
    assert hasattr(settings, 'MAX_SCRAPE_WORKERS')
    keys = settings.get_gemini_keys()
    assert isinstance(keys, list)
    # Check that secrets are not accidentally leaked in __repr__
    s_repr = repr(settings)
    if settings.SMTP_PASSWORD:
        assert settings.SMTP_PASSWORD.get_secret_value() not in s_repr
    print(" -> Config loaded and secrets securely masked.")


def test_database_persistence_and_deduplication():
    print("\n[TEST 2] SQLite Persistence, Deduplication & Recipient State...")
    init_db()
    jid = create_job("web_search", "test persistence query", {"num_websites": 5})
    assert jid
    job = get_job(jid)
    assert job["query"] == "test persistence query"
    assert job["status"] == "pending"

    # Insert test leads with duplicates
    leads = [
        {"name": "Pet Paradise", "phone": "9998887770", "email": "info@petparadise.com", "city": "Hyderabad"},
        {"name": "Pet Paradise", "phone": "9998887770", "email": "info@petparadise.com", "city": "Hyderabad"}, # Duplicate
        {"name": "Doggy Daycare", "phone": "9998887771", "email": "care@doggy.com", "city": "Secunderabad"},
        {"name": "No Email Shop", "phone": "9998887772", "email": "", "city": "Hyderabad"}
    ]
    inserted = insert_leads(jid, leads)
    assert inserted == 3, f"Expected 3 unique leads, got {inserted}"

    all_leads = get_leads(jid)
    assert len(all_leads) == 3
    with_email = get_leads_with_email(jid)
    assert len(with_email) == 2

    # Campaign & Recipient tracking
    lead_ids = [l["id"] for l in with_email]
    camp_id = create_campaign(jid, "Special Offer", "Hello {business_name}", "", lead_ids)
    assert camp_id
    camp = get_campaign(camp_id)
    assert camp["total_recipients"] == 2

    # Duplicate send prevention test
    lead_1_id = lead_ids[0]
    assert not is_lead_already_sent(camp_id, lead_1_id)
    mark_recipient_sent(camp_id, lead_1_id)
    assert is_lead_already_sent(camp_id, lead_1_id)
    print(" -> SQLite schema, UNIQUE constraints, and per-recipient tracking verified.")


def test_ai_provider_circuit_breaker():
    print("\n[TEST 3] AI Provider Key Rotation & Circuit Breaker...")
    provider = AIProvider()
    provider.keys = [
        KeyInfo(key="dummy_key_1", state=KeyState.HEALTHY),
        KeyInfo(key="dummy_key_2", state=KeyState.HEALTHY)
    ]

    # Test round robin
    k1 = provider.get_healthy_key()
    k2 = provider.get_healthy_key()
    assert k1.key != k2.key

    # Test rate limit triggers cooldown
    provider.classify_error(k1, 429, "Resource exhausted")
    assert k1.state == KeyState.COOLDOWN
    assert k1.cooldown_until > time.time()

    # Next call should skip cooled down key and return k2
    k_next = provider.get_healthy_key()
    assert k_next.key == "dummy_key_2"

    # Test invalid key triggers permanent failure
    provider.classify_error(k2, 400, "API_KEY_INVALID: Key not found")
    assert k2.state == KeyState.FAILED

    # Offline template fallback
    fallback = generate_template_fallback("QuickChiller", "Saves power")
    assert "email_body" in fallback
    assert len(fallback["subject_lines"]) >= 3
    print(" -> Circuit breaker state transitions (HEALTHY -> COOLDOWN -> FAILED) verified.")


def test_ssrf_protection():
    print("\n[TEST 4] SSRF Protection Filter...")
    scraper = ManualSearchScraper()
    assert not scraper._is_safe_url("http://127.0.0.1:8000/api/health")
    assert not scraper._is_safe_url("http://localhost:8000/")
    assert not scraper._is_safe_url("http://169.254.169.254/latest/meta-data/")
    assert not scraper._is_safe_url("http://10.0.0.1/admin")
    assert not scraper._is_safe_url("http://192.168.1.1/router")
    assert not scraper._is_safe_url("file:///etc/passwd")
    assert not scraper._is_safe_url("javascript:alert(1)")
    # Public URLs should be permitted
    assert scraper._is_safe_url("https://www.google.com")
    assert scraper._is_safe_url("https://httpbin.org/get")
    print(" -> SSRF filter correctly blocks loopback, private ranges, metadata IPs, and non-HTTP.")


def test_email_security_and_attachment_encoding():
    print("\n[TEST 5] Email HTML Escaping & Attachment Pre-Encoding...")
    malicious_rendered = "Hello <script>alert('pwned')</script> & <b>bold</b>"
    html_email = build_html_email(malicious_rendered)
    # Ensure raw script tags are NOT present in output
    assert "<script>" not in html_email
    assert "&lt;script&gt;alert(&#x27;pwned&#x27;)&lt;/script&gt;" in html_email or "&lt;script&gt;" in html_email
    print(" -> HTML injection successfully prevented via escaping.")

    # Pre-encoding test
    test_pdf = os.path.join(os.path.dirname(__file__), "downloads", "test_doc.pdf")
    os.makedirs(os.path.dirname(test_pdf), exist_ok=True)
    with open(test_pdf, "wb") as f:
        f.write(b"%PDF-1.4 dummy pdf content for testing attachment pre-encoding")
    
    part = pre_encode_attachment(test_pdf)
    assert part is not None
    assert part.get_payload()
    print(" -> Attachment pre-encoded once in memory successfully.")
    if os.path.exists(test_pdf):
        os.remove(test_pdf)


def test_api_endpoints_and_sanitization():
    print("\n[TEST 6] API Endpoints, Email Preview, History & File Upload Sanitization...")
    # Health endpoint
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "ai_keys" in data

    # Email preview endpoint
    prev_resp = client.post("/api/email/preview", json={
        "subject": "Exclusive offer for {business_name}",
        "body": "Hi {contact_person}, we love your store in {city}!",
        "video_link": "https://youtu.be/testvideo"
    })
    assert prev_resp.status_code == 200
    pdata = prev_resp.json()
    assert "subject" in pdata
    assert "body_html" in pdata
    assert "Sample Business" in pdata["subject"]
    assert "Watch 30-Sec Video Demo" in pdata["body_html"]

    # History endpoint
    hist_resp = client.get("/api/jobs/history")
    assert hist_resp.status_code == 200
    assert isinstance(hist_resp.json(), list)

    # Path traversal upload sanitization in app._safe_filename
    from app import _safe_filename
    assert _safe_filename("../../etc/passwd") == "passwd"
    assert _safe_filename("..\\..\\windows\\system32\\calc.exe") == "calc.exe"
    assert _safe_filename("normal_brochure.pdf") == "normal_brochure.pdf"

    print(" -> API preview, health, history, and path traversal protections verified.")


if __name__ == "__main__":
    test_config()
    test_database_persistence_and_deduplication()
    test_ai_provider_circuit_breaker()
    test_ssrf_protection()
    test_email_security_and_attachment_encoding()
    test_api_endpoints_and_sanitization()
    print("\n=======================================================")
    print("   ALL ARCHITECTURAL REDESIGN SUITE TESTS PASSED 100%!")
    print("=======================================================\n")
