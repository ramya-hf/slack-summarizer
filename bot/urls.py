from django.urls import path
from . import views

urlpatterns = [
    path('events/', views.slack_event_handler),
]
