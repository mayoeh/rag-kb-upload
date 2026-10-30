import logging

from .business import RCKBKnowledgeManager, WikiAuthenticationError


logger = logging.getLogger(__name__)

# Source updates

def update_website_knowledge():
    logger.info("Updating RC website knowledge")
    result = RCKBKnowledgeManager().generate_website_knowledge()
    logger.info("Website knowledge update complete")
    return result


def update_jira_knowledge(full_refresh=False):
    # Incremental by default: tickets updated in the last 2 days. Run with
    # full_refresh=True once to build the full history.
    logger.info("Updating JIRA knowledge (full_refresh=%s)", full_refresh)
    result = RCKBKnowledgeManager().generate_jira_knowledge(full_refresh=full_refresh)
    logger.info("JIRA knowledge update complete")
    return result


def update_video_knowledge():
    logger.info("Updating video knowledge")
    result = RCKBKnowledgeManager().generate_video_knowledge()
    logger.info("Video knowledge update complete")
    return result


def update_wiki_knowledge():
    # Needs the NetBadge cookies saved by netbadge_login(). When they have
    # expired, someone has to log in again and approve the Duo push, so this
    # logs an error instead of retrying.
    logger.info("Updating wiki knowledge")
    try:
        result = RCKBKnowledgeManager().generate_wiki_knowledge()
    except WikiAuthenticationError as error:
        logger.error("Wiki knowledge update skipped: %s", error)
        return {"error": str(error)}

    logger.info("Wiki knowledge update complete")
    return result


def update_all_sources(full_jira_refresh=False):
    # Runs every source in turn. A failing source is logged and recorded,
    # and does not stop the others.
    logger.info("Updating all knowledge sources")

    updates = {
        "website": update_website_knowledge,
        "jira": lambda: update_jira_knowledge(full_refresh=full_jira_refresh),
        "videos": update_video_knowledge,
        "wiki": update_wiki_knowledge,
    }

    results = {}
    for source, update in updates.items():
        try:
            results[source] = update()
        except Exception as error:
            logger.exception("%s knowledge update failed", source)
            results[source] = {"error": str(error)}

    logger.info("Knowledge source update complete")
    return results