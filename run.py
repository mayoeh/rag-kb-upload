import argparse
from app.kb_integration.business import RCKBKnowledgeManager

def main():
    parser = argparse.ArgumentParser(description="Build RC knowledge base files")
    parser.add_argument("--website", action="store_true", help="Crawl the RC websites")
    parser.add_argument("--jira", action="store_true", help="Pull recent JIRA tickets")
    parser.add_argument("--full", action="store_true", help="With --jira: pull all tickets, not just the last 2 days")
    args = parser.parse_args()

    # No flags = run everything
    run_all = not (args.website or args.jira)

    if args.website or run_all:
        RCKBKnowledgeManager(output_folder="data/website").generate_website_knowledge()

    if args.jira or run_all:
        RCKBKnowledgeManager(output_folder="data/jira").generate_jira_knowledge(full_refresh=args.full)

if __name__ == "__main__":
    main()