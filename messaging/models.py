from django.db import models


class Conversation(models.Model):
    phone_number = models.CharField(max_length=32, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.phone_number


class ConversationTurn(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        FAILED = 'failed', 'Failed'
        REPLIED = 'replied', 'Replied'

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='turns',
    )
    whatsapp_message_id = models.CharField(max_length=255, unique=True)
    user_message = models.TextField()
    assistant_message = models.TextField(blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('created_at', 'pk')
        indexes = [
            models.Index(fields=('conversation', 'status', 'created_at')),
        ]

    def __str__(self) -> str:
        return f'{self.conversation.phone_number}: {self.whatsapp_message_id}'
