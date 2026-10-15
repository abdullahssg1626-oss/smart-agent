import json
import logging
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from groq import APIError
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_groq import ChatGroq

from .history import DjangoChatMessageHistory
from .models import Conversation, ConversationTurn

logger = logging.getLogger(__name__)


class IntegrationError(Exception):
    pass


def _post_json(url: str, payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        logger.warning('External API returned HTTP %s.', exc.code)
        raise IntegrationError(f'External API returned HTTP {exc.code}.') from exc
    except (URLError, TimeoutError) as exc:
        raise IntegrationError('Could not connect to an external API.') from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError('External API returned an invalid response.') from exc

    if not isinstance(result, dict):
        raise IntegrationError('External API returned an invalid response.')
    return result


def generate_reply(conversation: Conversation, turn: ConversationTurn) -> str:
    if not settings.GROQ_API_KEY:
        raise IntegrationError('GROQ_API_KEY is not configured.')

    prompt = ChatPromptTemplate.from_messages(
        [
            ('system', settings.AGENT_SYSTEM_PROMPT),
            MessagesPlaceholder(variable_name='history'),
            ('human', '{input}'),
        ],
    )
    model = ChatGroq(
        model=settings.GROQ_MODEL,
        api_key=settings.GROQ_API_KEY,
        temperature=0.6,
        max_tokens=500,
        timeout=8,
        max_retries=0,
    )
    history = DjangoChatMessageHistory(conversation.pk, turn.pk)
    prompt_value = prompt.invoke({
        'history': history.messages,
        'input': turn.user_message,
    })
    try:
        response = model.invoke(prompt_value)
    except APIError as exc:
        logger.warning('Groq API request failed with HTTP %s.', exc.status_code)
        raise IntegrationError('Groq API request failed.') from exc

    if not isinstance(response.content, str) or not response.content.strip():
        raise IntegrationError('Groq returned an empty chat response.')
    reply = response.content.strip()[:4096]
    history.add_messages([
        HumanMessage(content=turn.user_message),
        AIMessage(content=reply),
    ])
    return reply


def send_whatsapp_message(recipient: str, message: str) -> None:
    if not settings.WHATSAPP_ACCESS_TOKEN or not settings.WHATSAPP_PHONE_NUMBER_ID:
        raise IntegrationError('WhatsApp access token or phone number ID is not configured.')

    url = (
        f'https://graph.facebook.com/{settings.WHATSAPP_GRAPH_API_VERSION}/'
        f'{settings.WHATSAPP_PHONE_NUMBER_ID}/messages'
    )
    _post_json(
        url,
        {
            'messaging_product': 'whatsapp',
            'recipient_type': 'individual',
            'to': recipient,
            'type': 'text',
            'text': {'preview_url': False, 'body': message[:4096]},
        },
        settings.WHATSAPP_ACCESS_TOKEN,
    )


def iter_incoming_text_messages(
    payload: dict[str, Any],
) -> Iterator[tuple[str, str, str]]:
    entries = payload.get('entry', [])
    if not isinstance(entries, list):
        return

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get('changes', [])
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get('value', {})
            if not isinstance(value, dict):
                continue
            messages = value.get('messages', [])
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict) or message.get('type') != 'text':
                    continue
                sender = message.get('from')
                message_id = message.get('id')
                text = message.get('text')
                body = text.get('body') if isinstance(text, dict) else None
                if (
                    isinstance(sender, str)
                    and sender
                    and isinstance(message_id, str)
                    and message_id
                    and isinstance(body, str)
                    and body.strip()
                ):
                    yield sender, message_id, body.strip()
