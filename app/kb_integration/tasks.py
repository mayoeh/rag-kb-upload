from pathlib import Path
import time

from .business_v2 import (
    RCKBKnowledgeManager,
    WikiAuthenticationError,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_FOLDER = PROJECT_ROOT / "data"
ALLOWED_EXTENSIONS = {".md", ".txt"}

MAX_RETRIES = 5
RETRY_DELAY = 2


# Source Updates

def update_jira_knowledge(full_refresh=False):
    # Scrape JIRA and generate Markdown knowledge documents

    print("Updating JIRA Knowledge")

    manager = RCKBKnowledgeManager(
        output_folder=KNOWLEDGE_FOLDER / "jira"
    )

    return manager.generate_jira_knowledge(full_refresh=full_refresh)

def update_website_knowledge():
    print("Updating RC Website(s) Knowledge")

    manager = RCKBKnowledgeManager(
        output_folder=KNOWLEDGE_FOLDER / "website"
    )

    return manager.generate_website_knowledge()

def update_video_knowledge():
    print("Updating Video Knowledge")

    manager = RCKBKnowledgeManager(
        output_folder=KNOWLEDGE_FOLDER / "video"
    )

    return manager.generate_video_knowledge()

def update_wiki_knowledge(max_pages=None):
    # Needs NetBadge cookies from `python run.py --wiki-login`. They cannot
    # be refreshed unattended (Duo push), so expired cookies raise
    # WikiAuthenticationError.

    print("Updating RCI Wiki Knowledge")

    manager = RCKBKnowledgeManager(
        output_folder=KNOWLEDGE_FOLDER / "wiki"
    )

    return manager.generate_wiki_knowledge(max_pages=max_pages)

def update_all_sources():
    # Regenerate local knowledge documents for every source.

    print("Updating Knowledge Sources")

    results = {}

    results["jira"] = update_jira_knowledge()
    results["website"] = update_website_knowledge()
    results["video"] = update_video_knowledge()

    # Expired wiki cookies must not take the other sources down with them
    try:
        results["wiki"] = update_wiki_knowledge()
    except WikiAuthenticationError as error:
        print(f"Skipping wiki: {error}")
        results["wiki"] = {"error": str(error)}

    print()
    print("Knowledge Source Update Complete")

    return results


# Open WebUI Upload Tasks

def add_file_with_retry(
    manager,
    file_id,
    file_name,
):


    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):
        print(
            f"  Attempt {attempt}/{MAX_RETRIES} "
            f"to add {file_name} to Knowledge Base..."
        )

        if manager.add_file_to_knowledge_base(
            file_id,
            file_name,
        ):
            return True

        if attempt < MAX_RETRIES:
            print(
                "  File may still be processing. "
                f"Waiting {RETRY_DELAY} seconds..."
            )

            time.sleep(RETRY_DELAY)

    print(
        f"  Failed after {MAX_RETRIES} attempts."
    )

    return False


def update_knowledge_base():
    # Upload local knowledge documents to Open WebUI.
    # UVARCKnowledgeBaseManager was removed with the old business.py and has
    # not been ported to business_v2 yet. add_file_with_retry() above is kept
    # for that port.

    raise NotImplementedError(
        "Open WebUI upload is being ported to business_v2"
    )
