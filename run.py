import argparse
import getpass

from app.kb_integration.business_v2 import (
    RCKBKnowledgeManager,
    WikiAuthenticationError,
    netbadge_login,
)

def main():
    parser = argparse.ArgumentParser(description="Build RC knowledge base files")
    parser.add_argument("--website", action="store_true", help="Crawl the RC websites")
    parser.add_argument("--jira", action="store_true", help="Pull recent JIRA tickets")
    parser.add_argument("--full", action="store_true", help="With --jira: pull all tickets, not just the last 2 days")
    parser.add_argument("--video", action="store_true", help="Pull the RC YouTube playlist")
    parser.add_argument("--wiki", action="store_true", help="Scrape the RCI wiki (needs NetBadge cookies)")
    parser.add_argument("--wiki-login", action="store_true", help="Log in through NetBadge + Duo and save the wiki cookies")
    parser.add_argument("--max-pages", type=int, default=None, help="With --wiki: stop after this many pages (for quick tests)")
    args = parser.parse_args()

    # Interactive: waits for a Duo push, so never part of run_all
    if args.wiki_login:
        netbadge_login(input("UVA Computing ID : "), getpass.getpass("Password         : "))
        return

    # No flags = run everything
    run_all = not (args.website or args.jira or args.video or args.wiki)

    if args.website or run_all:
        RCKBKnowledgeManager(output_folder="data/website").generate_website_knowledge()

    if args.jira or run_all:
        RCKBKnowledgeManager(output_folder="data/jira").generate_jira_knowledge(full_refresh=args.full)

    if args.video or run_all:
        RCKBKnowledgeManager(output_folder="data/video").generate_video_knowledge()

    if args.wiki or run_all:
        # Expired cookies need a person to approve Duo, so report it instead of
        # crashing. Exit non-zero when the wiki was asked for explicitly.
        try:
            RCKBKnowledgeManager(output_folder="data/wiki").generate_wiki_knowledge(max_pages=args.max_pages)
        except WikiAuthenticationError as error:
            print(f"Wiki skipped: {error}")
            print("Refresh the cookies with: python run.py --wiki-login")
            if args.wiki:
                raise SystemExit(1)

if __name__ == "__main__":
    main()