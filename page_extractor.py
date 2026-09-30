import re
import json
import logging
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse
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

class PageExtractor:
    """
    Universal multi-strategy business extractor that extracts structured business
    information (Name, Phone, WhatsApp, Email, Address, Rating, Website)
    from any web page in top search results.
    """

    GENERIC_WORDS = {
        "menu", "navigation", "nav", "about us", "contact us", "privacy policy",
        "terms of service", "terms & conditions", "disclaimer", "leave a reply",
        "leave a comment", "comments", "related posts", "recent posts", "popular posts",
        "categories", "archives", "sign in", "login", "register", "cart", "checkout",
        "advertisement", "sponsored", "table of contents", "subscribe", "newsletter",
        "share this", "follow us", "cookie policy", "all rights reserved", "copyright",
        "faqs", "faq", "frequently asked questions", "q&a", "questions & answers",
        "reviews", "customer reviews", "recent enquiries", "related searches",
        "people also ask", "shopping list", "find nearest", "find nearest shops"
    }

    BUSINESS_TYPES = {
        "localbusiness", "store", "organization", "place", "service", "restaurant",
        "hospital", "medicalclinic", "dentist", "lodging", "hostel", "hotel", "corporation",
        "financialservice", "professionalservice", "automotivebusiness", "childcare"
    }

    @staticmethod
    def _is_generic_title(title: str, domain: str = "", search_query: str = "") -> bool:
        t = title.strip().lower()
        if len(t) < 3 or len(t) > 90:
            return True
        if any(g in t for g in PageExtractor.GENERIC_WORDS):
            return True
        if re.search(r"\b(near you|near me|in your area|in your city|shopping list|recent enquiries|customer reviews)\b", t, re.I):
            return True
        if re.match(r"^(top|best|\d+|\bthe\b|\bfind\b|\blist of\b|\bfaqs?\b)\s*$", t):
            return True

        # Dynamic check: if title is identical to or derives directly from the search query
        if search_query:
            sq_clean = re.sub(r"\b(list|top|best|\d+|reviews?|directory|near me|in|around|at)\b", "", search_query.lower()).strip()
            sq_words = [w for w in re.findall(r"\w+", sq_clean) if len(w) > 2]
            t_words = [w for w in re.findall(r"\w+", t) if len(w) > 2]
            if sq_words and all(w in t for w in sq_words) and len(t_words) <= len(sq_words) + 3:
                return True

        # Dynamic listicle / category filter: e.g. "Top 10 ... in [location]" or "Best ... in [location]"
        if re.search(r"\b(in|near|around|at)\s+[a-zA-Z\s]+$", t):
            if re.match(r"^(top|best|\d+|list\s+of|find)\b", t):
                return True

        # If title matches the website domain name (e.g. 5bestincity, threebestrated)
        if domain:
            dom_clean = re.sub(r"^(www\.|ind\.|blog\.)", "", domain).split(".")[0].lower()
            if dom_clean and (dom_clean in t or t in dom_clean):
                return True
        return False

    @staticmethod
    def _clean_business_name(raw_name: str) -> str:
        s = re.sub(r"^\s*#?\d+[\.\)\-:]\s*", "", raw_name)
        s = re.sub(r"\s+", " ", s).strip()
        s = re.sub(r"\s*\(\s*\d+(\.\d+)?\s*(stars?|ratings?|reviews?|\/5)\s*\)", "", s, flags=re.I)
        return s.strip()

    @staticmethod
    def _extract_city_from_query(query: str) -> str:
        """Heuristic to detect target city from search query if present."""
        if not query:
            return ""
        m = re.search(r"\b(?:in|at|near|around)\s+([a-zA-Z\s]{3,20})\b", query, re.I)
        if m:
            city_candidate = m.group(1).strip()
            city_candidate = re.sub(r"\b(list|top|best|reviews?|services?)\b", "", city_candidate, flags=re.I).strip()
            if len(city_candidate) >= 3:
                return city_candidate.title()
        return ""

    @staticmethod
    def _extract_phone_from_text(text: str) -> str:
        """Extracts first valid phone number from text supporting Indian and International formats."""
        if not text:
            return ""
        # 1. Indian Mobile (91 + 10 digits starting 6-9)
        m = re.search(r"(?:\+?91[\-\s]?)?[6-9]\d{9}\b", text)
        if m:
            return clean_phone(m.group(0))
        # 2. International E.164 (+country_code ...)
        m = re.search(r"\+\d{1,4}[-.\s]?(?:\(?\d{1,4}\)?[-.\s]?)?\d{3,5}[-.\s]?\d{3,5}\b", text)
        if m:
            return clean_phone(m.group(0))
        # 3. North American format (NANP: +1 (xxx) xxx-xxxx)
        m = re.search(r"\b(?:\+?1[-.\s]?)?\(?[2-9]\d{2}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", text)
        if m:
            return clean_phone(m.group(0))
        # 4. Indian Landline (STD code + 6-8 digits)
        m = re.search(r"\b0\d{2,4}[-\s]?\d{6,8}\b", text)
        if m:
            return clean_phone(m.group(0))
        return ""

    @staticmethod
    def _extract_postal_from_text(text: str) -> str:
        """Extracts postal code (Indian 6-digit or US 5-digit) from text."""
        if not text:
            return ""
        # Indian 6-digit PIN
        m_pin = re.search(r"\b([1-9]\d{5})\b", text)
        if m_pin:
            return m_pin.group(1)
        # US 5-digit ZIP
        m_zip = re.search(r"\b([0-9]{5}(?:-[0-9]{4})?)\b", text)
        if m_zip:
            return m_zip.group(1)
        return ""

    def extract_from_json_ld(self, soup: BeautifulSoup, page_url: str, search_query: str = "") -> List[Dict[str, Any]]:
        """Extracts structured businesses from Schema.org JSON-LD scripts."""
        listings = []
        seen_names = set()
        domain = urlparse(page_url).netloc.lower()

        for s in soup.find_all("script", type="application/ld+json"):
            if not s.string:
                continue
            try:
                data = json.loads(s.string)
            except Exception:
                continue

            # Recursive unpacker for ItemList, @graph, or arrays
            def unpack_item(item: Any, out_list: List[Dict[str, Any]]):
                if not isinstance(item, dict):
                    return
                if item.get("@type") == "ItemList" or "itemListElement" in item:
                    for el in item.get("itemListElement", []):
                        if isinstance(el, dict):
                            sub = el.get("item", el)
                            unpack_item(sub, out_list)
                elif "@graph" in item and isinstance(item["@graph"], list):
                    for g in item["@graph"]:
                        unpack_item(g, out_list)
                else:
                    out_list.append(item)

            candidates = []
            if isinstance(data, list):
                for d in data:
                    unpack_item(d, candidates)
            elif isinstance(data, dict):
                unpack_item(data, candidates)

            for it in candidates:
                if not isinstance(it, dict):
                    continue
                type_str = str(it.get("@type", "")).lower()
                if any(b in type_str for b in self.BUSINESS_TYPES):
                    raw_name = str(it.get("name") or "").strip()
                    name = self._clean_business_name(raw_name)
                    if self._is_generic_title(name, domain=domain, search_query=search_query) or name.lower() in seen_names:
                        continue

                    phone = clean_phone(it.get("telephone") or it.get("phone") or "")
                    email = clean_email(it.get("email") or "")
                    
                    addr_obj = it.get("address")
                    full_addr = ""
                    city = ""
                    pincode = ""
                    area = ""

                    if isinstance(addr_obj, dict):
                        street = str(addr_obj.get("streetAddress") or "")
                        city = str(addr_obj.get("addressLocality") or "")
                        region = str(addr_obj.get("addressRegion") or "")
                        pincode = str(addr_obj.get("postalCode") or "")
                        parts = [street, city, region, pincode]
                        full_addr = clean_address(", ".join([p for p in parts if p]))
                        area = clean_area(street, city, pincode)
                    elif isinstance(addr_obj, str):
                        full_addr = clean_address(addr_obj)
                        m_pin = re.search(r"\b([1-9]\d{5})\b", full_addr)
                        if m_pin:
                            pincode = m_pin.group(1)

                    rating = ""
                    reviews = ""
                    agg = it.get("aggregateRating")
                    if isinstance(agg, dict):
                        rating = clean_rating(agg.get("ratingValue") or "")
                        reviews = clean_reviews(agg.get("reviewCount") or agg.get("ratingCount") or "")

                    web = clean_website(it.get("url") or it.get("website") or page_url)

                    # Only accept real businesses with contact info OR address
                    if phone or email or (full_addr and len(full_addr) > 5):
                        seen_names.add(name.lower())
                        listings.append(sanitize_listing({
                            "name": name,
                            "phone": phone,
                            "whatsapp": "",
                            "email": email,
                            "contact_person": "",
                            "full_address": full_addr,
                            "area": area,
                            "city": city,
                            "pincode": pincode,
                            "rating": rating,
                            "total_reviews": reviews,
                            "website": web,
                            "justdial_url": page_url,
                            "docid": ""
                        }))

        return listings

    def extract_from_dom_cards(self, soup: BeautifulSoup, page_url: str, default_city: str = "", search_query: str = "") -> List[Dict[str, Any]]:
        """Extracts listing cards from HTML using DOM structures and pattern recognition."""
        listings = []
        seen_names = set()
        domain = urlparse(page_url).netloc.lower()

        selectors = [
            ".listing-item", ".listing-card", ".business-card", ".vendor-card",
            ".store-item", ".search-result-item", ".place-item", "article.listing",
            "div[class*='listing']", "div[class*='vendor']", "div[class*='business']",
            "div[class*='result']", "div[class*='card']"
        ]
        
        cards = soup.select(", ".join(selectors))
        if not cards:
            cards = soup.find_all("article")

        for card in cards:
            title_el = card.find(["h2", "h3", "h4", "a"])
            if not title_el:
                continue
            raw_title = title_el.get_text(strip=True)
            name = self._clean_business_name(raw_title)
            if self._is_generic_title(name, domain=domain, search_query=search_query) or name.lower() in seen_names:
                continue

            card_text = card.get_text(" ", strip=True)

            phone = ""
            tel_link = card.select_one("a[href^='tel:']")
            if tel_link:
                phone = clean_phone(tel_link.get("href", "").replace("tel:", ""))
            if not phone:
                phone = self._extract_phone_from_text(card_text)

            whatsapp = ""
            wa_link = card.select_one("a[href*='wa.me'], a[href*='whatsapp.com']")
            if wa_link:
                whatsapp = clean_whatsapp(wa_link.get("href"))

            email = ""
            mailto_link = card.select_one("a[href^='mailto:']")
            if mailto_link:
                email = clean_email(mailto_link.get("href", "").replace("mailto:", "").split("?")[0])
            if not email:
                emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", card_text)
                if emails:
                    email = clean_email(emails[0])

            full_addr = ""
            addr_el = card.select_one("address, .address, .location, [class*='addr'], [class*='location']")
            if addr_el:
                full_addr = clean_address(addr_el.get_text(" ", strip=True))
            
            pincode = self._extract_postal_from_text(full_addr or card_text)

            rating = ""
            rate_m = re.search(r"(\d\.\d)\s*(?:★|stars?|ratings?|reviews?|\/5)", card_text, re.I)
            if rate_m:
                rating = clean_rating(rate_m.group(1))

            reviews = ""
            rev_m = re.search(r"(\d[\d,]*)\s*(?:ratings?|reviews?|votes?)", card_text, re.I)
            if rev_m:
                reviews = clean_reviews(rev_m.group(1))

            web = ""
            web_link = card.select_one("a[href^='http']:not([href*='" + domain + "'])")
            if web_link:
                web = clean_website(web_link.get("href"))
            if not web:
                web = page_url

            if phone or email or (full_addr and len(full_addr) > 12):
                seen_names.add(name.lower())
                listings.append(sanitize_listing({
                    "name": name,
                    "phone": phone,
                    "whatsapp": whatsapp,
                    "email": email,
                    "contact_person": "",
                    "full_address": full_addr,
                    "area": "",
                    "city": default_city,
                    "pincode": pincode,
                    "rating": rating,
                    "total_reviews": reviews,
                    "website": web,
                    "justdial_url": page_url,
                    "docid": ""
                }))

        return listings

    def extract_from_heading_sections(self, soup: BeautifulSoup, page_url: str, default_city: str = "", search_query: str = "") -> List[Dict[str, Any]]:
        """Extracts listings from curated blog lists or articles where each business is an H2, H3, H4 or numbered heading."""
        listings = []
        seen_names = set()
        domain = urlparse(page_url).netloc.lower()

        # Target h2, h3, h4 headings
        candidates = list(soup.find_all(["h2", "h3", "h4"]))
        # Also include numbered listicle tags like "1. Business Name" inside strong or b
        for b_tag in soup.find_all(["strong", "b"]):
            btxt = b_tag.get_text(strip=True)
            if re.match(r"^\d{1,2}[\.\)\-]\s+[A-Za-z]", btxt) and len(btxt) < 60:
                if b_tag.parent.name not in ["h2", "h3", "h4"]:
                    candidates.append(b_tag)

        for h in candidates:
            raw_title = h.get_text(strip=True)
            name = self._clean_business_name(raw_title)
            if self._is_generic_title(name, domain=domain, search_query=search_query) or name.lower() in seen_names:
                continue

            # Check if heading is wrapped in a card or column container
            parent_card = h.find_parent(["div", "article", "section", "li"])
            card_text = ""
            if parent_card and len(parent_card.find_all(["h2", "h3", "h4"])) <= 2:
                card_text = parent_card.get_text(" ", strip=True)

            section_nodes = []
            curr = h.next_sibling
            while curr and getattr(curr, "name", None) not in ["h1", "h2", "h3", "h4"]:
                if hasattr(curr, "get_text"):
                    section_nodes.append(curr)
                curr = curr.next_sibling

            section_text = " ".join([n.get_text(" ", strip=True) for n in section_nodes])
            combined_text = f"{card_text} {section_text}".strip() or section_text

            phone = self._extract_phone_from_text(combined_text)

            emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", combined_text)
            email = clean_email(emails[0]) if emails else ""

            # Address extraction: check label first, then check paragraphs for address indicators
            full_addr = ""
            addr_m = re.search(r"(?:Address|Location|Locality|Add)[\s:]+([^\n\r\|\.]{10,140})", combined_text, re.I)
            if addr_m:
                full_addr = clean_address(addr_m.group(1))
            else:
                # Inspect sibling or parent paragraphs
                check_nodes = []
                if parent_card:
                    check_nodes.extend(parent_card.find_all(["p", "div", "address"]))
                check_nodes.extend(section_nodes)

                for p_node in check_nodes:
                    ptxt = p_node.get_text(" ", strip=True) if hasattr(p_node, "get_text") else ""
                    if len(ptxt) >= 12 and len(ptxt) <= 180:
                        has_pin = bool(re.search(r"\b[1-9]\d{5}\b", ptxt))
                        has_city = bool(default_city and default_city.lower() in ptxt.lower())
                        has_loc = bool(re.search(r"\b(Road|Rd|Street|Nagar|Colony|Floor|Shop|Plot|Opp|Near|Lane|Building|Complex|Centre|Center)\b", ptxt, re.I))
                        if (has_pin or has_city or has_loc) and not any(g in ptxt.lower() for g in ["copyright", "privacy", "cookie", "all rights reserved"]):
                            full_addr = clean_address(ptxt)
                            break

            pincode = self._extract_postal_from_text(full_addr or combined_text)

            rating = ""
            rate_m = re.search(r"(\d\.\d)\s*(?:★|stars?|ratings?|reviews?|\/5)", combined_text, re.I)
            if rate_m:
                rating = clean_rating(rate_m.group(1))

            reviews = ""
            rev_m = re.search(r"\((\d{1,5})\)\s*(?:Votes|Reviews|ratings|votes)", combined_text, re.I)
            if rev_m:
                reviews = rev_m.group(1)

            web = page_url
            for node in section_nodes + ([parent_card] if parent_card else []):
                if hasattr(node, "find_all"):
                    link = node.find("a", href=re.compile(r"^https?://"))
                    if link and domain not in link.get("href", ""):
                        web = clean_website(link.get("href"))
                        break

            # Accept if business has contact or valid address or rating
            if phone or email or (full_addr and len(full_addr) > 8) or rating:
                seen_names.add(name.lower())
                listings.append(sanitize_listing({
                    "name": name,
                    "phone": phone,
                    "whatsapp": "",
                    "email": email,
                    "contact_person": "",
                    "full_address": full_addr,
                    "area": "",
                    "city": default_city,
                    "pincode": pincode,
                    "rating": rating,
                    "total_reviews": reviews,
                    "website": web,
                    "justdial_url": page_url,
                    "docid": ""
                }))

        return listings

    def extract_single_business_homepage(self, soup: BeautifulSoup, page_url: str, default_city: str = "", search_query: str = "") -> List[Dict[str, Any]]:
        """Extracts business details if the visited website is a standalone company homepage."""
        domain = urlparse(page_url).netloc.lower()
        title_el = soup.find("title")
        raw_title = title_el.get_text(strip=True) if title_el else ""
        name = re.split(r"[-|:•—]", raw_title)[0].strip()
        if self._is_generic_title(name, domain=domain, search_query=search_query):
            h1 = soup.find("h1")
            name = self._clean_business_name(h1.get_text(strip=True)) if h1 else ""

        if self._is_generic_title(name, domain=domain, search_query=search_query):
            return []

        full_text = soup.get_text(" ", strip=True)

        phone = self._extract_phone_from_text(full_text)

        emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", full_text)
        email = clean_email(emails[0]) if emails else ""

        addr_el = soup.select_one("footer address, address, footer .address, footer [class*='address']")
        full_addr = clean_address(addr_el.get_text(" ", strip=True)) if addr_el else ""

        pincode = self._extract_postal_from_text(full_addr or full_text)

        if phone or email or full_addr:
            return [sanitize_listing({
                "name": name,
                "phone": phone,
                "whatsapp": "",
                "email": email,
                "contact_person": "",
                "full_address": full_addr,
                "area": "",
                "city": default_city,
                "pincode": pincode,
                "rating": "",
                "total_reviews": "",
                "website": clean_website(page_url),
                "justdial_url": page_url,
                "docid": ""
            })]

        return []

    def extract_businesses(self, page_url: str, html: str, search_query: str = "") -> List[Dict[str, Any]]:
        """
        Executes multi-strategy extraction:
        1. Justdial engine (if justdial.com encountered in top results, deep scrape with enrichment)
        2. Combines Schema.org JSON-LD + DOM Listing Cards + Heading Listicles
        3. Standalone Homepage fallback
        """
        if not html or len(html) < 200:
            return []

        default_city = self._extract_city_from_query(search_query)

        # Strategy 1: If Justdial URL encountered in organic search results, deep-scrape up to 8 pages with enrichment!
        if "justdial.com" in page_url:
            from scraper import JustdialScraper
            jd = JustdialScraper()
            try:
                jd_results = jd.scrape(page_url, max_pages=6, scrape_all=False, enrich_details=False)
                if jd_results:
                    return jd_results
            except Exception as jd_err:
                logger.warning(f"Justdial deep scrape error, falling back to static parser: {jd_err}")

            jd_results = jd.parse_search_page(html)
            if jd_results:
                return jd_results

        soup = BeautifulSoup(html, "html.parser")

        # Collect and combine listings across all strategies (JSON-LD, DOM cards, Heading listicles)
        all_candidates: List[Dict[str, Any]] = []

        # Strategy 2: JSON-LD
        json_results = self.extract_from_json_ld(soup, page_url, search_query=search_query)
        all_candidates.extend(json_results)

        # Strategy 3: DOM Cards
        dom_results = self.extract_from_dom_cards(soup, page_url, default_city=default_city, search_query=search_query)
        all_candidates.extend(dom_results)

        # Strategy 4: Heading Sections & Listicles
        section_results = self.extract_from_heading_sections(soup, page_url, default_city=default_city, search_query=search_query)
        all_candidates.extend(section_results)

        # Deduplicate combined listings by business name
        if all_candidates:
            unique: List[Dict[str, Any]] = []
            seen: set = set()
            for item in all_candidates:
                k = item.get("name", "").strip().lower()
                if k and k not in seen:
                    seen.add(k)
                    unique.append(item)
            if unique:
                return unique

        # Strategy 5: Single Business Website fallback
        return self.extract_single_business_homepage(soup, page_url, default_city=default_city, search_query=search_query)
