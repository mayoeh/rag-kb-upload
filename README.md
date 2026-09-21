# rag-kb-upload

## Scraper output and knowledge-base uploads

Generated Markdown files are stored under the repository's `data/` directory:

| Scraper | Output folder |
| --- | --- |
| `scrapers/scrape-jira-new.ipynb` | `data/jira/` |
| `scrapers/scrape-md-only.ipynb` | `data/markdown/` |
| `scrapers/scrape-website-new.ipynb` | `data/website/` |
| `scrapers/scrape-wiki-only.ipynb` | `data/wiki/` |

The non-Jira notebooks can run with the repository root or `scrapers/` as
their working directory. They create their output folders automatically.

Run `python run.py` from the repository root to upload the generated files.
`app/kb_integration/tasks.py` recursively discovers all `.md` and `.txt` files
under `data/`, including deeper subfolders and uppercase extensions, and uses
`business.py` to upload each file and attach it to the configured knowledge base.
Other file types are skipped. Configure the Open WebUI settings in `.env` first.

Run `python -m unittest discover -s tests -v` to verify notebook exports and
uploads across sources using temporary files and simulated HTTP responses.

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
