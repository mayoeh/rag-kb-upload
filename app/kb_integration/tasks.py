from pathlib import Path
import time

from .business import (
    UVARCJiraKnowledgeDataManager,
    UVARCKnowledgeBaseManager,
    UVARCWebsiteKnowledgeDataManager
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_FOLDER = PROJECT_ROOT / "data"
ALLOWED_EXTENSIONS = {".md", ".txt"}

MAX_RETRIES = 5
RETRY_DELAY = 2


# Source Updates

def update_jira_knowledge():
    # Scrape JIRA and generate Markdown knowledge documents

    print("Updating JIRA Knowledge")

    jira_folder = KNOWLEDGE_FOLDER / "jira"

    manager = UVARCJiraKnowledgeDataManager(
        output_folder=jira_folder
    )

    result = manager.generate_knowledge_documents()

    print()
    print("JIRA Update Complete")
    print(f"Fetched:   {result['fetched']}")
    print(f"Generated: {result['generated']}")
    print(f"Skipped:   {result['skipped']}")
    print(f"Failed:    {result['failed']}")

    return result

def update_website_knowledge():

    print("Updating RC Website(s) Knowledge")

    website_folder = KNOWLEDGE_FOLDER / "website"

    manager = UVARCWebsiteKnowledgeDataManager(
        output_folder=website_folder
    )

    manager.generate_knowledge_documents()

    print()
    print("Website Update Complete")

def update_all_sources():
    # Regenerate local knowledge documents for every source.

    print("Updating Knowledge Sources")

    results = {}

    results["jira"] = update_jira_knowledge()


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
    # Upload existing local knowledge documents to Open WebUI
    # Does not access scrapers

    print("Updating Open WebUI Knowledge Base")

    print(f"Folder: {KNOWLEDGE_FOLDER}")
    print()

    if not KNOWLEDGE_FOLDER.exists():
        print(
            "ERROR: Knowledge folder does not exist."
        )
        return

    files = [
        file
        for file in KNOWLEDGE_FOLDER.rglob("*")
        if (
            file.is_file()
            and file.suffix.lower()
            in ALLOWED_EXTENSIONS
        )
    ]

    print(f"Found {len(files)} files.")
    print()

    if not files:
        print(
            "No .md or .txt files found."
        )
        return

    manager = UVARCKnowledgeBaseManager()

    successful = 0
    failed = 0

    for file_path in files:
        file_id = manager.upload_file(
            file_path
        )

        if file_id:
            if add_file_with_retry(
                manager,
                file_id,
                file_path.name,
            ):
                successful += 1
            else:
                failed += 1

        else:
            failed += 1

        print()

    print("Upload Complete")

    print(f"Total files: {len(files)}")
    print(f"Successful:  {successful}")
    print(f"Failed:      {failed}")