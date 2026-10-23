# `business_v2.py` — Function Reference

`business_v2.py` holds every knowledge-base scraper in one class, `RCKBKnowledgeManager`. Each source has one public entry point, writes one Markdown file per item into the manager's `output_folder`, and returns the same summary dict.

| Source | Entry point | Output folder (`run.py`) | File naming |
|---|---|---|---|
| RC websites (`rc.virginia.edu`, `learning.rc.virginia.edu`) | `generate_website_knowledge()` | `data/website/` | sanitized full URL |
| Jira (SUP, RIV, IVY, ACCORDSUP) | `generate_jira_knowledge(full_refresh=False)` | `data/jira/` | `jira_<KEY>.md` |
| YouTube (RC Tutorial Series playlist) | `generate_video_knowledge(max_videos=None)` | `data/video/` | `youtube_<video id>.md` |
| RCI wiki (Gitit behind NetBadge) | `generate_wiki_knowledge(cookies_path=None, max_pages=None, scrub_pii=True)` | `data/wiki/` | `wiki_<page path>.md` |

Scraping only writes local files. Uploading them to Open WebUI is a separate step that is not part of this file yet.

```bash
python run.py                     # all sources (wiki is skipped with a message if its cookies expired)
python run.py --website --jira    # any combination of --website / --jira / --video / --wiki
python run.py --jira --full       # all Jira tickets instead of the last 2 days
python run.py --wiki-login        # refresh the wiki cookies (asks for a Duo approval)
python run.py --wiki --max-pages 5
```

---

## Conventions

**Summary dict.** Every entry point returns `build_generation_result(**stats)`:

```python
{"fetched", "created", "updated", "unchanged", "skipped", "failed", "changed_files"}
```

- `fetched` is the number of items the source offered and this run **attempted** (tickets returned, videos in the playlist, wiki pages listed), so `fetched = created + updated + unchanged + skipped + failed`. The website scraper still counts successful HTML responses instead; see its section.
- `created` / `updated` / `unchanged` come from `save_document()`; `changed_files` lists the paths that were created or updated and is what an uploader should re-send.
- `skipped`: nothing worth saving (no content, form-only ticket, page not found). `failed`: an exception while processing that item.

**Stale files.** The video and wiki entry points add `stale_files` on full runs: documents in the output folder that the source no longer lists (a deleted page, a video removed from the playlist). They are only reported, never deleted, because a source that is briefly unreachable would otherwise wipe its documents. Runs limited by `max_videos` / `max_pages` do not report them.

**Lazy clients.** `self.jira`, `self.analyzer` and `self.anonymizer` are properties created on first use, so a website-only run needs neither Jira credentials nor the Presidio/spaCy models.

---

## Module contents

- `WikiAuthenticationError` — exception class
- `RCKBKnowledgeManager`
  - [Configuration](#configuration)
  - [Utility methods](#utility-methods)
  - [Website](#website) and [Jira](#jira) (summary)
  - [Video](#video)
  - [Wiki](#wiki)
- [`netbadge_login()`](#netbadge-login) — creates the wiki cookies

---

## `WikiAuthenticationError`

Subclass of `RuntimeError`. Raised when the NetBadge cookies are missing or expired, when the NetBadge password is rejected, or when an interactive login times out.

Refreshing the cookies needs a person to approve a Duo push, so this cannot be recovered from automatically. The wiki scraper re-raises it instead of counting a page as `failed`, so a dead session aborts the run rather than saving login pages. `run.py` catches it, prints how to log in again, and exits with status 1 when `--wiki` was requested explicitly.

Cookies have been observed to expire in under 7.5 hours, so the wiki cannot currently run unattended on a daily schedule.

---

<a id="configuration"></a>
## Configuration (class constants)

| Constant | Used by | Meaning |
|---|---|---|
| `WEBSITE_SEEDS`, `RC_SITEMAP`, `LEARNING_SITEMAP`, `ALLOWED_NETLOCS`, `SKIP_PATTERNS` | website | crawl start points, sitemaps, allowed hosts, sitemap URL filters |
| `SKIP_EXTENSIONS` | website, wiki | media/binary extensions never fetched |
| `REQUEST_DELAY`, `BLOCKED_STATUSES` | website | 0.2 s between requests; statuses (403/429/503) retried with backoff |
| `UVA_ID_RE` | Jira | computing-ID pattern replaced with `<UVA_ID>` |
| `JIRA_BASE_FILTER` | Jira | base JQL |
| `VIDEO_PLAYLIST_URL`, `VIDEO_REQUEST_DELAY` | video | playlist to read; 1 s between videos (the transcript API rate-limits) |
| `WIKI_BASE_URL`, `WIKI_INDEX_URL` | wiki | wiki root (also the login target) and Gitit's page listing |
| `WIKI_LOGIN_TARGET` | `netbadge_login()` | page opened to trigger NetBadge |
| `WIKI_REQUEST_DELAY` | wiki | 0.3 s between wiki requests |
| `WIKI_SKIP_PATH_SEGMENTS` | wiki | Gitit special paths (`/_edit/`, `/_history/`, `/_search`, …) and the `/OldWiki`, `/OldOldWiki` sections |
| `WIKI_BLOCK_TAGS` | wiki | tags that end a line when converting HTML to text |

---

<a id="utility-methods"></a>
## Utility methods

#### `_new_stats()` / `_record(stats, result)` *(staticmethods)*

`_new_stats()` returns a zeroed summary dict. `_record()` adds one `save_document()` result to it: increments the status count and appends the file to `changed_files` when it was created or updated. Skipped and failed items are counted directly (`stats["skipped"] += 1`).

#### `build_generation_result(self, fetched, created, updated, unchanged, skipped, failed, changed_files)`

Prints a "KNOWLEDGE GENERATION COMPLETE" summary and returns it as a dict. Called as `self.build_generation_result(**stats)`.

#### `_find_stale_files(self, prefix, expected_filenames)`

Returns the sorted paths of `<prefix>*.md` files in `output_folder` whose names are not in `expected_filenames`, and prints them. See *Stale files* above.

#### `save_document(self, filename, content)`

Writes `content` (stripped, one trailing newline) to `output_folder/filename`, adding `.md` if missing:

| Situation | Action | `status` |
|---|---|---|
| file does not exist | write | `created` |
| same content | nothing | `unchanged` |
| different content | overwrite | `updated` |

Returns `{"status", "file": Path}`. Unchanged files are not rewritten, so a rerun on unchanged sources leaves `changed_files` empty.

#### `generate_sanitized_filename(self, name, maxLength=200, hasSpaces=False)`

Safe filename, truncated to `maxLength`. `hasSpaces=True` (titles) only replaces characters illegal in filenames and collapses whitespace; `hasSpaces=False` (URLs) replaces everything except letters, digits, `_` and `-` with `_`.

#### `clean_markdown_text(self, text)` / `strip_html_tags(self, html)`

`clean_markdown_text` removes `.png` references, collapses blank lines and runs of spaces, and adds a trailing newline. `strip_html_tags` is a regex HTML → text conversion (used for Jira) that ends with `clean_markdown_text`.

#### `_scrub_text(self, text)` *(Jira)*

Replaces `PERSON`, `EMAIL_ADDRESS` and `PHONE_NUMBER` with Presidio placeholders, then computing IDs with `<UVA_ID>`. The wiki uses its own `_scrub_wiki_text()`; see the wiki section for why.

---

<a id="website"></a>
## Website (summary)

`generate_website_knowledge()` runs three passes that share one `stats` dict:

1. `_crawl()` from each of `WEBSITE_SEEDS` — recursive link following within `ALLOWED_NETLOCS`; pages with exactly one `<article>` are converted by `_parse_article()` and saved.
2. `fill_sitemap_gaps()` — fetches sitemap URLs that have no file on disk yet.
3. `patch_js_rendered_pages()` — writes hand-written text for pages rendered by JavaScript.

Requests go through `_fetch()`, which retries `BLOCKED_STATUSES` with backoff. Open points (for the website owner): `fetched` counts successful HTML responses and non-HTML pages are not counted at all; `_crawl()` is recursive and a `RecursionError` on a deep link chain would be swallowed as one `failed`; `fill_sitemap_gaps()` treats any existing file as up to date; the two JS patch texts end in `...`.

<a id="jira"></a>
## Jira (summary)

`generate_jira_knowledge(full_refresh=False)` fetches tickets matching `JIRA_BASE_FILTER` (all of them with `full_refresh=True`, otherwise those updated in the last 2 days), pages through results with `nextPageToken`, drops form-only tickets, keeps the free-text description and comments, scrubs every field with `_scrub_text()`, and saves `jira_<KEY>.md` with *User Request* and *Resolution and Discussion* sections.

---

<a id="video"></a>
## Video

#### `generate_video_knowledge(self, max_videos=None)`

1. `_fetch_playlist()` reads `VIDEO_PLAYLIST_URL` with yt-dlp in `extract_flat` mode (list only, nothing downloaded). `max_videos` keeps the first N for tests.
2. For each entry, `_process_video()` collects metadata and transcript, `_save_video()` writes the document, and the result is recorded. Exceptions count as `failed`. Waits `VIDEO_REQUEST_DELAY` between videos.
3. Returns the summary dict, plus `stale_files` on full runs.

#### `_video_id(entry)` *(staticmethod)* / `_video_filename(self, entry)`

`_video_id` returns `entry["id"]`, or the `v=` part of `entry["url"]`. Works for playlist entries and for `_process_video()` results, so the filename used for saving and for the stale check is always `youtube_<id>.md`.

#### `_get_video_metadata(self, video_url)`

One yt-dlp call per video for title, uploader, description, duration, upload date and canonical URL.

#### `_get_transcript(self, video_id)`

English transcript via `youtube_transcript_api`, joined into one paragraph. A video without English captions (`NoTranscriptFound`, `TranscriptsDisabled`) is still saved, with "No transcript available." as its transcript, so its title and description reach the knowledge base. Rate-limit errors (`IpBlocked`, `RequestBlocked`) are not caught and count as `failed`, so a blocked video is never saved as caption-less.

#### `_build_video_markdown(self, video)`

`# title`, then *Video Information* (source, author, URL, publish date), *Description* and *Transcript* sections.

---

<a id="wiki"></a>
## Wiki

The RCI wiki is a Gitit site behind NetBadge. Logging in needs a Duo approval, so the work is split: `netbadge_login()` (interactive, `run.py --wiki-login`) saves cookies, and `generate_wiki_knowledge()` only reads them.

#### `generate_wiki_knowledge(self, cookies_path=None, max_pages=None, scrub_pii=True)`

1. Builds a session from the cookies (`_make_wiki_session()`) and fetches the wiki root once; `_check_wiki_session()` raises if that returned a NetBadge page.
2. Gets every page URL with `_get_wiki_page_urls()`. `max_pages` keeps the first N for tests.
3. Sets `fetched` to the number of listed pages, then for each one: `_fetch_wiki_page()` → `_parse_wiki_page()` → `save_document(_wiki_filename(url), _build_wiki_markdown(page))`. A page that cannot be fetched or has no content is `skipped`; other exceptions are `failed`; `WikiAuthenticationError` aborts the run. Waits `WIKI_REQUEST_DELAY` between pages.
4. Returns the summary dict, plus `stale_files` on full runs.

Checked on 2026-10-22: 240 listed pages → 239 documents, 1 empty page skipped, 0 failures; about 270 requests in total.

#### `_get_wiki_page_urls(self, session)`

Gitit's `_index` lists only the top level; subdirectories appear as `<li class="folder">` links (e.g. `/wiki/Guides/`, 29 folders in total). The method reads `li.page` and `li.folder` entries, queues each folder's own listing until none are left, filters URLs with `_should_skip_wiki_url()`, and de-duplicates them in discovery order. The front page is not listed anywhere, so `WIKI_BASE_URL` is added first.

Expanding folders replaced an earlier link-following crawl: the crawl needed about 540 requests and still missed 5 pages that nothing links to, while the top-level `_index` alone missed the 138 pages under `Guides/`.

#### `_should_skip_wiki_url(self, url)`

`True` for URLs with a query string (`?printable` and `?revision=` map to the same filename as the plain page and would overwrite it), media/binary extensions (`SKIP_EXTENSIONS`), and paths containing any `WIKI_SKIP_PATH_SEGMENTS` entry.

#### `_fetch_wiki_page(self, session, url)`

Returns `(url, soup)`. If the listed URL cannot be fetched (it 404s), it retries with a leading space (`%20`) in the last path segment and returns that URL instead. The listing drops leading spaces from page names, so "HPC Support/ SLURM" is listed as `.../SLURM`. `soup` is `None` if both attempts fail.

#### `_get_wiki_soup(self, session, url)`

`BeautifulSoup` for an HTML 200 response, otherwise `None`. Runs `_check_wiki_session()`, so cookies expiring mid-run raise instead of returning a login page.

#### `_make_wiki_session(self, cookies_path=None)` / `wiki_default_cookies_path()`

The cookie file is taken from the `cookies_path` argument, then `WIKI_AUTH_COOKIES_PATH`, then `<project root>/scrapers/auth_cookies.json`. Raises `WikiAuthenticationError` if it does not exist.

#### `_parse_wiki_page(self, url, soup, scrub_pii=True)`

1. Uses only `#wikipage`; returns `None` if it is missing.
2. Removes the table of contents, edit links, footnotes and images.
3. Takes the title from the `<h1>` (falling back to the URL path) and removes the `<h1>`, since `_build_wiki_markdown()` adds the heading back.
4. Rewrites relative links as absolute Markdown links; absolute links are kept as written.
5. Converts to text with `_wiki_soup_to_text()`, removes `.jpg/.jpeg/.gif/.svg/.webp` references (`clean_markdown_text` only removes `.png`), and cleans whitespace. Returns `None` if nothing is left.
6. Reads the page categories, and scrubs title and text if `scrub_pii`.

Returns `{"title", "text", "source", "tags"}` with `tags = ["wiki"] + categories`.

#### `_wiki_soup_to_text(self, node)`

Inserts newlines around `WIKI_BLOCK_TAGS` and a space after table cells, then calls `get_text()` with no separator. `get_text(separator="\n")` would put every `<code>` and `<a>` on its own line, which breaks most sentences on this wiki.

#### `_scrub_wiki_text(self, text)`

Replaces only `EMAIL_ADDRESS` and `PHONE_NUMBER`. Jira's `_scrub_text()` rules damage technical content here: on a 60-page sample they changed 48 pages, replacing node and rack names (`aj25`, `ba38`), Lustre targets (`MDT0000`), tools (`pptx2md`) and product names (`Docker`, `Qualys`, `Rivanna`). As a result, the wiki documents currently **keep staff names and computing IDs** that do appear in the wiki. Whether to scrub those, for example with a stricter ID pattern and a Presidio allow-list shared with Jira, is an open team decision.

#### `_build_wiki_markdown(self, page)`

`# title` (unless the text already starts with a heading), the body, then a `## Wiki Page` section with the source URL and tags.

#### `_wiki_filename(self, url)`

`wiki_<slug>.md` from the URL path: each segment stripped, spaces → `_`, then `generate_sanitized_filename(..., 150, True)`. The root becomes `wiki_index.md`. Named from the path rather than the title because two pages can share an `<h1>`.

---

<a id="netbadge-login"></a>
## `netbadge_login(username, password, target=None, output=None, timeout=120, debug_dir=None)`

Logs into the wiki through NetBadge + Duo in a headless Chromium (Playwright **sync** API, so not from inside a running asyncio loop) and saves the browser cookies as JSON to `output` (default `wiki_default_cookies_path()`). Needs `playwright install chromium` once.

1. Opens `target` (default `WIKI_LOGIN_TARGET`). Clicks *Continue* on a "Certificate Login Failed" page, fills the username and password, and submits with **Enter** in the password field (the visible button can belong to the certificate-login form). If NetBadge answers "Incorrect computing ID or password", raises `WikiAuthenticationError` immediately.
2. If the wiki (`#wikipage`) is not shown yet, clicks the Duo push button and prints the 3-digit **Duo Verified Push code** from the page, which has to be typed into Duo Mobile.
3. Polls until the URL is back on the wiki, clicking "Trust this browser", "Yes", "Continue" and similar buttons. Raises `WikiAuthenticationError` after `timeout` seconds.
4. Writes the cookies and closes the browser.

`debug_dir`, if given, receives screenshots of the Duo page and of the final page on timeout, which is usually the only way to see why a headless login stalled.
