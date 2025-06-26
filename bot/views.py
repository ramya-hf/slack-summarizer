"""
Django views for handling Slack bot events and commands
"""
import json
import logging
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils.decorators import method_decorator
from django.conf import settings

from .slack import SlackBotHandler, verify_slack_signature

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["POST"])
def slack_event_handler(request):
    """
    Main handler for all Slack events and commands
    
    This endpoint handles:
    - Slash commands
    - Event subscriptions (messages, etc.)
    - Interactive components (buttons, etc.)
    """
    try:
        # Verify the request is from Slack
        timestamp = request.META.get('HTTP_X_SLACK_REQUEST_TIMESTAMP', '')
        signature = request.META.get('HTTP_X_SLACK_SIGNATURE', '')
        
        if not verify_slack_signature(request.body.decode(), timestamp, signature):
            logger.warning("Invalid Slack signature")
            return HttpResponse("Unauthorized", status=401)
        
        # Parse the request
        content_type = request.META.get('CONTENT_TYPE', '')
        
        if 'application/x-www-form-urlencoded' in content_type:
            # Slash command or interactive component
            return handle_slash_command(request)
        elif 'application/json' in content_type:
            # Event subscription
            return handle_event_subscription(request)
        else:
            logger.warning(f"Unsupported content type: {content_type}")
            return HttpResponse("Unsupported content type", status=400)
            
    except Exception as e:
        logger.error(f"Error in slack_event_handler: {str(e)}")
        return HttpResponse("Internal server error", status=500)


def handle_slash_command(request):
    """
    Handle Slack slash commands
    
    Args:
        request: Django HTTP request containing form data
        
    Returns:
        JsonResponse with Slack-formatted response
    """
    try:
        # Parse form data
        payload = {
            'token': request.POST.get('token'),
            'team_id': request.POST.get('team_id'),
            'team_domain': request.POST.get('team_domain'),
            'channel_id': request.POST.get('channel_id'),
            'channel_name': request.POST.get('channel_name'),
            'user_id': request.POST.get('user_id'),
            'user_name': request.POST.get('user_name'),
            'command': request.POST.get('command'),
            'text': request.POST.get('text'),
            'response_url': request.POST.get('response_url'),
            'trigger_id': request.POST.get('trigger_id'),
        }
        
        logger.info(f"Received slash command: {payload.get('command')} from user {payload.get('user_id')}")
        
        # Initialize bot handler and process command
        bot_handler = SlackBotHandler()
        response = bot_handler.process_slash_command(payload)
        
        return JsonResponse(response)
        
    except Exception as e:
        logger.error(f"Error handling slash command: {str(e)}")
        return JsonResponse({
            "response_type": "ephemeral",
            "text": "❌ An error occurred while processing your command. Please try again later."
        })


def handle_event_subscription(request):
    """
    Handle Slack event subscriptions
    
    Args:
        request: Django HTTP request containing JSON data
        
    Returns:
        HttpResponse or JsonResponse
    """
    try:
        event_data = json.loads(request.body.decode())
        
        # Handle URL verification challenge
        if event_data.get('type') == 'url_verification':
            challenge = event_data.get('challenge')
            logger.info("Responding to URL verification challenge")
            return JsonResponse({'challenge': challenge})
        
        # Handle event callbacks
        if event_data.get('type') == 'event_callback':
            event = event_data.get('event', {})
            event_type = event.get('type')
            
            logger.info(f"Received event: {event_type}")
            
            # Handle message events for follow-up questions
            if event_type == 'message':
                bot_handler = SlackBotHandler()
                bot_handler.process_message_event(event_data)
            
            # Handle other event types as needed
            # elif event_type == 'app_mention':
            #     handle_app_mention(event_data)
            
            return HttpResponse("OK")
        
        # Handle other event types
        logger.info(f"Unhandled event type: {event_data.get('type')}")
        return HttpResponse("OK")
        
    except json.JSONDecodeError:
        logger.error("Invalid JSON in event subscription")
        return HttpResponse("Bad request", status=400)
    except Exception as e:
        logger.error(f"Error handling event subscription: {str(e)}")
        return HttpResponse("Internal server error", status=500)


@csrf_exempt
@require_http_methods(["GET"])
def health_check(request):
    """
    Health check endpoint for monitoring
    
    Returns:
        JsonResponse with health status
    """
    try:
        # Basic health checks
        health_status = {
            "status": "healthy",
            "timestamp": json.dumps(request.META.get('timestamp', ''), default=str),
            "checks": {
                "database": "ok",  # You can add actual DB checks here
                "slack_config": "ok" if settings.SLACK_BOT_TOKEN else "missing_token",
                "gemini_config": "ok" if settings.GEMINI_API_KEY else "missing_token"
            }
        }
        
        # Determine overall status
        if any(check != "ok" for check in health_status["checks"].values()):
            health_status["status"] = "degraded"
            return JsonResponse(health_status, status=503)
        
        return JsonResponse(health_status)
        
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return JsonResponse({
            "status": "unhealthy",
            "error": str(e)
        }, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def bot_info(request):
    """
    Get information about the bot configuration and status
    
    Returns:
        JsonResponse with bot information
    """
    try:
        info = {
            "bot_name": "Slack Channel Summarizer",
            "version": "1.0.0",
            "features": [
                "Channel message summarization",
                "Follow-up question answering",
                "Multi-channel support",
                "AI-powered analysis"
            ],
            "commands": [
                {
                    "command": "/summary",
                    "description": "Summarize current channel (last 24 hours)",
                    "usage": "/summary"
                },
                {
                    "command": "/summary [channel-name]",
                    "description": "Summarize specific channel",
                    "usage": "/summary general"
                }
            ],
            "configuration": {
                "slack_configured": bool(settings.SLACK_BOT_TOKEN),
                "ai_configured": bool(settings.GEMINI_API_KEY),
                "debug_mode": settings.DEBUG
            }
        }
        
        return JsonResponse(info)
        
    except Exception as e:
        logger.error(f"Error getting bot info: {str(e)}")
        return JsonResponse({
            "error": "Failed to get bot information"
        }, status=500)
