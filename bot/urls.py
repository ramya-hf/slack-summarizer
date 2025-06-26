from django.urls import path
from . import views

app_name = 'bot'

urlpatterns = [
    # Main Slack event handler endpoint
    path('events/', views.slack_event_handler, name='slack_events'),
    
    # Health check endpoint
    path('health/', views.health_check, name='health_check'),
    
    # Bot information endpoint
    path('info/', views.bot_info, name='bot_info'),
]
