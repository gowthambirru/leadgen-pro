import os
import re
import smtplib
import time
import logging
import html
import copy
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import List, Dict, Any, Optional, Callable
from dotenv import load_dotenv

try:
    from config import settings
except ImportError:
    settings = None

try:
    import database
except ImportError:
    database = None

load_dotenv()

logger = logging.getLogger(__name__)


def get_default_smtp_config() -> Dict[str, Any]:
    """Load default SMTP configuration from environment or .env file."""
    if settings:
        smtp_pw = ""
        if settings.SMTP_PASSWORD:
            smtp_pw = settings.SMTP_PASSWORD.get_secret_value()
        return {
            "host": (settings.SMTP_HOST or "smtp.gmail.com").strip(),
            "port": int(settings.SMTP_PORT or 587),
            "user": (settings.SMTP_USER or "").strip(),
            "password": smtp_pw,
            "from_email": (settings.SMTP_FROM_EMAIL or settings.SMTP_USER or "").strip(),
            "from_name": (settings.SMTP_FROM_NAME or "Marketing Outreach").strip(),
        }
    return {
        "host": os.environ.get("SMTP_HOST", "smtp.gmail.com").strip(),
        "port": int(os.environ.get("SMTP_PORT", 587)),
        "user": os.environ.get("SMTP_USER", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", "").strip(),
        "from_email": os.environ.get("SMTP_FROM_EMAIL", "").strip() or os.environ.get("SMTP_USER", "").strip(),
        "from_name": os.environ.get("SMTP_FROM_NAME", "Marketing Outreach").strip(),
    }


def render_template_tags(template: str, lead: Dict[str, Any], sender_name: str = "", video_link: str = "") -> str:
    """Replaces personalized merge tags with lead data."""
    biz_name = lead.get("name") or "there"
    contact = lead.get("contact_person") or "Team"
    city = lead.get("city") or "your area"
    area = lead.get("area") or ""
    phone = lead.get("phone") or ""

    s = template
    s = s.replace("{business_name}", biz_name)
    s = s.replace("{contact_person}", contact)
    s = s.replace("{city}", city)
    s = s.replace("{area}", area)
    s = s.replace("{phone}", phone)
    s = s.replace("{sender_name}", sender_name or "Our Team")
    s = s.replace("{video_link}", video_link or "")
    return s.strip()


def build_html_email(
    rendered_body: str,
    video_link: str = "",
    has_pdf: bool = False,
    sender_name: str = ""
) -> str:
    """Generates a responsive, clean, professional HTML version of the cold email."""
    # Escape HTML to prevent injection, but preserve layout
    rendered_body = html.escape(rendered_body)
    
    # Convert double linebreaks to paragraphs
    paragraphs = [p.strip() for p in rendered_body.split("\n\n") if p.strip()]
    paragraphs_html = "".join([f"<p style='margin: 0 0 16px 0; line-height: 1.6;'>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs])

    video_card_html = ""
    if video_link:
        video_card_html = f"""
        <div style="margin: 20px 0; padding: 16px; background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; text-align: center;">
            <p style="margin: 0 0 10px 0; font-size: 13px; font-weight: 600; color: #475569;">Product Video & Live Ad Demo</p>
            <a href="{video_link}" target="_blank" style="display: inline-block; padding: 10px 22px; background-color: #4f46e5; color: #ffffff; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 14px; box-shadow: 0 2px 4px rgba(79, 70, 229, 0.2);">
                ▶ Watch 30-Sec Video Demo
            </a>
        </div>
        """

    pdf_badge_html = ""
    if has_pdf:
        pdf_badge_html = """
        <div style="margin: 15px 0; padding: 10px 14px; background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; font-size: 13px; color: #166534; display: inline-block;">
            📎 <strong>Attached:</strong> Product Brochure & Detailed Specifications (PDF)
        </div>
        """

    html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 24px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #1e293b; background-color: #f1f5f9; font-size: 15px;">
    <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; padding: 32px; border-radius: 16px; border: 1px solid #e2e8f0; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);">
        <div style="font-size: 15px; color: #1e293b;">
            {paragraphs_html}
        </div>
        {video_card_html}
        {pdf_badge_html}
        <div style="margin-top: 28px; padding-top: 20px; border-top: 1px solid #e2e8f0; font-size: 12px; color: #94a3b8; text-align: left;">
            Sent to your business contact email &bull; If this is not relevant, simply reply with "No" and we will remove your contact.
        </div>
    </div>
</body>
</html>
"""
    return html_content


def create_smtp_connection(config: Dict[str, Any]) -> smtplib.SMTP:
    """Establishes an authenticated SMTP connection."""
    host = config.get("host") or "smtp.gmail.com"
    port = int(config.get("port") or 587)
    user = config.get("user") or ""
    password = config.get("password") or ""

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        server.ehlo()
        server.starttls()
        server.ehlo()

    if user and password:
        server.login(user, password)
    return server


def test_smtp_connection(config: Dict[str, Any], test_email: str) -> Dict[str, Any]:
    """Tests SMTP credentials by sending a quick test verification email."""
    try:
        from_email = config.get("from_email") or config.get("user")
        from_name = config.get("from_name") or "Lead Outreach"
        
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "SMTP Verification Test - Cold Outreach Ready"
        msg["From"] = f"{from_name} <{from_email}>"
        msg["To"] = test_email

        body_text = "Your SMTP configuration is working perfectly! You are ready to launch cold email campaigns."
        body_html = f"<div style='font-family:sans-serif;padding:20px;color:#166534;background:#f0fdf4;border-radius:10px;'><h3>✅ SMTP Connected Successfully!</h3><p>{body_text}</p></div>"

        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        server = create_smtp_connection(config)
        server.sendmail(from_email, [test_email], msg.as_string())
        server.quit()

        return {"success": True, "message": f"Test email successfully sent to {test_email}!"}
    except Exception as e:
        logger.error(f"SMTP Test Error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def pre_encode_attachment(file_path: str, custom_filename: Optional[str] = None) -> Optional[MIMEBase]:
    """Reads and encodes a file ONCE, returning a pre-built MIMEBase object."""
    if not file_path or not os.path.exists(file_path):
        return None

    filename = custom_filename or os.path.basename(file_path)
    part = MIMEBase("application", "octet-stream")
    with open(file_path, "rb") as f:
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f"attachment; filename=\"{filename}\"")
    return part


def attach_file(msg: MIMEMultipart, file_path: str, custom_filename: Optional[str] = None):
    """Attaches a local file to the email message."""
    part = pre_encode_attachment(file_path, custom_filename)
    if part:
        msg.attach(part)


def send_campaign(
    leads: List[Dict[str, Any]],
    subject_template: str,
    body_template: str,
    smtp_config: Dict[str, Any],
    video_link: str = "",
    pdf_attachment_path: Optional[str] = None,
    img_attachment_path: Optional[str] = None,
    video_attachment_path: Optional[str] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    campaign_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Dispatches personalized cold emails to all leads with valid email addresses.
    Provides live progress updates and comprehensive delivery reporting.
    """
    eligible_leads = [l for l in leads if l.get("email") and "@" in str(l.get("email"))]
    total_eligible = len(eligible_leads)

    if total_eligible == 0:
        return {
            "status": "completed",
            "total": 0,
            "sent": 0,
            "failed": 0,
            "message": "No scraped leads with valid email addresses were found in this dataset."
        }

    from_email = smtp_config.get("from_email") or smtp_config.get("user")
    from_name = smtp_config.get("from_name") or "Outreach"

    sent_count = 0
    failed_count = 0
    failed_details = []

    def report(current_idx: int, extra_msg: str):
        if progress_callback:
            progress_callback({
                "status": "sending",
                "current": current_idx,
                "total": total_eligible,
                "sent": sent_count,
                "failed": failed_count,
                "percent": int((current_idx / total_eligible) * 100),
                "message": extra_msg
            })

    report(0, f"Connecting to SMTP server ({smtp_config.get('host')})...")

    server = None
    try:
        server = create_smtp_connection(smtp_config)
    except Exception as conn_err:
        logger.error(f"Failed to connect to SMTP server: {conn_err}")
        return {
            "status": "failed",
            "total": total_eligible,
            "sent": 0,
            "failed": total_eligible,
            "error": f"SMTP Connection Failed: {str(conn_err)}"
        }

    # Pre-encode attachments
    pre_encoded_pdf = pre_encode_attachment(pdf_attachment_path) if pdf_attachment_path else None
    pre_encoded_img = pre_encode_attachment(img_attachment_path) if img_attachment_path else None
    pre_encoded_video = pre_encode_attachment(video_attachment_path) if video_attachment_path else None

    has_pdf = pre_encoded_pdf is not None

    for idx, lead in enumerate(eligible_leads, start=1):
        to_email = lead["email"].strip()
        biz_name = lead.get("name", "Business")
        lead_id = lead.get("id")

        if campaign_id and lead_id and database:
            if hasattr(database, 'is_lead_already_sent') and database.is_lead_already_sent(campaign_id, lead_id):
                report(idx, f"[{idx}/{total_eligible}] Skipping already-sent lead {biz_name}")
                continue

        report(idx, f"[{idx}/{total_eligible}] Sending to {biz_name} ({to_email})...")

        try:
            # Render personalized templates
            subject = render_template_tags(subject_template, lead, sender_name=from_name, video_link=video_link)
            body_text = render_template_tags(body_template, lead, sender_name=from_name, video_link=video_link)
            body_html = build_html_email(
                rendered_body=body_text,
                video_link=video_link,
                has_pdf=has_pdf,
                sender_name=from_name
            )

            # Build message
            msg = MIMEMultipart("mixed")
            msg["Subject"] = subject
            msg["From"] = f"{from_name} <{from_email}>"
            msg["To"] = to_email
            msg["Reply-To"] = from_email

            # Alternative body (text + HTML)
            alt_part = MIMEMultipart("alternative")
            alt_part.attach(MIMEText(body_text, "plain", "utf-8"))
            alt_part.attach(MIMEText(body_html, "html", "utf-8"))
            msg.attach(alt_part)

            # Attachments using deepcopy
            if pre_encoded_pdf:
                msg.attach(copy.deepcopy(pre_encoded_pdf))
            if pre_encoded_img:
                msg.attach(copy.deepcopy(pre_encoded_img))
            if pre_encoded_video:
                msg.attach(copy.deepcopy(pre_encoded_video))

            # Dispatch
            server.sendmail(from_email, [to_email], msg.as_string())
            sent_count += 1
            
            if campaign_id and lead_id and database:
                if hasattr(database, 'mark_recipient_sent'):
                    database.mark_recipient_sent(campaign_id, lead_id)
                    
            report(idx, f"[{idx}/{total_eligible}] Sent successfully to {biz_name}")

            # Polite anti-spam delay between sends
            time.sleep(1.2)

        except Exception as send_err:
            logger.warning(f"Error sending email to {to_email}: {send_err}")
            failed_count += 1
            failed_details.append({"email": to_email, "name": biz_name, "error": str(send_err)})
            
            if campaign_id and lead_id and database:
                if hasattr(database, 'mark_recipient_failed'):
                    database.mark_recipient_failed(campaign_id, lead_id, str(send_err))
                    
            report(idx, f"[{idx}/{total_eligible}] Delivery issue with {to_email}: {str(send_err)[:35]}")

            # Attempt reconnect if pipe broken
            try:
                server.quit()
            except Exception:
                pass
                
            try:
                server = create_smtp_connection(smtp_config)
            except Exception as reconnect_err:
                logger.error(f"Reconnect failed: {reconnect_err}. Retrying in 2 seconds...")
                time.sleep(2)
                try:
                    server = create_smtp_connection(smtp_config)
                except Exception as second_err:
                    logger.error(f"Second reconnect failed: {second_err}. Breaking campaign loop.")
                    break

    if server:
        try:
            server.quit()
        except Exception:
            pass

    final_msg = f"Campaign complete! {sent_count} sent successfully, {failed_count} failed out of {total_eligible}."
    if progress_callback:
        progress_callback({
            "status": "completed",
            "current": total_eligible,
            "total": total_eligible,
            "sent": sent_count,
            "failed": failed_count,
            "percent": 100,
            "message": final_msg
        })

    return {
        "status": "completed",
        "total": total_eligible,
        "sent": sent_count,
        "failed": failed_count,
        "failed_details": failed_details,
        "message": final_msg
    }
