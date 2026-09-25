# rag-kb-upload

## Knowledge Base Integration

This project retrieves content from UVA Research Computing knowledge sources, converts the content into Markdown, stores it locally under `data/`, and provides a separate process for uploading generated documents to Open WebUI.

Production scraper logic is located in:

```text
app/kb_integration/
├── business.py
└── tasks.py
```

The notebooks under `scrapers/` are retained for development and reference, but are not used by the production workflow.

## Knowledge Sources

Generated knowledge documents are stored under the repository's `data/` directory:

| Source | Manager | Output folder |
| --- | --- | --- |
| JIRA | `UVARCJiraKnowledgeDataManager` | `data/jira/` |
| RC Learning / Markdown | `UVARCMarkdownKnowledgeDataManager` | `data/markdown/` |
| Website | `UVARCWebsiteKnowledgeDataManager` | `data/website/` |
| Wiki | `UVARCWikiKnowledgeDataManager` | `data/wiki/` |
| Video / YouTube | `UVARCVideoKnowledgeDataManager` | `data/video/` |

## Project Structure

```text
rag-kb-upload/
├── app/
│   └── kb_integration/
│       ├── __init__.py
│       ├── business.py
│       └── tasks.py
├── data/
│   ├── jira/
│   ├── markdown/
│   ├── video/
│   ├── website/
│   └── wiki/
├── scrapers/
├── .env
├── .env.example
├── requirements.txt
└── run.py
```

### `business.py`

Contains the source-specific knowledge managers and Open WebUI integration.

```text
UVARCLocalKnowledgeDataManager
UVARCJiraKnowledgeDataManager
UVARCMarkdownKnowledgeDataManager
UVARCVideoKnowledgeDataManager
UVARCWebsiteKnowledgeDataManager
UVARCWikiKnowledgeDataManager
UVARCKnowledgeBaseManager
```

### `tasks.py`

Contains the tasks used to run individual source updates, all source updates, and Open WebUI uploads.

```text
update_jira_knowledge()
update_markdown_knowledge()
update_video_knowledge()
update_website_knowledge()
update_wiki_knowledge()
update_all_sources()
update_knowledge_base()
```

## Local Document Updates

Each source uses a stable identifier when generating its local filename.

Examples:

```text
JIRA issue key       -> jira_SUP-1234.md
YouTube video ID     -> youtube_abc123.md
GitHub source path   -> tutorials_slurm.md
Website URL/path     -> userinfo_rivanna_overview.md
Wiki page ID/path    -> wiki_184928.md
```

When a document is scraped, its newly generated content is compared with the existing local file.

```text
File does not exist       -> Created
File exists and is same   -> Unchanged
File exists and changed   -> Updated
```

Existing files are overwritten when their source content changes rather than creating duplicate local files.

## Running the Scrapers

Run commands from the repository root.

### JIRA

```bash
python run.py jira
```

### Video

```bash
python run.py video
```

### Markdown / RC Learning

```bash
python run.py markdown
```

Additional individual source commands can be added as the Website and Wiki managers are integrated.

### All Sources

```bash
python run.py scrape
```

This runs all currently integrated source updates and generates/updates the local Markdown documents under `data/`.

**Running a scraper does not upload files to Open WebUI.**

## Uploading to Open WebUI

Uploading is a separate operation:

```bash
python run.py upload
```

This recursively discovers `.md` and `.txt` files under `data/` and uses `UVARCKnowledgeBaseManager` to upload them and attach them to the configured Open WebUI knowledge base.

Configure the required Open WebUI and source credentials in `.env` before running the application.

## Full Manual Refresh

To update all local knowledge documents and then upload them:

```bash
python run.py scrape
python run.py upload
```

These operations remain separate so generated knowledge documents can be inspected before they are uploaded.


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
