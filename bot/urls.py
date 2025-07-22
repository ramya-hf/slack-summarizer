from django.urls import path
from . import views

app_name = 'bot'

urlpatterns = [
    # Main Slack event handler endpoint
    path('events/', views.slack_event_handler, name='slack_events'),
    
    # Slash command endpoints
    path('task/', views.handle_slash_command, name='task_command'),
    path('category/', views.handle_slash_command, name='category_command'),
    path('summary/', views.handle_slash_command, name='summary_command'),
    
    # Interactive components endpoint
    path('interactive/', views.handle_interactive_component, name='interactive_components'),
    
    # Health check endpoint
    path('health/', views.health_check, name='health_check'),
]
