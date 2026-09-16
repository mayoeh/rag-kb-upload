from pathlib import Path
import time

from .business import upload_file, add_file_to_knowledge_base


PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_FOLDER = PROJECT_ROOT / "data"
ALLOWED_EXTENSIONS = {".md", ".txt"}

MAX_RETRIES = 5
RETRY_DELAY = 2


def add_file_with_retry(file_id, file_name):
    for attempt in range(1, MAX_RETRIES + 1):
        print(
            f"  Attempt {attempt}/{MAX_RETRIES} "
            f"to add {file_name} to Knowledge Base..."
        )

        if add_file_to_knowledge_base(file_id, file_name):
            return True

        if attempt < MAX_RETRIES:
            print(
                f"  File may still be processing. "
                f"Waiting {RETRY_DELAY} seconds..."
            )
            time.sleep(RETRY_DELAY)

    print(f"  Failed after {MAX_RETRIES} attempts.")
    return False


def update_knowledge_base():
    print(f"Folder: {KNOWLEDGE_FOLDER}")
    print()

    if not KNOWLEDGE_FOLDER.exists():
        print("ERROR: Knowledge folder does not exist.")
        return

    files = [
        file
        for file in KNOWLEDGE_FOLDER.rglob("*")
        if file.is_file() and file.suffix.lower() in ALLOWED_EXTENSIONS
    ]

    print(f"Found {len(files)} files.")
    print()

    if not files:
        print("No .md or .txt files found.")
        return

    successful = 0
    failed = 0

    for file_path in files:
        file_id = upload_file(file_path)

        if file_id:
            if add_file_with_retry(file_id, file_path.name):
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