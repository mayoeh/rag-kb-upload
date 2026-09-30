#!/usr/bin/env python3
"""
Refresh the NetBadge session cookies used by the RCI wiki scraper.

NetBadge requires a Duo push approved on a phone, so this cannot run
unattended — that is why it is a script and not a Celery task. Run it
whenever update_wiki_knowledge() reports expired cookies.

Usage, from the repository root:

    python scripts/save_wiki_auth.py             # log in and save cookies
    python scripts/save_wiki_auth.py --check     # test the saved cookies
    python scripts/save_wiki_auth.py --debug     # also save Duo screenshots

First-time setup on a machine:

    pip install playwright
    playwright install chromium
"""

import argparse
import getpass
import sys

from pathlib import Path


# Allow running this file directly from the repository root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.kb_integration.business import (  # noqa: E402
    UVARCWikiKnowledgeDataManager,
    WikiAuthenticationError,
    netbadge_login,
)


def check_cookies(cookies_path):
    # Report whether the saved cookies still reach the wiki.

    manager = UVARCWikiKnowledgeDataManager(
        output_folder=PROJECT_ROOT / "data" / "wiki",
        cookies_path=cookies_path,
    )

    print(f"Checking cookies at: {manager.cookies_path}")

    try:
        manager.make_session()
        manager.verify_session()
    except WikiAuthenticationError as error:
        print(f"[FAIL] {error}")
        return False

    print("[OK] Cookies are still valid.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Refresh NetBadge cookies for the RCI wiki scraper"
        )
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "only test the saved cookies; do not log in"
        ),
    )

    parser.add_argument(
        "--output",
        default=None,
        help=(
            "where to write the cookies "
            "(default: scrapers/auth_cookies.json)"
        ),
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help=(
            "seconds to wait for the Duo approval "
            "(default: 120)"
        ),
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "save screenshots of the Duo page and of any timeout"
        ),
    )

    args = parser.parse_args()

    cookies_path = Path(
        args.output
        or UVARCWikiKnowledgeDataManager.default_cookies_path()
    )

    if args.check:
        return 0 if check_cookies(cookies_path) else 1

    print("=" * 50)
    print("UVA NetBadge Login (headless)")
    print("=" * 50)

    username = input("UVA Computing ID : ")
    password = getpass.getpass("Password         : ")
    print()

    try:
        netbadge_login(
            username,
            password,
            output=cookies_path,
            timeout=args.timeout,
            debug_dir=(
                PROJECT_ROOT / "scrapers" / "debug"
                if args.debug
                else None
            ),
        )
    except WikiAuthenticationError as error:
        print()
        print(f"[FAIL] {error}")
        print(
            "Re-run with --debug to capture screenshots of "
            "the Duo page."
        )
        return 1

    print()
    print("Verifying the new cookies ...")

    return 0 if check_cookies(cookies_path) else 1


if __name__ == "__main__":
    sys.exit(main())
