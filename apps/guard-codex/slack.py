import logging
import os

from apps.shared.config import settings
from slack_sdk import WebhookClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

def send_slack_notification(pr_url: str, pr_title: str, pr_number: int, status: str, author: str, changed_files: list[str]):
    """
    Sends a notification to Slack about a PR status change.

    Args:
        pr_url: The URL of the pull request.
        pr_title: The title of the pull request.
        pr_number: The number of the pull request.
        status: The new status of the PR (e.g., 'created', 'approved', 'blocked').
        author: The author of the PR.
        changed_files: A list of files changed in the PR.
    """
    webhook_url = settings.slack_webhook_url
    if not webhook_url:
        logger.info("Slack webhook URL not configured, skipping notification.")
        return

    webhook = WebhookClient(webhook_url)

    status_emoji = {
        "created": ":new:",
        "approved": ":white_check_mark:",
        "blocked": ":no_entry_sign:",
        "merged": ":rocket:",
    }.get(status, ":question:")

    title = f"{status_emoji} PR #{pr_number} {status.capitalize()}: {pr_title}"

    changed_files_str = "\n".join(f"- `{f}`" for f in changed_files[:5])
    if len(changed_files) > 5:
        changed_files_str += f"\n- ...and {len(changed_files) - 5} more."

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": title,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Author:*
{author}"},
                {"type": "mrkdwn", "text": f"*Status:*
{status.capitalize()}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Changed Files:*
{changed_files_str}",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "View Pull Request",
                    },
                    "url": pr_url,
                    "style": "primary",
                }
            ],
        },
    ]

    try:
        response = webhook.send(
            text=f"PR #{pr_number} {status.capitalize()}",
            blocks=blocks,
        )
        logger.info(f"Slack notification sent for PR #{pr_number}, status: {status}")
    except SlackApiError as e:
        logger.error(f"Error sending Slack notification for PR #{pr_number}: {e.response['error']}")
