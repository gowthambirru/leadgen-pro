import time
import logging
import concurrent.futures
import threading
import ipaddress
import socket
from typing import List, Dict, Any, Optional, Callable
from urllib.parse import urlparse
from curl_cffi import requests

from web_searcher import WebSearcher
from page_extractor import PageExtractor
from cleaner import sanitize_listing
from scraper import JustdialScraper
from config import settings

logger = logging.getLogger(__name__)

class ManualSearchScraper:
    """
    Coordinates manual web search: takes any user search query,
    retrieves the top N websites via search engines, visits each website,
    extracts business leads, deduplicates them, and returns clean results.
    """

    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1"
    }

    def __init__(self, impersonate: str = "chrome120"):
        self.impersonate = impersonate
        self.session = requests.Session(impersonate=impersonate)
        self.searcher = WebSearcher(impersonate=impersonate)
        self.extractor = PageExtractor()
        # jd_scraper is instantiated lazily when needed

    def _is_safe_url(self, url: str) -> bool:
        """Protects against SSRF by checking URL scheme and resolved IP."""
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False
            
            hostname = parsed.hostname
            if not hostname:
                return False
                
            if hostname.lower() == "localhost":
                return False
                
            # Resolve hostname to IP
            addr_info = socket.getaddrinfo(hostname, None)
            for info in addr_info:
                ip_str = info[4][0]
                ip_obj = ipaddress.ip_address(ip_str)
                
                # Check for private or loopback or link-local
                if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                    return False
                    
                # Specific check for AWS metadata endpoint just in case
                if ip_str == "169.254.169.254":
                    return False
            
            return True
        except Exception as e:
            logger.debug(f"SSRF check failed for {url}: {e}")
            return False

    def fetch_page_html(self, url: str, timeout: int = 8) -> Optional[str]:
        """Fetches page HTML with realistic browser headers and retry logic."""
        if not self._is_safe_url(url):
            logger.warning(f"URL {url} failed SSRF safety check.")
            return None

        if "justdial.com" in url:
            if not hasattr(self, "jd_scraper"):
                self.jd_scraper = JustdialScraper(impersonate=self.impersonate)
            return self.jd_scraper.fetch_html(url)

        headers = dict(self.DEFAULT_HEADERS)
        parsed = urlparse(url)
        headers["Host"] = parsed.netloc
        headers["Referer"] = "https://www.google.com/"

        for attempt in range(1, 3):
            try:
                resp = self.session.get(url, headers=headers, timeout=timeout)
                if resp.status_code == 200 and len(resp.text) > 200:
                    return resp.text
                elif resp.status_code == 403 or resp.status_code == 429:
                    time.sleep(1.0)
            except Exception as e:
                logger.debug(f"Attempt {attempt} failed for {url}: {e}")
                time.sleep(1.0)

        return None

    def scrape_query(
        self,
        query: str,
        num_websites: int = 10,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> List[Dict[str, Any]]:
        """
        Executes search for query, visits top websites, and extracts all business leads.
        """
        all_listings: List[Dict[str, Any]] = []
        seen_keys = set()
        data_lock = threading.Lock()

        def report(status: str, current_page: int, total_pages: int, count: int, extra: str = ""):
            if progress_callback:
                with data_lock:
                    phone_count = sum(1 for r in all_listings if r.get("phone"))
                    email_count = sum(1 for r in all_listings if r.get("email"))
                progress_callback({
                    "status": status,
                    "current_page": current_page,
                    "total_pages": total_pages,
                    "count": count,
                    "phone_count": phone_count,
                    "email_count": email_count,
                    "detected_total": None,
                    "message": extra
                })

        report("scraping", 0, num_websites, 0, f"Searching web for top websites: '{query}'...")

        # Step 1: Discover top N websites from search engines
        top_sites = self.searcher.search_top_websites(query, num_results=num_websites)
        if not top_sites:
            report("completed", 0, 0, 0, f"No websites found for query: '{query}'")
            return []

        total_sites = len(top_sites)
        report("scraping", 0, total_sites, 0, f"Found {total_sites} top websites. Starting data extraction...")

        # Step 2: Visit each website and extract businesses
        def process_site(idx: int, site: Dict[str, Any]) -> None:
            site_url = site["url"]
            domain = site.get("domain", urlparse(site_url).netloc)
            title = site.get("title", domain)

            with data_lock:
                current_len = len(all_listings)
            
            report(
                "scraping",
                idx,
                total_sites,
                current_len,
                f"[{idx}/{total_sites}] Crawling {domain} ({title[:35]})..."
            )

            try:
                html = self.fetch_page_html(site_url)
                if not html:
                    logger.warning(f"Could not load HTML from {site_url}")
                    with data_lock:
                        current_len = len(all_listings)
                    report(
                        "scraping",
                        idx,
                        total_sites,
                        current_len,
                        f"[{idx}/{total_sites}] Could not load {domain}, moving to next site..."
                    )
                    return

                extracted = self.extractor.extract_businesses(site_url, html, search_query=query)
                new_from_site = 0

                with data_lock:
                    for item in extracted:
                        sanitized = sanitize_listing(item)
                        name_norm = sanitized["name"].strip().lower()
                        phone_norm = sanitized["phone"].strip()
                        city_norm = sanitized["city"].strip().lower()

                        key = (name_norm, phone_norm) if phone_norm else (name_norm, city_norm)
                        if key not in seen_keys:
                            seen_keys.add(key)
                            all_listings.append(sanitized)
                            new_from_site += 1
                    
                    current_len = len(all_listings)

                report(
                    "scraping",
                    idx,
                    total_sites,
                    current_len,
                    f"[{idx}/{total_sites}] Extracted {new_from_site} leads from {domain} (Total: {current_len})"
                )
            except Exception as site_err:
                logger.error(f"Error extracting from {domain} ({site_url}): {site_err}", exc_info=True)
                with data_lock:
                    current_len = len(all_listings)
                report(
                    "scraping",
                    idx,
                    total_sites,
                    current_len,
                    f"[{idx}/{total_sites}] Notice: Error reading {domain} ({str(site_err)[:35]}), continuing with remaining sites..."
                )

        max_workers = getattr(settings, "MAX_SCRAPE_WORKERS", 4)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(process_site, idx, site)
                for idx, site in enumerate(top_sites, start=1)
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Background thread error: {e}", exc_info=True)

        # Final sanitization pass
        with data_lock:
            sanitized_final = [sanitize_listing(x) for x in all_listings]
            
        report(
            "completed",
            total_sites,
            total_sites,
            len(sanitized_final),
            f"Successfully scraped {len(sanitized_final)} leads from top {total_sites} websites!"
        )

        return sanitized_final
