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

    ############################
    ######UTILITY METHODS#######
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
            filename = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)
            filename = re.sub(r"_+", "_", filename).strip("_")
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
