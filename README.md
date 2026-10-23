# rag-kb-upload

## Knowledge Base Integration

This project retrieves content from UVA Research Computing knowledge sources, converts it into Markdown, and stores it locally under `data/`. A separate step uploads the generated documents to an Open WebUI knowledge base for the UVA GenAI RAG used by the RC support team.

All scraper logic lives in one class, `RCKBKnowledgeManager`, in `app/kb_integration/business_v2.py`. See [`business_v2.md`](app/kb_integration/business_v2.md) for a function-by-function reference.

The notebooks under `scrapers/` are kept for development and reference and are not used by the workflow.

## Knowledge Sources

| Source | Entry point (`RCKBKnowledgeManager`) | Output folder | File naming |
| --- | --- | --- | --- |
| JIRA (SUP, RIV, IVY, ACCORDSUP) | `generate_jira_knowledge(full_refresh=False)` | `data/jira/` | `jira_SUP-1234.md` |
| RC websites | `generate_website_knowledge()` | `data/website/` | sanitized URL, e.g. `https_rc_virginia_edu_userinfo_hpc.md` |
| YouTube (RC Tutorial Series) | `generate_video_knowledge()` | `data/video/` | `youtube_<video id>.md` |
| RCI wiki (behind NetBadge) | `generate_wiki_knowledge()` | `data/wiki/` | `wiki_<page path>.md`, e.g. `wiki_Guides_HPC_Support_SLURM.md` |

## Project Structure

```text
rag-kb-upload/
├── app/
│   └── kb_integration/
│       ├── business_v2.py    # RCKBKnowledgeManager: all scrapers + netbadge_login()
│       ├── business_v2.md    # function reference
│       └── tasks.py          # update_* task wrappers (for Celery)
├── config/
├── data/                     # generated documents (not committed)
├── scrapers/                 # development notebooks
├── .env.example
├── requirements
└── run.py                    # command-line entry point
```

### `tasks.py`

Thin wrappers that pick the output folder and call the matching entry point. `update_all_sources()` runs every source and skips the wiki, with a message, if its cookies have expired.

```text
update_jira_knowledge(full_refresh=False)
update_website_knowledge()
update_video_knowledge()
update_wiki_knowledge(max_pages=None)
update_all_sources()
update_knowledge_base()   # not ported yet, raises NotImplementedError
```

## Setup

```bash
pip install -r requirements
python -m spacy download en_core_web_lg   # model used by Presidio for PII scrubbing
playwright install chromium               # headless browser for the wiki login
```

Copy `.env.example` to `.env` and fill in the JIRA and Open WebUI settings. The wiki needs no credentials in `.env`; see [Wiki login](#wiki-login).

## Local Document Updates

Each source uses a stable identifier for its filename, so a rerun overwrites a document instead of creating a duplicate. Newly generated content is compared with the existing file:

```text
File does not exist       -> Created
File exists and is same   -> Unchanged
File exists and changed   -> Updated
```

Every run prints and returns the same summary:

```text
Fetched     items the source offered and this run attempted
Created / Updated / Unchanged
Skipped     nothing worth saving (empty page, form-only ticket)
Failed      error while processing that item
```

Video and wiki runs also list **stale files**: documents in the output folder whose source no longer exists (a deleted wiki page, a video removed from the playlist). They are reported, not deleted.

## Running the Scrapers

Run commands from the repository root.

```bash
python run.py                    # all sources
python run.py --jira             # recent tickets (updated in the last 2 days)
python run.py --jira --full      # all tickets
python run.py --website
python run.py --video
python run.py --wiki
python run.py --wiki --max-pages 5   # quick test
```

Flags can be combined, e.g. `python run.py --jira --video`.

**Running a scraper does not upload files to Open WebUI.**

### Wiki login

The RCI wiki is behind NetBadge, which requires a Duo approval on a phone, so the wiki scraper reads saved cookies instead of logging in itself:

```bash
python run.py --wiki-login
```

This asks for your computing ID and password, prints a 3-digit Duo code to enter in Duo Mobile, and saves the cookies to `scrapers/auth_cookies.json` (git-ignored; set `WIKI_AUTH_COOKIES_PATH` to use another location). It replaces the older `scrapers/save_auth.py`.

The cookies have been seen to expire in under 7.5 hours. When they do, the wiki is skipped with a message and the other sources still run; log in again and rerun `python run.py --wiki`.

## Uploading to Open WebUI

Uploading is being ported to `business_v2.py` and is not available yet: `tasks.update_knowledge_base()` raises `NotImplementedError` and `run.py` has no upload option.

The previous uploader re-sent every file on each run and never removed the old copies, so the knowledge base accumulated duplicates. The port is expected to upload only `changed_files` and replace the earlier copy of an updated document.

Upload remains a separate step from scraping, so generated documents can be inspected before they are uploaded.


## Unified Services Repo Outline via Mohamed

## What this repo is
A **Python Flask backend** for UVA Research Computing that unifies integrations like:
- JIRA ticketing
- LDAP user/group data
- Workday FDM validation
- LibCal + Qualtrics workshop analytics
- MongoDB-backed resource request workflows

## Core tech stack
- **Python + Flask** (`app/__init__.py`)
- **Flask-RESTful** for API resources
- **Flasgger/Swagger** for API docs
- **MongoDB** via `Flask-PyMongo`
- **Celery** for async/background jobs
- **uWSGI + Nginx + Supervisor** for runtime process management
- **Docker** and **Kubernetes (Helm-style env folders)** for deployment

## Code organization

### `app/` (main application)
Feature-based modules, each with a similar pattern:
- `views.py`: route/resource registration
- `endpoints.py`: HTTP endpoint classes
- `business.py`: domain/data logic
- `tasks.py`: celery tasks (where applicable)

Main modules:
- `core/` → shared user/group/resource data management, LDAP sync logic
- `resource_requests/` → major workflow for HPC/storage request lifecycle
- `ticket_requests/` → support/office-hours ticket creation
- `workshop_visualization/` → attendance/survey analytics endpoints

### `common_service_handlers/`
Thin integration clients/wrappers for external systems:
- `jira_service_handler.py`
- `workday_service_handler.py`
- `ldap_service_handler.py`
- `libcal_service_handler.py`
- `qualtrics_service_handler.py`
- etc.

### `common_utils/`
Shared constants/utilities/auth/error handling:
- `auth.py` (token auth + SSO glue)
- `rest_exception.py`
- `__init__.py` (resource tier constants, CORS checks, billing metadata)

### Config/runtime/deploy
- `config/base.py` → primary runtime config (reads `SETTINGS_JSON` env)
- `instance/settings.py` is symlinked to `config/base.py` in Docker
- `docker/Dockerfile`, `docker-compose.yml` for local/container runs
- `kubernetes/{dev,test,prod,prod-rancher}/` for environment manifests
- `supervisor_app.conf`, `nginx_app.conf`, `uvarc_unified_service_wsgi.ini` for process/web stack

## How app boots
- `run.py` imports `app` and starts Flask (dev/simple run path).
- `app/__init__.py` creates Flask app, loads config, wires extensions (CORS/auth/mongo/celery/swagger), registers blueprints, and imports task modules.
- Production container runs via **Supervisor**, starting **uWSGI + Nginx + Celery worker**.

## Architectural style (in practice)
A modular Flask monolith with:
- API layer (`endpoints/views`)
- business/data managers (`business.py`)
- integration adapters (`common_service_handlers`)
- async processing (`Celery`) for background sync/processing tasks
