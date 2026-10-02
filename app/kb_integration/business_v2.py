import os
import re
import time
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

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


class RCKBKnowledgeManager:
    def __init__(self, output_folder):
        self.output_folder = Path(output_folder)
        self.output_folder.mkdir(
            parents=True,
            exist_ok=True,
        )

        # JIRA configuration
        self.server = os.getenv("JIRA_SERVER")
        self.email = os.getenv("JIRA_EMAIL")
        self.api_token = os.getenv("JIRA_API_TOKEN")

        self.jira = JIRA(
            server=self.server,
            basic_auth=(
                self.email,
                self.api_token,
            ),
        )

        # PII scrubbing
        self.analyzer = AnalyzerEngine()
        self.anonymizer = AnonymizerEngine()

    ############################
    #     UTILITY METHODS      #
    ############################

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

    ############################
    #    MAIN SCRAPER LOGIC    #
    ############################

    ############################
    #       WEBSITE LOGIC      #
    ############################

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

    def _crawl(self, url, netloc=None):

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

                return_link = article_soup.find(
                    "a", string=re.compile(r"^\u00ab Return to")
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

                raw_text = article_soup.get_text()

                self.documents[url] = {
                    "text": self.clean_markdown_text(raw_text),
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


    ############################
    #        JIRA LOGIC        #
    ############################

    UVA_ID_RE = re.compile(
        r"\b[a-z]{2,4}\d{1,2}[a-z0-9]{1,3}\b",
        re.IGNORECASE,
    )

    JIRA_BASE_FILTER = (
        "project IN (SUP, RIV, IVY, ACCORDSUP) "
        "AND type != Sub-task "
        'AND "Request Type[Dropdown]" '
        '!= "Provisioning/Deprovisioning" '
        "AND status != Open "
    )

    def get_all_jira_issues(self):
       # For initial population of kb or rull rebuild
       jql = (
            self.JIRA_BASE_FILTER
            + "ORDER BY updated DESC"
        )
       
       return self._fetch_issues(jql)

    def get_recent_jira_issues(self):
        # Incremental scrape for updated issues
        jql = (
            self.JIRA_BASE_FILTER
            + "AND updated >= -2d "
            + "ORDER BY updated DESC"
        )

        return self._fetch_issues(jql)

    def _fetch_issues(
        self,
        jql,
    ):
        issues = []
        next_page_token = None

        while True:
            batch = (
                self.jira
                .enhanced_search_issues(
                    jql,
                    maxResults=100,
                    fields=[
                        "summary",
                        "description",
                        "comment",
                    ],
                    expand="renderedFields",
                    nextPageToken=(
                        next_page_token
                    ),
                )
            )

            issues.extend(batch)

            print(
                f"Fetched {len(issues)} "
                "JIRA issues so far..."
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
    def _get_description(
        self,
        issue,
    ):
        rendered_description = ""

        if hasattr(
            issue,
            "renderedFields",
        ):
            rendered_description = (
                getattr(
                    issue.renderedFields,
                    "description",
                    "",
                )
                or ""
            )

        if not rendered_description:
            raw_description = (
                issue.fields.description
            )

            if isinstance(
                raw_description,
                str,
            ):
                rendered_description = (
                    raw_description
                )

        return self.strip_html_tags(
            rendered_description
        )

    def _classify_description(
        self,
        description,
    ):
        if (
            "Description: " in description
            or "Description:" in description
        ):
            return "form_with_description"

        if (
            description
            .strip()
            .startswith("Name:")
            or "Uid:" in description[:200]
        ):
            return "form_metadata"

        return "free_text"

    def _extract_form_description(
        self,
        description,
    ):
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
    def _extract_comments(
        self,
        issue,
    ):
        raw_comments = []

        if (
            hasattr(
                issue.fields,
                "comment",
            )
            and issue.fields.comment
        ):
            raw_comments = (
                issue.fields
                .comment
                .comments
            )

        rendered_comments = []

        if hasattr(
            issue,
            "renderedFields",
        ):
            rendered_comment_object = (
                getattr(
                    issue.renderedFields,
                    "comment",
                    None,
                )
            )

            if rendered_comment_object:
                rendered_comments = (
                    getattr(
                        rendered_comment_object,
                        "comments",
                        [],
                    )
                )

        comments = []

        for index, raw_comment in enumerate(
            raw_comments
        ):
            author = "Unknown"

            if (
                hasattr(
                    raw_comment,
                    "author",
                )
                and raw_comment.author
            ):
                author = (
                    raw_comment
                    .author
                    .displayName
                )

            rendered_body = ""

            if index < len(
                rendered_comments
            ):
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
                    rendered_body = (
                        raw_comment.body
                    )

            body = self.strip_html_tags(
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

    # PII scrubbing
    def _scrub_text(
        self,
        text,
    ):
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

    # Issue processing
    def _process_issue(
        self,
        issue,
    ):
        description = (
            self._get_description(issue)
        )

        if not description:
            return None

        ticket_type = (
            self._classify_description(
                description
            )
        )

        if ticket_type == "form_metadata":
            return None

        if (
            ticket_type
            == "form_with_description"
        ):
            description = (
                self._extract_form_description(
                    description
                )
            )

        if not description:
            return None

        comments = (
            self._extract_comments(issue)
        )

        processed_comments = []

        for comment in comments:
            processed_comments.append(
                {
                    "author":
                        self._scrub_text(
                            comment["author"]
                        ),
                    "body":
                        self._scrub_text(
                            comment["body"]
                        ),
                }
            )

        return {
            "key": issue.key,

            "summary":
                self._scrub_text(
                    (
                        issue.fields.summary
                        or ""
                    ).strip()
                ),

            "description":
                self._scrub_text(
                    description
                ),

            "comments":
                processed_comments,
        }

    # Markdown generation
    def _build_jira_markdown(
        self,
        issue,
    ):
        summary = (
            self.clean_markdown_text(
                issue["summary"]
            ).strip()
        )

        description = (
            self.clean_markdown_text(
                issue["description"]
            ).strip()
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
                body = (
                    self.clean_markdown_text(
                        comment["body"]
                    ).strip()
                )

                if not body:
                    continue

                markdown += f"""

### Comment {comment_number}

{body}
"""

                comment_number += 1

        return markdown.strip() + "\n"

    def _save_jira_issue(
        self,
        issue,
    ):
        filename = (
            f"jira_{issue['key']}.md"
        )

        markdown = (
            self._build_jira_markdown(
                issue
            )
        )

        return self.save_document(
            filename,
            markdown,
        )

    # JIRA generation
    def generate_jira_knowledge(
        self,
        full_refresh=False,
    ):
        if full_refresh:
            print(
                "Starting FULL JIRA "
                "knowledge generation..."
            )

            issues = (
                self.get_all_jira_issues()
            )

        else:
            print(
                "Starting incremental JIRA "
                "knowledge generation..."
            )

            issues = (
                self.get_recent_jira_issues()
            )

        created = 0
        updated = 0
        unchanged = 0
        skipped = 0
        failed = 0

        changed_files = []

        for issue in issues:
            try:
                processed_issue = (
                    self._process_issue(
                        issue
                    )
                )

                if processed_issue is None:
                    skipped += 1
                    continue

                result = (
                    self._save_jira_issue(
                        processed_issue
                    )
                )

                status = result["status"]
                file_path = result["file"]

                if status == "created":
                    created += 1
                    changed_files.append(
                        file_path
                    )

                elif status == "updated":
                    updated += 1
                    changed_files.append(
                        file_path
                    )

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

        return self.build_generation_result(
            fetched=len(issues),
            created=created,
            updated=updated,
            unchanged=unchanged,
            skipped=skipped,
            failed=failed,
            changed_files=changed_files,
        )