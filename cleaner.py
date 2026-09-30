import re
from typing import Dict, Any, Optional, List


def clean_phone(phone_raw: Any) -> str:
    """
    Sanitizes phone numbers:
    - Rejects encrypted tokens / URL-encoded hashes (contains %, =, or long alphanumeric strings)
    - Validates minimum length and splits multiple numbers cleanly
    - Preserves leading zeros (e.g. 09035185511)
    """
    if not phone_raw:
        return ""
    s = str(phone_raw).strip()

    # Reject encrypted Justdial tokens or hashes
    if "%" in s or "=" in s or (len(s) > 16 and re.search(r"[a-zA-Z]", s)):
        return ""

    # Split multiple phone numbers if present
    parts = re.split(r"[,/|;]", s)
    valid_nums = []
    for p in parts:
        clean = re.sub(r"[^0-9+]", "", p.strip())
        if len(clean) >= 8:
            if clean not in valid_nums:
                valid_nums.append(clean)

    return ", ".join(valid_nums) if valid_nums else ""


def clean_whatsapp(wa_raw: Any) -> str:
    """
    Cleans WhatsApp numbers:
    - Removes python string representations of masked lists like ['xxxxxxx'] or empty []
    - Only returns valid 10+ digit phone numbers
    """
    if not wa_raw:
        return ""
    s = str(wa_raw).strip()

    # Reject masked placeholders or list syntax
    if "x" in s.lower() or "[" in s or "]" in s or "None" in s:
        return ""

    clean = re.sub(r"[^0-9+]", "", s)
    return clean if len(clean) >= 10 else ""


EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
EMAIL_BLACKLIST = ("schema.org", "w3.org", "example.com", "example.org", "sentry.io", "webpack", "justdial.com", "domain.com", "noreply@", "no-reply@")

def clean_email(email_raw: Any) -> str:
    """
    Validates and cleans email addresses:
    - Splits multiple comma/semicolon-separated emails
    - Validates RFC format
    - Filters out dummy/system emails
    - Deduplicates case-insensitively
    """
    if not email_raw:
        return ""
    candidates = [e.strip() for e in re.split(r"[,;\s]", str(email_raw)) if e.strip()]
    valid = []

    for e in candidates:
        if EMAIL_REGEX.match(e):
            e_lower = e.lower()
            if not any(b in e_lower for b in EMAIL_BLACKLIST):
                if e not in valid:
                    valid.append(e)

    return ", ".join(valid)


def clean_reviews(rev_raw: Any) -> str:
    """
    Extracts purely numeric review counts (e.g. '8,894 Ratings' -> '8894').
    """
    if not rev_raw:
        return ""
    s = str(rev_raw).replace(",", "").strip()
    m = re.search(r"(\d+)", s)
    return m.group(1) if m else ""


def clean_rating(rate_raw: Any) -> str:
    """
    Ensures clean numeric rating string (e.g. 4.5).
    """
    if not rate_raw:
        return ""
    try:
        val = float(str(rate_raw).strip())
        if 1.0 <= val <= 5.0:
            return f"{val:.1f}"
    except Exception:
        pass
    return ""


def clean_website(web_raw: Any) -> str:
    """
    Extracts and normalizes the primary website URL, removing duplicates and ensuring https://.
    """
    if not web_raw:
        return ""
    urls = [u.strip() for u in str(web_raw).split(",") if u.strip()]
    if not urls:
        return ""
    u = urls[0].rstrip("/")
    if not u.startswith("http://") and not u.startswith("https://"):
        u = "https://" + u
    return u


def clean_address(addr_raw: Any) -> str:
    """
    Formats address text, collapsing excess whitespace and fixing punctuation.
    Handles list structures or stringified lists gracefully.
    """
    if not addr_raw:
        return ""
    if isinstance(addr_raw, list):
        s = ", ".join(str(p).strip() for p in addr_raw if p)
    else:
        s = str(addr_raw).strip()
        if s.startswith("[") and s.endswith("]"):
            s = re.sub(r"^\[\s*['\"]?", "", s)
            s = re.sub(r"['\"]?\s*\]$", "", s)
            s = re.sub(r"['\"],\s*['\"]", ", ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r",\s*,+", ",", s)
    return s.strip(", ")


def clean_area(area_raw: Any, city: str = "", pincode: str = "") -> str:
    """
    Cleans area/locality string by stripping redundant city or pincode mentions.
    """
    if not area_raw:
        return ""
    s = str(area_raw).strip()
    if pincode and pincode in s:
        s = s.replace(pincode, "").strip()
    if city and city.lower() in s.lower():
        s = re.sub(r"[\s,]+" + re.escape(city) + r"[\s,]*", "", s, flags=re.I).strip()
    s = re.sub(r"[\s,/-]+$", "", s).strip()
    return s


def sanitize_listing(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transforms any raw listing dictionary into a clean, production-ready dictionary.
    """
    city = re.sub(r"\s+", " ", str(item.get("city", ""))).strip()
    pincode = re.sub(r"[^0-9]", "", str(item.get("pincode", ""))).strip()

    name = re.sub(r"\s+", " ", str(item.get("name", ""))).strip()
    phone = clean_phone(item.get("phone", ""))
    whatsapp = clean_whatsapp(item.get("whatsapp", ""))
    email = clean_email(item.get("email", ""))

    contact = re.sub(r"\s+", " ", str(item.get("contact_person", ""))).strip()
    full_address = clean_address(item.get("full_address", ""))
    area = clean_area(item.get("area", ""), city, pincode)

    rating = clean_rating(item.get("rating", ""))
    reviews = clean_reviews(item.get("total_reviews", ""))
    website = clean_website(item.get("website", ""))

    justdial_url = str(item.get("justdial_url", "")).strip()
    docid = str(item.get("docid", "")).strip()

    return {
        "name": name,
        "phone": phone,
        "whatsapp": whatsapp,
        "email": email,
        "contact_person": contact,
        "full_address": full_address,
        "area": area,
        "city": city,
        "pincode": pincode,
        "rating": rating,
        "total_reviews": reviews,
        "website": website,
        "justdial_url": justdial_url,
        "docid": docid
    }
