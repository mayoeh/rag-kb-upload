import os
from pathlib import Path

from .business import (
    upload_file,
    add_file_to_knowledge_base
)


# Root of the repository
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# KB files are stored in /data
KNOWLEDGE_FOLDER = PROJECT_ROOT / "data"

ALLOWED_EXTENSIONS = {
    ".md",
    ".txt",
}


def update_knowledge_base():
    """Upload all generated files to the Knowledge Base."""

    print("=" * 60)
    print("Open WebUI Knowledge Base Uploader")
    print("=" * 60)

    print(f"Folder: {KNOWLEDGE_FOLDER}")
    print()

    if not KNOWLEDGE_FOLDER.exists():
        print("ERROR: Knowledge folder does not exist.")
        return

    files = [
        file
        for file in KNOWLEDGE_FOLDER.rglob("*")
        if file.is_file()
        and file.suffix.lower() in ALLOWED_EXTENSIONS
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
            if add_file_to_knowledge_base(
                file_id,
                file_path.name
            ):
                successful += 1
            else:
                failed += 1
        else:
            failed += 1

        print()

    print("=" * 60)
    print("UPLOAD COMPLETE")
    print("=" * 60)

    print(f"Total files: {len(files)}")
    print(f"Successful:  {successful}")
    print(f"Failed:      {failed}")