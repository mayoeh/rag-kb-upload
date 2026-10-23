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
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    YouTubeTranscriptApi,
)
from yt_dlp import YoutubeDL

load_dotenv()


class WikiAuthenticationError(RuntimeError):
    """Raised when the saved NetBadge cookies are missing or expired.

    Refreshing them needs a Duo push approved on a phone, so this can never
    be recovered from automatically. Callers should surface it as an alert
    rather than retry.
    """


class RCKBKnowledgeManager:
    ############################
    #      CONFIGURATION       #
    ############################

    # Website crawl
    WEBSITE_SEEDS = (
        "https://rc.virginia.edu/",
        "https://learning.rc.virginia.edu/",
    )
    RC_SITEMAP = "https://rc.virginia.edu/sitemap.xml"
    LEARNING_SITEMAP = "https://learning.rc.virginia.edu/sitemap.xml"
    ALLOWED_NETLOCS = {
        "rc.virginia.edu",
        "learning.rc.virginia.edu",
        "archive.rc.virginia.edu",
    }
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
    # URL substrings to ignore in the learning.rc sitemap (e.g. "/tags/")
    SKIP_PATTERNS = []
    REQUEST_DELAY = 0.2  # seconds between page requests
    BLOCKED_STATUSES = (403, 429, 503)

    # JIRA
    UVA_ID_RE = re.compile(
        r"\b[a-z]{2,4}\d{1,2}[a-z0-9]{1,3}\b",
        re.IGNORECASE,
    )
    JIRA_BASE_FILTER = (
        "project IN (SUP, RIV, IVY, ACCORDSUP) "
        "AND type != Sub-task "
        'AND "Request Type[Dropdown]" != "Provisioning/Deprovisioning" '
        "AND status != Open "
    )

    # YouTube
    VIDEO_PLAYLIST_URL = (
        "https://www.youtube.com/playlist?list=PLT4bryHgBcRP7N-hB9u6EWs6tq_2nMoRO"
    )
    VIDEO_REQUEST_DELAY = 1.0  # seconds between videos; the transcript API rate-limits

    # RCI wiki (Gitit behind NetBadge)
    WIKI_BASE_URL = "https://rci.hpc.virginia.edu/wiki/"
    WIKI_INDEX_URL = "https://rci.hpc.virginia.edu/wiki/_index"
    WIKI_LOGIN_TARGET = "https://rci.hpc.virginia.edu/wiki/"
    WIKI_REQUEST_DELAY = 0.3  # seconds between page requests
    # Gitit special paths and old wiki sections we never want indexed
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
    # Tags that end a line. Everything else (<code>, <a>, <strong>, ...) stays
    # inline, or sentences get chopped up around every command name.
    WIKI_BLOCK_TAGS = (
        "p", "div", "br", "hr",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "ul", "ol", "dl", "dt", "dd",
        "pre", "blockquote",
        "table", "tr", "thead", "tbody",
        "section", "article", "header", "footer",
    )

    def __init__(self, output_folder):
        self.output_folder = Path(output_folder)
        self.output_folder.mkdir(parents=True, exist_ok=True)

        # rc.virginia.edu intermittently returns 403s to automated requests
        # (bot protection). Pin one browser profile so every run looks the
        # same, and retry blocked requests with backoff (see _fetch).
        self.scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        )

        # Created on first use, so website-only runs don't need JIRA
        # credentials or the Presidio models.
        self._jira = None
        self._analyzer = None
        self._anonymizer = None

    @property
    def jira(self):
        if self._jira is None:
            self._jira = JIRA(
                server=os.getenv("JIRA_SERVER"),
                basic_auth=(os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")),
            )
        return self._jira

    @property
    def analyzer(self):
        if self._analyzer is None:
            self._analyzer = AnalyzerEngine()
        return self._analyzer

    @property
    def anonymizer(self):
        if self._anonymizer is None:
            self._anonymizer = AnonymizerEngine()
        return self._anonymizer

    ############################
    #     UTILITY METHODS      #
    ############################

    @staticmethod
    def _new_stats():
        return {
            "fetched": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "failed": 0,
            "changed_files": [],
        }

    @staticmethod
    def _record(stats, result):
        # Tally a save_document() result into a stats dict
        status = result["status"]
        stats[status] += 1
        if status in ("created", "updated"):
            stats["changed_files"].append(result["file"])

    def build_generation_result(
        self,
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

    # Given the title of a source, transform it to a usable, safe filename
    def generate_sanitized_filename(self, name, maxLength=200, hasSpaces=False):
        if not name:
            return ""

        # Sources whose titles may contain spaces (video/wiki/jira) keep them;
        # otherwise it is likely a website URL, sanitized to underscores.
        if hasSpaces:
            filename = re.sub(r'[<>:"/\\|?*]', "_", name)
            filename = re.sub(r"\s+", " ", filename).strip()
        else:
            filename = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)
            filename = re.sub(r"_+", "_", filename).strip("_")

        return filename[:maxLength]

    # Saving utility used by scrapers to generate update summaries
    def save_document(self, filename, content):
        if not filename.endswith(".md"):
            filename += ".md"
        file_path = self.output_folder / filename

        content = content.strip() + "\n"

        # New document
        if not file_path.exists():
            file_path.write_text(content, encoding="utf-8")
            return {"status": "created", "file": file_path}

        # Nothing changed
        if file_path.read_text(encoding="utf-8") == content:
            return {"status": "unchanged", "file": file_path}

        # Content changed
        file_path.write_text(content, encoding="utf-8")
        return {"status": "updated", "file": file_path}

    # Cleaning text using common regex patterns and removing whitespace
    def clean_markdown_text(self, text):
        if not text:
            return ""

        # Specifically important for website text cleanup
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

    ############################
    #       WEBSITE LOGIC      #
    ############################

    def generate_website_knowledge(self):
        print("Starting website knowledge generation...")
        stats = self._new_stats()
        visited = set()

        for seed in self.WEBSITE_SEEDS:
            self._crawl(seed, visited, stats)

        self.fill_sitemap_gaps(stats)
        self.patch_js_rendered_pages(stats)

        return self.build_generation_result(**stats)

    def _is_valid(self, url):
        parsed = urlparse(url)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.netloc in self.ALLOWED_NETLOCS
            and not parsed.path.lower().endswith(self.SKIP_EXTENSIONS)
        )

    def _doc_path(self, url):
        return self.output_folder / f"{self.generate_sanitized_filename(url)}.md"

    def _fetch(self, url, retries=3):
        # Retry requests that bot protection or rate limiting turned away
        for attempt in range(retries):
            response = self.scraper.get(url, timeout=15)
            if response.status_code not in self.BLOCKED_STATUSES:
                return response
            wait = 5 * (attempt + 1)
            print(f"  [{response.status_code}] blocked on {url}, retrying in {wait}s")
            time.sleep(wait)
        return response

    def _parse_article(self, soup, url):
        # Returns cleaned Markdown for the page's single <article>, or None
        articles = soup.find_all("article")
        if len(articles) != 1:
            return None

        article = BeautifulSoup(str(articles[0]), "html.parser")

        for tag in article.find_all("img"):
            tag.decompose()

        return_link = article.find("a", string=re.compile(r"^\u00ab Return to"))
        if return_link:
            return_link.decompose()

        metadata_tag = article.find("p", class_="blog-post-meta")
        if metadata_tag:
            metadata_tag.decompose()

        for a_tag in article.find_all("a", href=True):
            href = a_tag["href"]
            if not href.startswith("http"):
                href = urljoin(url, href)
            a_tag["href"] = href
            a_tag.string = f"[{a_tag.get_text(strip=True)}]({href})"

        title_tag = article.find("h2", class_="blog-post-title")
        if title_tag:
            title_tag.string = f"# {title_tag.get_text(strip=True)}\n\n"

        for h1_tag in article.find_all("h1"):
            h1_tag.string = f"\n\n## {h1_tag.get_text(strip=True)}\n"

        return self.clean_markdown_text(article.get_text())

    def _crawl(self, url, visited, stats=None):
        if stats is None:
            stats = self._new_stats()

        if url in visited:
            return stats
        visited.add(url)

        try:
            response = self._fetch(url)

            if response.url != url:
                url = response.url
                if url in visited:
                    return stats
                visited.add(url)

            if response.status_code != 200:
                print(f"Skipping {url} (status {response.status_code})")
                if response.status_code in self.BLOCKED_STATUSES:
                    stats["failed"] += 1
                else:
                    stats["skipped"] += 1
                return stats

            if "text/html" not in response.headers.get("Content-Type", ""):
                return stats

            stats["fetched"] += 1
            soup = BeautifulSoup(response.text, "html.parser")

            text = self._parse_article(soup, url)
            if text is None:
                print(f"Skipping {url} (no single article)")
                stats["skipped"] += 1
            else:
                result = self.save_document(self.generate_sanitized_filename(url), text)
                self._record(stats, result)
                print(f"Extracted ({result['status']}): {url}")

            # Follow links from the whole page, not just the article
            for a_tag in soup.find_all("a", href=True):
                next_url = urljoin(url, a_tag["href"]).split("#")[0]
                if next_url not in visited and self._is_valid(next_url):
                    self._crawl(next_url, visited, stats)

            time.sleep(self.REQUEST_DELAY)

        except Exception as e:
            stats["failed"] += 1
            print(f"Failed to crawl {url}: {e}")

        return stats

    def _get_sitemap_urls(self, sitemap_url, skip_patterns=None):
        skip_patterns = skip_patterns or []
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        print(f"Fetching sitemap: {sitemap_url}")
        resp = self._fetch(sitemap_url)
        if resp.status_code != 200:
            print(f"  Could not fetch sitemap (status {resp.status_code})")
            return []

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            print(f"  Could not parse sitemap XML: {e}")
            print(f"  Response starts with: {resp.text[:300]!r}")
            return []

        base = "https://" + urlparse(sitemap_url).netloc
        raw = [
            loc.text.strip()
            for entry in root.findall("sm:url", ns)
            if (loc := entry.find("sm:loc", ns)) is not None and loc.text
        ]

        urls = [base + u if u.startswith("/") else u for u in raw]
        urls = [u for u in urls if not any(p in u for p in skip_patterns)]

        print(f"  Found {len(urls)} URLs")
        return urls

    def fill_sitemap_gaps(self, stats=None):
        # Catch orphan pages the link crawl never reached
        if stats is None:
            stats = self._new_stats()

        all_sitemap_urls = self._get_sitemap_urls(
            self.RC_SITEMAP
        ) + self._get_sitemap_urls(self.LEARNING_SITEMAP, self.SKIP_PATTERNS)

        missing = []
        for url in all_sitemap_urls:
            path = self._doc_path(url)
            if not path.exists() or path.stat().st_size == 0:
                missing.append(url)

        print(
            f"Sitemap total: {len(all_sitemap_urls)}, "
            f"already crawled: {len(all_sitemap_urls) - len(missing)}, "
            f"missing: {len(missing)}"
        )

        for url in missing:
            try:
                response = self._fetch(url)
                if response.status_code != 200 or "text/html" not in response.headers.get(
                    "Content-Type", ""
                ):
                    print(f"Skipped (status {response.status_code}): {url}")
                    stats["skipped"] += 1
                    continue

                stats["fetched"] += 1
                soup = BeautifulSoup(response.text, "html.parser")
                text = self._parse_article(soup, url)

                if text is None:
                    print(f"Skipped (no article): {url}")
                    stats["skipped"] += 1
                else:
                    result = self.save_document(
                        self.generate_sanitized_filename(url), text
                    )
                    self._record(stats, result)
                    print(f"Added: {url}")

                time.sleep(self.REQUEST_DELAY)

            except Exception as e:
                stats["failed"] += 1
                print(f"Failed {url}: {e}")

        print("Finished filling sitemap gaps.")
        return stats

    def patch_js_rendered_pages(self, stats=None):
        # Pages whose content is rendered by JavaScript, so the crawler
        # can't see it. Text is maintained by hand here.
        if stats is None:
            stats = self._new_stats()

        manual_patches = {
            "https://rc.virginia.edu/userinfo/hpc/slurm-script-generator/": (
                "# Slurm Script Generator\n\n"
                "The UVA Research Computing Slurm Script Generator..."
            ),
            "https://rc.virginia.edu/userinfo/hpc/software/physics/": (
                "# Physics Software on UVA HPC\n\n"
                "UVA Research Computing provides several physics..."
            ),
        }

        patched = 0
        for url, text in manual_patches.items():
            path = self._doc_path(url)
            if path.exists() and path.stat().st_size >= 100:
                print(f"Already has content: {url}")
                continue

            result = self.save_document(self.generate_sanitized_filename(url), text)
            self._record(stats, result)
            patched += 1
            print(f"Patched: {url}")

        print(f"Manually patched {patched} JS-rendered pages.")
        return stats

    ############################
    #        JIRA LOGIC        #
    ############################

    def get_all_jira_issues(self):
        # Initial population of the KB or full rebuild
        jql = self.JIRA_BASE_FILTER + "ORDER BY updated DESC"
        return self._fetch_issues(jql)

    def get_recent_jira_issues(self):
        # Incremental scrape for recently updated issues
        jql = self.JIRA_BASE_FILTER + "AND updated >= -2d ORDER BY updated DESC"
        return self._fetch_issues(jql)

    def _fetch_issues(self, jql):
        issues = []
        next_page_token = None

        while True:
            batch = self.jira.enhanced_search_issues(
                jql,
                maxResults=100,
                fields=["summary", "description", "comment"],
                expand="renderedFields",
                nextPageToken=next_page_token,
            )
            issues.extend(batch)
            print(f"Fetched {len(issues)} JIRA issues so far...")

            next_page_token = getattr(batch, "nextPageToken", None)
            if not next_page_token:
                break

        return issues

    # Description processing
    def _get_description(self, issue):
        rendered_description = ""

        if hasattr(issue, "renderedFields"):
            rendered_description = (
                getattr(issue.renderedFields, "description", "") or ""
            )

        if not rendered_description:
            raw_description = issue.fields.description
            if isinstance(raw_description, str):
                rendered_description = raw_description

        return self.strip_html_tags(rendered_description)

    def _classify_description(self, description):
        if "Description:" in description:
            return "form_with_description"

        if description.strip().startswith("Name:") or "Uid:" in description[:200]:
            return "form_metadata"

        return "free_text"

    def _extract_form_description(self, description):
        content = description.split("Description:", 1)[1]

        if "Classification:" in content:
            content = content.split("Classification:", 1)[0]

        return content.strip()

    # Comment processing
    def _extract_comments(self, issue):
        raw_comments = []
        if hasattr(issue.fields, "comment") and issue.fields.comment:
            raw_comments = issue.fields.comment.comments

        rendered_comments = []
        if hasattr(issue, "renderedFields"):
            rendered_comment_object = getattr(issue.renderedFields, "comment", None)
            if rendered_comment_object:
                rendered_comments = getattr(rendered_comment_object, "comments", [])

        comments = []
        for index, raw_comment in enumerate(raw_comments):
            author = "Unknown"
            if getattr(raw_comment, "author", None):
                author = raw_comment.author.displayName

            rendered_body = ""
            if index < len(rendered_comments):
                rendered_body = getattr(rendered_comments[index], "body", "") or ""

            if not rendered_body and isinstance(raw_comment.body, str):
                rendered_body = raw_comment.body

            body = self.strip_html_tags(rendered_body)
            if body:
                comments.append({"author": author, "body": body})

        return comments

    # PII scrubbing
    def _scrub_text(self, text):
        if not text:
            return text

        results = self.analyzer.analyze(
            text=text,
            language="en",
            entities=["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"],
        )
        text = self.anonymizer.anonymize(text=text, analyzer_results=results).text

        return self.UVA_ID_RE.sub("<UVA_ID>", text)

    # Issue processing
    def _process_issue(self, issue):
        description = self._get_description(issue)
        if not description:
            return None

        ticket_type = self._classify_description(description)
        if ticket_type == "form_metadata":
            return None

        if ticket_type == "form_with_description":
            description = self._extract_form_description(description)
            if not description:
                return None

        processed_comments = [
            {
                "author": self._scrub_text(comment["author"]),
                "body": self._scrub_text(comment["body"]),
            }
            for comment in self._extract_comments(issue)
        ]

        return {
            "key": issue.key,
            "summary": self._scrub_text((issue.fields.summary or "").strip()),
            "description": self._scrub_text(description),
            "comments": processed_comments,
        }

    # Markdown generation
    def _build_jira_markdown(self, issue):
        summary = self.clean_markdown_text(issue["summary"]).strip()
        description = self.clean_markdown_text(issue["description"]).strip()

        markdown = f"""# {summary}

## JIRA Issue

**Issue Key:** {issue["key"]}

**Source:** JIRA Technical Support Ticket

---

## User Request

{description}
"""

        if issue["comments"]:
            markdown += "\n---\n\n## Resolution and Discussion\n"

            comment_number = 1
            for comment in issue["comments"]:
                body = self.clean_markdown_text(comment["body"]).strip()
                if not body:
                    continue

                markdown += f"""

### Comment {comment_number}

{body}
"""
                comment_number += 1

        return markdown.strip() + "\n"

    def _save_jira_issue(self, issue):
        return self.save_document(
            f"jira_{issue['key']}.md",
            self._build_jira_markdown(issue),
        )

    # JIRA generation
    def generate_jira_knowledge(self, full_refresh=False):
        if full_refresh:
            print("Starting FULL JIRA knowledge generation...")
            issues = self.get_all_jira_issues()
        else:
            print("Starting incremental JIRA knowledge generation...")
            issues = self.get_recent_jira_issues()

        stats = self._new_stats()
        stats["fetched"] = len(issues)

        for issue in issues:
            try:
                processed_issue = self._process_issue(issue)
                if processed_issue is None:
                    stats["skipped"] += 1
                    continue

                result = self._save_jira_issue(processed_issue)
                self._record(stats, result)
                print(f"{result['status'].capitalize()}: {result['file'].name}")

            except Exception as error:
                stats["failed"] += 1
                print(f"Failed to process {issue.key}: {error}")

        return self.build_generation_result(**stats)

    ############################
    #       VIDEO LOGIC        #
    ############################

    def _fetch_playlist(self):
        ydl_options = {
            "extract_flat": True,
            "quiet": True,
        }
        with YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(self.VIDEO_PLAYLIST_URL, download=False)

        entries = info.get("entries", [])
        print(f"Found {len(entries)} videos in: {info.get('title', 'Unknown')}")
        return entries

    def _get_video_metadata(self, video_url):
        ydl_options = {
            "quiet": True,
            "skip_download": True,
        }
        with YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(video_url, download=False)

        return {
            "title": info.get("title", ""),
            "author": info.get("uploader", ""),
            "description": info.get("description", ""),
            "length": info.get("duration", 0),
            "publish_date": info.get("upload_date", ""),
            "webpage_url": info.get("webpage_url", video_url),
        }

    def _get_transcript(self, video_id):
        # A video without English captions is still worth keeping for its
        # title and description. Rate limits (IpBlocked/RequestBlocked) are
        # not caught here, so they still count as failures.
        try:
            transcript = YouTubeTranscriptApi().fetch(video_id, languages=["en"])
        except (NoTranscriptFound, TranscriptsDisabled):
            print(f"  No English transcript for {video_id}")
            return "No transcript available."

        return " ".join(
            entry.text.strip() for entry in transcript if entry.text.strip()
        )

    def _process_video(self, entry):
        video_url = entry["url"]
        video_id = entry.get("id", video_url.split("v=")[-1])

        return {
            "id": video_id,
            "metadata": self._get_video_metadata(video_url),
            "transcript": self._get_transcript(video_id),
        }

    def _build_video_markdown(self, video):
        metadata = video["metadata"]

        return f"""# {metadata["title"]}

## Video Information

**Source:** YouTube

**Author:** {metadata["author"]}

**URL:** {metadata["webpage_url"]}

**Publish Date:** {metadata["publish_date"]}

---

## Description

{metadata["description"]}

---

## Transcript

{video["transcript"]}
""".strip() + "\n"

    def _save_video(self, video):
        return self.save_document(
            f"youtube_{video['id']}.md",
            self._build_video_markdown(video),
        )

    # Video generation
    def generate_video_knowledge(self, max_videos=None):
        print("Starting video knowledge generation...")
        entries = self._fetch_playlist()
        if max_videos:
            entries = entries[:max_videos]

        stats = self._new_stats()
        stats["fetched"] = len(entries)

        for entry in entries:
            try:
                video = self._process_video(entry)
                result = self._save_video(video)
                self._record(stats, result)
                print(f"{result['status'].capitalize()}: {result['file'].name}")

            except Exception as error:
                stats["failed"] += 1
                print(f"Failed to process {entry.get('title', 'Unknown')}: {error}")

            time.sleep(self.VIDEO_REQUEST_DELAY)

        return self.build_generation_result(**stats)

    ############################
    #        WIKI LOGIC        #
    ############################

    @staticmethod
    def wiki_default_cookies_path():
        project_root = Path(__file__).resolve().parents[2]
        return project_root / "scrapers" / "auth_cookies.json"

    def _make_wiki_session(self, cookies_path=None):
        # Load the NetBadge cookies saved by netbadge_login()
        cookies_path = Path(
            cookies_path
            or os.getenv("WIKI_AUTH_COOKIES_PATH")
            or self.wiki_default_cookies_path()
        )
        if not cookies_path.exists():
            raise WikiAuthenticationError(
                f"{cookies_path} not found. Run netbadge_login() to create it."
            )

        session = requests.Session()
        for cookie in json.loads(cookies_path.read_text(encoding="utf-8")):
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", "").lstrip("."),
                path=cookie.get("path", "/"),
            )
        session.headers.update(
            {"User-Agent": "Mozilla/5.0 (compatible; UVA-RC-RAG-Scraper/1.0)"}
        )
        return session

    def _check_wiki_session(self, response):
        # A NetBadge page returned with HTTP 200 means the cookies died
        # mid-run. Fail loudly instead of saving login pages as documents.
        if "NetBadge" in response.text or "shibidp" in response.url:
            raise WikiAuthenticationError(
                "NetBadge redirect — cookies expired. Re-run netbadge_login()."
            )

    def _should_skip_wiki_url(self, url):
        parsed = urlparse(url)

        # "?printable" and "?revision=" map to the same filename as the plain
        # page and would overwrite it
        if parsed.query:
            return True

        if parsed.path.lower().endswith(self.SKIP_EXTENSIONS):
            return True

        return any(segment in parsed.path for segment in self.WIKI_SKIP_PATH_SEGMENTS)

    def _wiki_page_title(self, url):
        path = urlparse(url).path
        if path.startswith("/wiki/"):
            return unquote(path[len("/wiki/"):])
        return url

    def _get_wiki_page_urls(self, session):
        # _index only lists the top level. Subdirectories show up as
        # <li class="folder"> links (e.g. /wiki/Guides/) whose own listing has
        # to be expanded in turn. The front page is not listed anywhere.
        urls = [self.WIKI_BASE_URL]
        folders = [self.WIKI_INDEX_URL]
        seen_folders = set(folders)

        while folders:
            folder_url = folders.pop(0)
            response = session.get(folder_url, timeout=20)
            self._check_wiki_session(response)

            soup = BeautifulSoup(response.text, "html.parser")
            entries = soup.select("li.page, li.folder")
            if not entries:
                print(f"Could not find page listing on {folder_url}")
                continue

            for entry in entries:
                a_tag = entry.find("a", href=True)
                if not a_tag:
                    continue

                url = urljoin(folder_url, a_tag["href"]).split("#")[0]
                if "/wiki/" not in url or self._should_skip_wiki_url(url):
                    continue

                if "folder" in entry.get("class", []):
                    if url not in seen_folders:
                        seen_folders.add(url)
                        folders.append(url)
                else:
                    urls.append(url)

            time.sleep(self.WIKI_REQUEST_DELAY)

        print(f"Expanded {len(seen_folders) - 1} folders")
        return list(dict.fromkeys(urls))

    def _get_wiki_soup(self, session, url):
        response = session.get(url, timeout=20)
        if response.status_code != 200:
            return None

        self._check_wiki_session(response)

        if "text/html" not in response.headers.get("Content-Type", ""):
            return None
        return BeautifulSoup(response.text, "html.parser")

    def _fetch_wiki_page(self, session, url):
        # Returns (url, soup). Gitit's listing drops a leading space from page
        # names, so "HPC Support/ SLURM" is listed as ".../SLURM" and 404s.
        # Retry with the space put back.
        soup = self._get_wiki_soup(session, url)
        if soup is not None:
            return url, soup

        head, _, name = url.rpartition("/")
        spaced_url = f"{head}/%20{name}"
        soup = self._get_wiki_soup(session, spaced_url)
        if soup is not None:
            return spaced_url, soup

        return url, None

    def _scrub_wiki_text(self, text):
        # Wiki pages are full of module names (gcc9, cuda11) that UVA_ID_RE
        # matches, and Presidio reads command and host names as PERSON, so
        # only emails and phone numbers are scrubbed here
        if not text:
            return text

        results = self.analyzer.analyze(
            text=text,
            language="en",
            entities=["EMAIL_ADDRESS", "PHONE_NUMBER"],
        )
        return self.anonymizer.anonymize(text=text, analyzer_results=results).text

    def _wiki_soup_to_text(self, node):
        # get_text(separator="\n") would put every inline tag on its own line,
        # so mark the block boundaries first and then join with nothing
        for tag in node.find_all(self.WIKI_BLOCK_TAGS):
            tag.insert_before("\n")
            tag.insert_after("\n")

        # Keep table cells apart on the same line
        for tag in node.find_all(["td", "th"]):
            tag.insert_after(" ")

        return node.get_text()

    def _parse_wiki_page(self, url, soup, scrub_pii=True):
        # Returns {"title", "text", "source", "tags"} for one Gitit page, or None
        wikipage = soup.find(id="wikipage")
        if not wikipage:
            return None

        for element in wikipage.select("#TOC, .editsection, .footnotes"):
            element.decompose()

        for tag in wikipage.find_all("img"):
            tag.decompose()

        # Take the title from the <h1> and drop it; _build_wiki_markdown adds
        # the heading back
        title_tag = wikipage.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else self._wiki_page_title(url)
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

        # clean_markdown_text only drops .png references
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

        if scrub_pii:
            title = self._scrub_wiki_text(title)
            text = self._scrub_wiki_text(text)

        return {
            "title": title,
            "text": text,
            "source": url,
            "tags": ["wiki"] + categories,
        }

    def _build_wiki_markdown(self, page):
        title = page["title"] or "Wiki Page"
        text = page["text"]
        body = text if text.startswith("#") else f"# {title}\n\n{text}"

        return f"""{body}

---

## Wiki Page

**Source:** {page["source"]}

**Tags:** {", ".join(page["tags"])}
"""

    def _wiki_filename(self, url):
        # Named from the URL path, not the title: two pages can share an <h1>.
        # Each segment is stripped so "HPC Support/ SLURM" does not become a
        # double underscore.
        title = "/".join(part.strip() for part in self._wiki_page_title(url).split("/"))
        slug = re.sub(r"\s+", "_", title)
        slug = self.generate_sanitized_filename(slug, 150, True).strip("_")
        return f"wiki_{slug or 'index'}.md"

    # Wiki generation
    def generate_wiki_knowledge(self, cookies_path=None, max_pages=None, scrub_pii=True):
        # Needs valid NetBadge cookies. They cannot be refreshed unattended
        # (Duo push), so WikiAuthenticationError aborts the run instead of
        # being counted as a failed page.
        print("Starting wiki knowledge generation...")
        session = self._make_wiki_session(cookies_path)

        response = session.get(self.WIKI_BASE_URL, timeout=20)
        self._check_wiki_session(response)

        page_urls = self._get_wiki_page_urls(session)
        print(f"Found {len(page_urls)} wiki pages")
        if max_pages:
            page_urls = page_urls[:max_pages]

        stats = self._new_stats()
        stats["fetched"] = len(page_urls)

        for index, url in enumerate(page_urls, 1):
            print(f"  [{index}/{len(page_urls)}] {self._wiki_page_title(url) or '(front page)'}")
            try:
                url, soup = self._fetch_wiki_page(session, url)
                if soup is None:
                    print("    Skipped (page not found)")
                    stats["skipped"] += 1
                    continue

                page = self._parse_wiki_page(url, soup, scrub_pii)
                if page is None:
                    print("    Skipped (no content)")
                    stats["skipped"] += 1
                    continue

                result = self.save_document(
                    self._wiki_filename(url), self._build_wiki_markdown(page)
                )
                self._record(stats, result)
                print(f"    {result['status'].capitalize()}: {result['file'].name}")

            except WikiAuthenticationError:
                raise

            except Exception as error:
                stats["failed"] += 1
                print(f"    Failed: {error}")

            time.sleep(self.WIKI_REQUEST_DELAY)

        return self.build_generation_result(**stats)


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
                if page.locator("#wikipage").count() > 0:
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

                    # A rejected password re-renders the NetBadge form, so
                    # without this the Duo wait below would just time out
                    if "Incorrect computing ID or password" in page.content():
                        raise WikiAuthenticationError(
                            "Incorrect computing ID or password."
                        )
                    break

            # Step 2: Duo two-factor

            if page.locator("#wikipage").count() == 0:
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
