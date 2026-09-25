import os
import re

from html import unescape
from pathlib import Path

import requests
from dotenv import load_dotenv
from jira import JIRA
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine

from yt_dlp import YoutubeDL
from youtube_transcript_api import YouTubeTranscriptApi


load_dotenv()

# Jira Knowledge
class UVARCJiraKnowledgeDataManager:
    # Handles all generation of JIRA knowledge base

    UVA_ID_RE = re.compile(
        r"\b[a-z]{2,4}\d{1,2}[a-z0-9]{1,3}\b",
        re.IGNORECASE,
    )

    def __init__(self, output_folder):
        self.output_folder = Path(output_folder)

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

        self.output_folder.mkdir(
            parents=True,
            exist_ok=True,
        )

        filename = (
            f"jira_{issue['key']}_0.md"
        )

        file_path = (
            self.output_folder / filename
        )

        markdown = self._build_markdown(
            issue
        )

        file_path.write_text(
            markdown,
            encoding="utf-8",
        )

        return file_path

    def generate_knowledge_documents(self):

        issues = self.get_resolved_issues()

        generated_files = []
        skipped = 0
        failed = 0

        for issue in issues:
            try:
                processed_issue = self._process_issue(
                    issue
                )

                if processed_issue is None:
                    skipped += 1
                    continue

                file_path = self._write_markdown(
                    processed_issue
                )

                generated_files.append(
                    file_path
                )

            except Exception as error:
                failed += 1

                print(
                    f"Failed to process "
                    f"{issue.key}: {error}"
                )

        return {
            "fetched": len(issues),
            "generated": len(generated_files),
            "skipped": skipped,
            "failed": failed,
            "files": generated_files,
        }


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

import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlparse

import cloudscraper
from bs4 import BeautifulSoup


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
        self.output_folder = output_folder

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
        project_root = next(
            (path for path in (Path.cwd(), *Path.cwd().parents)
             if (path / "app" / "kb_integration" / "tasks.py").is_file()
             and (path / "scrapers").is_dir()),
            None,
        )
        if project_root is None:
            raise RuntimeError("Run this notebook from the repository root or a subfolder.")

        output_folder = self.output_folder
        output_folder.mkdir(parents=True, exist_ok=True)

        print(f"Documents available: {len(self.documents)}")
        print(f"Writing files to: {output_folder}")

        created = 0
        failed = 0

        for url, doc in self.documents.items():
            try:
                flat_name = f"{self.safe_filename(url)}.md"
                file_path = output_folder / flat_name

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(doc["text"].strip() + "\n")

                created += 1
                print(f"Created: {file_path.name}")
            except Exception as e:
                failed += 1
                print(f"ERROR processing {url}: {e}")

        print("=" * 60)
        print("MARKDOWN GENERATION COMPLETE")
        print("=" * 60)
        print(f"Created: {created} files")
        print(f"Failed:  {failed} files")

    def generate_knowledge_documents(self):
        print("Starting knowledge file generation...")

        # Scraping Logic
        self.crawl("https://rc.virginia.edu/")
        self.crawl("https://learning.rc.virginia.edu/")
        self.fill_sitemap_gaps()
        self.patch_js_rendered_pages()

        # Generate Markdown files from generated documents
        self.generate_markdown_files()


class UVARCVideoKnowledgeDataManager:

    PLAYLIST_URL = (
        "https://www.youtube.com/playlist"
        "?list=PLT4bryHgBcRP7N-hB9u6EWs6tq_2nMoRO"
    )

    def __init__(self, output_folder):
        self.output_folder = Path(output_folder)

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

        self.output_folder.mkdir(
            parents=True,
            exist_ok=True,
        )

        title = video["metadata"]["title"]

        filename = self._safe_filename(
            f"youtube_{title}.md"
        )

        file_path = (
            self.output_folder / filename
        )

        markdown = self._build_markdown(
            video
        )

        file_path.write_text(
            markdown,
            encoding="utf-8",
        )

        return file_path

    def generate_knowledge_documents(self):

        entries = self._fetch_playlist()

        generated_files = []
        failed = 0

        for entry in entries:
            try:
                print(
                    f"Loading: "
                    f"{entry.get('title', 'Unknown')}"
                )

                video = self._process_video(
                    entry
                )

                file_path = self._write_markdown(
                    video
                )

                generated_files.append(
                    file_path
                )

                print(
                    f"Created: {file_path.name}"
                )

            except Exception as error:
                failed += 1

                print(
                    f"Failed to process "
                    f"{entry.get('title', 'Unknown')}: "
                    f"{error}"
                )

        return {
            "fetched": len(entries),
            "generated": len(
                generated_files
            ),
            "failed": failed,
            "files": generated_files,
        }