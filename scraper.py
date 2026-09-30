import re
import json
import time
import logging
from typing import List, Dict, Any, Optional, Callable
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from curl_cffi import requests
from bs4 import BeautifulSoup

from cleaner import (
    clean_phone,
    clean_whatsapp,
    clean_email,
    clean_reviews,
    clean_rating,
    clean_website,
    clean_address,
    clean_area,
    sanitize_listing
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class JustdialScraper:
    """
    Robust scraper for Justdial categories and business listings.
    Uses TLS impersonation to bypass Akamai Bot Management and extracts
    data from Next.js hydration payload and DOM structures.
    """

    BASE_URL = "https://www.justdial.com"

    DEFAULT_HEADERS = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "referer": "https://www.google.com/search?q=justdial",
        "sec-ch-ua": '"Google Chrome";v="120", "Chromium";v="120", "Not?A_Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "cross-site",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    def __init__(self, impersonate: str = "chrome120"):
        self.impersonate = impersonate
        self.session = requests.Session(impersonate=self.impersonate)

    def _normalize_url(self, url: str) -> str:
        """Ensure URL has scheme, host, and clean path without query strings that break pagination."""
        url = url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        # Keep clean base URL without query parameters that corrupt pagination
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))

    def _build_page_url(self, base_url: str, page_num: int) -> str:
        """Construct URL with page query parameter."""
        parsed = urlparse(base_url)
        query = parse_qs(parsed.query)
        if page_num > 1:
            query["page"] = [str(page_num)]
        else:
            query.pop("page", None)
            query.pop("pg", None)

        new_query = urlencode(query, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    def fetch_html(self, url: str, retries: int = 2, backoff: float = 1.0, timeout: int = 8) -> Optional[str]:
        """Fetch page HTML with retry and backoff logic."""
        headers = dict(self.DEFAULT_HEADERS)
        parsed = urlparse(url)
        headers["Host"] = parsed.netloc

        for attempt in range(1, retries + 1):
            try:
                resp = self.session.get(url, headers=headers, timeout=timeout)
                if resp.status_code == 200 and len(resp.text) > 500:
                    return resp.text
                elif resp.status_code == 403 or len(resp.text) <= 500:
                    logger.warning(f"Attempt {attempt}: Received status {resp.status_code}, length {len(resp.text)}. Retrying...")
                    time.sleep(backoff * attempt)
            except Exception as e:
                logger.warning(f"Attempt {attempt} failed for {url}: {e}")
                time.sleep(backoff * attempt)

        return None

    def parse_search_page(self, html: str) -> List[Dict[str, Any]]:
        """
        Extract business listings from search page HTML.
        Prioritizes Next.js __NEXT_DATA__ JSON table; falls back to BeautifulSoup DOM.
        """
        listings = []
        soup = BeautifulSoup(html, "html.parser")

        # Method 1: Extract from Next.js __NEXT_DATA__ script
        nd_script = soup.find("script", id="__NEXT_DATA__")
        if not nd_script:
            for s in soup.find_all("script"):
                if s.string and '"pageProps":' in s.string and '"results":' in s.string:
                    nd_script = s
                    break

        if nd_script and nd_script.string:
            try:
                data = json.loads(nd_script.string)
                page_props = data.get("props", {}).get("pageProps", {})
                results_obj = page_props.get("listData", {}).get("results", {})
                columns = results_obj.get("columns", [])
                rows = results_obj.get("data", [])

                if columns and rows:
                    for row in rows:
                        item = dict(zip(columns, row))
                        name = item.get("name") or item.get("nameln") or ""
                        if not name:
                            continue

                        phone = clean_phone(item.get("VNumber") or item.get("callalocation") or "")
                        whatsapp = clean_whatsapp(item.get("wpnumber") or "")
                        area = item.get("area") or item.get("arealn") or ""
                        city = item.get("loccity") or item.get("city") or ""
                        pincode = item.get("pincode") or ""
                        rating = clean_rating(item.get("compRating") or "")
                        reviews = clean_reviews(item.get("totalReviews") or item.get("totJdReviews") or "")
                        docid = item.get("docid") or ""
                        weburl = item.get("weburl") or ""

                        detail_url = ""
                        if weburl:
                            detail_url = f"{self.BASE_URL}/{weburl.lstrip('/')}"
                        elif docid:
                            detail_url = f"{self.BASE_URL}/{city}/{name.replace(' ', '-')}/{docid}_BZDET"

                        # Build initial address fallback from available fields
                        addr_parts = [p for p in [area, city, pincode] if p]
                        address = ", ".join(addr_parts)

                        raw_record = {
                            "name": name.strip(),
                            "phone": phone,
                            "whatsapp": whatsapp,
                            "email": "",
                            "contact_person": "",
                            "full_address": address,
                            "area": str(area).strip(),
                            "city": str(city).strip(),
                            "pincode": str(pincode).strip(),
                            "rating": rating,
                            "total_reviews": reviews,
                            "website": "",
                            "justdial_url": detail_url,
                            "docid": str(docid).strip()
                        }
                        listings.append(sanitize_listing(raw_record))

                    if listings:
                        return listings
            except Exception as e:
                logger.error(f"Error parsing __NEXT_DATA__: {e}")

        # Method 2: DOM fallback parsing
        cards = soup.select(".resultbox, div[id*='bcard_'], [class*='cardWrapper']")
        for card in cards:
            title_el = card.select_one(".resultbox_title_anchor, h2 a, h2, a[class*='title']")
            name = title_el.get_text(strip=True) if title_el else ""
            if not name:
                continue

            href = title_el.get("href") if (title_el and title_el.name == "a") else ""
            if not href and title_el:
                a_tag = title_el.find("a")
                href = a_tag.get("href") if a_tag else ""

            detail_url = ""
            if href:
                detail_url = href if href.startswith("http") else f"{self.BASE_URL}/{href.lstrip('/')}"

            phone_el = card.select_one(".callcontent, .callbutton")
            phone = clean_phone(phone_el.get_text(strip=True) if phone_el else "")

            addr_el = card.select_one(".locatcity, .resultbox_address, address")
            address = clean_address(addr_el.get_text(strip=True) if addr_el else "")

            rating_el = card.select_one(".resultbox_countrate, [class*='star_rate']")
            rating = clean_rating(rating_el.get_text(strip=True) if rating_el else "")

            votes_el = card.select_one(".resultbox_totalrate, [class*='totalrate']")
            reviews = clean_reviews(votes_el.get_text(strip=True) if votes_el else "")

            raw_record = {
                "name": name,
                "phone": phone,
                "whatsapp": "",
                "email": "",
                "contact_person": "",
                "full_address": address,
                "area": "",
                "city": "",
                "pincode": "",
                "rating": rating,
                "total_reviews": reviews,
                "website": "",
                "justdial_url": detail_url,
                "docid": card.get("id") or ""
            }
            listings.append(sanitize_listing(raw_record))

        return listings

    def scrape_detail_page(self, detail_url: str) -> Dict[str, str]:
        """
        Fetch vendor detail page to extract Email ID, Contact Person,
        Full Address, and Website.
        """
        details = {
            "email": "",
            "contact_person": "",
            "full_address": "",
            "website": "",
            "pincode": ""
        }
        if not detail_url:
            return details

        html = self.fetch_html(detail_url, retries=2, backoff=0.5, timeout=8)
        if not html:
            return details

        soup = BeautifulSoup(html, "html.parser")

        # Try __NEXT_DATA__ on detail page
        nd_script = soup.find("script", id="__NEXT_DATA__")
        if nd_script and nd_script.string:
            try:
                data = json.loads(nd_script.string)
                props = data.get("props", {}).get("pageProps", {})
                res = props.get("results", {})
                inner = res.get("results", {}) if isinstance(res, dict) else {}

                if inner and isinstance(inner, dict):
                    email = clean_email(inner.get("email") or "")
                    contact = str(inner.get("contactperson") or "").strip()
                    addr = clean_address(inner.get("addressln") or inner.get("address") or "")
                    web = clean_website(inner.get("website") or "")
                    pincode = str(inner.get("pincode") or "").strip()

                    if email:
                        details["email"] = email
                    if contact:
                        details["contact_person"] = contact
                    if addr:
                        details["full_address"] = addr
                    if web:
                        details["website"] = web
                    if pincode:
                        details["pincode"] = pincode

                    return details
            except Exception as e:
                logger.debug(f"Detail __NEXT_DATA__ parse note: {e}")

        # DOM fallback for detail page
        mailto = soup.select_one("a[href^='mailto:']")
        if mailto:
            details["email"] = clean_email(mailto.get("href", "").replace("mailto:", "").split("?")[0].strip())

        if not details["email"]:
            found_emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', html)
            clean_em = clean_email(", ".join(found_emails))
            if clean_em:
                details["email"] = clean_em.split(", ")[0]

        full_addr = soup.select_one(".full_address, .addressTxt, address, .comp-text, [class*='address']")
        if full_addr and not details["full_address"]:
            details["full_address"] = clean_address(full_addr.get_text(strip=True))

        return details

    def harvest_docids_from_html(self, html: str) -> List[str]:
        """Extract list of docids from page __NEXT_DATA__ payload."""
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        nd_script = soup.find("script", id="__NEXT_DATA__")
        if not nd_script:
            return []
        try:
            data = json.loads(nd_script.string or "{}")
            ld = data.get("props", {}).get("pageProps", {}).get("listData", {})
            raw = ld.get("nextdocid", "")
            if raw:
                return [d.strip() for d in raw.split(",") if d.strip()]
        except Exception:
            pass
        return []

    def scrape_full_lead_by_docid(self, docid: str, city: str = "city") -> Optional[Dict[str, Any]]:
        """Directly fetch and parse complete vendor details using its unique docid."""
        clean_doc = docid.replace(".", "-")
        city_slug = city or "India"
        detail_url = f"{self.BASE_URL}/{city_slug}/Biz/{clean_doc}_BZDET"
        html = self.fetch_html(detail_url, retries=2, backoff=0.5)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        nd_script = soup.find("script", id="__NEXT_DATA__")
        if nd_script and nd_script.string:
            try:
                data = json.loads(nd_script.string)
                props = data.get("props", {}).get("pageProps", {})
                res = props.get("results", {})
                inner = res.get("results", {}) if isinstance(res, dict) else {}
                if inner and isinstance(inner, dict):
                    name = inner.get("name") or inner.get("compname") or ""
                    phone = clean_phone(inner.get("VNumber") or inner.get("mobile") or inner.get("contact") or "")
                    whatsapp = clean_whatsapp(inner.get("wpnumber") or "")
                    email = clean_email(inner.get("email") or "")
                    contact = str(inner.get("contactperson") or "").strip()
                    address = clean_address(inner.get("addressln") or inner.get("address") or "")
                    area = str(inner.get("area") or "").strip()
                    res_city = str(inner.get("city") or city or "").strip()
                    pincode = str(inner.get("pincode") or "").strip()
                    rating = clean_rating(inner.get("comprating") or inner.get("rating") or "")
                    reviews = clean_reviews(inner.get("totalReviews") or "")
                    website = clean_website(inner.get("website") or "")

                    if not name:
                        return None

                    if not address:
                        address = clean_address(", ".join([p for p in [area, res_city, pincode] if p]))

                    return sanitize_listing({
                        "name": name,
                        "phone": phone,
                        "whatsapp": whatsapp,
                        "email": email,
                        "contact_person": contact,
                        "full_address": address,
                        "area": area,
                        "city": res_city,
                        "pincode": pincode,
                        "rating": rating,
                        "total_reviews": reviews,
                        "website": website,
                        "justdial_url": detail_url,
                        "docid": docid
                    })
            except Exception as e:
                logger.debug(f"Docid detail parse exception for {docid}: {e}")

        return None

    def detect_total_listings(self, html: str) -> Optional[int]:
        """
        Auto-detect the total number of listings reported by Justdial
        (e.g., '745+ Listings' or JSON-LD numberOfItems: 745).
        """
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        
        # Method 1: Check JSON-LD schema
        for s_tag in soup.find_all("script", type="application/ld+json"):
            try:
                sd = json.loads(s_tag.string or "{}")
                if isinstance(sd, dict) and "numberOfItems" in sd:
                    val = str(sd["numberOfItems"]).replace("+", "").replace(",", "").strip()
                    if val.isdigit():
                        return int(val)
            except Exception:
                pass

        # Method 2: Check regex for "XYZ+ Listings"
        m = re.search(r'(\d[\d,]*)\+?\s*Listings', html, re.I)
        if m:
            num_str = m.group(1).replace(",", "")
            if num_str.isdigit():
                return int(num_str)

        return None

    def scrape(
        self,
        url: str,
        max_pages: Optional[int] = 1,
        scrape_all: bool = False,
        max_listings: Optional[int] = None,
        enrich_details: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> List[Dict[str, Any]]:
        """
        Scrape listings from Justdial across multiple pages with optional detail enrichment.
        Uses a robust 3-pillar extraction engine:
          1. Multi-page search scraping
          2. Direct Next.js docid harvesting (extracts up to 170 listings per page)
          3. City-wide locality expansion (for comprehensive 500-750+ metro scrapes)
        """
        clean_url = self._normalize_url(url)
        parsed_u = urlparse(clean_url)
        path_parts = [p for p in parsed_u.path.split('/') if p]
        city_name = path_parts[0] if path_parts else "India"

        all_listings = []
        seen_keys = set()
        all_harvested_docids = []
        detected_total = None

        seen_areas = set()
        areas_to_explore = []

        def queue_area(raw_area: str):
            if not raw_area:
                return
            cleaned = re.sub(r"[^\w\s-]", "", str(raw_area)).strip()
            if not cleaned:
                return
            slug = re.sub(r"\s+", "-", cleaned)
            if len(slug) >= 3 and slug.lower() not in seen_areas:
                seen_areas.add(slug.lower())
                areas_to_explore.append(slug)

            # Also queue main locality if area is composite (e.g. "Owaisipura Masab Tank" -> "Masab-Tank")
            words = cleaned.split()
            if len(words) >= 3:
                last_slug = "-".join(words[-2:])
                if len(last_slug) >= 3 and last_slug.lower() not in seen_areas:
                    seen_areas.add(last_slug.lower())
                    areas_to_explore.append(last_slug)

        # Determine target count and pages
        if scrape_all:
            target_pages = 100
            target_count = 1000
        elif max_listings:
            target_count = max_listings
            target_pages = max(1, (max_listings + 9) // 10)
        else:
            target_pages = max_pages or 1
            target_count = target_pages * 10

        def report(status: str, current_page: int, total_pages: int, count: int, extra: str = "", enrich_index: int = 0, enrich_total: int = 0):
            if progress_callback:
                phone_count = sum(1 for r in all_listings if r.get("phone"))
                email_count = sum(1 for r in all_listings if r.get("email"))
                progress_callback({
                    "status": status,
                    "current_page": current_page,
                    "total_pages": total_pages,
                    "count": count,
                    "phone_count": phone_count,
                    "email_count": email_count,
                    "enrich_index": enrich_index,
                    "enrich_total": enrich_total,
                    "detected_total": detected_total,
                    "message": extra
                })

        report("scraping", 1, target_pages, 0, "Connecting to Justdial...")

        # ----------------------------------------------------
        # Pillar 1: Search Page Pagination
        # ----------------------------------------------------
        consecutive_empty = 0

        for page in range(1, target_pages + 1):
            if len(all_listings) >= target_count:
                break

            page_url = self._build_page_url(clean_url, page)
            report("scraping", page, target_pages, len(all_listings), f"Scraping page {page} of {target_pages}...")

            html = self.fetch_html(page_url)
            if not html:
                logger.warning(f"Failed to fetch content for page {page}")
                report("warning", page, target_pages, len(all_listings), f"Page {page} could not be loaded, skipping...")
                consecutive_empty += 1
                if consecutive_empty >= 4:
                    break
                continue

            # On page 1, auto-detect total listings and update targets
            if page == 1:
                detected_total = self.detect_total_listings(html)
                if detected_total:
                    logger.info(f"Detected total category listings: {detected_total}")
                    if scrape_all:
                        target_count = detected_total
                        target_pages = max(1, (detected_total + 9) // 10)
                        report("scraping", 1, target_pages, 0, f"Detected {detected_total}+ total listings! Planning extraction...")

            # Harvest docids from Next.js payload
            page_docids = self.harvest_docids_from_html(html)
            for d in page_docids:
                if d not in all_harvested_docids:
                    all_harvested_docids.append(d)

            page_listings = self.parse_search_page(html)
            new_in_page = 0
            for item in page_listings:
                queue_area(item.get("area"))
                key = item.get("docid") or (item.get("name"), item.get("phone"))
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_listings.append(item)
                    new_in_page += 1

                if len(all_listings) >= target_count:
                    break

            if new_in_page == 0:
                consecutive_empty += 1
            else:
                consecutive_empty = 0

            report("scraping", page, target_pages, len(all_listings), f"Page {page}: found {new_in_page} new listings (Total: {len(all_listings)})")

            # Stop standard pagination if pages stop giving new listings; proceed to docids
            if consecutive_empty >= 2:
                logger.info(f"Page pagination saturated at page {page}. Transitioning to category docid registry...")
                break

            if page < target_pages:
                time.sleep(0.6)

        # ----------------------------------------------------
        # Pillar 2: Harvested Docids Extraction (Extracts from Next.js registry)
        # ----------------------------------------------------
        seen_docids = set(x.get("docid") for x in all_listings if x.get("docid"))
        remaining_docids = [d for d in all_harvested_docids if d not in seen_docids]

        if remaining_docids and len(all_listings) < target_count:
            needed = target_count - len(all_listings)
            to_fetch = remaining_docids[:needed]
            report("scraping", target_pages, target_pages, len(all_listings), f"Extracting {len(to_fetch)} direct leads from category registry...")

            for idx, docid in enumerate(to_fetch, 1):
                if len(all_listings) >= target_count:
                    break

                report("scraping", target_pages, target_pages, len(all_listings), f"Registry lead [{idx}/{len(to_fetch)}]: Fetching docid {docid[:15]}...")
                lead = self.scrape_full_lead_by_docid(docid, city_name)
                if lead:
                    queue_area(lead.get("area"))
                    key = lead.get("docid") or (lead.get("name"), lead.get("phone"))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_listings.append(lead)
                        report("scraping", target_pages, target_pages, len(all_listings), f"Extracted [{len(all_listings)}/{target_count}] {lead['name'][:30]}")

                time.sleep(0.3)

        # ----------------------------------------------------
        # Pillar 3: Dynamic Locality Expansion (Automatic, zero hardcoding)
        # ----------------------------------------------------
        if (scrape_all or len(all_listings) < target_count) and len(path_parts) >= 2 and "-in-" not in path_parts[1]:
            city_slug = path_parts[0]
            cat_slug = path_parts[1]
            nct_slug = path_parts[2] if len(path_parts) > 2 else ""

            report("scraping", target_pages, target_pages, len(all_listings), f"Dynamically expanding category search across discovered localities in {city_slug}...")

            while areas_to_explore and len(all_listings) < target_count:
                area_slug = areas_to_explore.pop(0)
                area_url = (
                    f"{self.BASE_URL}/{city_slug}/{cat_slug}-in-{area_slug}/{nct_slug}"
                    if nct_slug
                    else f"{self.BASE_URL}/{city_slug}/{cat_slug}-in-{area_slug}"
                )
                report("scraping", target_pages, target_pages, len(all_listings), f"Scanning {area_slug.replace('-', ' ')} ({len(all_listings)} unique listings so far)...")

                area_html = self.fetch_html(area_url)
                if not area_html:
                    continue

                # Parse area search page
                area_items = self.parse_search_page(area_html)
                new_area_count = 0
                for item in area_items:
                    queue_area(item.get("area"))
                    k = item.get("docid") or (item.get("name"), item.get("phone"))
                    if k not in seen_keys:
                        seen_keys.add(k)
                        all_listings.append(item)
                        new_area_count += 1
                        if len(all_listings) >= target_count:
                            break

                # Also harvest docids from this area page
                area_docids = self.harvest_docids_from_html(area_html)
                for ad in area_docids:
                    if len(all_listings) >= target_count:
                        break
                    ad_clean = ad.replace(".", "-")
                    if ad not in seen_keys and ad not in [x.get("docid") for x in all_listings]:
                        a_lead = self.scrape_full_lead_by_docid(ad, city_slug)
                        if a_lead:
                            queue_area(a_lead.get("area"))
                            ak = a_lead.get("docid") or (a_lead.get("name"), a_lead.get("phone"))
                            if ak not in seen_keys:
                                seen_keys.add(ak)
                                all_listings.append(a_lead)
                                new_area_count += 1

                if new_area_count > 0:
                    report("scraping", target_pages, target_pages, len(all_listings), f"Area {area_slug.replace('-', ' ')}: added {new_area_count} new leads (Total: {len(all_listings)})")

                time.sleep(0.3)

        # ----------------------------------------------------
        # Optional Detail Enrichment (for any leads that lack email)
        # ----------------------------------------------------
        if enrich_details and all_listings:
            need_enrich = [item for item in all_listings if not item.get("email") and item.get("justdial_url")]
            if need_enrich:
                total_items = len(need_enrich)
                report("enriching", target_pages, target_pages, len(all_listings), f"Enriching {total_items} listings with profile details...", enrich_index=0, enrich_total=total_items)

                for idx, item in enumerate(need_enrich, 1):
                    detail_url = item.get("justdial_url")
                    if detail_url:
                        report("enriching", target_pages, target_pages, len(all_listings), f"Enriching [{idx}/{total_items}] {item.get('name', '')[:30]}...", enrich_index=idx, enrich_total=total_items)
                        try:
                            det = self.scrape_detail_page(detail_url)
                            if det.get("email"):
                                item["email"] = det["email"]
                            if det.get("contact_person"):
                                item["contact_person"] = det["contact_person"]
                            if det.get("full_address"):
                                # Detail page street address always supersedes synthetic search address
                                item["full_address"] = det["full_address"]
                            if det.get("website"):
                                item["website"] = det["website"]
                            if det.get("pincode"):
                                item["pincode"] = det["pincode"]
                        except Exception as e:
                            logger.error(f"Error enriching {detail_url}: {e}")

                        report("enriching", target_pages, target_pages, len(all_listings), f"Enriched [{idx}/{total_items}] {item.get('name', '')[:30]}", enrich_index=idx, enrich_total=total_items)

                    time.sleep(0.3)

        # Final sanitization pass over all collected listings
        sanitized_all = [sanitize_listing(item) for item in all_listings]
        report("completed", target_pages, target_pages, len(sanitized_all), f"Successfully extracted {len(sanitized_all)} listings!")
        return sanitized_all
