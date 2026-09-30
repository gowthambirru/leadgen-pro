import urllib.parse
import re
import logging
import base64
from typing import List, Dict, Any
from urllib.parse import urlparse
from curl_cffi import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

class WebSearcher:
    """
    Fetches the top organic search result websites for any search query
    using a multi-engine pipeline:
    1. DuckDuckGo Lite (native Chrome 120 TLS & headers)
    2. DuckDuckGo HTML
    3. Bing Organic Search fallback
    """

    BLOCKED_DOMAINS = {
        "duckduckgo.com", "google.com", "google.co.in", "bing.com",
        "microsoft.com", "yahoo.com", "yandex.com", "baidu.com",
        "facebook.com", "instagram.com", "twitter.com", "x.com",
        "youtube.com", "linkedin.com", "pinterest.com", "reddit.com",
        "quora.com", "wikipedia.org", "play.google.com", "apps.apple.com"
    }

    def __init__(self, impersonate: str = "chrome120"):
        self.impersonate = impersonate
        self.session = requests.Session(impersonate=impersonate)

    def _is_valid_result_url(self, url: str) -> bool:
        if not url or not url.startswith("http"):
            return False
        try:
            parsed = urlparse(url)
            netloc = parsed.netloc.lower()
            netloc = re.sub(r":\d+$", "", netloc)
            domain = re.sub(r"^www\.", "", netloc)
            if any(domain == b or domain.endswith("." + b) for b in self.BLOCKED_DOMAINS):
                return False
            if domain.endswith(".gov") or domain.endswith(".gov.in") or domain.endswith(".nic.in") or domain.endswith(".edu"):
                return False
            if re.search(r"\.(pdf|jpg|jpeg|png|gif|svg|mp4|zip)$", parsed.path, re.I):
                return False
            return True
        except Exception:
            return False

    def search_duckduckgo_lite(self, query: str, num_results: int = 15) -> List[Dict[str, str]]:
        """Primary search method using DuckDuckGo Lite with native Chrome impersonation."""
        results = []
        seen_domains = set()
        url = "https://lite.duckduckgo.com/lite/"

        try:
            # Note: Do not pass custom conflicting headers; let curl_cffi send native Chrome 120 headers
            resp = self.session.post(
                url,
                data={"q": query},
                timeout=12
            )
            if resp.status_code in (200, 202) and len(resp.text) > 1000:
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.find_all("a", class_="result-link"):
                    href = a.get("href", "")
                    if "uddg=" in href:
                        try:
                            href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
                        except Exception:
                            pass

                    if not self._is_valid_result_url(href):
                        continue

                    title = a.get_text(strip=True)
                    netloc = urlparse(href).netloc.lower()
                    if netloc not in seen_domains:
                        seen_domains.add(netloc)
                        results.append({
                            "title": title,
                            "url": href,
                            "snippet": "",
                            "domain": netloc
                        })
                        if len(results) >= num_results:
                            break
        except Exception as e:
            logger.warning(f"DuckDuckGo Lite search error: {e}")

        return results

    def search_duckduckgo_html(self, query: str, num_results: int = 15) -> List[Dict[str, str]]:
        """Secondary search method using DuckDuckGo HTML."""
        results = []
        seen_domains = set()
        url = "https://html.duckduckgo.com/html/"

        try:
            resp = self.session.post(
                url,
                data={"q": query},
                timeout=12
            )
            if resp.status_code in (200, 202) and len(resp.text) > 1000:
                soup = BeautifulSoup(resp.text, "html.parser")
                for item in soup.select(".result"):
                    title_a = item.select_one(".result__title a, a.result__url, a")
                    if not title_a:
                        continue

                    href = title_a.get("href", "")
                    if "uddg=" in href:
                        try:
                            href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
                        except Exception:
                            pass

                    if not self._is_valid_result_url(href):
                        continue

                    title = title_a.get_text(strip=True)
                    snippet_el = item.select_one(".result__snippet")
                    snippet = snippet_el.get_text(strip=True) if snippet_el else ""

                    netloc = urlparse(href).netloc.lower()
                    if netloc not in seen_domains:
                        seen_domains.add(netloc)
                        results.append({
                            "title": title,
                            "url": href,
                            "snippet": snippet,
                            "domain": netloc
                        })
                        if len(results) >= num_results:
                            break
        except Exception as e:
            logger.warning(f"DuckDuckGo HTML search error: {e}")

        return results

    def search_bing(self, query: str, num_results: int = 15) -> List[Dict[str, str]]:
        """Fallback and complementary search engine: Bing Organic Search."""
        results = []
        seen_domains = set()
        encoded_query = urllib.parse.quote_plus(query)
        url = f"https://www.bing.com/search?q={encoded_query}&count=20"

        try:
            resp = self.session.get(url, timeout=12)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for li in soup.select("li.b_algo"):
                    a = li.select_one("h2 a")
                    if not a:
                        continue

                    href = a.get("href", "")
                    # Decode Bing redirection URLs
                    if "/ck/a?!" in href and "&u=" in href:
                        try:
                            m = re.search(r"[?&]u=([^&]+)", href)
                            if m:
                                raw_b64 = m.group(1)
                                if raw_b64.startswith("a1"):
                                    raw_b64 = raw_b64[2:]
                                raw_b64 += "=" * (-len(raw_b64) % 4)
                                decoded = base64.b64decode(raw_b64).decode("utf-8", errors="ignore")
                                if decoded.startswith("http"):
                                    href = decoded
                        except Exception:
                            pass

                    if not self._is_valid_result_url(href):
                        continue

                    title = a.get_text(strip=True)
                    snippet_el = li.select_one(".b_caption p, p")
                    snippet = snippet_el.get_text(strip=True) if snippet_el else ""

                    netloc = urlparse(href).netloc.lower()
                    if netloc not in seen_domains:
                        seen_domains.add(netloc)
                        results.append({
                            "title": title,
                            "url": href,
                            "snippet": snippet,
                            "domain": netloc
                        })
                        if len(results) >= num_results:
                            break
        except Exception as e:
            logger.warning(f"Bing search error: {e}")

        return results

    def search_top_websites(self, query: str, num_results: int = 10) -> List[Dict[str, str]]:
        """
        Executes multi-engine search across DuckDuckGo Lite, DuckDuckGo HTML,
        and Bing Organic Search, returning the top unique target websites.
        """
        query_clean = query.strip()
        logger.info(f"Initiating multi-engine web search for: '{query_clean}' (target top {num_results})")

        all_results: List[Dict[str, str]] = []
        seen_domains = set()

        def add_batch(batch: List[Dict[str, str]]):
            for r in batch:
                domain = r["domain"]
                if domain not in seen_domains:
                    seen_domains.add(domain)
                    all_results.append(r)

        # 1. DuckDuckGo Lite (fastest, clean results)
        lite_res = self.search_duckduckgo_lite(query_clean, num_results=num_results)
        add_batch(lite_res)

        # 2. Bing Organic Search (essential fallback and complement)
        if len(all_results) < num_results:
            bing_res = self.search_bing(query_clean, num_results=num_results)
            add_batch(bing_res)

        # 3. DuckDuckGo HTML (further fallback if needed)
        if len(all_results) < num_results:
            html_res = self.search_duckduckgo_html(query_clean, num_results=num_results)
            add_batch(html_res)

        logger.info(f"Retrieved {len(all_results)} top website results for '{query_clean}'")
        return all_results[:num_results]
