# instance/settings.py
#
# The minimum settings needed to run the RAG knowledge base scrapes and the
# Open WebUI upload. Loaded by app/__init__.py with
# app.config.from_pyfile("settings.py"); every UPPERCASE name here becomes
# app.config['<NAME>'].
#
# Credentials come from connections.json in the repo root (sibling of app/,
# data/ and instance/), so this file holds no secrets.

import json
from pathlib import Path

from celery.schedules import crontab


# instance/settings.py -> repo root is one folder up
with open(Path(__file__).resolve().parents[1] / "connections.json", encoding="utf-8") as _file:
    _connections = json.load(_file)


# ---------------------------------------------------------
# Jira: the RAG Jira scrape (JiraServiceHandler)
# ---------------------------------------------------------

JIRA_CONN_INFO = {
    "HOST": _connections["JIRA"]["HOSTS"][0],
    "PORT": _connections["JIRA"]["PORT"],
    "CLIENT_ID": _connections["JIRA"]["CLIENT_ID"],
    "PASSWORD": _connections["JIRA"]["CLIENT_SECRET"],
}

# Only used to create tickets, but JiraServiceHandler reads them at startup,
# so they must exist
JIRA_PROJECT_INFO_LOOKUP = {}
JIRA_PROJECT_REQUEST_TYPE_LOOKUP = {}
CUSTOMFIELD_VALUES = []
JIRA_CUSTOM_FIELDS = {}


# ---------------------------------------------------------
# Open WebUI: the upload (OpenWebUIServiceHandler)
# ---------------------------------------------------------

# A local Open WebUI runs on plain http; everything else uses https
OPEN_WEBUI_CONN_INFO = {
    "PROTOCOL": "http" if _connections.get("ENV") == "local" else "https",
    "HOST": _connections["OPEN_WEBUI"]["HOSTS"][0],
    "PORT": _connections["OPEN_WEBUI"]["PORT"],
    "PASSWORD": _connections["OPEN_WEBUI"]["CLIENT_SECRET"],
    "KNOWLEDGE_ID": _connections["OPEN_WEBUI"]["KNOWLEDGE_ID"],
}


# ---------------------------------------------------------
# MongoDB and Celery: required for app/__init__.py to start
# ---------------------------------------------------------

# PyMongo(app) refuses to start without MONGO_URI, and app/__init__.py uses
# CELERY_BROKER_URL as both the Celery broker and its MongoDB result backend
MONGO_URI = "mongodb://{}:{}@{}:{}/uvarc_unified_db".format(
    _connections["MONGODB"]["CLIENT_ID"],
    _connections["MONGODB"]["CLIENT_SECRET"],
    _connections["MONGODB"]["HOSTS"][0],
    _connections["MONGODB"]["PORT"],
)
CELERY_BROKER_URL = MONGO_URI
CELERY_BACKEND_SETTINGS = {
    "database": "uvarc_unified_db",
    "taskmeta_collection": "celery_taskmeta",
}

# The RAG schedule. Task names match @celery.task(name=...) in app/rag/tasks.py;
# times are in Celery's timezone (UTC unless set otherwise).
CELERY_BEAT_SCHEDULE = {
    "rag-jira-daily": {
        "task": "rag.generate_jira_knowledge",
        "schedule": crontab(hour=1, minute=0),
    },
    "rag-website-weekly": {
        "task": "rag.generate_website_knowledge",
        "schedule": crontab(day_of_week="sun", hour=2, minute=0),
    },
    "rag-videos-weekly": {
        "task": "rag.generate_video_knowledge",
        "schedule": crontab(day_of_week="sun", hour=3, minute=0),
    },
    "rag-wiki-weekly": {
        "task": "rag.generate_wiki_knowledge",
        "schedule": crontab(day_of_week="sun", hour=4, minute=0),
    },
    "rag-export-daily": {
        "task": "rag.export_to_knowledge_base",
        "schedule": crontab(hour=6, minute=0),
    },
}
