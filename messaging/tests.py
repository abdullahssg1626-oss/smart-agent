import json
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from .history import DjangoChatMessageHistory
from .models import Conversation, ConversationTurn
from .services import (
    IntegrationError,
    generate_reply,
    iter_incoming_text_messages,
    send_whatsapp_message,
)


@override_settings(WHATSAPP_BRIDGE_TOKEN='bridge-secret')
class WhatsAppWebhookTests(TestCase):
    @staticmethod
    def post_bridge_message(client, message_id='baileys.message-1'):
        return client.post(
            '/webhook/baileys/',
            data=json.dumps({
                'id': message_id,
                'from': '15551234567',
                'text': 'Hi',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer bridge-secret',
        )

    def test_home_page_reports_service_status_and_webhook_path(self):
        response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                'status': 'ok',
                'service': 'whatsapp-business-agent',
                'webhook': '/webhook/baileys/',
            },
        )

    @patch('messaging.views.send_whatsapp_message')
    @patch('messaging.views.generate_reply', return_value='Hello!')
    def test_bridge_text_message_gets_a_reply(self, generate_reply_mock, send_mock):
        response = self.post_bridge_message(self.client)

        self.assertEqual(response.status_code, 200)
        generate_reply_mock.assert_called_once()
        turn = ConversationTurn.objects.get(whatsapp_message_id='baileys.message-1')
        self.assertEqual(turn.user_message, 'Hi')
        self.assertEqual(turn.assistant_message, 'Hello!')
        self.assertEqual(turn.status, ConversationTurn.Status.REPLIED)
        send_mock.assert_called_once_with('15551234567', 'Hello!')

    @patch('messaging.views.send_whatsapp_message')
    @patch('messaging.views.generate_reply', return_value='Hello!')
    def test_duplicate_webhook_message_is_not_answered_twice(
        self,
        generate_reply_mock,
        send_mock,
    ):
        first_response = self.post_bridge_message(self.client, 'baileys.duplicate')
        duplicate_response = self.post_bridge_message(self.client, 'baileys.duplicate')

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertEqual(ConversationTurn.objects.count(), 1)
        generate_reply_mock.assert_called_once()
        send_mock.assert_called_once()

    @patch(
        'messaging.views.send_whatsapp_message',
        side_effect=[IntegrationError('WhatsApp API unavailable.'), None],
    )
    @patch('messaging.views.generate_reply', return_value='Saved reply')
    def test_retry_reuses_persisted_reply_after_send_failure(
        self,
        generate_reply_mock,
        send_mock,
    ):
        failed_response = self.post_bridge_message(self.client, 'baileys.retry')
        retry_response = self.post_bridge_message(self.client, 'baileys.retry')

        self.assertEqual(failed_response.status_code, 502)
        self.assertEqual(retry_response.status_code, 200)
        turn = ConversationTurn.objects.get(whatsapp_message_id='baileys.retry')
        self.assertEqual(turn.assistant_message, 'Saved reply')
        self.assertEqual(turn.status, ConversationTurn.Status.REPLIED)
        generate_reply_mock.assert_called_once()
        self.assertEqual(send_mock.call_count, 2)

    def test_invalid_bridge_token_is_rejected(self):
        response = self.client.post(
            '/webhook/baileys/',
            data='{}',
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer invalid',
        )

        self.assertEqual(response.status_code, 403)

    @override_settings(VERCEL_DEPLOYMENT=True, DATABASE_URL='')
    def test_webhook_reports_missing_persistent_database(self):
        response = self.client.post(
            '/webhook/baileys/',
            data='{}',
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer bridge-secret',
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

    def test_incoming_message_iterator_ignores_non_text_messages(self):
        self.assertEqual(
            list(iter_incoming_text_messages({
                'id': 'baileys.text',
                'from': '15557654321',
                'text': '  Help  ',
            })),
            [('15557654321', 'baileys.text', 'Help')],
        )
        self.assertEqual(
            list(iter_incoming_text_messages({
                'id': 'baileys.image',
                'from': '15551234567',
                'text': '',
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
        WHATSAPP_BRIDGE_URL='http://127.0.0.1:3001',
        WHATSAPP_BRIDGE_TOKEN='bridge-secret',
    )
    @patch('messaging.services._post_json', return_value={'messages': [{'id': 'message-id'}]})
    def test_send_whatsapp_message_uses_baileys_bridge(self, post_mock):
        send_whatsapp_message('15551234567', 'Hello!')

        self.assertEqual(
            post_mock.call_args.args[0],
            'http://127.0.0.1:3001/messages',
        )
        self.assertEqual(
            post_mock.call_args.args[1],
            {'to': '15551234567', 'message': 'Hello!'},
        )
        self.assertEqual(post_mock.call_args.args[2], 'bridge-secret')

    @override_settings(
        WHATSAPP_BRIDGE_URL='baileys-bridge.onrender.com',
        WHATSAPP_BRIDGE_TOKEN='bridge-secret',
    )
    @patch('messaging.services._post_json')
    def test_send_whatsapp_message_uses_https_for_render_hostname(self, post_mock):
        send_whatsapp_message('15551234567', 'Hello!')

        self.assertEqual(
            post_mock.call_args.args[0],
            'https://baileys-bridge.onrender.com/messages',
        )
