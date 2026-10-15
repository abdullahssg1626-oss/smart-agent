from collections.abc import Sequence

from django.db import transaction
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from .models import ConversationTurn

HISTORY_TURN_LIMIT = 20


class DjangoChatMessageHistory(BaseChatMessageHistory):
    def __init__(self, conversation_id: int, active_turn_id: int):
        self.conversation_id = conversation_id
        self.active_turn_id = active_turn_id

    @property
    def messages(self) -> list[BaseMessage]:
        turns = list(
            ConversationTurn.objects.filter(
                conversation_id=self.conversation_id,
                status=ConversationTurn.Status.REPLIED,
            )
            .exclude(pk=self.active_turn_id)
            .order_by('-created_at', '-pk')[:HISTORY_TURN_LIMIT],
        )
        history: list[BaseMessage] = []
        for turn in reversed(turns):
            history.extend((
                HumanMessage(content=turn.user_message),
                AIMessage(content=turn.assistant_message),
            ))
        return history

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        user_message = next(
            (message for message in messages if isinstance(message, HumanMessage)),
            None,
        )
        assistant_message = next(
            (message for message in messages if isinstance(message, AIMessage)),
            None,
        )
        if user_message is None or assistant_message is None:
            raise ValueError('Conversation history requires a human and assistant message.')
        if not isinstance(user_message.content, str) or not isinstance(
            assistant_message.content,
            str,
        ):
            raise ValueError('Conversation history only supports text messages.')

        with transaction.atomic():
            turn = ConversationTurn.objects.select_for_update().get(
                pk=self.active_turn_id,
                conversation_id=self.conversation_id,
            )
            turn.user_message = user_message.content
            turn.assistant_message = assistant_message.content
            turn.status = ConversationTurn.Status.PROCESSING
            turn.save(update_fields=(
                'user_message',
                'assistant_message',
                'status',
                'updated_at',
            ))

    def clear(self) -> None:
        ConversationTurn.objects.filter(
            conversation_id=self.conversation_id,
        ).delete()
