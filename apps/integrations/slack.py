import logging
import os
import json
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

QGP_BASE_URL = os.getenv("QGP_BASE_URL", "http://localhost:8000")


def send_slack_message(channel: str, blocks: list, text: str = ""):
    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token:
        logger.warning("SLACK_BOT_TOKEN not set, skipping notification")
        return False

    payload = {
        "channel": channel,
        "text": text,
        "blocks": blocks,
    }

    req = Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )

    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                logger.error(f"Slack API error: {result.get('error')}")
                return False
            return True
    except URLError as e:
        logger.error(f"Slack API request failed: {e}")
        return False


def build_gate_result_blocks(gate_run) -> list:
    result_emoji = ":white_check_mark:" if gate_run.result == "PASS" else ":x:"
    env_name = gate_run.environment.name
    service_name = gate_run.service.name
    portal_url = f"{QGP_BASE_URL}/gates/{gate_run.pk}/"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{result_emoji} Quality Gate {gate_run.result}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Service:*\n{service_name}"},
                {"type": "mrkdwn", "text": f"*Environment:*\n{env_name}"},
                {"type": "mrkdwn", "text": f"*Version:*\n{gate_run.version or 'N/A'}"},
                {"type": "mrkdwn", "text": f"*Duration:*\n{gate_run.duration_seconds or 'N/A'}s"},
            ],
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*API Test:*\n{gate_run.api_test_result or 'N/A'}"},
                {"type": "mrkdwn", "text": f"*E2E Rate:*\n{gate_run.e2e_test_result or 'N/A'}%"},
            ],
        },
    ]

    # Failure Top 3
    failures = gate_run.failures.all()[:3]
    if failures:
        fail_text = "\n".join(
            f"• `{f.test_name}` - {f.error_message[:80]}" for f in failures
        )
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Fail Summary (Top {len(failures)}):*\n{fail_text}",
            },
        })

    # Actions
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Open Report"},
                "url": portal_url,
                "action_id": "open_report",
            },
        ],
    })

    return blocks
