import os
import re
import time
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import cloudscraper
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from jira import JIRA
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine

load_dotenv()


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
