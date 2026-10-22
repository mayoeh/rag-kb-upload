import argparse

from app.kb_integration.tasks import (
    update_all_sources,
    update_jira_knowledge,
    update_website_knowledge,
    update_video_knowledge,
    update_wiki_knowledge,
    refresh_wiki_auth,
    update_knowledge_base,
)


def main():
    parser = argparse.ArgumentParser(
        description="RC Knowledge Base Integration"
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="wiki only: stop after this many pages (for quick tests)",
    )

    parser.add_argument(
        "task",
        choices=[
            "jira",
            "website",
            "video",
            "wiki",
            "wiki-login",
            "scrape",
            "upload",
        ],
        help=(
            "jira = update JIRA knowledge, "
            "website = update website knowledge (v2), "
            "video = update Youtube knowledge, "
            "wiki = update RCI wiki knowledge (v2), "
            "wiki-login = refresh NetBadge cookies (needs Duo), "
            "scrape = update all knowledge sources, "
            "upload = upload existing files to Open WebUI"
        ),
    )

    args = parser.parse_args()

    if args.task == "jira":
        update_jira_knowledge()

    elif args.task == "website":
        update_website_knowledge()

    elif args.task == "video":
        update_video_knowledge()

    elif args.task == "wiki":
        update_wiki_knowledge(max_pages=args.max_pages)

    elif args.task == "wiki-login":
        refresh_wiki_auth()

    elif args.task == "scrape":
        update_all_sources()

    elif args.task == "upload":
        update_knowledge_base()


if __name__ == "__main__":
    main()