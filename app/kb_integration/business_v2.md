# `business_v2.py` — Function Reference

`business_v2.py` is the single-class refactor of the knowledge-base scrapers. It currently covers two sources:

| Source | Entry point | Output folder (set in `tasks.py`) | File naming |
|---|---|---|---|
| RC public websites (`rc.virginia.edu`, `learning.rc.virginia.edu`) | `RCKBKnowledgeManager._generate_website_knowledge()` | `data/website/` | sanitized full URL, e.g. `https_rc_virginia_edu_userinfo_hpc.md` |
| RCI wiki (Gitit behind NetBadge) | `RCKBKnowledgeManager._generate_wiki_knowledge()` | `data/wiki/` | `wiki_<page path>.md` |

Jira, video and markdown sources still live in `business.py`. (The `jira`, `youtube_transcript_api` and `yt_dlp` imports at the top of this file are not used yet; they are there for those future ports.)

Both scrapers write one Markdown file per page through `save_document()` and return the same summary dict (see `build_generation_result()`), so `tasks.py` can treat them the same way.

---

## Module contents

- `WikiAuthenticationError` — exception class
- `RCKBKnowledgeManager` — the scraper class
  - [Main scraper logic (websites)](#website-scraper)
  - [Wiki scraper logic](#wiki-scraper)
  - [Utility methods](#utility-methods)
- `netbadge_login()` — [module-level function](#netbadge-login) that creates the wiki cookies

---

## `WikiAuthenticationError`

Subclass of `RuntimeError`. Raised when the saved NetBadge cookies are missing or expired, or when an interactive login times out.

Refreshing the cookies requires a person to approve a Duo push on their phone, so this error **cannot be recovered from automatically**. Callers should surface it as an alert, not retry. The wiki scraper re-raises it immediately instead of counting the page as `failed`, so a dead session aborts the whole run rather than writing hundreds of login pages to disk.

---

## `RCKBKnowledgeManager`

### `__init__(self, output_folder)`

Stores `output_folder` as a `Path` and creates it (including parents) if it does not exist. Every document the manager saves goes into this folder.

---

<a id="website-scraper"></a>
### Main scraper logic (websites)

#### `_generate_website_knowledge(self)`

**Entry point for the website scraper.** Runs four steps in order and returns the summary dict:

1. `_crawl()` from `https://rc.virginia.edu/` (recursive link-following).
2. `_crawl()` from `https://learning.rc.virginia.edu/`, sharing the same `visited` set and `results` map.
3. `fill_sitemap_gaps()` — fetch any page listed in the sitemaps that the crawl did not reach.
4. `patch_js_rendered_pages()` — write hand-written content for pages that are rendered by JavaScript and therefore empty when scraped.

All steps record their outcome in a shared `results` dict (`url -> save_document result` or `{"status": "skipped" | "failed"}`), which `_summarize_results()` turns into the final counts.

#### `SKIP_PATTERNS` (class attribute)

`["/author/", "/category/", "/tag/"]` — URL fragments excluded from the `learning.rc.virginia.edu` sitemap (blog listing pages, not content).

#### `_is_valid(url)` *(staticmethod)*

Returns `True` if a URL should be crawled: scheme is `http`/`https`, host is one of `rc.virginia.edu`, `learning.rc.virginia.edu` or `archive.rc.virginia.edu`, and the path does not end in a binary/media extension (`.png`, `.pdf`, `.zip`, `.mp4`, …).

#### `_website_filename(self, url)`

Builds the output filename for a website page: the full URL passed through `generate_sanitized_filename(url, 150, False)` plus `.md`. Every website code path uses this, so a page always maps to the same file no matter how it was reached.

#### `_crawl(self, scraper, url, visited, results, netloc=None)`

Recursive depth-first crawler.

- Skips URLs already in `visited`; follows redirects and also marks the final URL as visited.
- Ignores responses that are not HTML or not HTTP 200.
- If the page contains **exactly one `<article>`**, it extracts that article:
  - removes `<img>` tags, the "« Return to …" link and the `blog-post-meta` paragraph;
  - rewrites every link as Markdown `[text](absolute-url)`;
  - turns the `blog-post-title` `<h2>` into a `#` heading and every `<h1>` into `##`;
  - cleans the text with `clean_markdown_text()` and saves it via `_save_website_page()`.

  Pages with zero or several `<article>` elements are not saved (they are listing/index pages), but their links are still followed.
- Follows every `<a href>` on the full page (fragment stripped) that passes `_is_valid()`.
- Sleeps 0.2 s between pages.
- On any exception, records the URL as `failed` (unless it already has a result) and moves on.

The `netloc` argument is passed down the recursion but is not currently used for filtering; host filtering is done by `_is_valid()`.

#### `_get_sitemap_urls(self, scraper, sitemap_url, skip_patterns=None)`

Downloads and parses a `sitemap.xml`, returning the list of `<loc>` URLs. Relative entries are made absolute against the sitemap's host, and URLs containing any of `skip_patterns` are dropped. Returns `[]` if the XML cannot be parsed (and prints the first 300 characters of the response for debugging). Raises on a non-2xx HTTP status.

#### `_extract_article(self, scraper, url)`

Single-page version of the article extraction used by `fill_sitemap_gaps()`. Fetches `url` and, if it is an HTML 200 response with exactly one `<article>`, returns `{"text": cleaned_markdown, "filename": _website_filename(url)}`; otherwise returns `None`.

Its cleanup is lighter than `_crawl()`'s: it removes images and the metadata paragraph and converts links to Markdown, but does **not** remove the "« Return to" link or convert titles/`<h1>` into Markdown headings.

#### `fill_sitemap_gaps(self, scraper, results)`

Catches pages the link crawl missed.

1. Collects URLs from `https://rc.virginia.edu/sitemap.xml` and `https://learning.rc.virginia.edu/sitemap.xml` (the latter filtered by `SKIP_PATTERNS`).
2. Works out which ones were **not saved in this run**, comparing by filename rather than URL so that redirects and trailing-slash variants count as the same page. Files left on disk from earlier runs are deliberately ignored, since they may be stale.
3. For each missing URL, calls `_extract_article()` and saves the result with `_save_website_page()`; records `skipped` if there is no single article and `failed` on exceptions. Sleeps 0.2 s between requests.

#### `JS_RENDERED_PATCHES` (class attribute)

`{url: markdown}` map of hand-written content for pages whose real content is rendered by JavaScript (e.g. the Slurm Script Generator), so a plain HTTP fetch only sees an empty shell.

#### `_save_website_page(self, url, text, results)`

**The single save point for website pages.** Computes the filename, and if that filename belongs to a `JS_RENDERED_PATCHES` entry *and* the scraped text is shorter than 100 characters, substitutes the patch text. Then calls `save_document()`, stores the result in `results[url]` and returns it.

Doing the substitution here (rather than saving the empty shell and patching later) means a JS-rendered page is written once per run and reports `unchanged` on reruns instead of flipping to `updated` every time.

#### `patch_js_rendered_pages(self, results)`

Final website step. For each entry in `JS_RENDERED_PATCHES` whose file was **not** already saved in this run (by the crawl or the sitemap pass), saves the patch text via `_save_website_page()`. Prints how many pages were patched.

---

<a id="wiki-scraper"></a>
### Wiki scraper logic

#### Class constants

| Constant | Meaning |
|---|---|
| `WIKI_BASE_URL` | `https://rci.hpc.virginia.edu/wiki/` — wiki root and crawl start point |
| `WIKI_INDEX_URL` | `.../wiki/_index` — Gitit's list of all pages |
| `WIKI_LOGIN_TARGET` | `.../wiki/Special:AllPages` — page `netbadge_login()` opens to trigger NetBadge |
| `WIKI_REQUEST_DELAY` | `0.3` s pause between wiki requests |
| `WIKI_SKIP_EXTENSIONS` | media/binary extensions never fetched |
| `WIKI_SKIP_PATH_SEGMENTS` | Gitit special paths (`/_edit/`, `/_history/`, `/_login`, `/_search`, …) and the `/OldWiki`, `/OldOldWiki` sections |
| `WIKI_BLOCK_TAGS` | HTML tags that end a line when converting to text (everything else stays inline) |
| `WIKI_SAVED_STATUSES` | `("created", "updated", "unchanged")` — statuses that mean a page was written |

#### `_generate_wiki_knowledge(self, cookies_path=None, max_pages=None, crawl_links=False, scrub_pii=True, scrub_uva_ids=False)`

**Entry point for the wiki scraper.** Requires cookies created by `netbadge_login()`.

1. Builds an authenticated session (`_make_wiki_session()`) and checks it works (`_verify_wiki_session()`); raises `WikiAuthenticationError` if not.
2. Defines a `scrub()` helper applied to each page's title and text:
   - `scrub_pii=True` (default) removes **email addresses and phone numbers** with presidio.
   - `PERSON` detection is intentionally off — presidio reads module, command and host names as people's names.
   - `scrub_uva_ids` is off by default because the computing-ID pattern matches module names such as `gcc9` or `cuda11`.
3. **Phase 1** — `_wiki_extract_index_pages()`: scrape every page listed on `_index`, including the pages inside its folders.
4. **Phase 2** (only if `crawl_links=True`, off by default) — `_wiki_crawl_links()`: breadth-first crawl from the wiki root to find pages missing from `_index`. Since Phase 1 expands the folders it finds nothing extra (checked 2026-10-22: Phase 1 alone saved 239 pages, a superset of the old two-phase run), so it is only kept as an occasional cross-check.
5. Returns the summary dict from `_summarize_results()`.

`max_pages` limits both phases (useful for testing).

#### `wiki_default_cookies_path()` *(staticmethod)*

Returns `<project root>/scrapers/auth_cookies.json`, the default location for the NetBadge cookie file.

#### `_make_wiki_session(self, cookies_path=None)`

Creates a `requests.Session` loaded with the saved cookies. The cookie file is chosen in this order: the `cookies_path` argument → the `WIKI_AUTH_COOKIES_PATH` environment variable → `wiki_default_cookies_path()`. Raises `WikiAuthenticationError` if the file does not exist. Sets a custom `User-Agent`.

#### `_check_wiki_session(self, response)`

Raises `WikiAuthenticationError` if a response is actually a NetBadge login page (text contains "NetBadge" or URL contains "shibidp"). This catches cookies expiring **mid-run**, where the server still returns HTTP 200.

#### `_verify_wiki_session(self, session)`

Fetches `WIKI_BASE_URL` once and runs `_check_wiki_session()` on it, so an expired session fails before any scraping starts.

#### `_should_skip_wiki_url(self, url)`

Returns `True` if a wiki URL should not be fetched:

- it has a query string (e.g. `?printable`, `?revision=` — these map to the same filename as the plain page and would fetch it twice and overwrite it);
- it ends in a `WIKI_SKIP_EXTENSIONS` extension;
- its path contains any `WIKI_SKIP_PATH_SEGMENTS` entry.

#### `_wiki_page_title(self, url)`

Returns the URL-decoded path after `/wiki/` (e.g. `/wiki/Slurm%20Basics` → `Slurm Basics`). Used as a fallback title, for progress output and as the basis of the filename. Returns the full URL if the path does not start with `/wiki/`.

#### `_count_saved_wiki_pages(self, results)`

Counts entries in `results` whose status is in `WIKI_SAVED_STATUSES`. Used for progress messages and the `max_pages` limit in phase 2.

#### `_get_wiki_page_urls(self, session)`

Returns every content-page URL in the wiki, in discovery order and de-duplicated. `_index` only lists the top level: subdirectories appear as `<li class="folder">` links (e.g. `/wiki/Guides/`), so each folder's own listing is fetched and expanded in turn. Only `li.page` / `li.folder` entries are read, which also leaves out the "(delete)" links. Fragments are stripped and URLs filtered with `_should_skip_wiki_url()` (which drops `OldWiki`/`OldOldWiki`). The front page is not listed anywhere, so `WIKI_BASE_URL` is added first.

#### `_get_wiki_soup(self, session, url)`

Fetches one wiki URL and returns a `BeautifulSoup`, or `None` if the response is not HTTP 200 or not HTML. Runs `_check_wiki_session()` so expired cookies raise instead of returning a login page.

#### `_wiki_soup_to_text(self, node)`

Converts an HTML node to plain text while keeping inline elements inline. It inserts newlines around `WIKI_BLOCK_TAGS` and a space after table cells, then calls `get_text()` with no separator. (Using `get_text(separator="\n")` would put every `<code>`, `<a>` or `<strong>` on its own line, which breaks most sentences on this wiki.) Modifies `node` in place.

#### `_parse_wiki_page(self, url, soup, scrub)`

Turns one Gitit page into a page dict. Shared by both phases so they produce identical output.

1. Finds the `#wikipage` element; returns `None` if missing.
2. Removes the table of contents, edit-section links, footnotes and images.
3. Takes the title from the `<h1>` (falling back to `_wiki_page_title(url)`), then removes the `<h1>` so it is not repeated.
4. Rewrites links as Markdown `[text](absolute-url)`.
5. Converts to text with `_wiki_soup_to_text()`, removes image references (`.jpg/.jpeg/.gif/.svg/.webp`, plus `.png` via `clean_markdown_text()`), and cleans whitespace. Returns `None` if nothing is left.
6. Reads the page's category links.

Returns `{"title", "text", "source", "tags"}`, with title and text passed through `scrub()` and tags = `["wiki"] + categories`.

#### `_build_wiki_markdown(self, page)`

Renders a page dict as the final Markdown document: a `# title` heading (unless the text already starts with `#`), the body, then a footer section `## Wiki Page` with the source URL and comma-separated tags.

#### `_wiki_filename(self, url)`

Builds the filename `wiki_<slug>.md` from the URL path (each path segment stripped, spaces → `_`, then `generate_sanitized_filename(..., 150, True)`). The wiki root becomes `wiki_index.md`. Files are named from the **path, not the title**, because two pages can share the same `<h1>` and would otherwise overwrite each other.

#### `_save_wiki_page(self, url, soup, results, scrub)`

Parses the page with `_parse_wiki_page()`. If it yields nothing, records `skipped`; otherwise builds the Markdown, saves it with `save_document()` under `_wiki_filename(url)`, and stores the result in `results[url]`.

#### `_wiki_extract_index_pages(self, session, results, scrub, max_pages=None)`

**Phase 1.** Gets all URLs from `_get_wiki_page_urls()` (trimmed to `max_pages` if set) and, for each one, fetches it and calls `_save_wiki_page()`. If a listed page 404s it is retried with a leading space (`%20`) in its name, since the listing drops leading spaces ("HPC Support/ SLURM" is listed as `.../SLURM`). Pages that still cannot be fetched are `skipped`, other exceptions are `failed`, and `WikiAuthenticationError` is re-raised to abort the run. Waits `WIKI_REQUEST_DELAY` between pages.

#### `_wiki_crawl_links(self, session, results, scrub, max_pages=None)`

**Phase 2.** Breadth-first crawl starting at `WIKI_BASE_URL`, used to find pages that exist but are not listed on `_index`.

- Keeps its own `visited` set of *links followed*, separate from *pages saved*. Phase 1 pages are still visited so their outgoing links are followed; they are just not saved again. (Seeding `visited` with the phase 1 URLs would stop the crawl on its first page.)
- A page is saved only if its filename is not already among the saved results — compared by filename because the same page appears under different URL spellings (e.g. `%3c` vs `%3C`).
- Queues links on the same host, under `/wiki/`, that pass `_should_skip_wiki_url()`.
- Stops when `max_pages` saved pages are reached. Re-raises `WikiAuthenticationError`; records other errors as `failed`.

---

<a id="utility-methods"></a>
### Utility methods

#### `build_generation_result(fetched, created, updated, unchanged, skipped, failed, changed_files)` *(staticmethod)*

Prints a "KNOWLEDGE GENERATION COMPLETE" summary and returns it as a dict with the same keys. This is the return format of both scraper entry points.

#### `_summarize_results(self, results)`

Converts a scraper's `{url: result}` map into `build_generation_result()`: `fetched` is the number of URLs with a result, each status is counted, and `changed_files` lists the paths of `created` and `updated` files (useful for deciding what to re-upload).

#### `generate_sanitized_filename(self, name, maxLength, hasSpaces)`

Turns a title or URL into a safe filename, truncated to `maxLength`. Returns `""` for empty input.

- `hasSpaces=True` (titles — wiki/Jira/video): replaces only characters that are illegal in filenames (`<>:"/\|?*`) with `_` and collapses whitespace.
- `hasSpaces=False` (website URLs): replaces everything except letters, digits, `_` and `-` with `_`, collapses repeated `_`, and trims `_` from the ends.

#### `save_document(self, filename, content)`

Writes `content` (stripped, with a single trailing newline) to `output_folder/filename` and reports what happened:

| Situation | Action | Returned `status` |
|---|---|---|
| File does not exist | write | `created` |
| File exists, same content | nothing | `unchanged` |
| File exists, different content | overwrite | `updated` |

Returns `{"status": ..., "file": Path}`. Because unchanged files are not rewritten, rerunning a scraper on unchanged sources reports all `unchanged` and leaves `changed_files` empty.

#### `clean_markdown_text(self, text)`

General text cleanup: removes `.png` references (full URLs and bare filenames), collapses three or more newlines to two, collapses runs of spaces/tabs to one space, strips the ends and adds a trailing newline. Returns `""` for empty input.

#### `strip_html_tags(self, html)`

Lightweight HTML → text without BeautifulSoup: turns `<br>`, `</p>` and `</li>` into newlines, removes all other tags, unescapes HTML entities, then runs `clean_markdown_text()`. Not used by the website or wiki scrapers; kept for the sources still being ported (e.g. Jira descriptions).

#### `UVA_ID_RE` (class attribute)

Regex for UVA computing IDs: 2–4 letters, 1–2 digits, then 1–3 letters/digits (e.g. `abc1d`, `ab12cd`), case-insensitive, on word boundaries.

#### `scrub_text(self, text, entities=("PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"), scrub_uva_ids=True)`

Removes personal information from `text`:

1. Runs presidio's analyzer for the given `entities` and replaces matches with placeholders such as `<EMAIL_ADDRESS>`.
2. If `scrub_uva_ids` is true, also replaces computing IDs matching `UVA_ID_RE` with `<UVA_ID>`.

The presidio engines load a large spaCy model, so they are created lazily on the first call and reused afterwards. Empty input is returned unchanged.

---

<a id="netbadge-login"></a>
## `netbadge_login(username, password, target=None, output=None, timeout=120, debug_dir=None)`

Module-level function that logs into the wiki through NetBadge + Duo in a headless Chromium (Playwright **sync** API) and saves the session cookies as JSON. Returns the path of the cookie file.

- `target` defaults to `WIKI_LOGIN_TARGET`; `output` defaults to `wiki_default_cookies_path()`.
- `timeout` is how many seconds to wait for the Duo approval.
- `debug_dir`, if given, receives screenshots of the Duo page (`debug_duo.png`) and of the final page on timeout (`debug_final.png`). Duo sometimes blocks headless browsers, and a screenshot is usually the only way to see why.

**Requires a person:** someone must approve the Duo push on their phone, so this is meant to be run from an interactive script (`tasks.refresh_wiki_auth()`), never from a scheduled job. Because it uses Playwright's sync API, it must not be called from inside a running asyncio event loop.

Login flow:

1. **Certificate / username-password page.** Opens `target`. If the "Certificate Login Failed" page appears, clicks *Continue*. When the login form appears, fills username and password and submits by pressing **Enter** in the password field (clicking the visible button can submit the separate certificate-login form instead).
2. **Duo.** If the wiki is not yet shown, clicks the first matching push button ("Send Me a Push", "Send push", …), then prints the 3-digit **Duo Verified Push code** found on the page so the user can type it into Duo Mobile.
3. **Wait for the wiki.** Polls every 5 s until the URL is on `rci.hpc.virginia.edu/wiki/` and no longer on the NetBadge/Duo hosts. While waiting it keeps printing any new Duo code and clicks intermediate buttons such as "Trust this browser", "Stay signed in" or "Continue". Raises `WikiAuthenticationError` on timeout.
4. **Save cookies.** Writes all browser cookies to `output` (creating the parent folder) and closes the browser.

### Inner helpers

- **`save_debug(page, name)`** — saves a screenshot `<debug_dir>/<name>.png` if `debug_dir` is set; failures are only printed as warnings.
- **`report_duo_code(page)`** — reads the page text and prints every new 3-digit number as a highlighted `DUO CODE`. If no code has been found yet, prints a short snippet of the page text instead (only when it changes), so a 2-digit code, a device picker or a bot-block message is still visible in the console.
