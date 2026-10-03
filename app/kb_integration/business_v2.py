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

    # Website Logic
    def _generate_website_knowledge(self):
        scraper = cloudscraper.create_scraper()
        visited = set()
        self.crawl(scraper, "https://rc.virginia.edu/", visited)
        self.crawl(scraper, "https://learning.rc.virginia.edu/", visited)
        self.fill_sitemap_gaps()
        self.patch_js_rendered_pages()

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

    def _crawl(self, scraper, url, visited, netloc=None):

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

                clean_text = self.clean_markdown_text(article_soup.get_text())
                safe_filename = self.generate_sanitized_filename(url)

                self.save_document(safe_filename, clean_text)
                print(f"Extracted: {url}")

            for a_tag in soup.find_all("a", href=True):
                next_url = a_tag["href"]
                if not next_url.startswith(("http://", "https://")):
                    next_url = urljoin(url, next_url)
                next_url = next_url.split("#")[0]

                if next_url not in visited and self.is_valid(next_url):
                    self._crawl(scraper, next_url, visited, netloc)

            time.sleep(0.2)

        except Exception as e:
            print(f"Failed to crawl {url}: {e}")

    def _get_sitemap_urls(self, sitemap_url, skip_patterns=None):
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

        return {"text": clean_text, "filename": self.generate_sanitized_filename(url)}

    def fill_sitemap_gaps(self):
        rc_urls = self.get_sitemap_urls("https://rc.virginia.edu/sitemap.xml")
        learn_urls = self.get_sitemap_urls(
            "https://learning.rc.virginia.edu/sitemap.xml", self.SKIP_PATTERNS
        )
        all_sitemap_urls = rc_urls + learn_urls

        missing = []
        for url in all_sitemap_urls:
            filename = f"{self.generate_sanitized_filename(url)}.md"
            file_path = self.output_folder / filename

            if not file_path.exists() or file_path.stat().st_size == 0:
                missing.append(url)
        print(
            f"Sitemap total: {len(all_sitemap_urls)}, already crawled: {len(all_sitemap_urls) - len(missing)}, missing: {len(missing)}"
        )

        for url in missing:
            try:
                result = self._extract_article(url)
                if result:
                    self.save_document(result["filename"], result["text"])
                    print(f"Added: {url}")
                else:
                    print(f"Skipped (no article): {url}")
                time.sleep(0.2)
            except Exception as e:
                print(f"Failed {url}: {e}")

        print("Fully done filling sitemap gaps!")

    def patch_js_rendered_pages(self):
        manual_patches = {
            "https://rc.virginia.edu/userinfo/hpc/slurm-script-generator/": {
                "text": "# Slurm Script Generator\n\nThe UVA Research Computing Slurm Script Generator...",
                "source": "https://rc.virginia.edu/userinfo/hpc/slurm-script-generator/",
            },
            "https://rc.virginia.edu/userinfo/hpc/software/physics/": {
                "text": "# Physics Software on UVA HPC\n\nUVA Research Computing provides several physics...",
                "source": "https://rc.virginia.edu/userinfo/hpc/software/physics/",
            },
        }

        patched = 0
        for url, doc in manual_patches.items():
            filename = f"{self.generate_sanitized_filename(url)}"
            file_path = self.output_folder / filename
            if (
                not file_path.exists()
                or file_path.stat().st_size == 0
                or len(self.documents.get(url, {}).get("text", "")) < 100
            ):
                self.save_document(filename, doc)
                patched += 1
                print(f"Patched: {url}")
            else:
                print(f"Already has content: {url}")

        print(f"\nManually patched {patched} JS-rendered pages.")
