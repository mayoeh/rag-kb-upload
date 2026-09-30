import argparse

from app.kb_integration.tasks import (
    update_all_sources,
    update_jira_knowledge,
    update_website_knowledge,
    update_video_knowledge,
    update_markdown_knowledge,
    update_knowledge_base,
    update_wiki_knowledge,
    refresh_wiki_auth,
)


def main():
    parser = argparse.ArgumentParser(
        description="RC Knowledge Base Integration"
    )

    parser.add_argument(
        "task",
        choices=[
            "jira",
            "website",
            "video",
            "markdown",
            "wiki",
            "scrape",
            "upload",
            "auth",
        ],
        help=(
            "jira = update JIRA knowledge, "
            "website = update website knowledge, "
            "video = update Youtube knowledge, "
            "markdown = update tutorial knowledge, "
            "wiki = update RCI wiki knowledge, "
            "scrape = update all knowledge sources, "
            "upload = upload existing files to Open WebUI, "
            "auth = refresh the NetBadge cookies the wiki needs"
        ),
    )

    args = parser.parse_args()

    if args.task == "jira":
        update_jira_knowledge()

    elif args.task == "website":
        update_website_knowledge()

    elif args.task == "video":
        update_video_knowledge()

    elif args.task == "markdown":
        update_markdown_knowledge()

    elif args.task == "wiki":
        update_wiki_knowledge()

    elif args.task == "scrape":
        update_all_sources()

    elif args.task == "upload":
        update_knowledge_base()

    elif args.task == "auth":
        # Interactive: prompts for credentials and waits for a Duo push,
        # so this is the one task that cannot be scheduled.
        refresh_wiki_auth()


if __name__ == "__main__":
    main()