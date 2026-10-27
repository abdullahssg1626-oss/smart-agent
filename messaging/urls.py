from django.urls import path

from .views import whatsapp_webhook

urlpatterns = [
    path('baileys/', whatsapp_webhook, name='baileys-webhook'),
]
