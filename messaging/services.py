import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
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


@dataclass(frozen=True)
class IncomingTextMessage:
    sender_id: str
    chat_id: str
    message_id: str
    text: str
    is_group: bool


def _post_json(url: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'X-API-Key': api_key,
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
    if not settings.OPENWA_API_URL or not settings.OPENWA_API_KEY or not settings.OPENWA_SESSION_ID:
        raise IntegrationError('OpenWA API URL, API key, and session ID must be configured.')
    api_url = settings.OPENWA_API_URL.rstrip('/')
    if not api_url.startswith(('http://', 'https://')):
        api_url = f'http://{api_url}'
    chat_id = recipient.strip()
    if '@' not in chat_id:
        chat_id = f'{chat_id}@c.us'
    session_id = quote(settings.OPENWA_SESSION_ID, safe='')
    _post_json(
        f'{api_url}/sessions/{session_id}/messages/send-text',
        {'chatId': chat_id, 'text': message[:4096]},
        settings.OPENWA_API_KEY,
    )


def iter_incoming_text_messages(
    payload: dict[str, Any],
) -> Iterator[IncomingTextMessage]:
    if payload.get('event') != 'message.received':
        return
    message = payload.get('data')
    if not isinstance(message, dict):
        return
    kind = message.get('kind')
    if (
        message.get('fromMe') is True
        or message.get('isStatusBroadcast') is True
        or kind == 'status'
        or kind == 'broadcast'
    ):
        return

    sender = (
        message.get('senderPhone')
        or message.get('author')
        or message.get('sender')
        or message.get('from')
    )
    chat_id = message.get('chatId') or message.get('from') or sender
    message_id = message.get('id') or message.get('messageId')
    text = message.get('body') or message.get('content')
    if (
        isinstance(sender, str)
        and sender
        and isinstance(chat_id, str)
        and chat_id
        and isinstance(message_id, str)
        and message_id
        and isinstance(text, str)
        and text.strip()
    ):
        is_group = (
            message.get('isGroup') is True
            or kind == 'group'
            or chat_id.endswith('@g.us')
        )
        yield IncomingTextMessage(
            sender_id=sender,
            chat_id=chat_id,
            message_id=message_id,
            text=text.strip(),
            is_group=is_group,
        )
