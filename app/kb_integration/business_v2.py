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

    # Utility Methods
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

    def sanitize_filename(filename):
        return

    def _scrub_pii(text):
        return
