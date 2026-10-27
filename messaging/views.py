import hmac
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Conversation, ConversationTurn
from .services import (
    IntegrationError,
    generate_reply,
    iter_incoming_text_messages,
    send_whatsapp_message,
)

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(['POST'])
def whatsapp_webhook(request: HttpRequest) -> HttpResponse:
    bridge_token = settings.WHATSAPP_BRIDGE_TOKEN
    if not bridge_token or not hmac.compare_digest(
        request.headers.get('Authorization', ''),
        f'Bearer {bridge_token}',
    ):
        return JsonResponse({'error': 'Invalid bridge credentials.'}, status=403)

    if settings.VERCEL_DEPLOYMENT and not settings.DATABASE_URL:
        logger.error('WhatsApp webhook is unavailable because DATABASE_URL is missing.')
        return JsonResponse(
            {'error': 'Persistent database is not configured for this deployment.'},
            status=503,
        )

    try:
        payload = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'error': 'Request body must be valid JSON.'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'error': 'Request body must be a JSON object.'}, status=400)

    try:
        for sender, message_id, text in iter_incoming_text_messages(payload):
            with transaction.atomic():
                conversation, _ = Conversation.objects.get_or_create(
                    phone_number=sender,
                )
                turn, _ = ConversationTurn.objects.get_or_create(
                    whatsapp_message_id=message_id,
                    defaults={
                        'conversation': conversation,
                        'user_message': text,
                    },
                )
                turn = ConversationTurn.objects.select_for_update().get(pk=turn.pk)
                if turn.conversation_id != conversation.pk:
                    raise IntegrationError('WhatsApp message ID belongs to another conversation.')
                if turn.status == ConversationTurn.Status.REPLIED:
                    continue
                if (
                    turn.status == ConversationTurn.Status.PROCESSING
                    and turn.updated_at > timezone.now() - timedelta(minutes=2)
                ):
                    raise IntegrationError('This message is already being processed.')
                if not turn.assistant_message:
                    turn.status = ConversationTurn.Status.PROCESSING
                    turn.save(update_fields=('status', 'updated_at'))

            if turn.assistant_message:
                reply = turn.assistant_message
            else:
                try:
                    reply = generate_reply(conversation, turn)
                except IntegrationError:
                    ConversationTurn.objects.filter(pk=turn.pk).update(
                        status=ConversationTurn.Status.FAILED,
                        updated_at=timezone.now(),
                    )
                    raise
                turn.refresh_from_db()
                if not turn.assistant_message:
                    turn.assistant_message = reply
                    turn.save(update_fields=('assistant_message', 'updated_at'))

            try:
                send_whatsapp_message(sender, reply)
            except IntegrationError:
                ConversationTurn.objects.filter(pk=turn.pk).update(
                    status=ConversationTurn.Status.FAILED,
                    updated_at=timezone.now(),
                )
                raise
            turn.status = ConversationTurn.Status.REPLIED
            turn.save(update_fields=('status', 'updated_at'))
    except IntegrationError:
        logger.exception('WhatsApp webhook could not process an incoming message.')
        return JsonResponse({'error': 'Message processing failed; the bridge may retry.'}, status=502)

    return JsonResponse({'status': 'ok'})
