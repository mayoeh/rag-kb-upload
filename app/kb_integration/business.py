import os
import re

from html import unescape
from pathlib import Path

import requests
from dotenv import load_dotenv
from jira import JIRA
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine


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