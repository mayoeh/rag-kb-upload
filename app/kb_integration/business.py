import json
import os
import re

from html import unescape
from pathlib import Path

import requests
from dotenv import load_dotenv
from jira import JIRA
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine

import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import cloudscraper
from bs4 import BeautifulSoup

from yt_dlp import YoutubeDL
from youtube_transcript_api import YouTubeTranscriptApi


load_dotenv()

class UVARCLocalKnowledgeDataManager:

    def __init__(self, output_folder):
        self.output_folder = Path(output_folder)
        self.output_folder.mkdir(
            parents=True,
            exist_ok=True,
        )

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
        existing_content = file_path.read_text(
            encoding="utf-8"
        )

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

# Jira Knowledge
class UVARCJiraKnowledgeDataManager:
    # Handles all generation of JIRA knowledge base

    UVA_ID_RE = re.compile(
        r"\b[a-z]{2,4}\d{1,2}[a-z0-9]{1,3}\b",
        re.IGNORECASE,
    )

    def __init__(self, output_folder):
        self.output_folder = Path(output_folder)

        self.local_documents = (
            UVARCLocalKnowledgeDataManager(
                output_folder
            )
        )

        self.server = os.getenv("JIRA_SERVER")
        self.email = os.getenv("JIRA_EMAIL")
        self.api_token = os.getenv("JIRA_API_TOKEN")
        self.project_key = os.getenv("JIRA_PROJECT_KEY")

        self.jira = JIRA(
            server=self.server,
            basic_auth=(
                self.email,
                self.api_token,
            ),
        )

        self.analyzer = AnalyzerEngine()
        self.anonymizer = AnonymizerEngine()

    # JIRA retrieval

    def get_resolved_issues(self):
        jql = (
            f"project = {self.project_key} "
            "AND status = Resolved "
            'AND created >= "2026-08-19" '
            'AND created < "2026-08-26" '
            "ORDER BY created DESC"
        )

        return self._fetch_issues(jql)

    def _fetch_issues(self, jql):

        issues = []
        next_page_token = None

        while True:
            batch = self.jira.enhanced_search_issues(
                jql,
                maxResults=100,
                fields=[
                    "summary",
                    "description",
                    "comment",
                ],
                expand="renderedFields",
                nextPageToken=next_page_token,
            )

            issues.extend(batch)

            print(
                f"Fetched {len(issues)} JIRA issues so far..."
            )

            next_page_token = getattr(
                batch,
                "nextPageToken",
                None,
            )

            if not next_page_token:
                break

        return issues

    # Description processing

    def _get_description(self, issue):

        rendered_description = ""

        if hasattr(issue, "renderedFields"):
            rendered_description = (
                getattr(
                    issue.renderedFields,
                    "description",
                    "",
                )
                or ""
            )

        if not rendered_description:
            raw_description = issue.fields.description

            if isinstance(raw_description, str):
                rendered_description = raw_description

        return self._html_to_text(
            rendered_description
        )

    def _classify_description(self, description):
        """
        Determine which known JIRA description format is used.

        form_with_description:
            Structured form containing a useful Description field.

        form_metadata:
            Form containing primarily identifying/form metadata.
            These tickets are skipped.

        free_text:
            Normal free-text support request.
        """

        if (
            "Description: " in description
            or "Description:" in description
        ):
            return "form_with_description"

        if (
            description.strip().startswith("Name:")
            or "Uid:" in description[:200]
        ):
            return "form_metadata"

        return "free_text"

    def _extract_form_description(self, description):

        if "Description: " in description:
            content = description.split(
                "Description: ",
                1,
            )[1]
        else:
            content = description.split(
                "Description:",
                1,
            )[1]

        for cutoff in [
            "Classification: ",
            "Classification:",
        ]:
            if cutoff in content:
                content = content.split(
                    cutoff,
                    1,
                )[0]

        return content.strip()

    # Comment processing

    def _extract_comments(self, issue):

        raw_comments = []

        if (
            hasattr(issue.fields, "comment")
            and issue.fields.comment
        ):
            raw_comments = (
                issue.fields.comment.comments
            )

        rendered_comments = []

        if hasattr(issue, "renderedFields"):
            rendered_comment_object = getattr(
                issue.renderedFields,
                "comment",
                None,
            )

            if rendered_comment_object:
                rendered_comments = getattr(
                    rendered_comment_object,
                    "comments",
                    [],
                )

        comments = []

        for index, raw_comment in enumerate(
            raw_comments
        ):
            author = "Unknown"

            if (
                hasattr(raw_comment, "author")
                and raw_comment.author
            ):
                author = (
                    raw_comment.author.displayName
                )

            rendered_body = ""

            if index < len(rendered_comments):
                rendered_body = (
                    getattr(
                        rendered_comments[index],
                        "body",
                        "",
                    )
                    or ""
                )

            if not rendered_body:
                if isinstance(
                    raw_comment.body,
                    str,
                ):
                    rendered_body = raw_comment.body

            body = self._html_to_text(
                rendered_body
            )

            if body:
                comments.append(
                    {
                        "author": author,
                        "body": body,
                    }
                )

        return comments

    # Text cleanup / PII

    def _html_to_text(self, html):
        if not html:
            return ""

        text = re.sub(
            r"<br\s*/?>",
            "\n",
            html,
        )

        text = re.sub(
            r"</p>",
            "\n",
            text,
        )

        text = re.sub(
            r"</li>",
            "\n",
            text,
        )

        text = re.sub(
            r"<[^>]+>",
            "",
            text,
        )

        text = unescape(text)

        text = re.sub(
            r"\n{3,}",
            "\n\n",
            text,
        )

        return text.strip()

    def _scrub_text(self, text):

        if not text:
            return text

        results = self.analyzer.analyze(
            text=text,
            language="en",
            entities=[
                "PERSON",
                "EMAIL_ADDRESS",
                "PHONE_NUMBER",
            ],
        )

        text = self.anonymizer.anonymize(
            text=text,
            analyzer_results=results,
        ).text

        text = self.UVA_ID_RE.sub(
            "<UVA_ID>",
            text,
        )

        return text

    def _clean_markdown_text(self, text):

        if not text:
            return ""

        text = text.strip()

        text = re.sub(
            r"\n{3,}",
            "\n\n",
            text,
        )

        return text

    # Issue processing

    def _process_issue(self, issue):

        description = self._get_description(
            issue
        )

        if not description:
            return None

        ticket_type = (
            self._classify_description(
                description
            )
        )

        # Type 2 tickets contain primarily form metadata
        if ticket_type == "form_metadata":
            return None

        if ticket_type == "form_with_description":
            description = (
                self._extract_form_description(
                    description
                )
            )

        if not description:
            return None

        comments = self._extract_comments(
            issue
        )

        processed_comments = []

        for comment in comments:
            processed_comments.append(
                {
                    "author": self._scrub_text(
                        comment["author"]
                    ),
                    "body": self._scrub_text(
                        comment["body"]
                    ),
                }
            )

        return {
            "key": issue.key,
            "summary": self._scrub_text(
                (
                    issue.fields.summary
                    or ""
                ).strip()
            ),
            "description": self._scrub_text(
                description
            ),
            "comments": processed_comments,
        }

    # Markdown generation

    def _build_markdown(self, issue):


        summary = self._clean_markdown_text(
            issue["summary"]
        )

        description = (
            self._clean_markdown_text(
                issue["description"]
            )
        )

        markdown = f"""# {summary}

## JIRA Issue

**Issue Key:** {issue["key"]}

**Source:** JIRA Technical Support Ticket

---

## User Request

{description}
"""

        if issue["comments"]:
            markdown += (
                "\n---\n\n"
                "## Resolution and Discussion\n"
            )

            comment_number = 1

            for comment in issue["comments"]:
                body = self._clean_markdown_text(
                    comment["body"]
                )

                if not body:
                    continue

                markdown += f"""

### Comment {comment_number}

{body}
"""

                comment_number += 1

        return markdown.strip() + "\n"

    def _write_markdown(self, issue):

        filename = f"jira_{issue['key']}.md"

        markdown = self._build_markdown(issue)

        return self.local_documents.save_document(
            filename,
            markdown,
        )

    def generate_knowledge_documents(self):
        issues = self.get_resolved_issues()

        created = 0
        updated = 0
        unchanged = 0
        skipped = 0
        failed = 0

        changed_files = []

        for issue in issues:
            try:
                processed_issue = self._process_issue(
                    issue
                )

                if processed_issue is None:
                    skipped += 1
                    continue

                result = self._write_markdown(
                    processed_issue
                )

                status = result["status"]
                file_path = result["file"]

                if status == "created":
                    created += 1
                    changed_files.append(file_path)

                elif status == "updated":
                    updated += 1
                    changed_files.append(file_path)

                elif status == "unchanged":
                    unchanged += 1

                print(
                    f"{status.capitalize()}: "
                    f"{file_path.name}"
                )

            except Exception as error:
                failed += 1

                print(
                    f"Failed to process "
                    f"{issue.key}: {error}"
                )

        return build_generation_result(
            fetched=len(issues),
            created=created,
            updated=updated,
            unchanged=unchanged,
            skipped=skipped,
            failed=failed,
            changed_files=changed_files,
        )


class UVARCWebsiteKnowledgeDataManager:
    SKIP_EXTENSIONS = (
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", 
        ".pdf", ".zip", ".tar", ".gz", ".mp4", ".webp"
    )
    ALLOWED_NETLOCS = {
        "rc.virginia.edu", 
        "learning.rc.virginia.edu", 
        "archive.rc.virginia.edu"
    }
    SKIP_PATTERNS = ['/author/', '/category/', '/tag/']

    def __init__(self, output_folder):
        self.scraper = cloudscraper.create_scraper()
        self.visited = set()
        self.documents = {}
        self.output_folder = Path(output_folder)

        self.local_documents = (
            UVARCLocalKnowledgeDataManager(
                output_folder
            )
        )

    def is_valid(self, url):
        parsed = urlparse(url)
        path = parsed.path.lower()
        return (
            parsed.scheme in {"http", "https"}
            and parsed.netloc in self.ALLOWED_NETLOCS
            and not path.endswith(self.SKIP_EXTENSIONS)
        )

    def crawl(self, url, netloc=None):
        if url in self.visited:
            return self.documents
        self.visited.add(url)

        netloc = netloc or urlparse(url).netloc

        try:
            response = self.scraper.get(url, timeout=15)

            if response.url != url:
                url = response.url
                if url in self.visited:
                    return self.documents
                self.visited.add(url)

            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                return self.documents
            if response.status_code != 200:
                return self.documents

            soup = BeautifulSoup(response.text, "html.parser")

            if len(articles := soup.find_all("article")) != 1:
                print(f"Skipping {url} (no single article)")
            else:
                article_soup = BeautifulSoup(str(articles[0]), "html.parser")

                for tag in article_soup.find_all("img"):
                    tag.decompose()

                return_link = article_soup.find("a", string=re.compile(r"^\u00ab Return to"))
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

                text = article_soup.get_text()
                text = re.sub(r"https?:\/\/\S+?\.png", "", text)
                text = re.sub(r"\S+\.png", "", text)
                text = re.sub(r"\n{3,}", "\n\n", text)
                text = re.sub(r"[ \t]+", " ", text)

                self.documents[url] = {
                    "text": text.strip(),
                    "source": url,
                }
                print(f"Extracted: {url}")

            for a_tag in soup.find_all("a", href=True):
                next_url = a_tag["href"]
                if not next_url.startswith(("http://", "https://")):
                    next_url = urljoin(url, next_url)
                next_url = next_url.split("#")[0]

                if next_url not in self.visited and self.is_valid(next_url):
                    self.crawl(next_url, netloc)

            time.sleep(0.2)

        except Exception as e:
            print(f"Failed to crawl {url}: {e}")

        return self.documents

    def get_sitemap_urls(self, sitemap_url, skip_patterns=None):
        skip_patterns = skip_patterns or []
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        print(f"Fetching sitemap: {sitemap_url}")
        resp = self.scraper.get(sitemap_url, timeout=15)
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

    def extract_article(self, url):
        response = self.scraper.get(url, timeout=15)
        if response.status_code != 200 or 'text/html' not in response.headers.get('Content-Type', ''):
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        articles = soup.find_all('article')
        if len(articles) != 1:
            return None
            
        article_soup = BeautifulSoup(str(articles[0]), 'html.parser')
        
        for tag in article_soup.find_all('img'):
            tag.decompose()
            
        metadata_tag = article_soup.find('p', class_='blog-post-meta')
        
        for a_tag in article_soup.find_all('a', href=True):
            href = a_tag['href']
            if not href.startswith('http'):
                href = urljoin(url, href)
            a_tag['href'] = href
            a_tag.string = '[' + a_tag.get_text(strip=True) + '](' + href + ')'
            
        if metadata_tag:
            metadata_tag.decompose()
            
        text = article_soup.get_text()
        text = re.sub(r'https?:\/\/\S+?\.png', '', text)
        text = re.sub(r'\S+\.png', '', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]+', ' ', text)
        
        return {'text': text.strip(), 'source': url}

    def fill_sitemap_gaps(self):
        rc_urls = self.get_sitemap_urls('https://rc.virginia.edu/sitemap.xml')
        learn_urls = self.get_sitemap_urls('https://learning.rc.virginia.edu/sitemap.xml', self.SKIP_PATTERNS)
        all_sitemap_urls = rc_urls + learn_urls

        missing = [u for u in all_sitemap_urls if u not in self.documents]
        print(f"Sitemap total: {len(all_sitemap_urls)}, already crawled: {len(all_sitemap_urls)-len(missing)}, missing: {len(missing)}")

        for url in missing:
            try:
                result = self.extract_article(url)
                if result:
                    self.documents[url] = result
                    print(f"Added: {url}")
                else:
                    print(f"Skipped (no article): {url}")
                time.sleep(0.2)
            except Exception as e:
                print(f"Failed {url}: {e}")

        print(f"\nTotal documents after gap fill: {len(self.documents)}")

    def patch_js_rendered_pages(self):
        manual_patches = {
            "https://rc.virginia.edu/userinfo/hpc/slurm-script-generator/": {
                "text": "# Slurm Script Generator\n\nThe UVA Research Computing Slurm Script Generator...",
                "source": "https://rc.virginia.edu/userinfo/hpc/slurm-script-generator/",
            },
            "https://rc.virginia.edu/userinfo/hpc/software/physics/": {
                "text": "# Physics Software on UVA HPC\n\nUVA Research Computing provides several physics...",
                "source": "https://rc.virginia.edu/userinfo/hpc/software/physics/",
            }
        }

        patched = 0
        for url, doc in manual_patches.items():
            if url not in self.documents or len(self.documents.get(url, {}).get("text", "")) < 100:
                self.documents[url] = doc
                patched += 1
                print(f"Patched: {url}")
            else:
                print(f"Already has content: {url}")

        print(f"\nManually patched {patched} JS-rendered pages.")
        print(f"Total documents: {len(self.documents)}")

    @staticmethod
    def safe_filename(url):
        filename = re.sub(r'[^a-zA-Z0-9_\-]', '_', url)
        filename = re.sub(r'_+', '_', filename).strip('_')
        return filename[:150]

    def generate_markdown_files(self):
        print(
            f"Documents available: "
            f"{len(self.documents)}"
        )

        print(
            f"Writing files to: "
            f"{self.output_folder}"
        )

        created = 0
        updated = 0
        unchanged = 0
        skipped = 0
        failed = 0

        changed_files = []

        for url, doc in self.documents.items():
            try:
                filename = (
                    f"{self.safe_filename(url)}.md"
                )

                result = (
                    self.local_documents.save_document(
                        filename,
                        doc["text"],
                    )
                )

                status = result["status"]
                file_path = result["file"]

                if status == "created":
                    created += 1
                    changed_files.append(file_path)

                elif status == "updated":
                    updated += 1
                    changed_files.append(file_path)

                elif status == "unchanged":
                    unchanged += 1

                print(
                    f"{status.capitalize()}: "
                    f"{file_path.name}"
                )

            except Exception as e:
                failed += 1

                print(
                    f"ERROR processing {url}: {e}"
                )

        return build_generation_result(
            fetched=len(self.documents),
            created=created,
            updated=updated,
            unchanged=unchanged,
            skipped=skipped,
            failed=failed,
            changed_files=changed_files,
        )

    def generate_knowledge_documents(self):
        print("Starting knowledge file generation...")

        self.crawl(
            "https://rc.virginia.edu/"
        )

        self.crawl(
            "https://learning.rc.virginia.edu/"
        )

        self.fill_sitemap_gaps()
        self.patch_js_rendered_pages()

        return self.generate_markdown_files()


class UVARCVideoKnowledgeDataManager:

    PLAYLIST_URL = (
        "https://www.youtube.com/playlist"
        "?list=PLT4bryHgBcRP7N-hB9u6EWs6tq_2nMoRO"
    )

    def __init__(self, output_folder):
        self.output_folder = Path(output_folder)

        self.local_documents = (
            UVARCLocalKnowledgeDataManager(
                output_folder
            )
        )

    def _fetch_playlist(self):
        ydl_options = {
            "extract_flat": True,
            "quiet": True,
        }

        with YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(
                self.PLAYLIST_URL,
                download=False,
            )

        entries = info.get("entries", [])

        print(
            f"Found {len(entries)} videos in: "
            f"{info.get('title', 'Unknown')}"
        )

        return entries

    def _get_video_metadata(self, video_url):

        ydl_options = {
            "quiet": True,
            "skip_download": True,
        }

        with YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(
                video_url,
                download=False,
            )

        return {
            "title": info.get("title", ""),
            "author": info.get("uploader", ""),
            "description": info.get(
                "description",
                "",
            ),
            "length": info.get("duration", 0),
            "publish_date": info.get(
                "upload_date",
                "",
            ),
            "webpage_url": info.get(
                "webpage_url",
                video_url,
            ),
        }

    def _get_transcript(self, video_id):

        api = YouTubeTranscriptApi()

        transcript = api.fetch(
            video_id,
            languages=["en"],
        )

        return " ".join(
            entry.text.strip()
            for entry in transcript
            if entry.text.strip()
        )

    def _process_video(self, entry):

        video_url = entry["url"]

        video_id = entry.get(
            "id",
            video_url.split("v=")[-1],
        )

        metadata = self._get_video_metadata(
            video_url
        )

        transcript = self._get_transcript(
            video_id
        )

        return {
            "id": video_id,
            "metadata": metadata,
            "transcript": transcript,
        }
    
    def _safe_filename(self, filename):

        filename = re.sub(
            r'[<>:"/\\|?*]',
            "_",
            filename,
        )

        filename = re.sub(
            r"\s+",
            " ",
            filename,
        ).strip()

        return filename[:150]

    def _build_markdown(self, video):

        metadata = video["metadata"]
        transcript = video["transcript"]

        markdown = f"""# {metadata["title"]}

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

            {transcript}
            """

        return markdown.strip() + "\n"

    def _write_markdown(self, video):

        filename = (
            f"youtube_{video['id']}.md"
        )

        markdown = self._build_markdown(video)

        return self.local_documents.save_document(
            filename,
            markdown,
        )

    def generate_knowledge_documents(self):

        entries = self._fetch_playlist()

        created = 0
        updated = 0
        unchanged = 0
        skipped = 0
        failed = 0

        changed_files = []

        for entry in entries:
            try:
                print(
                    f"Loading: "
                    f"{entry.get('title', 'Unknown')}"
                )

                video = self._process_video(
                    entry
                )

                result = self._write_markdown(
                    video
                )

                status = result["status"]
                file_path = result["file"]

                if status == "created":
                    created += 1
                    changed_files.append(file_path)

                elif status == "updated":
                    updated += 1
                    changed_files.append(file_path)

                elif status == "unchanged":
                    unchanged += 1

                print(
                    f"{status.capitalize()}: "
                    f"{file_path.name}"
                )

            except Exception as error:
                failed += 1

                print(
                    f"Failed to process "
                    f"{entry.get('title', 'Unknown')}: "
                    f"{error}"
                )

        return build_generation_result(
            fetched=len(entries),
            created=created,
            updated=updated,
            unchanged=unchanged,
            skipped=skipped,
            failed=failed,
            changed_files=changed_files,
        )

class UVARCMarkdownKnowledgeDataManager:

    GITHUB_REPO = "uvarc/rc-learning"
    GITHUB_BRANCH = "main"
    CONTENT_PATH = "content"

    def __init__(self, output_folder):
        self.output_folder = Path(output_folder)

        self.local_documents = (
            UVARCLocalKnowledgeDataManager(
                output_folder
            )
        )

    def _fetch_repository_tree(self):
        tree_url = (
            f"https://api.github.com/repos/"
            f"{self.GITHUB_REPO}/git/trees/"
            f"{self.GITHUB_BRANCH}?recursive=1"
        )

        response = requests.get(
            tree_url,
            timeout=30,
        )

        response.raise_for_status()

        return response.json().get("tree", [])

    def _get_markdown_files(self, tree):
        return [
            item
            for item in tree
            if item["type"] == "blob"
            and item["path"].startswith(
                self.CONTENT_PATH + "/"
            )
            and item["path"].endswith(".md")
        ]

    def _fetch_markdown_file(self, path):
        raw_url = (
            f"https://raw.githubusercontent.com/"
            f"{self.GITHUB_REPO}/"
            f"{self.GITHUB_BRANCH}/"
            f"{path}"
        )

        response = requests.get(
            raw_url,
            timeout=15,
        )

        response.raise_for_status()

        return response.text

    def _is_draft(self, text):
        # TOML frontmatter
        if text.startswith("+++"):
            end = text.find("+++", 3)

            if end != -1:
                frontmatter = text[3:end]

                if "draft = true" in frontmatter:
                    return True

        # YAML frontmatter
        if text.startswith("---"):
            end = text.find("---", 3)

            if end != -1:
                frontmatter = text[3:end]

                if "draft: true" in frontmatter:
                    return True

        return False

    def _safe_filename(self, filename):
        filename = re.sub(
            r'[<>:"\\|?*]',
            "_",
            filename,
        )

        filename = re.sub(
            r"\s+",
            " ",
            filename,
        ).strip()

        return filename[:150]

    def _build_filename(self, source):
        relative_path = source.split(
            f"{self.CONTENT_PATH}/",
            1,
        )[-1]

        flat_name = self._safe_filename(
            relative_path
            .removesuffix(".md")
            .replace("/", "_")
        )

        return f"{flat_name}.md"

    def _write_markdown(self, document):
        filename = self._build_filename(
            document["source"]
        )

        return self.local_documents.save_document(
            filename,
            document["content"],
        )

    def generate_knowledge_documents(self):
        tree = self._fetch_repository_tree()

        markdown_files = self._get_markdown_files(
            tree
        )

        print(
            f"Found {len(markdown_files)} "
            f"Markdown files in "
            f"{self.GITHUB_REPO}/{self.CONTENT_PATH}"
        )

        documents = []

        created = 0
        updated = 0
        unchanged = 0
        skipped = 0
        failed = 0

        changed_files = []

        for item in markdown_files:
            path = item["path"]

            try:
                text = self._fetch_markdown_file(
                    path
                )

                if self._is_draft(text):
                    print(
                        f"Skipping draft: {path}"
                    )

                    skipped += 1
                    continue

                documents.append({
                    "source": path,
                    "content": text,
                })

            except Exception as error:
                failed += 1

                print(
                    f"Failed to fetch {path}: "
                    f"{error}"
                )

        # Deduplicate identical content
        unique_documents = {
            document["content"]: document
            for document in documents
        }

        duplicate_count = (
            len(documents)
            - len(unique_documents)
        )

        skipped += duplicate_count

        documents = list(
            unique_documents.values()
        )

        print(
            f"Loaded {len(documents)} "
            f"documents after deduplication"
        )

        for document in documents:
            try:
                result = self._write_markdown(
                    document
                )

                status = result["status"]
                file_path = result["file"]

                if status == "created":
                    created += 1
                    changed_files.append(file_path)

                elif status == "updated":
                    updated += 1
                    changed_files.append(file_path)

                elif status == "unchanged":
                    unchanged += 1

                print(
                    f"{status.capitalize()}: "
                    f"{file_path.name}"
                )

            except Exception as error:
                failed += 1

                print(
                    f"Failed to write "
                    f"{document['source']}: "
                    f"{error}"
                )

        return build_generation_result(
            fetched=len(markdown_files),
            created=created,
            updated=updated,
            unchanged=unchanged,
            skipped=skipped,
            failed=failed,
            changed_files=changed_files,
        )

# Open WebUI Knowledge Base

class UVARCKnowledgeBaseManager:

    def __init__(self):
        self.open_webui_url = os.getenv(
            "OPENWEBUI_URL"
        )

        self.api_key = os.getenv(
            "OPENWEBUI_API_KEY"
        )

        self.knowledge_base_id = os.getenv(
            "OPENWEBUI_KB_ID"
        )

        self.headers = {
            "Authorization": (
                f"Bearer {self.api_key}"
            )
        }

    def upload_file(self, file_path):

        file_path = Path(file_path)

        print(
            f"Uploading: {file_path.name}"
        )

        try:
            with open(file_path, "rb") as file:
                response = requests.post(
                    (
                        f"{self.open_webui_url}"
                        "/api/v1/files/"
                    ),
                    headers=self.headers,
                    files={
                        "file": (
                            file_path.name,
                            file,
                            "text/plain",
                        )
                    },
                    data={
                        "metadata": "{}"
                    },
                )

            if response.status_code not in (
                200,
                201,
            ):
                print(
                    "  ERROR uploading file "
                    f"(HTTP "
                    f"{response.status_code})"
                )

                print(
                    f"  {response.text}"
                )

                return None

            result = response.json()

            file_id = result.get("id")

            print(
                "  Uploaded successfully."
            )

            print(
                f"  File ID: {file_id}"
            )

            return file_id

        except Exception as error:
            print(
                f"  ERROR: {error}"
            )

            return None

    def add_file_to_knowledge_base(
        self,
        file_id,
        file_name,
    ):

        print(
            f"  Adding {file_name} "
            "to Knowledge Base..."
        )

        try:
            response = requests.post(
                (
                    f"{self.open_webui_url}"
                    "/api/v1/knowledge/"
                    f"{self.knowledge_base_id}"
                    "/file/add"
                ),
                headers={
                    **self.headers,
                    "Content-Type":
                        "application/json",
                },
                json={
                    "file_id": file_id
                },
            )

            if response.status_code not in (
                200,
                201,
            ):
                print(
                    "  ERROR adding to "
                    "Knowledge Base "
                    f"(HTTP "
                    f"{response.status_code})"
                )

                print(
                    f"  {response.text}"
                )

                return False

            print(
                "  Added to Knowledge Base "
                "successfully."
            )

            return True

        except Exception as error:
            print(
                f"  ERROR: {error}"
            )

            return False

# RCI Wiki Knowledge
class WikiAuthenticationError(RuntimeError):
    """Raised when the saved NetBadge cookies are missing or expired.

    Refreshing them needs a Duo push approved on a phone, so this can never
    be recovered from automatically. Callers should surface it as an alert
    rather than retry.
    """


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
    used the async API). Intended to be driven by scripts/save_wiki_auth.py,
    where a human is present to approve the Duo push.

    When `debug_dir` is given, the Duo page and any timeout are captured as
    screenshots there. Duo can block headless browsers outright, and the
    screenshot is usually the only way to see why.
    """

    from playwright.sync_api import sync_playwright

    target = target or UVARCWikiKnowledgeDataManager.LOGIN_TARGET
    output = Path(output or UVARCWikiKnowledgeDataManager.default_cookies_path())

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


class UVARCWikiKnowledgeDataManager:
    # Handles all generation of RCI wiki knowledge base

    BASE_URL = "https://rci.hpc.virginia.edu/wiki/"
    INDEX_URL = "https://rci.hpc.virginia.edu/wiki/_index"
    LOGIN_TARGET = (
        "https://rci.hpc.virginia.edu/wiki/Special:AllPages"
    )

    SKIP_EXTENSIONS = (
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg",
        ".pdf", ".zip", ".tar", ".gz", ".mp4", ".webp",
    )

    # Gitit special-path prefixes and wiki sections we never want indexed
    SKIP_PATH_SEGMENTS = (
        "/_edit/", "/_history/", "/_discuss/", "/_delete/",
        "/_login", "/_logout", "/_index", "/_categories",
        "/_random", "/_search", "/_upload", "/_activity",
        "/_export",
        "/OldWiki", "/OldOldWiki",
    )

    REQUEST_DELAY = 0.3

    UVA_ID_RE = re.compile(
        r"\b[a-z]{2,4}\d{1,2}[a-z0-9]{1,3}\b",
        re.IGNORECASE,
    )

    CLEAN_RE = [
        (
            re.compile(
                r"https?://\S+?\.(?:png|jpg|jpeg|gif|svg|webp)"
            ),
            "",
        ),
        (
            re.compile(
                r"\S+\.(?:png|jpg|jpeg|gif|svg|webp)"
            ),
            "",
        ),
        (re.compile(r"\n{3,}"), "\n\n"),
        (re.compile(r"[ \t]+"), " "),
    ]

    def __init__(
        self,
        output_folder,
        cookies_path=None,
        request_delay=None,
        crawl_links_enabled=True,
        scrub_pii=True,
        scrub_uva_ids=False,
        max_pages=None,
    ):
        self.output_folder = Path(output_folder)

        self.local_documents = (
            UVARCLocalKnowledgeDataManager(
                output_folder
            )
        )

        self.cookies_path = Path(
            cookies_path
            or os.getenv("WIKI_AUTH_COOKIES_PATH")
            or self.default_cookies_path()
        )

        self.request_delay = (
            self.REQUEST_DELAY
            if request_delay is None
            else request_delay
        )

        self.crawl_links_enabled = crawl_links_enabled

        # Stop after this many pages. For smoke-testing against the live
        # wiki without pulling every page; None means no limit.
        self.max_pages = max_pages

        # Wiki pages are full of module names and shell snippets that the
        # UVA-ID pattern matches (gcc9, cuda11, abc123), so ID scrubbing is
        # opt-in here even though JIRA always applies it.
        self.scrub_pii = scrub_pii
        self.scrub_uva_ids = scrub_uva_ids

        self.analyzer = None
        self.anonymizer = None

        self.session = None
        self.documents = {}

    @staticmethod
    def default_cookies_path():
        project_root = Path(__file__).resolve().parents[2]
        return project_root / "scrapers" / "auth_cookies.json"

    # Authentication

    def load_cookies(self):

        if not self.cookies_path.exists():
            raise WikiAuthenticationError(
                f"{self.cookies_path} not found. "
                "Run scripts/save_wiki_auth.py to create it."
            )

        with open(self.cookies_path) as file:
            return json.load(file)

    def make_session(self):

        cookies = self.load_cookies()

        session = requests.Session()

        for cookie in cookies:
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", "").lstrip("."),
                path=cookie.get("path", "/"),
            )

        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; "
                    "UVA-RC-RAG-Scraper/1.0)"
                )
            }
        )

        print(f"Loaded {len(cookies)} cookies")

        self.session = session

        return session

    def check_session(self, response):
        # A NetBadge page returned with HTTP 200 means the cookies died
        # mid-run. Fail loudly instead of writing login pages to disk.

        if (
            "NetBadge" in response.text
            or "shibidp" in response.url
        ):
            raise WikiAuthenticationError(
                "NetBadge redirect — cookies expired. "
                "Re-run scripts/save_wiki_auth.py."
            )

    def verify_session(self):

        response = self.session.get(
            self.BASE_URL,
            timeout=20,
        )

        self.check_session(response)

        print(
            "Session OK — wiki reachable "
            f"(HTTP {response.status_code})"
        )

        return True

    # URL helpers

    def should_skip_url(self, url):

        path = urlparse(url).path

        if path.lower().endswith(self.SKIP_EXTENSIONS):
            return True

        return any(
            segment in path
            for segment in self.SKIP_PATH_SEGMENTS
        )

    def page_title_from_url(self, url):

        path = urlparse(url).path

        if path.startswith("/wiki/"):
            return unquote(path[len("/wiki/"):])

        return url

    # Text cleanup / PII

    def clean_text(self, text):

        for pattern, replacement in self.CLEAN_RE:
            text = pattern.sub(replacement, text)

        return text.strip()

    def scrub_text(self, text):

        if not text or not self.scrub_pii:
            return text

        if self.analyzer is None:
            self.analyzer = AnalyzerEngine()
            self.anonymizer = AnonymizerEngine()

        results = self.analyzer.analyze(
            text=text,
            language="en",
            entities=[
                # PERSON is off for the wiki. JIRA tickets are prose, but
                # these pages are technical docs, where presidio reads
                # module, command and host names as people's names.
                # "PERSON",
                "EMAIL_ADDRESS",
                "PHONE_NUMBER",
            ],
        )

        text = self.anonymizer.anonymize(
            text=text,
            analyzer_results=results,
        ).text

        if self.scrub_uva_ids:
            text = self.UVA_ID_RE.sub(
                "<UVA_ID>",
                text,
            )

        return text

    # Page retrieval

    def get_all_page_urls(self):
        # Every content page listed on /wiki/_index

        response = self.session.get(
            self.INDEX_URL,
            timeout=20,
        )

        self.check_session(response)

        soup = BeautifulSoup(response.text, "html.parser")

        content = (
            soup.find(id="wikipage")
            or soup.find(id="content")
        )

        if not content:
            print(
                "[ERROR] Could not find page listing on _index"
            )
            return []

        urls = []

        for anchor in content.find_all("a", href=True):
            if anchor.get_text(strip=True) == "(delete)":
                continue

            full_url = urljoin(
                self.BASE_URL,
                anchor["href"],
            ).split("#")[0]

            if (
                not self.should_skip_url(full_url)
                and "/wiki/" in full_url
            ):
                urls.append(full_url)

        # Deduplicate while preserving discovery order
        return list(dict.fromkeys(urls))

    def _get_soup(self, url):

        response = self.session.get(url, timeout=20)

        if response.status_code != 200:
            return None

        self.check_session(response)

        if "text/html" not in response.headers.get(
            "Content-Type",
            "",
        ):
            return None

        return BeautifulSoup(response.text, "html.parser")

    # Tags that should end a line. Everything else — <code>, <strong>, <a>,
    # <em>, <tt> — has to stay inline, or sentences get chopped up around
    # every command name, which is most of this wiki.
    BLOCK_TAGS = (
        "p", "div", "br", "hr",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "ul", "ol", "dl", "dt", "dd",
        "pre", "blockquote",
        "table", "tr", "thead", "tbody",
        "section", "article", "header", "footer",
    )

    def _soup_to_text(self, node):
        # get_text(separator="\n") would break inline tags onto their own
        # lines, so mark up the block boundaries first and then join with
        # nothing.

        for tag in node.find_all(self.BLOCK_TAGS):
            tag.insert_before("\n")
            tag.insert_after("\n")

        # Keep table cells apart on the same line.
        for tag in node.find_all(["td", "th"]):
            tag.insert_after(" ")

        return node.get_text()

    def _parse_page(self, url, soup):
        # Turn one Gitit page into a knowledge document.
        # Shared by the _index pass and the link-crawl pass so both
        # produce identically formatted Markdown.

        wikipage = soup.find(id="wikipage")

        if not wikipage:
            return None

        for element in wikipage.select(
            "#TOC, .editsection, .footnotes"
        ):
            element.decompose()

        for image in wikipage.find_all("img"):
            image.decompose()

        # Take the title from the <h1>, then drop it. build_markdown adds the
        # heading back, so leaving it here repeats it in every document.
        title_tag = wikipage.find("h1")

        title = (
            title_tag.get_text(strip=True)
            if title_tag
            else self.page_title_from_url(url)
        )

        if title_tag:
            title_tag.decompose()

        # Rewrite links as absolute Markdown links
        for anchor in wikipage.find_all("a", href=True):
            href = anchor["href"]

            if not href.startswith("http"):
                href = urljoin(url, href)

            anchor["href"] = href

            text = anchor.get_text(strip=True)

            if text:
                anchor.string = f"[{text}]({href})"

        text = self.clean_text(
            self._soup_to_text(wikipage)
        )

        if not text:
            return None

        category_div = (
            soup.find(id="categories")
            or soup.find(class_="categories")
        )

        categories = []

        if category_div:
            categories = [
                anchor.get_text(strip=True)
                for anchor in category_div.find_all("a")
            ]

        return {
            "text": self.scrub_text(text),
            "metadata": {
                "source_type": "wiki",
                "source": url,
                "chunk_number": 0,
                "tags": ["wiki"] + categories,
                "date_updated": None,
                "title": self.scrub_text(title),
            },
        }

    def extract_page(self, url):

        soup = self._get_soup(url)

        if soup is None:
            return None

        return self._parse_page(url, soup)

    def extract_index_pages(self):
        # Phase 1: everything the wiki itself lists

        print("Collecting page URLs from _index ...")

        page_urls = self.get_all_page_urls()

        print(
            f"Found {len(page_urls)} pages "
            "(OldWiki/OldOldWiki excluded)"
        )

        if self.max_pages:
            page_urls = page_urls[: self.max_pages]
            print(
                f"Limited to the first {len(page_urls)} pages "
                "(max_pages set)"
            )

        print()

        skipped = 0
        failed = 0

        for index, url in enumerate(page_urls, 1):
            print(
                f"  [{index}/{len(page_urls)}] "
                f"{self.page_title_from_url(url)}"
            )

            try:
                document = self.extract_page(url)

                if document:
                    self.documents[url] = document
                else:
                    skipped += 1
                    print("    (skipped — no content)")

            except WikiAuthenticationError:
                raise

            except Exception as error:
                failed += 1
                print(f"    [error] {error}")

            time.sleep(self.request_delay)

        print()
        print(
            f"Phase 1 done — {len(self.documents)} "
            "documents extracted"
        )

        return {
            "discovered": len(page_urls),
            "skipped": skipped,
            "failed": failed,
        }

    def crawl_links(self, start_url=None):
        # Phase 2: follow links to reach pages missing from _index

        start_url = start_url or self.BASE_URL

        # Track "links already followed" separately from "document already
        # held". Seeding this with the phase 1 URLs would skip those pages
        # entirely — including their outgoing links — and since phase 1
        # almost always grabs the front page, the crawl would stop on its
        # first iteration and visit nothing at all.
        visited = set()

        queue = [start_url.split("#")[0]]
        netloc = urlparse(start_url).netloc

        failed = 0

        while queue:
            if (
                self.max_pages
                and len(self.documents) >= self.max_pages
            ):
                print(
                    f"  [crawl] stopping at {self.max_pages} "
                    "pages (max_pages set)"
                )
                break

            url = queue.pop(0)

            if url in visited or self.should_skip_url(url):
                continue

            visited.add(url)

            try:
                soup = self._get_soup(url)

                if soup is None:
                    continue

                if url not in self.documents:
                    document = self._parse_page(url, soup)

                    if document:
                        self.documents[url] = document

                        print(
                            "  [crawl] "
                            f"{self.page_title_from_url(url)}"
                        )

                for anchor in soup.find_all("a", href=True):
                    href = anchor["href"]

                    if href.startswith(("http://", "https://")):
                        if urlparse(href).netloc != netloc:
                            continue
                        next_url = href.split("#")[0]
                    else:
                        next_url = urljoin(
                            url,
                            href,
                        ).split("#")[0]

                    if (
                        next_url not in visited
                        and not self.should_skip_url(next_url)
                        and urlparse(next_url).netloc == netloc
                        and "/wiki/" in next_url
                    ):
                        queue.append(next_url)

                time.sleep(self.request_delay)

            except WikiAuthenticationError:
                raise

            except Exception as error:
                failed += 1
                print(f"  [error] {url}: {error}")

        return {"failed": failed}

    # Markdown generation

    @staticmethod
    def safe_filename(filename):

        filename = re.sub(
            r'[<>:"/\\|?*]',
            "_",
            filename,
        )

        filename = re.sub(
            r"\s+",
            "_",
            filename,
        ).strip("_")

        return filename[:150]

    def build_markdown(self, document):

        metadata = document["metadata"]

        title = metadata.get("title") or "Wiki Page"

        text = document["text"].strip()

        tags = ", ".join(metadata.get("tags") or [])

        body = (
            text
            if text.startswith("#")
            else f"# {title}\n\n{text}"
        )

        markdown = f"""{body}

---

## Wiki Page

**Source:** {metadata["source"]}

**Tags:** {tags}
"""

        return markdown.strip() + "\n"

    def write_markdown(self, url, document):

        # Name files from the URL path, not the title — two wiki pages can
        # share an <h1> and would otherwise overwrite each other.
        slug = self.safe_filename(
            self.page_title_from_url(url)
        )

        # The wiki root has nothing after /wiki/, which would leave the
        # filename as just "wiki_.md".
        if not slug:
            slug = "index"

        return self.local_documents.save_document(
            f"wiki_{slug}.md",
            self.build_markdown(document),
        )

    def generate_knowledge_documents(self):

        self.make_session()
        self.verify_session()

        index_stats = self.extract_index_pages()

        crawl_failed = 0

        if self.crawl_links_enabled:
            before = len(self.documents)

            crawl_stats = self.crawl_links()
            crawl_failed = crawl_stats["failed"]

            print(
                "Phase 2 done — added "
                f"{len(self.documents) - before} "
                "additional pages"
            )

        print(f"Total documents: {len(self.documents)}")
        print()
        print(f"Writing files to: {self.output_folder}")

        created = 0
        updated = 0
        unchanged = 0
        failed = index_stats["failed"] + crawl_failed

        changed_files = []

        for url, document in self.documents.items():
            try:
                result = self.write_markdown(
                    url,
                    document,
                )

                status = result["status"]
                file_path = result["file"]

                if status == "created":
                    created += 1
                    changed_files.append(file_path)

                elif status == "updated":
                    updated += 1
                    changed_files.append(file_path)

                elif status == "unchanged":
                    unchanged += 1

                print(
                    f"{status.capitalize()}: "
                    f"{file_path.name}"
                )

            except Exception as error:
                failed += 1
                print(f"ERROR processing {url}: {error}")

        return build_generation_result(
            fetched=len(self.documents),
            created=created,
            updated=updated,
            unchanged=unchanged,
            skipped=index_stats["skipped"],
            failed=failed,
            changed_files=changed_files,
        )
