import hashlib
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


def _valid_signature(request: HttpRequest) -> bool:
    app_secret = settings.WHATSAPP_APP_SECRET
    signature = request.headers.get('X-Hub-Signature-256', '')
    if not app_secret or not signature.startswith('sha256='):
        return False

    expected = 'sha256=' + hmac.new(
        app_secret.encode('utf-8'),
        request.body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def whatsapp_webhook(request: HttpRequest) -> HttpResponse:
    if request.method == 'GET':
        if not settings.WHATSAPP_VERIFY_TOKEN:
            return JsonResponse({'error': 'Webhook verification is not configured.'}, status=503)
        if (
            request.GET.get('hub.mode') == 'subscribe'
            and hmac.compare_digest(
                request.GET.get('hub.verify_token', ''),
                settings.WHATSAPP_VERIFY_TOKEN,
            )
        ):
            return HttpResponse(request.GET.get('hub.challenge', ''), content_type='text/plain')
        return JsonResponse({'error': 'Webhook verification failed.'}, status=403)

    if not _valid_signature(request):
        return JsonResponse({'error': 'Invalid webhook signature.'}, status=403)

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
        return JsonResponse({'error': 'Message processing failed; Meta may retry.'}, status=502)

    return JsonResponse({'status': 'ok'})
