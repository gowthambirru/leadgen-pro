import time
import logging
import httpx
import json
import re
import os
from typing import Optional, List, Dict, Any
from enum import Enum
from dataclasses import dataclass, field

try:
    from config import settings
except ImportError:
    settings = None

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

class KeyState(Enum):
    HEALTHY = 'healthy'
    COOLDOWN = 'cooldown'
    FAILED = 'failed'

@dataclass
class KeyInfo:
    key: str
    state: KeyState = KeyState.HEALTHY
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    total_requests: int = 0
    total_failures: int = 0
    last_error: str = ''

class AIProvider:
    """Multi-key Gemini AI provider with circuit breaker and health-aware routing."""
    
    MODELS = ['gemini-2.0-flash', 'gemini-1.5-flash']
    BASE_URL = 'https://generativelanguage.googleapis.com/v1beta/models'
    MAX_CONSECUTIVE_FAILURES = 3
    COOLDOWN_SECONDS = 60
    REQUEST_TIMEOUT = 25.0

    def __init__(self):
        self.keys: List[KeyInfo] = []
        self._current_index = 0
        self._load_keys()

    def _load_keys(self):
        keys = []
        if settings and hasattr(settings, 'get_gemini_keys'):
            keys = settings.get_gemini_keys()
        if not keys:
            env_keys = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY", "")
            keys = [k.strip() for k in env_keys.split(',') if k.strip()]
        for k in keys:
            if k and not any(ki.key == k for ki in self.keys):
                self.keys.append(KeyInfo(key=k))

    def get_healthy_key(self) -> Optional[KeyInfo]:
        if not self.keys:
            return None
            
        now = time.time()
        
        for _ in range(len(self.keys)):
            kinfo = self.keys[self._current_index]
            self._current_index = (self._current_index + 1) % len(self.keys)
            
            if kinfo.state == KeyState.HEALTHY:
                return kinfo
            elif kinfo.state == KeyState.COOLDOWN:
                if now >= kinfo.cooldown_until:
                    kinfo.state = KeyState.HEALTHY
                    kinfo.consecutive_failures = 0
                    return kinfo
                    
        return None

    def classify_error(self, kinfo: KeyInfo, status_code: int, response_body: str):
        kinfo.total_failures += 1
        kinfo.last_error = f"{status_code}: {response_body[:100]}"
        
        if status_code in (400, 401, 403) and 'API_KEY_INVALID' in response_body:
            kinfo.state = KeyState.FAILED
        elif status_code == 429:
            kinfo.state = KeyState.COOLDOWN
            kinfo.cooldown_until = time.time() + self.COOLDOWN_SECONDS
        elif status_code in (500, 503):
            kinfo.consecutive_failures += 1
            if kinfo.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                kinfo.state = KeyState.COOLDOWN
                kinfo.cooldown_until = time.time() + 30
        else:
            kinfo.consecutive_failures += 1

    def get_key_status(self) -> List[Dict[str, Any]]:
        status = []
        for kinfo in self.keys:
            key_preview = kinfo.key[:8] + '...' if len(kinfo.key) > 8 else kinfo.key
            status.append({
                'key_preview': key_preview,
                'state': kinfo.state.value,
                'consecutive_failures': kinfo.consecutive_failures,
                'total_requests': kinfo.total_requests,
                'total_failures': kinfo.total_failures,
                'cooldown_remaining': max(0.0, kinfo.cooldown_until - time.time()) if kinfo.state == KeyState.COOLDOWN else 0.0
            })
        return status

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, api_key_override: Optional[str] = None) -> Optional[Dict[str, Any]]:
        keys_to_try = []
        if api_key_override:
            keys_to_try = [KeyInfo(key=api_key_override)]
        else:
            # Gather all healthy keys we can try
            for _ in range(len(self.keys)):
                k = self.get_healthy_key()
                if k and k not in keys_to_try:
                    keys_to_try.append(k)
                    
        if not keys_to_try:
            logger.warning("No healthy API keys available.")
            return None

        async with httpx.AsyncClient(timeout=self.REQUEST_TIMEOUT) as client:
            for kinfo in keys_to_try:
                kinfo.total_requests += 1
                for model in self.MODELS:
                    url = f"{self.BASE_URL}/{model}:generateContent?key={kinfo.key}"
                    payload = {
                        "contents": [
                            {
                                "role": "user",
                                "parts": [
                                    {"text": system_prompt + "\n\n" + user_prompt}
                                ]
                            }
                        ],
                        "generationConfig": {
                            "temperature": temperature,
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
                                    kinfo.consecutive_failures = 0
                                    kinfo.last_error = ''
                                    return clean_json
                        else:
                            if not api_key_override:
                                self.classify_error(kinfo, resp.status_code, resp.text)
                            if kinfo.state != KeyState.HEALTHY:
                                break  # Stop trying this key if it failed/cooldown
                    except httpx.TimeoutException:
                        if not api_key_override:
                            kinfo.total_failures += 1
                            kinfo.consecutive_failures += 1
                            kinfo.last_error = "Timeout"
                    except Exception as e:
                        if not api_key_override:
                            kinfo.total_failures += 1
                            kinfo.consecutive_failures += 1
                            kinfo.last_error = str(e)

        return None


def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = re.sub(r"^```json\s*", "", text.strip(), flags=re.I)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"```$", "", cleaned.strip())

    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r"(\{.*\})", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return None

def generate_template_fallback(product_name: str, product_desc: str, video_link: str = "") -> Dict[str, Any]:
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

COLD_EMAIL_SYSTEM_PROMPT = """You are an elite B2B and B2C Cold Outreach Copywriter trained on the highest-converting cold email frameworks in the world (Lavender 90+ Score, Josh Braun Poke Method, Justin Welsh Direct Response, Chris Voss Frictionless CTAs).

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

ai_provider = AIProvider()

async def generate_cold_email(
    product_name: str,
    product_description: str,
    target_niche: str = '',
    tone: str = 'conversational',
    video_link: str = '',
    sample_leads: Optional[List[Dict[str, Any]]] = None,
    api_key_override: Optional[str] = None
) -> Dict[str, Any]:
    
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

    try:
        res = await ai_provider.generate(
            system_prompt=COLD_EMAIL_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.7,
            api_key_override=api_key_override
        )
        if res:
            logger.info("Successfully generated cold email via AIProvider")
            return res
    except Exception as e:
        logger.warning(f"AIProvider error: {e}")

    logger.info("Using battle-tested template fallback.")
    return generate_template_fallback(product_name, product_description, video_link)
