"""
hr/reports/aisensy.py - sends one WhatsApp message through AiSensy's campaign API.

WhatsApp (through Meta) only allows a business to message someone it hasn't heard back from in the
last 24 hours using an approved message TEMPLATE - a plain free-text message would simply be
refused. So this expects an AiSensy campaign built on a template with ONE body variable that holds
the whole report text (see areas/hrms-whatsapp-report.md for how to set that template up).

Three settings, read from the environment (see app/core/config.py):
    AISENSY_API_KEY        - from the AiSensy dashboard
    AISENSY_CAMPAIGN_NAME  - the campaign/template name created in AiSensy for this report
    AISENSY_SENDER_NAME    - optional; the "userName" AiSensy shows as the sender (default below)

None of these exist yet (the super admin's AiSensy account isn't set up) - until they are added to
the .env file (locally) or the Render environment (in production), send_text_report() raises
AiSensyError immediately with that exact explanation. Nothing is silently skipped or retried.
"""
import requests

from app.core.config import get_setting

AISENSY_URL = "https://backend.aisensy.com/campaign/t1/api/v2"
REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_SENDER_NAME = "HR Assistant"

NOT_CONFIGURED_MESSAGE = ("AiSensy is not set up yet (AISENSY_API_KEY and/or AISENSY_CAMPAIGN_NAME are "
                          "missing). See areas/hrms-whatsapp-report.md for how to create the template "
                          "and where to add these settings.")


class AiSensyError(Exception):
    """AiSensy is not configured yet, or refused the request."""


def is_configured():
    return bool(get_setting("AISENSY_API_KEY") and get_setting("AISENSY_CAMPAIGN_NAME"))


def send_text_report(destination_number, report_text):
    """destination_number: a WhatsApp number with a country code (a leading "+" is fine; AiSensy
    wants digits only, so it is stripped here). report_text becomes the template's one body variable."""
    if not is_configured():
        raise AiSensyError(NOT_CONFIGURED_MESSAGE)
    payload = {
        "apiKey": get_setting("AISENSY_API_KEY"),
        "campaignName": get_setting("AISENSY_CAMPAIGN_NAME"),
        "destination": destination_number.lstrip("+"),
        "userName": get_setting("AISENSY_SENDER_NAME", DEFAULT_SENDER_NAME),
        "templateParams": [report_text],
    }
    try:
        response = requests.post(AISENSY_URL, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as error:
        raise AiSensyError(f"Could not reach AiSensy: {error}")
    if not response.ok:
        raise AiSensyError(f"AiSensy refused the message ({response.status_code}): {response.text[:300]}")
    return response.json()
