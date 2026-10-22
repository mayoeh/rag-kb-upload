import json
import os
import re
import time
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import cloudscraper
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from jira import JIRA
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from youtube_transcript_api import YouTubeTranscriptApi
from yt_dlp import YoutubeDL

load_dotenv()


class WikiAuthenticationError(RuntimeError):
    """Raised when the saved NetBadge cookies are missing or expired.

    Refreshing them needs a Duo push approved on a phone, so this can never
    be recovered from automatically. Callers should surface it as an alert
    rather than retry.
    """


class RCKBKnowledgeManager:
    def __init__(self, output_folder):
        self.output_folder = Path(output_folder)
        self.output_folder.mkdir(
            parents=True,
            exist_ok=True,
        )

    ############################
    #    MAIN SCRAPER LOGIC    #
    ############################

    # Most general website scraper
    def _generate_website_knowledge(self):
        scraper = cloudscraper.create_scraper()
        visited = set()

        # url -> save_document result, or {"status": "skipped" / "failed"}
        results = {}

        self._crawl(scraper, "https://rc.virginia.edu/", visited, results)
        self._crawl(scraper, "https://learning.rc.virginia.edu/", visited, results)
        self.fill_sitemap_gaps(scraper, results)
        self.patch_js_rendered_pages(results)

        return self._summarize_results(results)

    # Helper Functions

    SKIP_PATTERNS = ["/author/", "/category/", "/tag/"]

    @staticmethod
    def _is_valid(url):
        SKIP_EXTENSIONS = (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".bmp",
            ".svg",
            ".pdf",
            ".zip",
            ".tar",
            ".gz",
            ".mp4",
            ".webp",
        )
        ALLOWED_NETLOCS = {
            "rc.virginia.edu",
            "learning.rc.virginia.edu",
            "archive.rc.virginia.edu",
        }
        parsed = urlparse(url)
        path = parsed.path.lower()
        return (
            parsed.scheme in {"http", "https"}
            and parsed.netloc in ALLOWED_NETLOCS
            and not path.endswith(SKIP_EXTENSIONS)
        )

    # Website files are named from the full URL
    def _website_filename(self, url):
        return f"{self.generate_sanitized_filename(url, 150, False)}.md"

    def _crawl(self, scraper, url, visited, results, netloc=None):

        if url in visited:
            return
        visited.add(url)

        netloc = netloc or urlparse(url).netloc

        try:
            response = scraper.get(url, timeout=15)

            if response.url != url:
                url = response.url
                if url in visited:
                    return
                visited.add(url)

            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                return
            if response.status_code != 200:
                return

            soup = BeautifulSoup(response.text, "html.parser")

            if len(articles := soup.find_all("article")) != 1:
                print(f"Skipping {url} (no single article)")
            else:
                article_soup = BeautifulSoup(str(articles[0]), "html.parser")

                for tag in article_soup.find_all("img"):
                    tag.decompose()

                return_link = article_soup.find(
                    "a", string=re.compile(r"^« Return to")
                )
                if return_link:
                    return_link.decompose()

                metadata_tag = article_soup.find("p", class_="blog-post-meta")

                for a_tag in article_soup.find_all("a", href=True):
                    href = a_tag["href"]
                    if not href.startswith("http"):
                        href = urljoin(url, href)
                    a_tag["href"] = href
                    a_tag.string = f"[{a_tag.get_text(strip=True)}]({href})"

                title_tag = article_soup.find("h2", class_="blog-post-title")
                if title_tag:
                    title_text = title_tag.get_text(strip=True)
                    title_tag.string = f"# {title_text}\n\n"

                for h1_tag in article_soup.find_all("h1"):
                    h1_text = h1_tag.get_text(strip=True)
                    h1_tag.string = f"\n\n## {h1_text}\n"

                if metadata_tag:
                    metadata_tag.decompose()

                clean_text = self.clean_markdown_text(article_soup.get_text())

                result = self._save_website_page(url, clean_text, results)
                print(f"{result['status'].capitalize()}: {url}")

            for a_tag in soup.find_all("a", href=True):
                next_url = a_tag["href"]
                if not next_url.startswith(("http://", "https://")):
                    next_url = urljoin(url, next_url)
                next_url = next_url.split("#")[0]

                if next_url not in visited and self._is_valid(next_url):
                    self._crawl(scraper, next_url, visited, results, netloc)

            time.sleep(0.2)

        except Exception as e:
            results.setdefault(url, {"status": "failed"})
            print(f"Failed to crawl {url}: {e}")

    def _get_sitemap_urls(self, scraper, sitemap_url, skip_patterns=None):
        skip_patterns = skip_patterns or []
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        print(f"Fetching sitemap: {sitemap_url}")
        resp = scraper.get(sitemap_url, timeout=15)
        resp.raise_for_status()

        print(f"  Status: {resp.status_code}")
        print(f"  Content-Type: {resp.headers.get('Content-Type')}")

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            print(f"  Could not parse sitemap XML: {e}")
            print(f"  Response starts with: {resp.text[:300]!r}")
            return []

        base = "https://" + sitemap_url.split("/")[2]
        raw = []

        for url in root.findall("sm:url", ns):
            loc = url.find("sm:loc", ns)
            if loc is not None and loc.text:
                raw.append(loc.text.strip())

        urls = [base + u if u.startswith("/") else u for u in raw]
        urls = [u for u in urls if not any(pattern in u for pattern in skip_patterns)]

        print(f"  Found {len(urls)} URLs")
        return urls

    def _extract_article(self, scraper, url):
        response = scraper.get(url, timeout=15)
        if response.status_code != 200 or "text/html" not in response.headers.get(
            "Content-Type", ""
        ):
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        articles = soup.find_all("article")
        if len(articles) != 1:
            return None

        article_soup = BeautifulSoup(str(articles[0]), "html.parser")

        for tag in article_soup.find_all("img"):
            tag.decompose()

        metadata_tag = article_soup.find("p", class_="blog-post-meta")

        for a_tag in article_soup.find_all("a", href=True):
            href = a_tag["href"]
            if not href.startswith("http"):
                href = urljoin(url, href)
            a_tag["href"] = href
            a_tag.string = "[" + a_tag.get_text(strip=True) + "](" + href + ")"

        if metadata_tag:
            metadata_tag.decompose()

        text = article_soup.get_text()
        clean_text = self.clean_markdown_text(text)

        return {"text": clean_text, "filename": self._website_filename(url)}

    def fill_sitemap_gaps(self, scraper, results):
        rc_urls = self._get_sitemap_urls(scraper, "https://rc.virginia.edu/sitemap.xml")
        learn_urls = self._get_sitemap_urls(
            scraper, "https://learning.rc.virginia.edu/sitemap.xml", self.SKIP_PATTERNS
        )
        all_sitemap_urls = rc_urls + learn_urls

        # Compare by filename rather than URL so that redirects and trailing
        # slashes (".../page" vs ".../page/") count as the same page. Only pages
        # saved in this run count; old files on disk may be stale.
        saved_files = {
            result["file"].name for result in results.values() if "file" in result
        }

        missing = []
        for url in all_sitemap_urls:
            if self._website_filename(url) not in saved_files:
                missing.append(url)
        print(
            f"Sitemap total: {len(all_sitemap_urls)}, already crawled: {len(all_sitemap_urls) - len(missing)}, missing: {len(missing)}"
        )

        for url in missing:
            try:
                result = self._extract_article(scraper, url)
                if result:
                    result = self._save_website_page(url, result["text"], results)
                    print(f"Added ({result['status']}): {url}")
                else:
                    results.setdefault(url, {"status": "skipped"})
                    print(f"Skipped (no article): {url}")
                time.sleep(0.2)
            except Exception as e:
                results.setdefault(url, {"status": "failed"})
                print(f"Failed {url}: {e}")

        print("Fully done filling sitemap gaps!")

    # Pages whose content is rendered by JavaScript, so the scraper only sees an empty shell
    JS_RENDERED_PATCHES = {
        "https://rc.virginia.edu/userinfo/hpc/slurm-script-generator/": "# Slurm Script Generator\n\nThe UVA Research Computing Slurm Script Generator...",
        "https://rc.virginia.edu/userinfo/hpc/software/physics/": "# Physics Software on UVA HPC\n\nUVA Research Computing provides several physics...",
    }

    # Single save point for website pages. A JS-rendered page is swapped for its
    # manual patch before writing, so it is not rewritten (and reported as
    # "updated") twice on every run.
    def _save_website_page(self, url, text, results):
        filename = self._website_filename(url)

        for patch_url, patch_text in self.JS_RENDERED_PATCHES.items():
            if self._website_filename(patch_url) == filename and len(text.strip()) < 100:
                text = patch_text

        result = self.save_document(filename, text)
        results[url] = result
        return result

    # Patches for JS-rendered pages the crawl and sitemap never reached
    def patch_js_rendered_pages(self, results):
        saved_files = {
            result["file"].name for result in results.values() if "file" in result
        }

        patched = 0
        for url, text in self.JS_RENDERED_PATCHES.items():
            if self._website_filename(url) not in saved_files:
                self._save_website_page(url, text, results)
                patched += 1
                print(f"Patched: {url}")
            else:
                print(f"Already handled: {url}")

        print(f"\nManually patched {patched} JS-rendered pages.")

    ############################
    #    WIKI SCRAPER LOGIC    #
    ############################

    WIKI_BASE_URL = "https://rci.hpc.virginia.edu/wiki/"
    WIKI_INDEX_URL = "https://rci.hpc.virginia.edu/wiki/_index"
    WIKI_LOGIN_TARGET = "https://rci.hpc.virginia.edu/wiki/Special:AllPages"

    WIKI_REQUEST_DELAY = 0.3

    WIKI_SKIP_EXTENSIONS = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".svg",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".mp4",
        ".webp",
    )

    # Gitit special-path prefixes and wiki sections we never want indexed
    WIKI_SKIP_PATH_SEGMENTS = (
        "/_edit/",
        "/_history/",
        "/_discuss/",
        "/_delete/",
        "/_login",
        "/_logout",
        "/_index",
        "/_categories",
        "/_random",
        "/_search",
        "/_upload",
        "/_activity",
        "/_export",
        "/OldWiki",
        "/OldOldWiki",
    )

    # Tags that should end a line. Everything else — <code>, <strong>, <a>,
    # <em>, <tt> — has to stay inline, or sentences get chopped up around
    # every command name, which is most of this wiki.
    WIKI_BLOCK_TAGS = (
        "p", "div", "br", "hr",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "ul", "ol", "dl", "dt", "dd",
        "pre", "blockquote",
        "table", "tr", "thead", "tbody",
        "section", "article", "header", "footer",
    )

    WIKI_SAVED_STATUSES = ("created", "updated", "unchanged")

    # RCI wiki (Gitit behind NetBadge). Needs the cookies saved by netbadge_login()
    def _generate_wiki_knowledge(
        self,
        cookies_path=None,
        max_pages=None,
        crawl_links=True,
        scrub_pii=True,
        scrub_uva_ids=False,
    ):
        session = self._make_wiki_session(cookies_path)
        self._verify_wiki_session(session)

        # Wiki pages are full of module names and shell snippets that the
        # UVA-ID pattern matches (gcc9, cuda11, abc123), so ID scrubbing is
        # opt-in. PERSON is off as well: in these technical docs presidio
        # reads module, command and host names as people's names.
        def scrub(text):
            if not scrub_pii:
                return text
            return self.scrub_text(
                text,
                entities=["EMAIL_ADDRESS", "PHONE_NUMBER"],
                scrub_uva_ids=scrub_uva_ids,
            )

        # url -> save_document result, or {"status": "skipped" / "failed"}
        results = {}

        # Phase 1: everything the wiki itself lists
        self._wiki_extract_index_pages(session, results, scrub, max_pages)

        # Phase 2: follow links to reach pages missing from _index
        if crawl_links:
            saved_before = self._count_saved_wiki_pages(results)
            self._wiki_crawl_links(session, results, scrub, max_pages)
            print(
                "Phase 2 done — added "
                f"{self._count_saved_wiki_pages(results) - saved_before} "
                "additional pages"
            )

        return self._summarize_results(results)

    # Helper Functions

    @staticmethod
    def wiki_default_cookies_path():
        project_root = Path(__file__).resolve().parents[2]
        return project_root / "scrapers" / "auth_cookies.json"

    def _make_wiki_session(self, cookies_path=None):
        cookies_path = Path(
            cookies_path
            or os.getenv("WIKI_AUTH_COOKIES_PATH")
            or self.wiki_default_cookies_path()
        )

        if not cookies_path.exists():
            raise WikiAuthenticationError(
                f"{cookies_path} not found. "
                "Run netbadge_login() to create it."
            )

        cookies = json.loads(cookies_path.read_text(encoding="utf-8"))

        session = requests.Session()

        for cookie in cookies:
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", "").lstrip("."),
                path=cookie.get("path", "/"),
            )

        session.headers.update(
            {"User-Agent": "Mozilla/5.0 (compatible; UVA-RC-RAG-Scraper/1.0)"}
        )

        print(f"Loaded {len(cookies)} cookies from {cookies_path}")

        return session

    def _check_wiki_session(self, response):
        # A NetBadge page returned with HTTP 200 means the cookies died
        # mid-run. Fail loudly instead of writing login pages to disk.
        if "NetBadge" in response.text or "shibidp" in response.url:
            raise WikiAuthenticationError(
                "NetBadge redirect — cookies expired. "
                "Re-run netbadge_login()."
            )

    def _verify_wiki_session(self, session):
        response = session.get(self.WIKI_BASE_URL, timeout=20)
        self._check_wiki_session(response)
        print(f"Session OK — wiki reachable (HTTP {response.status_code})")

    def _should_skip_wiki_url(self, url):
        parsed = urlparse(url)
        path = parsed.path

        # Every page links to "?printable" (and history pages to "?revision=").
        # Same filename as the plain page, so crawling them fetches each page
        # twice and overwrites the plain version.
        if parsed.query:
            return True

        if path.lower().endswith(self.WIKI_SKIP_EXTENSIONS):
            return True

        return any(segment in path for segment in self.WIKI_SKIP_PATH_SEGMENTS)

    def _wiki_page_title(self, url):
        path = urlparse(url).path

        if path.startswith("/wiki/"):
            return unquote(path[len("/wiki/"):])

        return url

    def _count_saved_wiki_pages(self, results):
        return sum(
            1
            for result in results.values()
            if result["status"] in self.WIKI_SAVED_STATUSES
        )

    def _get_wiki_page_urls(self, session):
        # Every content page listed on /wiki/_index
        response = session.get(self.WIKI_INDEX_URL, timeout=20)
        self._check_wiki_session(response)

        soup = BeautifulSoup(response.text, "html.parser")
        content = soup.find(id="wikipage") or soup.find(id="content")

        if not content:
            print("[ERROR] Could not find page listing on _index")
            return []

        urls = []

        for a_tag in content.find_all("a", href=True):
            if a_tag.get_text(strip=True) == "(delete)":
                continue

            full_url = urljoin(self.WIKI_BASE_URL, a_tag["href"]).split("#")[0]

            if not self._should_skip_wiki_url(full_url) and "/wiki/" in full_url:
                urls.append(full_url)

        # Deduplicate while preserving discovery order
        return list(dict.fromkeys(urls))

    def _get_wiki_soup(self, session, url):
        response = session.get(url, timeout=20)

        if response.status_code != 200:
            return None

        self._check_wiki_session(response)

        if "text/html" not in response.headers.get("Content-Type", ""):
            return None

        return BeautifulSoup(response.text, "html.parser")

    def _wiki_soup_to_text(self, node):
        # get_text(separator="\n") would break inline tags onto their own
        # lines, so mark up the block boundaries first and then join with
        # nothing.
        for tag in node.find_all(self.WIKI_BLOCK_TAGS):
            tag.insert_before("\n")
            tag.insert_after("\n")

        # Keep table cells apart on the same line.
        for tag in node.find_all(["td", "th"]):
            tag.insert_after(" ")

        return node.get_text()

    def _parse_wiki_page(self, url, soup, scrub):
        # Turn one Gitit page into a knowledge document. Shared by the _index
        # pass and the link-crawl pass so both produce identical Markdown.
        wikipage = soup.find(id="wikipage")

        if not wikipage:
            return None

        for element in wikipage.select("#TOC, .editsection, .footnotes"):
            element.decompose()

        for tag in wikipage.find_all("img"):
            tag.decompose()

        # Take the title from the <h1>, then drop it. _build_wiki_markdown
        # adds the heading back, so leaving it here repeats it.
        title_tag = wikipage.find("h1")
        title = (
            title_tag.get_text(strip=True)
            if title_tag
            else self._wiki_page_title(url)
        )
        if title_tag:
            title_tag.decompose()

        for a_tag in wikipage.find_all("a", href=True):
            href = a_tag["href"]
            if not href.startswith("http"):
                href = urljoin(url, href)
            a_tag["href"] = href

            link_text = a_tag.get_text(strip=True)
            if link_text:
                a_tag.string = f"[{link_text}]({href})"

        text = self._wiki_soup_to_text(wikipage)

        # clean_markdown_text only drops .png references; the wiki also
        # links other image types
        text = re.sub(r"\S+\.(?:jpg|jpeg|gif|svg|webp)", "", text)
        text = self.clean_markdown_text(text).strip()

        if not text:
            return None

        category_div = soup.find(id="categories") or soup.find(class_="categories")
        categories = (
            [a_tag.get_text(strip=True) for a_tag in category_div.find_all("a")]
            if category_div
            else []
        )

        return {
            "title": scrub(title),
            "text": scrub(text),
            "source": url,
            "tags": ["wiki"] + categories,
        }

    def _build_wiki_markdown(self, page):
        title = page["title"] or "Wiki Page"
        text = page["text"]
        tags = ", ".join(page["tags"])

        body = text if text.startswith("#") else f"# {title}\n\n{text}"

        return f"""{body}

---

## Wiki Page

**Source:** {page["source"]}

**Tags:** {tags}
"""

    # Name files from the URL path, not the title — two wiki pages can
    # share an <h1> and would otherwise overwrite each other.
    def _wiki_filename(self, url):
        slug = re.sub(r"\s+", "_", self._wiki_page_title(url))
        slug = self.generate_sanitized_filename(slug, 150, True).strip("_")

        # The wiki root has nothing after /wiki/
        if not slug:
            slug = "index"

        return f"wiki_{slug}.md"

    def _save_wiki_page(self, url, soup, results, scrub):
        page = self._parse_wiki_page(url, soup, scrub)

        if page is None:
            results[url] = {"status": "skipped"}
            print("    (skipped — no content)")
            return

        result = self.save_document(self._wiki_filename(url), self._build_wiki_markdown(page))
        results[url] = result
        print(f"{result['status'].capitalize()}: {result['file'].name}")

    def _wiki_extract_index_pages(self, session, results, scrub, max_pages=None):
        print("Collecting page URLs from _index ...")

        page_urls = self._get_wiki_page_urls(session)
        print(f"Found {len(page_urls)} pages (OldWiki/OldOldWiki excluded)")

        if max_pages:
            page_urls = page_urls[:max_pages]
            print(f"Limited to the first {len(page_urls)} pages (max_pages set)")

        for index, url in enumerate(page_urls, 1):
            print(f"  [{index}/{len(page_urls)}] {self._wiki_page_title(url)}")

            try:
                soup = self._get_wiki_soup(session, url)

                if soup is None:
                    results[url] = {"status": "skipped"}
                else:
                    self._save_wiki_page(url, soup, results, scrub)

            except WikiAuthenticationError:
                raise

            except Exception as e:
                results[url] = {"status": "failed"}
                print(f"    [error] {e}")

            time.sleep(self.WIKI_REQUEST_DELAY)

        print(
            f"Phase 1 done — {self._count_saved_wiki_pages(results)} "
            "documents extracted"
        )

    def _wiki_crawl_links(self, session, results, scrub, max_pages=None):
        # Track "links already followed" separately from "page already
        # saved". Seeding this with the phase 1 URLs would skip those pages
        # entirely — including their outgoing links — and since phase 1
        # almost always grabs the front page, the crawl would stop on its
        # first iteration and visit nothing at all.
        visited = set()

        queue = [self.WIKI_BASE_URL]
        netloc = urlparse(self.WIKI_BASE_URL).netloc

        while queue:
            if max_pages and self._count_saved_wiki_pages(results) >= max_pages:
                print(f"  [crawl] stopping at {max_pages} pages (max_pages set)")
                break

            url = queue.pop(0)

            if url in visited or self._should_skip_wiki_url(url):
                continue
            visited.add(url)

            try:
                soup = self._get_wiki_soup(session, url)

                if soup is None:
                    continue

                # Compare by filename, not URL: the same page is linked under
                # several spellings ("%3c" vs "%3C" in _index vs page links)
                saved_files = {
                    result["file"].name
                    for result in results.values()
                    if result["status"] in self.WIKI_SAVED_STATUSES
                }

                if self._wiki_filename(url) not in saved_files:
                    print(f"  [crawl] {self._wiki_page_title(url)}")
                    self._save_wiki_page(url, soup, results, scrub)

                for a_tag in soup.find_all("a", href=True):
                    next_url = a_tag["href"]
                    if not next_url.startswith(("http://", "https://")):
                        next_url = urljoin(url, next_url)
                    next_url = next_url.split("#")[0]

                    if (
                        next_url not in visited
                        and urlparse(next_url).netloc == netloc
                        and "/wiki/" in next_url
                        and not self._should_skip_wiki_url(next_url)
                    ):
                        queue.append(next_url)

                time.sleep(self.WIKI_REQUEST_DELAY)

            except WikiAuthenticationError:
                raise

            except Exception as e:
                results.setdefault(url, {"status": "failed"})
                print(f"  [error] {url}: {e}")

    ############################
    #     UTILITY METHODS      #
    ############################

    @staticmethod
    def build_generation_result(
        fetched,
        created,
        updated,
        unchanged,
        skipped,
        failed,
        changed_files,
    ):
        print("KNOWLEDGE GENERATION COMPLETE")
        print(f"Fetched:   {fetched}")
        print(f"Created:   {created}")
        print(f"Updated:   {updated}")
        print(f"Unchanged: {unchanged}")
        print(f"Skipped:   {skipped}")
        print(f"Failed:    {failed}")

        return {
            "fetched": fetched,
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "skipped": skipped,
            "failed": failed,
            "changed_files": changed_files,
        }

    # Turn a scraper's {url: save_document result} map into the generation summary
    def _summarize_results(self, results):
        statuses = [result["status"] for result in results.values()]

        return self.build_generation_result(
            fetched=len(results),
            created=statuses.count("created"),
            updated=statuses.count("updated"),
            unchanged=statuses.count("unchanged"),
            skipped=statuses.count("skipped"),
            failed=statuses.count("failed"),
            changed_files=[
                result["file"]
                for result in results.values()
                if result["status"] in ("created", "updated")
            ],
        )

    # Given the title of a source, transform it to a usable, safe filename
    def generate_sanitized_filename(self, name, maxLength, hasSpaces):
        if not name:
            return ""

        # specifically for sources whose titles may contain spaces (video/wiki/jira)
        # if it doesnt, it is likely a website url that needs to be sanitzied in a special manner
        if hasSpaces:
            filename = re.sub(r'[<>:"/\\|?*]', "_", name)
            filename = re.sub(
                r"\s+",
                " ",
                filename,
            ).strip()
        else:
            filename = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)
            filename = re.sub(r"_+", "_", filename).strip("_")

        return filename[:maxLength]

    # Atomic Saving utility used by scrapers to generate update summaries
    def save_document(self, filename, content):
        file_path = self.output_folder / filename

        content = content.strip() + "\n"

        # New document
        if not file_path.exists():
            file_path.write_text(
                content,
                encoding="utf-8",
            )

            return {
                "status": "created",
                "file": file_path,
            }

        # Existing document
        existing_content = file_path.read_text(encoding="utf-8")

        # Nothing changed
        if existing_content == content:
            return {
                "status": "unchanged",
                "file": file_path,
            }

        # Content changed
        file_path.write_text(
            content,
            encoding="utf-8",
        )

        return {
            "status": "updated",
            "file": file_path,
        }

    # Cleaning text using common regex patterns and removing whitespace
    def clean_markdown_text(self, text):
        if not text:
            return ""

        # Specifically important for Website text cleanup
        text = re.sub(r"https?:\/\/\S+?\.png", "", text)
        text = re.sub(r"\S+\.png", "", text)

        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip() + "\n"

    def strip_html_tags(self, html):
        if not html:
            return ""
        text = re.sub(r"<br\s*/?>", "\n", html)
        text = re.sub(r"</p>", "\n", text)
        text = re.sub(r"</li>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = unescape(text)
        return self.clean_markdown_text(text)

    UVA_ID_RE = re.compile(
        r"\b[a-z]{2,4}\d{1,2}[a-z0-9]{1,3}\b",
        re.IGNORECASE,
    )

    # PII scrubbing with presidio, plus UVA computing IDs
    # The presidio engines load a large spaCy model, so they are only built on first use
    def scrub_text(
        self,
        text,
        entities=("PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"),
        scrub_uva_ids=True,
    ):
        if not text:
            return text

        if getattr(self, "_analyzer", None) is None:
            self._analyzer = AnalyzerEngine()
            self._anonymizer = AnonymizerEngine()

        results = self._analyzer.analyze(
            text=text,
            language="en",
            entities=list(entities),
        )
        text = self._anonymizer.anonymize(
            text=text,
            analyzer_results=results,
        ).text

        if scrub_uva_ids:
            text = self.UVA_ID_RE.sub("<UVA_ID>", text)

        return text


############################
#   WIKI NETBADGE LOGIN    #
############################


def netbadge_login(
    username,
    password,
    target=None,
    output=None,
    timeout=120,
    debug_dir=None,
):
    """Log in through NetBadge + Duo and save the session cookies to `output`.

    Runs a headless Chromium via Playwright's *sync* API, so it must not be
    called from inside a running asyncio event loop (that is why the notebook
    used the async API). Intended to be driven by an interactive script,
    where a human is present to approve the Duo push.

    When `debug_dir` is given, the Duo page and any timeout are captured as
    screenshots there. Duo can block headless browsers outright, and the
    screenshot is usually the only way to see why.
    """

    from playwright.sync_api import sync_playwright

    target = target or RCKBKnowledgeManager.WIKI_LOGIN_TARGET
    output = Path(output or RCKBKnowledgeManager.wiki_default_cookies_path())

    username_selector = (
        "input[name='j_username'], "
        "input[name='username'], "
        "input[id='username']"
    )

    password_selector = (
        "input[name='j_password'], "
        "input[name='password'], "
        "input[id='password']"
    )

    if debug_dir:
        debug_dir = Path(debug_dir)
        debug_dir.mkdir(parents=True, exist_ok=True)

    def save_debug(page, name):
        if not debug_dir:
            return

        try:
            path = debug_dir / f"{name}.png"
            page.screenshot(path=str(path))
            print(f"  Screenshot saved -> {path}")
        except Exception as error:
            print(f"  [warn] Could not capture {name}: {error}")

    # Duo Verified Push shows a 3-digit code in the browser that has to be
    # typed into Duo Mobile. Headless means nobody can see it, so pull it out
    # of the page and print it. The code renders a moment after the push is
    # triggered, so this gets called repeatedly until it turns up.
    seen_codes = set()
    seen_snippets = set()

    def report_duo_code(page):

        try:
            body_text = page.locator("body").inner_text()
        except Exception:
            return

        codes = re.findall(
            r"(?<!\d)(\d{3})(?!\d)",
            body_text,
        )

        for code in codes:
            if code in seen_codes:
                continue

            seen_codes.add(code)

            print()
            print("  " + "=" * 44)
            print(
                f"  DUO CODE: {code}   "
                "<- enter this in Duo Mobile"
            )
            print("  " + "=" * 44)
            print(flush=True)

        if not codes and not seen_codes:
            # No 3-digit code found. Show the page text instead, so a
            # 2-digit code, a device picker or a bot block is still visible.
            # Only on change, or it repeats on every poll.
            snippet = " | ".join(
                line.strip()
                for line in body_text.splitlines()
                if line.strip()
            )[:400]

            if snippet and snippet not in seen_snippets:
                seen_snippets.add(snippet)
                print(f"  Duo page: {snippet}", flush=True)

    print("Launching headless browser ...", flush=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(target, timeout=30000)
            page.wait_for_load_state(
                "domcontentloaded",
                timeout=20000,
            )
            page.wait_for_timeout(2000)

            # Step 1: the certificate-auth failure page, if it appears

            for _ in range(4):
                if page.locator("#mw-content-text").count() > 0:
                    break

                if (
                    "my.policy" in page.url
                    or "Certificate Login Failed" in page.content()
                ):
                    print(
                        "  Certificate auth failed page "
                        "— clicking Continue ..."
                    )

                    page.locator(
                        "a:has-text('Continue'), "
                        "input[value='Continue'], "
                        "button:has-text('Continue')"
                    ).first.click()

                    page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=20000,
                    )
                    page.wait_for_timeout(2000)

                    print(f"  Now at: {page.url}")

                if page.locator(username_selector).count() > 0:
                    page.locator(username_selector).first.fill(username)
                    page.locator(password_selector).first.fill(password)

                    # Enter submits the password form only. Clicking the
                    # visible button can hit the separate certificate-login
                    # form instead.
                    page.locator(password_selector).first.press("Enter")

                    page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=30000,
                    )
                    page.wait_for_timeout(2000)

                    print(f"  URL after submit: {page.url}")
                    break

            # Step 2: Duo two-factor

            if page.locator("#mw-content-text").count() == 0:
                page.wait_for_timeout(3000)

                for push_text in [
                    "Send Me a Push",
                    "Send push",
                    "Push",
                    "Send request",
                ]:
                    push_button = page.locator(
                        f"button:has-text('{push_text}'), "
                        f"input[value='{push_text}'], "
                        f"a:has-text('{push_text}')"
                    )

                    if push_button.count() > 0:
                        print(f"  Clicking Duo button: '{push_text}'")
                        push_button.first.click()
                        page.wait_for_timeout(1500)
                        break

                save_debug(page, "debug_duo")

                # Give the prompt a moment to render the code, then read it.
                page.wait_for_timeout(2000)
                report_duo_code(page)

                print()
                print(
                    "Waiting for you to approve the Duo push "
                    "on your phone ...",
                    flush=True,
                )

            # Step 3: wait for the wiki itself

            deadline = time.time() + timeout

            while time.time() < deadline:
                current_url = page.url

                if (
                    "rci.hpc.virginia.edu/wiki/" in current_url
                    and "shibidp" not in current_url
                    and "duosecurity" not in current_url
                ):
                    break

                print(
                    f"  [{int(deadline - time.time())}s left] "
                    f"URL: {current_url}",
                    flush=True,
                )

                if "duosecurity" in current_url:
                    report_duo_code(page)

                for button_text in [
                    "Trust this browser",
                    "Stay signed in",
                    "Yes",
                    "Continue",
                    "Proceed",
                ]:
                    button = page.locator(
                        f"button:has-text('{button_text}'), "
                        f"input[value='{button_text}']"
                    )

                    if button.count() > 0:
                        print(
                            "  Clicking intermediate button: "
                            f"'{button_text}'"
                        )
                        button.first.click()
                        page.wait_for_load_state(
                            "domcontentloaded",
                            timeout=15000,
                        )
                        break

                page.wait_for_timeout(5000)
            else:
                save_debug(page, "debug_final")

                raise WikiAuthenticationError(
                    "NetBadge/Duo login timed out. "
                    f"Final URL: {page.url}"
                )

            print("Login successful!")

            cookies = context.cookies()

            output.parent.mkdir(parents=True, exist_ok=True)

            output.write_text(
                json.dumps(cookies, indent=2),
                encoding="utf-8",
            )

            print(f"Saved {len(cookies)} cookies -> {output}")

            return output

        finally:
            browser.close()
