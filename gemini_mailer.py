import os
import re
import json
import logging
import httpx
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# World-Class Cold Email Copywriting System Prompt
# Incorporates proven frameworks (Lavender, Josh Braun, Justin Welsh, Alex Hormozi)
BEST_COLD_EMAIL_SYSTEM_PROMPT = """You are an elite B2B and B2C Cold Outreach Copywriter trained on the highest-converting cold email frameworks in the world (Lavender 90+ Score, Josh Braun Poke Method, Justin Welsh Direct Response, Chris Voss Frictionless CTAs).

Your mission is to write high-converting, personalized cold email copy designed to get replies from busy business owners.

STRICT COPYWRITING RULES:
1. BREVITY: Keep the total email between 60 to 110 words. Busy owners delete long walls of text.
2. NO ROBOTIC OPENERS: NEVER start with "I hope this email finds you well", "My name is X and I am writing to...", or "Allow me to introduce...". Start directly with a conversational observation about their industry or local market.
3. DYNAMIC MERGE TAGS:
   - Use `{business_name}` for the company/shop name.
   - Use `{contact_person}` for the owner or manager name.
   - Use `{city}` for the target city/location.
   - Use `{video_link}` to reference the product video/ad demo.
4. VALUE PROPOSITION: Focus entirely on OUTCOMES (e.g. saving hours, reducing waste, acquiring more local clients, lowering costs), not feature lists.
5. MEDIA INTEGRATION:
   - If a brochure PDF is attached, casually mention: "I've attached our 1-page brochure PDF with complete specs and pricing."
   - If a product video link is provided, include `{video_link}` with a low-friction prompt (e.g., "You can see a 30-second live demo here: {video_link}").
6. LOW-FRICTION CTA: End with an interest-based, zero-pressure question.
   - Good: "Open to exploring if this makes sense for {business_name}?", "Worth a 2-minute look?", "Would it be against company policy to take a quick peek?"
   - Bad: "Can we schedule a 30-minute demo on Tuesday at 2 PM?" (Too high friction).
7. SPAM EVASION: Avoid spam triggers ("FREE", "GUARANTEED", "ACT NOW", ALL-CAPS, exclamation marks).
8. OUTPUT FORMAT: Return ONLY valid JSON with keys:
   - "subject_lines": Array of 3 high-open subject lines (3-5 words each).
   - "email_body": The email body text containing merge tags ({business_name}, {contact_person}, {city}, {video_link}).
   - "strategy_breakdown": 1-2 sentences explaining why this angle converts for this audience.
"""

def get_gemini_api_key(override_key: Optional[str] = None) -> str:
    """Retrieve Gemini API key from override, environment, or .env file."""
    if override_key and override_key.strip():
        return override_key.strip()
    return os.environ.get("GEMINI_API_KEY", "").strip()


async def generate_cold_email(
    product_name: str,
    product_description: str,
    target_niche: str = "",
    tone: str = "conversational",
    video_link: str = "",
    sample_leads: Optional[List[Dict[str, Any]]] = None,
    api_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Calls Google Gemini API (gemini-2.0-flash / gemini-1.5-flash) to write an optimized cold email
    tailored to the target leads scraped from the web.
    """
    key = get_gemini_api_key(api_key)
    
    # Extract rich lead context from scraped samples
    lead_context = ""
    if sample_leads:
        sample_names = [l.get("name", "") for l in sample_leads[:4] if l.get("name")]
        sample_cities = list(set([l.get("city", "") for l in sample_leads[:6] if l.get("city")]))
        sample_types = list(set([l.get("area", "") for l in sample_leads[:4] if l.get("area")]))
        lead_context = f"""
TARGET AUDIENCE REAL CONTEXT (From Scraped Leads):
- Sample Business Names: {', '.join(sample_names) if sample_names else 'Local businesses'}
- Geographic Markets / Cities: {', '.join(sample_cities) if sample_cities else 'Metropolitan hubs'}
- Primary Niche / Category: {target_niche or 'Target industry'}
"""

    user_prompt = f"""
Write a high-converting cold outreach email marketing campaign for:
- Product / Service Name: {product_name}
- Product Description & Offer: {product_description}
- Target Niche / Industry: {target_niche or 'Discovered B2B Leads'}
- Desired Tone: {tone} (Options: conversational, direct & punchy, professional, friendly)
- Product Video / Ad Link: {video_link if video_link else 'Not provided'}
{lead_context}

Format your response strictly as JSON with keys:
1. "subject_lines": [3 short, curiosity-inducing subject lines]
2. "email_body": "The cold email body with merge tags {{business_name}}, {{contact_person}}, {{city}}, {{video_link}}"
3. "strategy_breakdown": "Explanation of the copywriting hooks used"
"""

    if not key:
        logger.info("No GEMINI_API_KEY provided. Using battle-tested template fallback.")
        return generate_template_fallback(product_name, product_description, video_link)

    # Models in order of preference
    models = ["gemini-2.0-flash", "gemini-1.5-flash"]
    
    async with httpx.AsyncClient(timeout=25.0) as client:
        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": BEST_COLD_EMAIL_SYSTEM_PROMPT + "\n\n" + user_prompt}
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.7,
                    "topP": 0.95,
                    "maxOutputTokens": 1024,
                    "responseMimeType": "application/json"
                }
            }

            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        text_out = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        clean_json = extract_json_from_text(text_out)
                        if clean_json and "email_body" in clean_json:
                            logger.info(f"Successfully generated cold email via Gemini ({model})")
                            return clean_json
                else:
                    logger.warning(f"Gemini API ({model}) returned {resp.status_code}: {resp.text[:120]}")
            except Exception as e:
                logger.warning(f"Gemini API request error on {model}: {e}")

    # If all models fail, return premium fallback
    return generate_template_fallback(product_name, product_description, video_link)


def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract and parse JSON object from markdown fenced blocks or raw strings."""
    if not text:
        return None
    # Strip markdown ```json code blocks
    cleaned = re.sub(r"^```json\s*", "", text.strip(), flags=re.I)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"```$", "", cleaned.strip())

    try:
        return json.loads(cleaned)
    except Exception:
        # Fallback regex search for { ... }
        m = re.search(r"(\{.*\})", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return None


def generate_template_fallback(product_name: str, product_desc: str, video_link: str = "") -> Dict[str, Any]:
    """High-converting battle-tested cold email template when Gemini is offline or unkeyed."""
    video_sentence = f"\n\nHere is a 30-second video demo of how it works: {video_link}" if video_link else ""
    
    body = f"""Hi {{contact_person}},

Noticed {product_name or 'your business'} operations in {{city}} and wanted to share a quick thought.

Most owners we speak with find that handling daily bottlenecks eats into profitability. We built {product_name} specifically to solve this:

{product_desc or 'We help businesses streamline operations and increase revenue by over 25% with zero downtime.'}

I've attached our 1-page brochure PDF with complete details and transparent pricing.{video_sentence}

Open to exploring if this makes sense for {{business_name}}?

Best regards,
{{sender_name}}"""

    return {
        "subject_lines": [
            f"quick question re: {{business_name}}",
            f"idea for {{business_name}} in {{city}}",
            f"{{contact_person}} - 2 min thought"
        ],
        "email_body": body,
        "strategy_breakdown": "Battle-tested 4-part framework: Contextual Hook -> Value Proposition -> Proof / PDF attachment -> Frictionless CTA."
    }
