import hashlib
import hmac
import json
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from .history import DjangoChatMessageHistory
from .models import Conversation, ConversationTurn
from .services import (
    IntegrationError,
    _post_json,
    generate_reply,
    iter_incoming_text_messages,
    send_whatsapp_message,
)


@override_settings(OPENWA_WEBHOOK_SECRET='openwa-test-secret')
class WhatsAppWebhookTests(TestCase):
    @staticmethod
    def post_openwa_message(
        client,
        message_id='openwa.message-1',
        signature_header=None,
        include_signature=True,
    ):
        body = json.dumps({
            'event': 'message.received',
            'timestamp': '2026-10-30T12:00:00.000Z',
            'sessionId': 'test-session',
            'idempotencyKey': f'msg_{message_id}',
            'deliveryId': 'delivery-1',
            'data': {
                'id': message_id,
                'from': '15551234567@c.us',
                'to': '15557654321@c.us',
                'chatId': '15551234567@c.us',
                'body': 'Hi',
                'type': 'text',
                'fromMe': False,
                'kind': 'individual',
            },
        }).encode('utf-8')
        signature = 'sha256=' + hmac.new(
            b'openwa-test-secret', body, hashlib.sha256,
        ).hexdigest()
        headers = {}
        if include_signature:
            headers['HTTP_X_OPENWA_SIGNATURE'] = signature_header or signature
        return client.post(
            '/api/webhook/',
            data=body,
            content_type='application/json',
            **headers,
        )

    def test_home_page_reports_service_status_and_webhook_path(self):
        response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                'status': 'ok',
                'service': 'whatsapp-business-agent',
                'webhook': '/api/webhook/',
            },
        )

    @patch('messaging.views.send_whatsapp_message')
    @patch('messaging.views.generate_reply', return_value='Hello!')
    def test_openwa_text_message_gets_a_reply(self, generate_reply_mock, send_mock):
        response = self.post_openwa_message(self.client)

        self.assertEqual(response.status_code, 200)
        generate_reply_mock.assert_called_once()
        turn = ConversationTurn.objects.get(whatsapp_message_id='openwa.message-1')
        self.assertEqual(turn.user_message, 'Hi')
        self.assertEqual(turn.conversation.phone_number, '15551234567@c.us')
        self.assertEqual(turn.assistant_message, 'Hello!')
        self.assertEqual(turn.status, ConversationTurn.Status.REPLIED)
        send_mock.assert_called_once_with('15551234567@c.us', 'Hello!')

    @override_settings(OPENWA_WEBHOOK_SECRET='', DEBUG=False)
    @patch('messaging.views.send_whatsapp_message')
    @patch('messaging.views.generate_reply', return_value='Hello!')
    def test_openwa_message_without_configured_secret_returns_200(
        self,
        generate_reply_mock,
        send_mock,
    ):
        response = self.post_openwa_message(self.client, include_signature=False)

        self.assertEqual(response.status_code, 200)
        generate_reply_mock.assert_called_once()
        send_mock.assert_called_once_with('15551234567@c.us', 'Hello!')

    @override_settings(DEBUG=True)
    @patch('messaging.views.send_whatsapp_message')
    @patch('messaging.views.generate_reply', return_value='Hello!')
    def test_debug_mode_skips_invalid_openwa_signature(
        self,
        generate_reply_mock,
        send_mock,
    ):
        response = self.post_openwa_message(
            self.client,
            signature_header='sha256=invalid',
        )

        self.assertEqual(response.status_code, 200)
        generate_reply_mock.assert_called_once()
        send_mock.assert_called_once_with('15551234567@c.us', 'Hello!')

    @patch('messaging.views.send_whatsapp_message')
    @patch('messaging.views.generate_reply', return_value='Hello!')
    def test_duplicate_webhook_message_is_not_answered_twice(
        self,
        generate_reply_mock,
        send_mock,
    ):
        first_response = self.post_openwa_message(self.client, 'openwa.duplicate')
        duplicate_response = self.post_openwa_message(self.client, 'openwa.duplicate')

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertEqual(ConversationTurn.objects.count(), 1)
        generate_reply_mock.assert_called_once()
        send_mock.assert_called_once()

    @patch(
        'messaging.views.send_whatsapp_message',
        side_effect=[IntegrationError('OpenWA API unavailable.'), None],
    )
    @patch('messaging.views.generate_reply', return_value='Saved reply')
    def test_retry_reuses_persisted_reply_after_send_failure(
        self,
        generate_reply_mock,
        send_mock,
    ):
        failed_response = self.post_openwa_message(self.client, 'openwa.retry')
        retry_response = self.post_openwa_message(self.client, 'openwa.retry')

        self.assertEqual(failed_response.status_code, 502)
        self.assertEqual(retry_response.status_code, 200)
        turn = ConversationTurn.objects.get(whatsapp_message_id='openwa.retry')
        self.assertEqual(turn.assistant_message, 'Saved reply')
        self.assertEqual(turn.status, ConversationTurn.Status.REPLIED)
        generate_reply_mock.assert_called_once()
        self.assertEqual(send_mock.call_count, 2)

    @override_settings(DEBUG=False)
    def test_invalid_openwa_signature_is_rejected_in_production(self):
        response = self.client.post(
            '/api/webhook/',
            data='{}',
            content_type='application/json',
            HTTP_X_OPENWA_SIGNATURE='sha256=invalid',
        )

        self.assertEqual(response.status_code, 403)

    @override_settings(VERCEL_DEPLOYMENT=True, DATABASE_URL='')
    def test_webhook_reports_missing_persistent_database(self):
        body = b'{}'
        signature = 'sha256=' + hmac.new(
            b'openwa-test-secret', body, hashlib.sha256,
        ).hexdigest()
        response = self.client.post(
            '/api/webhook/',
            data=body,
            content_type='application/json',
            HTTP_X_OPENWA_SIGNATURE=signature,
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {'error': 'Persistent database is not configured for this deployment.'},
        )


class GroqServiceTests(TransactionTestCase):
    @override_settings(GROQ_API_KEY='')
    def test_generate_reply_requires_api_key(self):
        conversation = Conversation.objects.create(phone_number='15551234567')
        turn = ConversationTurn.objects.create(
            conversation=conversation,
            whatsapp_message_id='wamid.no-key',
            user_message='Hi',
        )

        with self.assertRaises(IntegrationError):
            generate_reply(conversation, turn)

    def test_incoming_message_iterator_uses_openwa_payload_and_ignores_events(self):
        message = next(iter_incoming_text_messages({
            'event': 'message.received',
            'data': {
                'id': 'openwa.text',
                'from': '15557654321@c.us',
                'chatId': '15557654321@c.us',
                'body': '  Help  ',
                'fromMe': False,
            },
        }))
        self.assertEqual(
            (
                message.sender_id,
                message.chat_id,
                message.message_id,
                message.text,
            ),
            ('15557654321@c.us', '15557654321@c.us', 'openwa.text', 'Help'),
        )
        self.assertEqual(
            list(iter_incoming_text_messages({
                'event': 'message.received',
                'data': {
                    'id': 'openwa.image',
                    'from': '15551234567@c.us',
                    'chatId': '15551234567@c.us',
                    'body': '',
                    'fromMe': False,
                },
            })),
            [],
        )
        group_message = next(iter_incoming_text_messages({
            'event': 'message.received',
            'data': {
                'id': 'openwa.group',
                'from': '120363000000000000@g.us',
                'author': '15551234567@c.us',
                'chatId': '120363000000000000@g.us',
                'body': 'Group message',
                'fromMe': False,
                'isGroup': True,
            },
        }))
        self.assertEqual(group_message.sender_id, '15551234567@c.us')
        self.assertEqual(group_message.chat_id, '120363000000000000@g.us')
        self.assertTrue(group_message.is_group)
        self.assertEqual(
            list(iter_incoming_text_messages({
                'event': 'message.ack',
                'data': {
                    'id': 'openwa.status',
                    'chatId': '15551234567@c.us',
                    'body': 'Not an incoming message',
                },
            })),
            [],
        )
        self.assertEqual(
            list(iter_incoming_text_messages({
                'event': 'message.received',
                'data': {
                    'id': 'openwa.self',
                    'chatId': '15551234567@c.us',
                    'body': 'Self message',
                    'fromMe': True,
                },
            })),
            [],
        )

    @override_settings(GROQ_API_KEY='groq-key')
    def test_langchain_reply_uses_and_persists_conversation_history(self):
        conversation = Conversation.objects.create(phone_number='15551234567')
        first_turn = ConversationTurn.objects.create(
            conversation=conversation,
            whatsapp_message_id='wamid.first',
            user_message='Hello',
            status=ConversationTurn.Status.PROCESSING,
        )

        with patch(
            'messaging.services.ChatGroq',
            return_value=RunnableLambda(
                lambda prompt_value: AIMessage(
                    content=f'messages={len(prompt_value.to_messages())}',
                ),
            ),
        ) as chat_model:
            first_reply = generate_reply(conversation, first_turn)

        first_turn.refresh_from_db()
        self.assertEqual(first_reply, 'messages=2')
        self.assertEqual(first_turn.assistant_message, 'messages=2')
        self.assertEqual(first_turn.status, ConversationTurn.Status.PROCESSING)
        self.assertEqual(chat_model.call_args.kwargs['model'], 'llama-3.3-70b-versatile')

        first_turn.status = ConversationTurn.Status.REPLIED
        first_turn.save(update_fields=('status',))
        second_turn = ConversationTurn.objects.create(
            conversation=conversation,
            whatsapp_message_id='wamid.second',
            user_message='What did I say?',
            status=ConversationTurn.Status.PROCESSING,
        )

        with patch(
            'messaging.services.ChatGroq',
            return_value=RunnableLambda(
                lambda prompt_value: AIMessage(
                    content=f'messages={len(prompt_value.to_messages())}',
                ),
            ),
        ):
            second_reply = generate_reply(conversation, second_turn)

        self.assertEqual(second_reply, 'messages=4')
        history = DjangoChatMessageHistory(conversation.pk, second_turn.pk)
        self.assertEqual(len(history.messages), 2)
        self.assertEqual(history.messages[0].content, 'Hello')
        self.assertEqual(history.messages[1].content, 'messages=2')

    @override_settings(
        OPENWA_API_URL='http://127.0.0.1:2785/api',
        OPENWA_API_KEY='openwa-api-key',
        OPENWA_SESSION_ID='session-1',
    )
    @patch('messaging.services._post_json', return_value={'id': 'message-id'})
    def test_send_whatsapp_message_uses_openwa_api(self, post_mock):
        send_whatsapp_message('15551234567', 'Hello!')

        self.assertEqual(
            post_mock.call_args.args[0],
            'http://127.0.0.1:2785/api/sessions/session-1/messages/send-text',
        )
        self.assertEqual(
            post_mock.call_args.args[1],
            {'chatId': '15551234567@c.us', 'text': 'Hello!'},
        )
        self.assertEqual(post_mock.call_args.args[2], 'openwa-api-key')

    @override_settings(
        OPENWA_API_URL='openwa.example.com/api',
        OPENWA_API_KEY='openwa-api-key',
        OPENWA_SESSION_ID='session-1',
    )
    @patch('messaging.services._post_json')
    def test_send_whatsapp_message_adds_http_scheme_when_missing(self, post_mock):
        send_whatsapp_message('15551234567@c.us', 'Hello!')

        self.assertEqual(
            post_mock.call_args.args[0],
            'http://openwa.example.com/api/sessions/session-1/messages/send-text',
        )

    @patch('messaging.services.urlopen')
    def test_post_json_authenticates_with_openwa_api_key(self, urlopen_mock):
        urlopen_mock.return_value.__enter__.return_value.read.return_value = b'{}'

        response = _post_json(
            'http://127.0.0.1:2785/api/sessions/session-1/messages/send-text',
            {'chatId': '15551234567@c.us', 'text': 'Hello!'},
            'openwa-api-key',
        )

        request = urlopen_mock.call_args.args[0]
        self.assertEqual(request.get_header('X-api-key'), 'openwa-api-key')
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(response, {})
