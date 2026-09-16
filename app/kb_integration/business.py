import os
from pathlib import Path

import requests
from dotenv import load_dotenv


load_dotenv()

OPEN_WEBUI_URL = os.getenv("OPENWEBUI_URL")
API_KEY = os.getenv("OPENWEBUI_API_KEY")
KNOWLEDGE_BASE_ID = os.getenv("OPENWEBUI_KB_ID")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}"
}


def upload_file(file_path):
    """Upload one file to Open WebUI."""

    print(f"Uploading: {file_path.name}")

    try:
        with open(file_path, "rb") as f:
            response = requests.post(
                f"{OPEN_WEBUI_URL}/api/v1/files/",
                headers=HEADERS,
                files={
                    "file": (
                        file_path.name,
                        f,
                        "text/plain"
                    )
                },
                data={
                    "metadata": "{}"
                }
            )

        if response.status_code not in (200, 201):
            print(
                f"  ERROR uploading file "
                f"(HTTP {response.status_code})"
            )
            print(f"  {response.text}")
            return None

        result = response.json()
        file_id = result.get("id")

        print("  Uploaded successfully.")
        print(f"  File ID: {file_id}")

        return file_id

    except Exception as e:
        print(f"  ERROR: {e}")
        return None


def add_file_to_knowledge_base(file_id, file_name):
    """Add an uploaded file to the Knowledge Base."""

    print(f"  Adding {file_name} to Knowledge Base...")

    try:
        response = requests.post(
            f"{OPEN_WEBUI_URL}/api/v1/knowledge/"
            f"{KNOWLEDGE_BASE_ID}/file/add",
            headers={
                **HEADERS,
                "Content-Type": "application/json"
            },
            json={
                "file_id": file_id
            }
        )

        if response.status_code not in (200, 201):
            print(
                f"  ERROR adding to Knowledge Base "
                f"(HTTP {response.status_code})"
            )
            print(f"  {response.text}")
            return False

        print("  Added to Knowledge Base successfully.")
        return True

    except Exception as e:
        print(f"  ERROR: {e}")
        return False