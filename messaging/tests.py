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
    generate_reply,
    iter_incoming_text_messages,
    send_whatsapp_message,
)


class WhatsAppWebhookTests(TestCase):
    @override_settings(WHATSAPP_VERIFY_TOKEN='verify-me')
    def test_meta_verification_returns_challenge(self):
        response = self.client.get(
            '/webhook/whatsapp/',
            {
                'hub.mode': 'subscribe',
                'hub.verify_token': 'verify-me',
                'hub.challenge': 'challenge-value',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'challenge-value')

    @override_settings(WHATSAPP_APP_SECRET='app-secret')
    @patch('messaging.views.send_whatsapp_message')
    @patch('messaging.views.generate_reply', return_value='Hello!')
    def test_signed_text_message_gets_a_reply(self, generate_reply_mock, send_mock):
        body = json.dumps(
            {
                'entry': [{
                    'changes': [{
                        'value': {
                            'messages': [{
                                'id': 'wamid.message-1',
                                'from': '15551234567',
                                'type': 'text',
                                'text': {'body': 'Hi'},
                            }],
                        },
                    }],
                }],
            },
        ).encode('utf-8')
        signature = hmac.new(b'app-secret', body, hashlib.sha256).hexdigest()

        response = self.client.post(
            '/webhook/whatsapp/',
            data=body,
            content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=f'sha256={signature}',
        )

        self.assertEqual(response.status_code, 200)
        generate_reply_mock.assert_called_once()
        turn = ConversationTurn.objects.get(whatsapp_message_id='wamid.message-1')
        self.assertEqual(turn.user_message, 'Hi')
        self.assertEqual(turn.assistant_message, 'Hello!')
        self.assertEqual(turn.status, ConversationTurn.Status.REPLIED)
        send_mock.assert_called_once_with('15551234567', 'Hello!')

    @override_settings(WHATSAPP_APP_SECRET='app-secret')
    @patch('messaging.views.send_whatsapp_message')
    @patch('messaging.views.generate_reply', return_value='Hello!')
    def test_duplicate_webhook_message_is_not_answered_twice(
        self,
        generate_reply_mock,
        send_mock,
    ):
        body = json.dumps(
            {
                'entry': [{
                    'changes': [{
                        'value': {
                            'messages': [{
                                'id': 'wamid.duplicate',
                                'from': '15551234567',
                                'type': 'text',
                                'text': {'body': 'Hi'},
                            }],
                        },
                    }],
                }],
            },
        ).encode('utf-8')
        signature = hmac.new(b'app-secret', body, hashlib.sha256).hexdigest()
        headers = {'HTTP_X_HUB_SIGNATURE_256': f'sha256={signature}'}

        first_response = self.client.post(
            '/webhook/whatsapp/',
            data=body,
            content_type='application/json',
            **headers,
        )
        duplicate_response = self.client.post(
            '/webhook/whatsapp/',
            data=body,
            content_type='application/json',
            **headers,
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertEqual(ConversationTurn.objects.count(), 1)
        generate_reply_mock.assert_called_once()
        send_mock.assert_called_once()

    @override_settings(WHATSAPP_APP_SECRET='app-secret')
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
        body = json.dumps(
            {
                'entry': [{
                    'changes': [{
                        'value': {
                            'messages': [{
                                'id': 'wamid.retry',
                                'from': '15551234567',
                                'type': 'text',
                                'text': {'body': 'Hi'},
                            }],
                        },
                    }],
                }],
            },
        ).encode('utf-8')
        signature = hmac.new(b'app-secret', body, hashlib.sha256).hexdigest()
        headers = {'HTTP_X_HUB_SIGNATURE_256': f'sha256={signature}'}

        failed_response = self.client.post(
            '/webhook/whatsapp/',
            data=body,
            content_type='application/json',
            **headers,
        )
        retry_response = self.client.post(
            '/webhook/whatsapp/',
            data=body,
            content_type='application/json',
            **headers,
        )

        self.assertEqual(failed_response.status_code, 502)
        self.assertEqual(retry_response.status_code, 200)
        turn = ConversationTurn.objects.get(whatsapp_message_id='wamid.retry')
        self.assertEqual(turn.assistant_message, 'Saved reply')
        self.assertEqual(turn.status, ConversationTurn.Status.REPLIED)
        generate_reply_mock.assert_called_once()
        self.assertEqual(send_mock.call_count, 2)

    @override_settings(WHATSAPP_APP_SECRET='app-secret')
    def test_invalid_signature_is_rejected(self):
        response = self.client.post(
            '/webhook/whatsapp/',
            data='{}',
            content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256='sha256=invalid',
        )

        self.assertEqual(response.status_code, 403)


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
        payload = {
            'entry': [{
                'changes': [{
                    'value': {
                        'messages': [
                            {'from': '15551234567', 'type': 'image'},
                            {
                                'id': 'wamid.text',
                                'from': '15557654321',
                                'type': 'text',
                                'text': {'body': '  Help  '},
                            },
                        ],
                    },
                }],
            }],
        }

        self.assertEqual(
            list(iter_incoming_text_messages(payload)),
            [('15557654321', 'wamid.text', 'Help')],
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
        WHATSAPP_ACCESS_TOKEN='whatsapp-token',
        WHATSAPP_PHONE_NUMBER_ID='phone-id',
        WHATSAPP_GRAPH_API_VERSION='v23.0',
    )
    @patch('messaging.services._post_json', return_value={'messages': [{'id': 'message-id'}]})
    def test_send_whatsapp_message_uses_cloud_api(self, post_mock):
        send_whatsapp_message('15551234567', 'Hello!')

        self.assertEqual(
            post_mock.call_args.args[0],
            'https://graph.facebook.com/v23.0/phone-id/messages',
        )
        self.assertEqual(
            post_mock.call_args.args[1],
            {
                'messaging_product': 'whatsapp',
                'recipient_type': 'individual',
                'to': '15551234567',
                'type': 'text',
                'text': {'preview_url': False, 'body': 'Hello!'},
            },
        )
        self.assertEqual(post_mock.call_args.args[2], 'whatsapp-token')
