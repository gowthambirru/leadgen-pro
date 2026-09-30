import os
from fastapi.testclient import TestClient
from app import app, jobs
from gemini_mailer import generate_cold_email, extract_json_from_text, BEST_COLD_EMAIL_SYSTEM_PROMPT
from email_sender import render_template_tags, build_html_email, get_default_smtp_config

client = TestClient(app)

def test_gemini_template_fallback():
    print("\n[1] Testing Cold Email Generation (Fallback / Offline)...")
    sample_leads = [
        {"name": "Sri Krishna Chicken", "city": "Hyderabad", "contact_person": "Krishna", "email": "krishna@example.com"},
        {"name": "Eagle Fresh Poultry", "city": "Secunderabad", "contact_person": "Rao", "email": "eagle@example.com"}
    ]

    import asyncio
    res = asyncio.run(generate_cold_email(
        product_name="EcoCoolers Pro",
        product_description="Industrial meat chiller with 40% power savings and WhatsApp temperature alerts.",
        target_niche="Poultry & Meat Retailers",
        tone="conversational",
        video_link="https://youtu.be/demo123",
        sample_leads=sample_leads,
        api_key="" # Testing fallback
    ))

    assert "subject_lines" in res, "Missing subject_lines"
    assert len(res["subject_lines"]) >= 3, "Expected at least 3 subject line options"
    assert "email_body" in res, "Missing email_body"
    assert "{business_name}" in res["email_body"] or "{contact_person}" in res["email_body"], "Merge tags missing"
    assert "https://youtu.be/demo123" in res["email_body"], "Video link missing from body"
    print(" -> Generated Subject Lines:", res["subject_lines"])
    print(" -> Email Body Sample:\n", res["email_body"][:180] + "...")


def test_email_template_rendering_and_html():
    print("\n[2] Testing Email Template Tag Replacement & Responsive HTML...")
    template = "Hi {contact_person}, noticed {business_name} in {city}. Watch demo: {video_link}"
    lead = {
        "name": "Bismillah Chicken Center",
        "contact_person": "Rahman",
        "city": "Hyderabad",
        "email": "bismillah@test.com"
    }

    rendered = render_template_tags(
        template=template,
        lead=lead,
        sender_name="Alex from EcoCoolers",
        video_link="https://youtu.be/demo123"
    )

    assert "Hi Rahman" in rendered
    assert "Bismillah Chicken Center in Hyderabad" in rendered
    assert "https://youtu.be/demo123" in rendered
    print(" -> Rendered text:", rendered)

    html = build_html_email(
        rendered_body=rendered,
        video_link="https://youtu.be/demo123",
        has_pdf=True,
        sender_name="Alex"
    )
    assert "Watch 30-Sec Video Demo" in html
    assert "Product Brochure & Detailed Specifications (PDF)" in html
    print(" -> HTML email generated successfully (len: " + str(len(html)) + " chars).")


def test_api_endpoints():
    print("\n[3] Testing FastAPI Endpoints for Email Marketing & Gemini...")
    # 1. GET /api/email/settings
    resp_settings = client.get("/api/email/settings")
    assert resp_settings.status_code == 200
    cfg = resp_settings.json()
    assert "gemini_configured" in cfg
    assert "smtp_host" in cfg
    print(" -> Settings Endpoint OK. Host:", cfg.get("smtp_host"))

    # 2. POST /api/ai/compose-email
    resp_ai = client.post("/api/ai/compose-email", json={
        "product_name": "QuickPOS Billing",
        "product_description": "GST billing machine for meat shops with daily WhatsApp profit reports.",
        "target_niche": "Chicken Retailers",
        "tone": "direct",
        "video_link": "https://youtu.be/quickpos"
    })
    assert resp_ai.status_code == 200
    ai_data = resp_ai.json()
    assert "subject_lines" in ai_data
    assert "email_body" in ai_data
    print(" -> AI Compose Endpoint OK. Subject:", ai_data["subject_lines"][0])

    # 3. POST /api/email/send-campaign with dummy job
    test_job_id = "test-job-999"
    jobs[test_job_id] = {
        "status": "completed",
        "results": [
            {"name": "Lead 1", "email": "lead1@test.com", "city": "Hyderabad"},
            {"name": "Lead 2", "email": "lead2@test.com", "city": "Secunderabad"}
        ]
    }

    # Test missing credentials rejection
    resp_camp = client.post("/api/email/send-campaign", data={
        "job_id": test_job_id,
        "subject": "quick question for {business_name}",
        "body": "Hi {contact_person}, check our brochure.",
        "smtp_host": "smtp.gmail.com",
        "smtp_user": "",  # Empty user should fail validation
        "smtp_password": ""
    })
    # Should reject gracefully with 400 because SMTP credentials are not set
    assert resp_camp.status_code == 400
    print(" -> Validation OK (Rejected campaign when SMTP credentials missing).")

    print("\n=======================================================")
    print("   ALL EMAIL MARKETING & GEMINI TESTS PASSED 100%!")
    print("=======================================================")


if __name__ == "__main__":
    test_gemini_template_fallback()
    test_email_template_rendering_and_html()
    test_api_endpoints()
