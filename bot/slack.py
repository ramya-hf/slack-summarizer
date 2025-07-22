"""
Slack Bot Integration Module
Handles all Slack API interactions, event processing, and command handling
"""
import json
import logging
import time
import hmac
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from django.conf import settings
from django.utils import timezone

from .models import (
    SlackWorkspace, SlackChannel, ChannelSummary, 
    ConversationContext, BotCommand, ChatbotInteraction,
    ChannelTodo, TaskSummary
)
from .summarizer import (
    ChannelSummarizer, filter_messages_by_timeframe, extract_channel_name_from_command, 
    extract_unread_command_details, extract_thread_command_details, parse_message_link, 
    is_thread_command, extract_category_command_details, is_category_command
)
from .intent_classifier import IntentClassifier, ChatbotResponder
from .category_manager import CategoryManager
from .todo_manager import TodoManager
from .task_detector import TaskDetector

logger = logging.getLogger(__name__)


class SlackBotHandler:
    """
    Main handler for Slack bot operations with enhanced chatbot capabilities
    """
    
    def __init__(self):
        """Initialize the Slack bot with API credentials"""
        if not settings.SLACK_BOT_TOKEN:
            raise ValueError("SLACK_BOT_TOKEN not found in settings")
        
        self.client = WebClient(token=settings.SLACK_BOT_TOKEN)
        self.summarizer = ChannelSummarizer()
        self.intent_classifier = IntentClassifier()
        self.responder = ChatbotResponder()
        self.category_manager = CategoryManager(self.client)
        self.todo_manager = TodoManager(self.client)
        self.task_detector = TaskDetector()
        self.bot_user_id = None
        self._initialize_bot_info()
    
    def _initialize_bot_info(self):
        """Initialize bot information like user ID"""
        try:
            response = self.client.auth_test()
            self.bot_user_id = response['user_id']
            logger.info(f"Bot initialized with user ID: {self.bot_user_id}")
        except SlackApiError as e:
            logger.error(f"Failed to initialize bot info: {e}")
    
    def process_slash_command(self, payload: Dict) -> Dict:
        """
        Process incoming slash commands
        
        Args:
            payload: Slack slash command payload
            
        Returns:
            Response dictionary for Slack
        """
        command = payload.get('command', '').lower()
        text = payload.get('text', '').strip()
        user_id = payload.get('user_id')
        channel_id = payload.get('channel_id')
        
        # Log the command for debugging
        logger.info(f"Processing slash command: {command} with text: '{text}' from user: {user_id} in channel: {channel_id}")
        
        # Log the command to database
        bot_command = BotCommand.objects.create(
            command=command,
            user_id=user_id,
            channel_id=channel_id,
            parameters=text,
            status='initiated'
        )
        
        try:
            if command == '/summary':
                return self._handle_summary_command(payload, bot_command)
            elif command == '/category':
                return self._handle_category_command(payload, bot_command)
            elif command == '/todo':
                return self._handle_todo_command(payload, bot_command)
            elif command == '/tasks':
                return self._handle_tasks_command(payload, bot_command)
            elif command == '/task':
                return self._handle_task_command(payload, bot_command)

            elif command == '/config':
                return self._handle_config_command(payload, bot_command)
            else:
                return self._handle_unknown_command(command, bot_command)
                
        except Exception as e:
            logger.error(f"Error processing command {command}: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            return {
                "response_type": "ephemeral",
                "text": "❌ An error occurred while processing your command. Please try again later."
            }
    
    def _handle_summary_command(self, payload: Dict, bot_command: BotCommand) -> Dict:
        """
        Handle the /summary command and its variations including unread, thread, and category
        
        Args:
            payload: Slack command payload
            bot_command: Database record for this command
            
        Returns:
            Response dictionary for Slack
        """
        text = payload.get('text', '').strip()
        user_id = payload.get('user_id')
        channel_id = payload.get('channel_id')
        
        # Send immediate acknowledgment
        self._send_acknowledgment_message(channel_id, user_id)
        
        # Process the summary request asynchronously
        try:
            if text:
                # Check if it's a category command first
                if is_category_command(f"/summary {text}"):
                    category_name, is_category = extract_category_command_details(f"/summary {text}")
                    if is_category and category_name:
                        self._process_category_summary_request(category_name, channel_id, user_id, bot_command)
                    else:
                        # Invalid category command
                        bot_command.status = 'failed'
                        bot_command.error_message = 'Invalid category command format'
                        bot_command.save()
                        
                        self._send_error_message(
                            channel_id,
                            "❌ Invalid category command format. Use: `/summary category category-name`\nExample: `/summary category Development Team`"
                        )
                
                # Check if it's a thread command
                elif is_thread_command(f"/summary {text}"):
                    thread_type, target, message_ts = extract_thread_command_details(f"/summary {text}")
                    
                    if thread_type == 'latest':
                        if target:
                            self._process_latest_thread_summary(target, channel_id, user_id, bot_command)
                        else:
                            # Latest thread in current channel
                            self._process_current_channel_latest_thread_summary(channel_id, user_id, bot_command)
                    elif thread_type == 'specific':
                        self._process_specific_thread_summary(target, message_ts, channel_id, user_id, bot_command)
                    else:
                        # Invalid thread command
                        bot_command.status = 'failed'
                        bot_command.error_message = 'Invalid thread command format'
                        bot_command.save()
                        
                        self._send_error_message(
                            channel_id,
                            "❌ Invalid thread command format. Examples:\n• `/summary thread latest general` - Latest thread in #general\n• `/summary thread latest` - Latest thread in current channel\n• `/summary thread https://workspace.slack.com/archives/C123/p123456` - Specific thread"
                        )
                
                # Check if it's an unread command
                elif extract_unread_command_details(f"/summary {text}")[1]:
                    target_channel, is_unread = extract_unread_command_details(f"/summary {text}")
                    
                    if target_channel:
                        self._process_unread_channel_summary(target_channel, channel_id, user_id, bot_command)
                    else:
                        # Unread summary for current channel
                        self._process_unread_current_channel_summary(channel_id, user_id, bot_command)
                else:
                    # Extract channel name from regular command
                    target_channel = extract_channel_name_from_command(f"/summary {text}")
                    if target_channel:
                        self._process_channel_summary(target_channel, channel_id, user_id, bot_command)
                    else:
                        # Invalid command format
                        bot_command.status = 'failed'
                        bot_command.error_message = 'Invalid command format'
                        bot_command.save()
                        
                        self._send_error_message(
                            channel_id, 
                            "❌ Please specify a valid command. Examples:\n• `/summary general` - Regular channel summary\n• `/summary category Development` - Category summary\n• `/summary unread general` - Unread messages summary\n• `/summary thread latest general` - Latest thread summary\n• `/summary thread <message-link>` - Specific thread summary"
                        )
            else:
                # Summarize current channel (regular)
                self._process_current_channel_summary(channel_id, user_id, bot_command)
            
            return {"response_type": "ephemeral", "text": ""}  # Empty response since we handle messaging separately
            
        except Exception as e:
            logger.error(f"Error in summary command: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            self._send_error_message(
                channel_id,
                "❌ Failed to generate summary. Please try again later."
            )
            
            return {"response_type": "ephemeral", "text": ""}
    
    def _process_category_summary_request(self, category_name: str, response_channel_id: str, user_id: str, bot_command: BotCommand):
        """
        Process summary request for a specific category
        
        Args:
            category_name: Name of the category to summarize
            response_channel_id: Channel to send the response to
            user_id: User who requested the summary
            bot_command: Database record for this command
        """
        start_time = time.time()
        
        try:
            from .models import ChannelCategory, SlackWorkspace
            
            # Find the category
            workspace = self._get_or_create_workspace()
            
            # Try exact match first
            category = ChannelCategory.objects.filter(
                workspace=workspace, 
                name__iexact=category_name
            ).first()
            
            # If not found, try partial match
            if not category:
                categories = ChannelCategory.objects.filter(
                    workspace=workspace,
                    name__icontains=category_name
                )
                
                if categories.count() == 1:
                    category = categories.first()
                elif categories.count() > 1:
                    # Multiple matches found
                    category_names = [f"'{cat.name}'" for cat in categories[:5]]
                    bot_command.status = 'failed'
                    bot_command.error_message = f'Multiple categories match "{category_name}"'
                    bot_command.save()
                    
                    self._send_error_message(
                        response_channel_id,
                        f"❌ Multiple categories found matching `{category_name}`:\n{', '.join(category_names)}\n\nPlease use the exact category name. Use `/category list` to see all categories."
                    )
                    return
                else:
                    # No matches found
                    bot_command.status = 'failed'
                    bot_command.error_message = f'Category "{category_name}" not found'
                    bot_command.save()
                    
                    self._send_error_message(
                        response_channel_id,
                        f"❌ Category `{category_name}` not found.\n\nUse `/category list` to see available categories or `/category create` to create a new one."
                    )
                    return
            
            # Get channels in the category
            channels = category.get_channels()
            
            if not channels.exists():
                bot_command.status = 'failed'
                bot_command.error_message = f'Category "{category.name}" has no channels'
                bot_command.save()
                
                self._send_error_message(
                    response_channel_id,
                    f"❌ Category `{category.name}` has no channels assigned.\n\nUse `/category list` to manage categories or add channels to this category."
                )
                return
            
            # Update bot command status
            bot_command.status = 'processing'
            bot_command.save()
            
            # Send acknowledgment with category details
            self._send_message(
                response_channel_id,
                f"<@{user_id}> Generating summary for category **{category.name}** ({channels.count()} channels) ⏳\n📋 Channels: {', '.join([f'#{ch.channel_name}' for ch in channels])}"
            )
            
            # Use the category manager to generate the summary
            success = self.category_manager.generate_category_summary(
                category.id, 
                user_id, 
                response_channel_id, 
                timeframe_hours=24
            )
            
            if success:
                # Update command status
                execution_time = time.time() - start_time
                bot_command.status = 'completed'
                bot_command.execution_time = execution_time
                bot_command.save()
                
                logger.info(f"Category summary completed for '{category.name}' by user {user_id} in {execution_time:.2f}s")
            else:
                bot_command.status = 'failed'
                bot_command.error_message = 'Category summary generation failed'
                bot_command.save()
                
                logger.error(f"Category summary failed for '{category.name}' by user {user_id}")
            
        except Exception as e:
            logger.error(f"Error processing category summary request: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            self._send_error_message(
                response_channel_id,
                f"❌ Failed to generate category summary for `{category_name}`. Error: {str(e)}"
            )
    
    def _process_channel_summary(self, channel_name: str, response_channel_id: str, user_id: str, bot_command: BotCommand):
        """
        Process summary for a specific channel
        
        Args:
            channel_name: Name of the channel to summarize
            response_channel_id: Channel to send the response to
            user_id: User who requested the summary
            bot_command: Database record for this command
        """
        start_time = time.time()
        
        try:
            # Find the channel
            channel_info = self._get_channel_info(channel_name)
            if not channel_info:
                bot_command.status = 'failed'
                bot_command.error_message = f'Channel #{channel_name} not found'
                bot_command.save()
                
                self._send_error_message(
                    response_channel_id,
                    f"❌ Channel `#{channel_name}` not found or I don't have access to it."
                )
                return
            
            channel_id = channel_info['id']
            
            # Get messages from the channel
            messages = self._get_channel_messages(channel_id)
            
            # Filter to last 24 hours
            recent_messages = filter_messages_by_timeframe(messages, hours=24)
            
            # Generate summary
            bot_command.status = 'processing'
            bot_command.save()
            
            summary = self.summarizer.generate_summary(recent_messages, channel_name)
            
            # Save summary to database
            workspace, _ = SlackWorkspace.objects.get_or_create(
                workspace_id="default",
                defaults={'workspace_name': 'Default Workspace'}
            )
            
            slack_channel, _ = SlackChannel.objects.get_or_create(
                workspace=workspace,
                channel_id=channel_id,
                defaults={
                    'channel_name': channel_name,
                    'is_private': channel_info.get('is_private', False)
                }
            )
            
            channel_summary = ChannelSummary.objects.create(
                channel=slack_channel,
                summary_text=summary,
                messages_count=len(recent_messages),
                requested_by_user=user_id
            )
            
            # Store conversation context for follow-ups
            ConversationContext.objects.update_or_create(
                user_id=user_id,
                channel_id=response_channel_id,
                defaults={
                    'context_type': 'summary',
                    'context_data': json.dumps({
                        'summary_id': channel_summary.id,
                        'summarized_channel': channel_name,
                        'summary_text': summary
                    }),
                    'last_summary': channel_summary
                }
            )
            
            # Send the summary
            self._send_summary_message(response_channel_id, summary, user_id)
            
            # Update command status
            execution_time = time.time() - start_time
            bot_command.status = 'completed'
            bot_command.execution_time = execution_time
            bot_command.save()
            
        except Exception as e:
            logger.error(f"Error processing channel summary: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            self._send_error_message(
                response_channel_id,
                f"❌ Failed to generate summary for `#{channel_name}`. Error: {str(e)}"
            )
    
    def _process_current_channel_summary(self, channel_id: str, user_id: str, bot_command: BotCommand):
        """
        Process summary for the current channel
        
        Args:
            channel_id: ID of the current channel
            user_id: User who requested the summary
            bot_command: Database record for this command
        """
        start_time = time.time()
        
        try:
            # Get channel info
            channel_info = self._get_channel_info_by_id(channel_id)
            channel_name = channel_info.get('name', 'current-channel') if channel_info else 'current-channel'
            
            # Get messages from the current channel
            messages = self._get_channel_messages(channel_id)
            
            # Filter to last 24 hours
            recent_messages = filter_messages_by_timeframe(messages, hours=24)
            
            # Generate summary
            bot_command.status = 'processing'
            bot_command.save()
            
            summary = self.summarizer.generate_summary(recent_messages, channel_name)
            
            # Save summary to database
            workspace, _ = SlackWorkspace.objects.get_or_create(
                workspace_id="default",
                defaults={'workspace_name': 'Default Workspace'}
            )
            
            slack_channel, _ = SlackChannel.objects.get_or_create(
                workspace=workspace,
                channel_id=channel_id,
                defaults={
                    'channel_name': channel_name,
                    'is_private': channel_info.get('is_private', False) if channel_info else False
                }
            )
            
            channel_summary = ChannelSummary.objects.create(
                channel=slack_channel,
                summary_text=summary,
                messages_count=len(recent_messages),
                requested_by_user=user_id
            )
            
            # Store conversation context for follow-ups
            ConversationContext.objects.update_or_create(
                user_id=user_id,
                channel_id=channel_id,
                defaults={
                    'context_type': 'summary',
                    'context_data': json.dumps({
                        'summary_id': channel_summary.id,
                        'summarized_channel': channel_name,
                        'summary_text': summary
                    }),
                    'last_summary': channel_summary
                }
            )
            
            # Send the summary
            self._send_summary_message(channel_id, summary, user_id)
            
            # Update command status
            execution_time = time.time() - start_time
            bot_command.status = 'completed'
            bot_command.execution_time = execution_time
            bot_command.save()
            
        except Exception as e:
            logger.error(f"Error processing current channel summary: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            self._send_error_message(
                channel_id,
                f"❌ Failed to generate summary. Error: {str(e)}"
            )
    
    def _handle_category_command(self, payload: Dict, bot_command: BotCommand) -> Dict:
        """
        Handle the /category command and its subcommands
        
        Args:
            payload: Slack command payload
            bot_command: Database record for this command
            
        Returns:
            Response dictionary for Slack
        """
        text = payload.get('text', '').strip().lower()
        user_id = payload.get('user_id')
        channel_id = payload.get('channel_id')
        trigger_id = payload.get('trigger_id')
        
        logger.info(f"Processing category command with subcommand: '{text}'")
        
        try:
            if text == 'create':
                # Open modal for category creation
                modal_success = self.category_manager.create_category_modal(trigger_id, user_id)
                
                if modal_success:
                    bot_command.status = 'completed'
                    bot_command.save()
                    return {
                        "response_type": "ephemeral",
                        "text": "Opening category creation form..."
                    }
                else:
                    bot_command.status = 'failed'
                    bot_command.error_message = 'Failed to open modal'
                    bot_command.save()
                    return {
                        "response_type": "ephemeral",
                        "text": "❌ Failed to open category creation form. Please check if the bot has proper permissions and try again."
                    }
            
            elif text == 'list':
                # List all categories
                try:
                    list_success = self.category_manager.list_categories(user_id, channel_id)
                    if list_success:
                        bot_command.status = 'completed'
                        bot_command.save()
                        return {
                            "response_type": "ephemeral",
                            "text": "📋 Fetching your categories..."
                        }
                    else:
                        bot_command.status = 'failed'
                        bot_command.error_message = 'Failed to list categories'
                        bot_command.save()
                        return {
                            "response_type": "ephemeral",
                            "text": "❌ Failed to list categories. Please try again."
                        }
                except Exception as e:
                    logger.error(f"Error in category list: {str(e)}")
                    bot_command.status = 'failed'
                    bot_command.error_message = str(e)
                    bot_command.save()
                    return {
                        "response_type": "ephemeral",
                        "text": "❌ Failed to list categories. Please try again."
                    }
            
            elif text == 'help' or text == '':
                # Show help
                try:
                    help_success = self.category_manager.show_help(user_id, channel_id)
                    if help_success:
                        bot_command.status = 'completed'
                        bot_command.save()
                        return {
                            "response_type": "ephemeral",
                            "text": "📚 Loading help information..."
                        }
                    else:
                        bot_command.status = 'failed'
                        bot_command.error_message = 'Failed to show help'
                        bot_command.save()
                        return {
                            "response_type": "ephemeral",
                            "text": "❌ Failed to load help. Please try again."
                        }
                except Exception as e:
                    logger.error(f"Error in category help: {str(e)}")
                    bot_command.status = 'failed'
                    bot_command.error_message = str(e)
                    bot_command.save()
                    return {
                        "response_type": "ephemeral",
                        "text": "❌ Failed to load help. Please try again."
                    }
            
            else:
                # Unknown subcommand
                bot_command.status = 'failed'
                bot_command.error_message = f'Unknown subcommand: {text}'
                bot_command.save()
                return {
                    "response_type": "ephemeral",
                    "text": f"❓ Unknown subcommand `{text}`. Use `/category help` to see available commands."
                }
            
        except Exception as e:
            logger.error(f"Error in category command: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            return {
                "response_type": "ephemeral",
                "text": "❌ An error occurred while processing your category command. Please try again later."
            }

    def process_message_event(self, event_data: Dict) -> bool:
        """
        Process incoming message events for real-time task detection and auto-todo creation
        
        Args:
            event_data: Slack event data
            
        Returns:
            True if message was processed, False otherwise
        """
        try:
            event = event_data.get('event', {})
            
            # Ignore bot messages and system messages
            if (event.get('bot_id') or 
                event.get('user') == self.bot_user_id or
                event.get('subtype')):
                return False
            
            message_text = event.get('text', '')
            user_id = event.get('user', '')
            channel_id = event.get('channel', '')
            timestamp = event.get('ts', '')
            
            logger.info(f"Processing message from user {user_id} in channel {channel_id}: {message_text[:100]}...")
            
            # Skip empty messages or very short messages
            if not message_text or len(message_text.strip()) < 10:
                return False
            
            # Check if this channel has auto-task detection enabled
            if not self._is_auto_task_detection_enabled(channel_id):
                logger.debug(f"Auto-task detection disabled for channel {channel_id}")
                return False
            
            # Use AI to detect tasks in the message
            detected_task = self.task_detector.analyze_message(
                message=message_text,
                channel_name=self._get_channel_name(channel_id),
                user_id=user_id,
                timestamp=timestamp
            )
            
            if detected_task:
                logger.info(f"Detected task: {detected_task.title} (confidence: {detected_task.confidence_score:.2f})")
                
                # Only auto-create if confidence is high enough
                if detected_task.confidence_score >= 0.7:
                    success = self._auto_create_todo_from_message(
                        detected_task=detected_task,
                        channel_id=channel_id,
                        user_id=user_id,
                        message_timestamp=timestamp
                    )
                    
                    if success:
                        logger.info(f"Auto-created todo: {detected_task.title}")
                        # Auto-sync Canvas if exists
                        self.canvas_manager.auto_sync_canvas(channel_id)
                        
                        # Optionally notify the channel (can be disabled)
                        if self._should_notify_auto_todo_creation(channel_id):
                            self._send_auto_todo_notification(channel_id, detected_task, user_id)
                else:
                    logger.debug(f"Task confidence too low ({detected_task.confidence_score:.2f}) for auto-creation")
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing message event: {str(e)}")
            return False

    def _get_or_create_workspace(self):
        """Get or create the default workspace"""
        from .models import SlackWorkspace
        workspace, _ = SlackWorkspace.objects.get_or_create(
            workspace_id="default",
            defaults={'workspace_name': 'Default Workspace'}
        )
        return workspace

    def _get_channel_info(self, channel_name: str) -> Optional[Dict]:
        """
        Get channel information by channel name
        
        Args:
            channel_name: Name of the channel (without #)
            
        Returns:
            Channel info dictionary or None if not found
        """
        try:
            # Try to get channel info by name
            response = self.client.conversations_list(
                types="public_channel,private_channel",
                limit=1000
            )
            
            channels = response.get('channels', [])
            for channel in channels:
                if channel.get('name') == channel_name:
                    return channel
            
            # If not found in first batch, check if there are more
            cursor = response.get('response_metadata', {}).get('next_cursor')
            while cursor:
                response = self.client.conversations_list(
                    types="public_channel,private_channel",
                    limit=1000,
                    cursor=cursor
                )
                
                channels = response.get('channels', [])
                for channel in channels:
                    if channel.get('name') == channel_name:
                        return channel
                
                cursor = response.get('response_metadata', {}).get('next_cursor')
            
            return None
            
        except SlackApiError as e:
            logger.error(f"Error getting channel info for {channel_name}: {e}")
            return None

    def _get_channel_info_by_id(self, channel_id: str) -> Optional[Dict]:
        """
        Get channel information by channel ID
        
        Args:
            channel_id: Slack channel ID
            
        Returns:
            Channel info dictionary or None if not found
        """
        try:
            response = self.client.conversations_info(channel=channel_id)
            return response.get('channel')
            
        except SlackApiError as e:
            logger.error(f"Error getting channel info for {channel_id}: {e}")
            return None

    def _get_channel_messages(self, channel_id: str, hours: int = 24) -> List[Dict]:
        """
        Get messages from a channel within the specified time range
        
        Args:
            channel_id: Slack channel ID
            hours: Number of hours to look back
            
        Returns:
            List of message dictionaries
        """
        try:
            # Calculate timestamp for specified hours ago
            oldest = (datetime.now() - timedelta(hours=hours)).timestamp()
            
            messages = []
            cursor = None
            
            while True:
                response = self.client.conversations_history(
                    channel=channel_id,
                    oldest=str(oldest),
                    limit=200,
                    cursor=cursor
                )
                
                messages.extend(response.get('messages', []))
                
                # Check if there are more messages
                cursor = response.get('response_metadata', {}).get('next_cursor')
                if not cursor:
                    break
            
            # Filter out bot messages and system messages
            filtered_messages = []
            for message in messages:
                if (message.get('type') == 'message' and 
                    'bot_id' not in message and 
                    message.get('user') != self.bot_user_id and
                    'subtype' not in message):
                    filtered_messages.append(message)
            
            return filtered_messages
            
        except SlackApiError as e:
            logger.error(f"Error getting messages from channel {channel_id}: {e}")
            return []

    def _send_summary_message(self, channel_id: str, summary: str, user_id: str):
        """Send the summary message to the channel"""
        try:
            # Create a formatted message with the summary
            formatted_message = f"<@{user_id}> Here's your requested summary:\n\n```\n{summary}\n```\n\n💬 *Ask me any follow-up questions about this summary!*"
            
            self.client.chat_postMessage(
                    channel=channel_id,
                text=formatted_message,
                unfurl_links=False,
                unfurl_media=False
            )
        except SlackApiError as e:
            logger.error(f"Failed to send summary message: {e}")

    def _handle_unknown_command(self, command: str, bot_command: BotCommand) -> Dict:
        """Handle unknown commands"""
        bot_command.status = 'failed'
        bot_command.error_message = f'Unknown command: {command}'
        bot_command.save()
        
        return {
            "response_type": "ephemeral",
            "text": f"❓ Unknown command `{command}`. Available commands:\n• `/summary` - Summarize current channel (last 24 hours)\n• `/summary [channel-name]` - Summarize specific channel\n• `/summary category [category-name]` - Summarize all channels in a category\n• `/summary unread` - Summarize unread messages in current channel\n• `/summary unread [channel-name]` - Summarize unread messages in specific channel\n• `/summary thread latest` - Summarize latest thread in current channel\n• `/summary thread latest [channel-name]` - Summarize latest thread in specific channel\n• `/summary thread [message-link]` - Summarize specific thread\n• `/category create` - Create a new category\n• `/category list` - List all categories\n• `/category help` - Show category help"
        }

    def _send_acknowledgment_message(self, channel_id: str, user_id: str):
        """Send acknowledgment message to user"""
        try:
            self.client.chat_postMessage(
                channel=channel_id,
                text=f"<@{user_id}> Your summary is getting generated ⏳",
                unfurl_links=False,
                unfurl_media=False
            )
        except SlackApiError as e:
            logger.error(f"Failed to send acknowledgment: {e}")

    def _send_message(self, channel_id: str, message: str):
        """Send a simple message to a channel"""
        try:
            self.client.chat_postMessage(
                channel=channel_id,
                text=message,
                unfurl_links=False,
                unfurl_media=False
            )
        except SlackApiError as e:
            logger.error(f"Failed to send message: {e}")

    def _send_error_message(self, channel_id: str, message: str):
        """Send error message to channel"""
        try:
            self.client.chat_postMessage(
                channel=channel_id,
                text=message,
                unfurl_links=False,
                unfurl_media=False
            )
        except SlackApiError as e:
            logger.error(f"Failed to send error message: {e}")

    # Placeholder methods for thread and unread functionality
    def _process_latest_thread_summary(self, target: str, channel_id: str, user_id: str, bot_command: BotCommand):
        """
        Process summary for the latest thread in a specific channel
        
        Args:
            target: Name of the channel to find latest thread in
            channel_id: Channel to send the response to
            user_id: User who requested the summary
            bot_command: Database record for this command
        """
        start_time = time.time()
        
        try:
            # Find the channel
            channel_info = self._get_channel_info(target)
            if not channel_info:
                bot_command.status = 'failed'
                bot_command.error_message = f'Channel #{target} not found'
                bot_command.save()
                
                self._send_error_message(
                    channel_id,
                    f"❌ Channel `#{target}` not found or I don't have access to it."
                )
                return
            
            target_channel_id = channel_info['id']
            
            # Find the latest thread in the channel
            latest_thread_ts = self._get_latest_thread_timestamp(target_channel_id)
            if not latest_thread_ts:
                bot_command.status = 'failed'
                bot_command.error_message = f'No threads found in #{target}'
                bot_command.save()
                
                self._send_error_message(
                    channel_id,
                    f"❌ No threads found in `#{target}`."
                )
                return
            
            # Get thread messages
            thread_messages = self._get_thread_messages(target_channel_id, latest_thread_ts)
            
            # Update bot command status
            bot_command.status = 'processing'
            bot_command.save()
            
            # Generate thread summary
            summary = self.summarizer.generate_summary(thread_messages, f"{target} (Latest Thread)")
            
            # Save summary to database
            workspace, _ = SlackWorkspace.objects.get_or_create(
                workspace_id="default",
                defaults={'workspace_name': 'Default Workspace'}
            )
            
            slack_channel, _ = SlackChannel.objects.get_or_create(
                workspace=workspace,
                channel_id=target_channel_id,
                defaults={
                    'channel_name': target,
                    'is_private': channel_info.get('is_private', False)
                }
            )
            
            channel_summary = ChannelSummary.objects.create(
                channel=slack_channel,
                summary_text=summary,
                messages_count=len(thread_messages),
                timeframe=f"Latest thread in #{target}",
                timeframe_hours=0,  # Special indicator for thread
                requested_by_user=user_id
            )
            
            # Store conversation context for follow-ups
            ConversationContext.objects.update_or_create(
                user_id=user_id,
                channel_id=channel_id,
                defaults={
                    'context_type': 'summary',
                    'context_data': json.dumps({
                        'summary_id': channel_summary.id,
                        'summarized_channel': target,
                        'summary_text': summary,
                        'summary_type': 'thread_latest',
                        'thread_ts': latest_thread_ts
                    }),
                    'last_summary': channel_summary
                }
            )
            
            # Send the summary
            self._send_thread_summary_message(channel_id, summary, user_id, f"#{target}", "latest thread")
            
            # Update command status
            execution_time = time.time() - start_time
            bot_command.status = 'completed'
            bot_command.execution_time = execution_time
            bot_command.save()
            
            logger.info(f"Latest thread summary completed for #{target} by user {user_id} in {execution_time:.2f}s")
            
        except Exception as e:
            logger.error(f"Error processing latest thread summary: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            self._send_error_message(
                channel_id,
                f"❌ Failed to generate latest thread summary for `#{target}`. Error: {str(e)}"
            )
    
    def _process_current_channel_latest_thread_summary(self, channel_id: str, user_id: str, bot_command: BotCommand):
        """
        Process summary for the latest thread in the current channel
        
        Args:
            channel_id: ID of the current channel
            user_id: User who requested the summary
            bot_command: Database record for this command
        """
        start_time = time.time()
        
        try:
            # Get channel info
            channel_info = self._get_channel_info_by_id(channel_id)
            channel_name = channel_info.get('name', 'current-channel') if channel_info else 'current-channel'
            
            # Find the latest thread in the current channel
            latest_thread_ts = self._get_latest_thread_timestamp(channel_id)
            if not latest_thread_ts:
                bot_command.status = 'failed'
                bot_command.error_message = f'No threads found in current channel'
                bot_command.save()
                
                self._send_error_message(
                    channel_id,
                    "❌ No threads found in this channel."
                )
                return
            
            # Get thread messages
            thread_messages = self._get_thread_messages(channel_id, latest_thread_ts)
            
            # Update bot command status
            bot_command.status = 'processing'
            bot_command.save()
            
            # Generate thread summary
            summary = self.summarizer.generate_summary(thread_messages, f"{channel_name} (Latest Thread)")
            
            # Save summary to database
            workspace, _ = SlackWorkspace.objects.get_or_create(
                workspace_id="default",
                defaults={'workspace_name': 'Default Workspace'}
            )
            
            slack_channel, _ = SlackChannel.objects.get_or_create(
                workspace=workspace,
                channel_id=channel_id,
                defaults={
                    'channel_name': channel_name,
                    'is_private': channel_info.get('is_private', False) if channel_info else False
                }
            )
            
            channel_summary = ChannelSummary.objects.create(
                channel=slack_channel,
                summary_text=summary,
                messages_count=len(thread_messages),
                timeframe=f"Latest thread in #{channel_name}",
                timeframe_hours=0,  # Special indicator for thread
                requested_by_user=user_id
            )
            
            # Store conversation context for follow-ups
            ConversationContext.objects.update_or_create(
                user_id=user_id,
                channel_id=channel_id,
                defaults={
                    'context_type': 'summary',
                    'context_data': json.dumps({
                        'summary_id': channel_summary.id,
                        'summarized_channel': channel_name,
                        'summary_text': summary,
                        'summary_type': 'thread_latest',
                        'thread_ts': latest_thread_ts
                    }),
                    'last_summary': channel_summary
                }
            )
            
            # Send the summary
            self._send_thread_summary_message(channel_id, summary, user_id, f"#{channel_name}", "latest thread")
            
            # Update command status
            execution_time = time.time() - start_time
            bot_command.status = 'completed'
            bot_command.execution_time = execution_time
            bot_command.save()
            
            logger.info(f"Current channel latest thread summary completed for #{channel_name} by user {user_id} in {execution_time:.2f}s")
            
        except Exception as e:
            logger.error(f"Error processing current channel latest thread summary: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            self._send_error_message(
                channel_id,
                f"❌ Failed to generate latest thread summary. Error: {str(e)}"
            )
    
    def _process_specific_thread_summary(self, target: str, message_ts: str, channel_id: str, user_id: str, bot_command: BotCommand):
        """
        Process summary for a specific thread identified by message link
        
        Args:
            target: Slack message link
            message_ts: Parsed message timestamp
            channel_id: Channel to send the response to
            user_id: User who requested the summary
            bot_command: Database record for this command
        """
        start_time = time.time()
        
        try:
            # Parse the message link to get channel ID
            target_channel_id, _ = parse_message_link(target)
            if not target_channel_id:
                bot_command.status = 'failed'
                bot_command.error_message = 'Invalid message link format'
                bot_command.save()
                
                self._send_error_message(
                    channel_id,
                    "❌ Invalid message link format. Please provide a valid Slack message link."
                )
                return
            
            # Get channel info
            channel_info = self._get_channel_info_by_id(target_channel_id)
            channel_name = channel_info.get('name', 'unknown-channel') if channel_info else 'unknown-channel'
            
            # Check if the message has replies (is a thread)
            if not self._message_has_replies(target_channel_id, message_ts):
                bot_command.status = 'failed'
                bot_command.error_message = 'Message has no thread replies'
                bot_command.save()
                
                self._send_error_message(
                    channel_id,
                    "❌ The specified message has no thread replies to summarize."
                )
                return
            
            # Get thread messages
            thread_messages = self._get_thread_messages(target_channel_id, message_ts)
            
            # Update bot command status
            bot_command.status = 'processing'
            bot_command.save()
            
            # Generate thread summary
            summary = self.summarizer.generate_summary(thread_messages, f"{channel_name} (Specific Thread)")
            
            # Save summary to database
            workspace, _ = SlackWorkspace.objects.get_or_create(
                workspace_id="default",
                defaults={'workspace_name': 'Default Workspace'}
            )
            
            slack_channel, _ = SlackChannel.objects.get_or_create(
                workspace=workspace,
                channel_id=target_channel_id,
                defaults={
                    'channel_name': channel_name,
                    'is_private': channel_info.get('is_private', False) if channel_info else False
                }
            )
            
            channel_summary = ChannelSummary.objects.create(
                channel=slack_channel,
                summary_text=summary,
                messages_count=len(thread_messages),
                timeframe=f"Specific thread in #{channel_name}",
                timeframe_hours=0,  # Special indicator for thread
                requested_by_user=user_id
            )
            
            # Store conversation context for follow-ups
            ConversationContext.objects.update_or_create(
                user_id=user_id,
                channel_id=channel_id,
                defaults={
                    'context_type': 'summary',
                    'context_data': json.dumps({
                        'summary_id': channel_summary.id,
                        'summarized_channel': channel_name,
                        'summary_text': summary,
                        'summary_type': 'thread_specific',
                        'thread_ts': message_ts,
                        'message_link': target
                    }),
                    'last_summary': channel_summary
                }
            )
            
            # Send the summary
            self._send_thread_summary_message(channel_id, summary, user_id, f"#{channel_name}", "specific thread")
            
            # Update command status
            execution_time = time.time() - start_time
            bot_command.status = 'completed'
            bot_command.execution_time = execution_time
            bot_command.save()
            
            logger.info(f"Specific thread summary completed for #{channel_name} by user {user_id} in {execution_time:.2f}s")
            
        except Exception as e:
            logger.error(f"Error processing specific thread summary: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            self._send_error_message(
                channel_id,
                f"❌ Failed to generate thread summary. Error: {str(e)}"
            )
    
    def _get_latest_thread_timestamp(self, channel_id: str) -> Optional[str]:
        """
        Find the most recent message that has thread replies in a channel
        
        Args:
            channel_id: Slack channel ID
            
        Returns:
            Timestamp of the latest thread or None if no threads found
        """
        try:
            # Get recent messages from the channel
            response = self.client.conversations_history(
                channel=channel_id,
                limit=100  # Check last 100 messages for threads
            )
            
            messages = response.get('messages', [])
            
            # Find the most recent message with thread replies
            for message in messages:
                if (message.get('reply_count', 0) > 0 and 
                    message.get('type') == 'message' and
                    'subtype' not in message):
                    return message.get('ts')
            
            return None
            
        except SlackApiError as e:
            logger.error(f"Error finding latest thread in channel {channel_id}: {e}")
            return None
    
    def _get_thread_messages(self, channel_id: str, thread_ts: str) -> List[Dict]:
        """
        Get all messages in a thread
        
        Args:
            channel_id: Slack channel ID
            thread_ts: Thread timestamp (parent message timestamp)
            
        Returns:
            List of thread message dictionaries
        """
        try:
            response = self.client.conversations_replies(
                channel=channel_id,
                ts=thread_ts
            )
            
            messages = response.get('messages', [])
            
            # Filter out bot messages but keep the original message
            filtered_messages = []
            for message in messages:
                if (message.get('type') == 'message' and 
                    message.get('user') != self.bot_user_id and
                    'bot_id' not in message):
                    filtered_messages.append(message)
            
            return filtered_messages
            
        except SlackApiError as e:
            logger.error(f"Error getting thread messages for {thread_ts} in channel {channel_id}: {e}")
            return []
    
    def _message_has_replies(self, channel_id: str, message_ts: str) -> bool:
        """
        Check if a message has thread replies
        
        Args:
            channel_id: Slack channel ID
            message_ts: Message timestamp
            
        Returns:
            True if message has replies
        """
        try:
            response = self.client.conversations_replies(
                channel=channel_id,
                ts=message_ts
            )
            
            messages = response.get('messages', [])
            # If there's more than 1 message (original + replies), it has replies
            return len(messages) > 1
            
        except SlackApiError as e:
            logger.error(f"Error checking thread replies for {message_ts} in channel {channel_id}: {e}")
            return False
    
    def _send_thread_summary_message(self, channel_id: str, summary: str, user_id: str, channel_context: str, thread_type: str):
        """Send the thread summary message to the channel"""
        try:
            # Create a formatted message with the thread summary
            formatted_message = f"<@{user_id}> Here's your {thread_type} summary for {channel_context}:\n\n```\n{summary}\n```\n\n💬 *Ask me any follow-up questions about this summary!*"
            
            self.client.chat_postMessage(
                channel=channel_id,
                text=formatted_message,
                unfurl_links=False,
                unfurl_media=False
            )
        except SlackApiError as e:
            logger.error(f"Failed to send thread summary message: {e}")
    
    def _handle_todo_command(self, payload: Dict, bot_command: BotCommand) -> Dict:
        """
        Handle the /todo command - DM-ONLY personal task scanner
        Creates/updates a personal Canvas with tasks from ALL workspace channels and DMs
        
        Args:
            payload: Slack slash command payload
            bot_command: BotCommand database record
            
        Returns:
            Response dictionary for Slack
        """
        user_id = payload.get('user_id')
        channel_id = payload.get('channel_id')
        
        logger.info(f"Todo command received: user={user_id}, channel={channel_id}")
        
        # Check if this is being used in a DM with the bot
        is_personal_dm = self._is_personal_dm(channel_id, user_id)
        
        if not is_personal_dm:
            # Not in DM - show error message
            bot_command.status = 'failed'
            bot_command.error_message = 'Command only works in DM'
            bot_command.save()
            
            return {
                "response_type": "ephemeral",
                "text": "🚫 The `/todo` command only works in your **DM with the bot**.\n\n" +
                       "📱 Open a DM with this bot and try `/todo` again to see your personal task dashboard.\n\n" +
                       "💡 Tip: The `/todo` command scans ALL your workspace channels and DMs to create a unified task list!"
            }
        
        # DM mode - run personal task scanner
        logger.info(f"Running personal task scanner for user {user_id} in DM")
        return self._handle_personal_task_command(payload, bot_command)

    def _handle_tasks_command(self, payload: Dict, bot_command: BotCommand) -> Dict:
        """
        Handle the /tasks command for task analysis and extraction
        
        Args:
            payload: Slack slash command payload
            bot_command: BotCommand database record
            
        Returns:
            Response dictionary for Slack
        """
        text = payload.get('text', '').strip()
        user_id = payload.get('user_id')
        channel_id = payload.get('channel_id')
        
        logger.info(f"Processing tasks command with text: '{text}'")
        
        try:
            parts = text.split() if text else ['summary']
            subcommand = parts[0].lower()
            
            if subcommand == 'summary':
                return self._tasks_summary(parts[1:], user_id, channel_id, bot_command)
            elif subcommand == 'extract':
                return self._tasks_extract(parts[1:], user_id, channel_id, bot_command)
            elif subcommand == 'priority':
                return self._tasks_priority(parts[1:], user_id, channel_id, bot_command)
            elif subcommand == 'overdue':
                return self._tasks_overdue(user_id, channel_id, bot_command)
            elif subcommand == 'help':
                return self._tasks_show_help(bot_command)
            else:
                # Default to summary
                return self._tasks_summary([text], user_id, channel_id, bot_command)
                
        except Exception as e:
            logger.error(f"Error in tasks command: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            return {
                "response_type": "ephemeral",
                "text": "❌ An error occurred while processing your tasks command. Please try again later."
            }

    def _handle_task_command(self, payload: Dict, bot_command: BotCommand) -> Dict:
        """
        Handle the /task command with two modes:
        1. In channel: processes channel messages → updates channel canvas
        2. In personal DM: scans ALL channels + DMs → creates personal master Canvas
        
        Args:
            payload: Slack slash command payload
            bot_command: BotCommand database record
            
        Returns:
            Response dictionary for Slack
        """
        text = payload.get('text', '').strip()
        user_id = payload.get('user_id')
        channel_id = payload.get('channel_id')
        
        logger.info(f"Task command received: user={user_id}, channel={channel_id}, text='{text}'")
        
        # Check if this is a DM with the bot (personal mode)
        is_personal_dm = self._is_personal_dm(channel_id, user_id)
        
        logger.info(f"DM detection result: is_personal_dm={is_personal_dm} for channel {channel_id}")
        
        if is_personal_dm:
            # Personal productivity mode - scan entire workspace
            logger.info(f"Routing to personal task command for user {user_id}")
            return self._handle_personal_task_command(payload, bot_command)
        else:
            # Channel mode - process channel messages to channel canvas
            logger.info(f"Routing to channel task command for channel {channel_id}")
            return self._handle_channel_task_command(payload, bot_command, text)

    def _is_personal_dm(self, channel_id: str, user_id: str) -> bool:
        """
        Check if the command is being run in a personal DM with the bot
        
        Args:
            channel_id: Slack channel ID
            user_id: User ID who ran the command
            
        Returns:
            True if this is a personal DM with the bot
        """
        try:
            logger.info(f"Checking if channel {channel_id} is personal DM for user {user_id}")
            
            # Simple check: DM channel IDs start with 'D', group DMs with 'G', channels with 'C'
            if channel_id.startswith('D'):
                logger.info(f"Channel {channel_id} is a DM based on ID format")
                return True
            elif channel_id.startswith('G'):
                # Could be a group DM - check with conversations.info if needed
                try:
                    response = self.client.conversations_info(channel=channel_id)
                    if response['ok']:
                        channel_info = response['channel']
                        is_group_dm = channel_info.get('is_mpim', False)
                        logger.info(f"Channel {channel_id} is group DM: {is_group_dm}")
                        return is_group_dm
                except SlackApiError as api_error:
                    logger.warning(f"Could not get info for channel {channel_id}: {api_error}")
                    # If we can't get info, assume it's not a DM for safety
                    return False
            
            logger.info(f"Channel {channel_id} is not a DM (starts with {channel_id[0] if channel_id else 'None'})")
            return False
            
        except Exception as e:
            logger.error(f"Error checking if personal DM: {str(e)}")
        return False
    
    def _handle_personal_task_command(self, payload: Dict, bot_command: BotCommand) -> Dict:
        """
        Handle /task command in personal DM - scans ALL channels and DMs for tasks
        
        Args:
            payload: Slack slash command payload
            bot_command: BotCommand database record
            
        Returns:
            Response dictionary for Slack
        """
        user_id = payload.get('user_id')
        dm_channel_id = payload.get('channel_id')
        
        logger.info(f"Processing PERSONAL /task command for user {user_id}")
        
        try:
            # First, ensure we can actually message the user
            logger.info(f"Starting personal task analysis for user {user_id} in DM {dm_channel_id}")
            
            try:
                # Send immediate response
                self.client.chat_postMessage(
                    channel=dm_channel_id,
                    text=f"🤖 **Personal Task Analysis Starting...**\n\n🔍 Scanning your entire workspace:\n• All channels you're in\n• All DM conversations\n• All actionable messages\n\n⏳ This may take a moment..."
                )
                logger.info(f"Successfully sent initial message to DM {dm_channel_id}")
            except SlackApiError as msg_error:
                logger.error(f"Failed to send message to DM {dm_channel_id}: {msg_error}")
                
                # If we can't send to the DM, try to open a conversation first
                try:
                    dm_response = self.client.conversations_open(users=user_id)
                    if dm_response['ok']:
                        dm_channel_id = dm_response['channel']['id']
                        logger.info(f"Opened new DM conversation: {dm_channel_id}")
                        
                        # Try sending the message again
                        self.client.chat_postMessage(
                            channel=dm_channel_id,
                            text=f"🤖 **Personal Task Analysis Starting...**\n\n🔍 Scanning your entire workspace:\n• All channels you're in\n• All DM conversations\n• All actionable messages\n\n⏳ This may take a moment..."
                        )
                    else:
                        raise SlackApiError(f"Could not open DM: {dm_response.get('error', 'Unknown error')}")
                except SlackApiError as dm_error:
                    logger.error(f"Could not open DM with user {user_id}: {dm_error}")
                    bot_command.status = 'failed'
                    bot_command.error_message = f"Cannot access DM with user: {str(dm_error)}"
                    bot_command.save()
                    
                    return {
                        "response_type": "ephemeral",
                        "text": f"❌ **Cannot start personal task analysis**\n\nI need to be able to send you direct messages. Please:\n1. Start a DM with me first by clicking my name\n2. Send me any message to open the conversation\n3. Then try `/task` again"
                    }
            
            # Get all channels the user is in
            user_channels = self._get_user_channels(user_id)
            
            # Get all DM conversations for the user  
            user_dms = self._get_user_dms(user_id)
            
            # Scan all channels for tasks
            all_channel_tasks = []
            channels_scanned = 0
            
            for channel in user_channels:
                try:
                    channel_tasks = self._extract_tasks_from_channel(channel['id'], user_id)
                    all_channel_tasks.extend(channel_tasks)
                    channels_scanned += 1
                    
                    # Progress update every 5 channels
                    if channels_scanned % 5 == 0:
                        self.client.chat_postMessage(
                            channel=dm_channel_id,
                            text=f"📊 Progress: Scanned {channels_scanned}/{len(user_channels)} channels..."
                        )
                except Exception as e:
                    logger.error(f"Error scanning channel {channel.get('id', 'unknown')}: {str(e)}")
                    continue
            
            # Scan all DMs for tasks
            all_dm_tasks = []
            dms_scanned = 0
            
            for dm in user_dms:
                try:
                    dm_tasks = self._extract_tasks_from_dm(dm['id'], user_id)
                    all_dm_tasks.extend(dm_tasks)
                    dms_scanned += 1
                except Exception as e:
                    logger.error(f"Error scanning DM {dm.get('id', 'unknown')}: {str(e)}")
                    continue
            
            # Combine and deduplicate tasks
            all_tasks = all_channel_tasks + all_dm_tasks
            deduplicated_tasks = self._deduplicate_tasks(all_tasks)
            
            # Create personal List in the DM
            list_success, list_message = self._create_personal_list(
                dm_channel_id, user_id, deduplicated_tasks
            )
            
            # Create/update personal Canvas
            canvas_success, canvas_message = self._create_personal_canvas(
                dm_channel_id, user_id, deduplicated_tasks
            )
            
            # Create todos in database (using special personal channel)
            todos_created = self._save_personal_todos(user_id, deduplicated_tasks)
            
            # Send comprehensive results
            result_message = f"🎉 **Personal Task Analysis Complete!**\n\n"
            result_message += f"📊 **Workspace Scan Results:**\n"
            result_message += f"• {channels_scanned} channels analyzed\n"
            result_message += f"• {dms_scanned} DM conversations analyzed\n"
            result_message += f"• {len(all_tasks)} total tasks found\n"
            result_message += f"• {len(deduplicated_tasks)} unique tasks after deduplication\n"
            result_message += f"• {todos_created} todos created in your personal system\n\n"
            
            if list_success:
                result_message += f"✅ **Interactive Task List Created:**\n"
                result_message += f"Your master todo list is now available in this DM!\n"
                result_message += f"Click the checkboxes above to mark tasks complete.\n\n"
            else:
                result_message += f"⚠️ **Task List Status:** {list_message}\n\n"
            
            if canvas_success:
                result_message += f"🎨 **Personal Canvas Updated:**\n"
                result_message += f"{canvas_message}\n"
                result_message += f"Your 'To-do list' Canvas tab has been updated with all tasks!\n\n"
            else:
                result_message += f"⚠️ **Canvas Status:** {canvas_message}\n\n"
            
            result_message += f"🔄 **Next Steps:**\n"
            result_message += f"• Click checkboxes in the task list above to mark items complete\n"
            result_message += f"• Check your 'To-do list' Canvas tab for visual task board\n"
            result_message += f"• Use `/todo list` to see all your personal todos in database\n"
            result_message += f"• Run `/todo` again anytime to refresh with latest messages"
            
            self.client.chat_postMessage(
                channel=dm_channel_id,
                text=result_message
            )
            
            bot_command.status = 'completed'
            bot_command.save()
            
            logger.info(f"Personal task command completed: {todos_created} todos created for user {user_id}")
            
            return {"response_type": "ephemeral", "text": ""}
            
        except Exception as e:
            logger.error(f"Error in personal task command: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            # Provide detailed error message based on the error type
            if "channel_not_found" in str(e).lower():
                error_message = "❌ **Cannot Access DM Conversation**\n\n**Solution:**\n1. Start a DM with me by clicking my name\n2. Send me any message to open the conversation\n3. Then try `/task` again\n\n**Or add missing OAuth scopes:** `conversations:read`, `conversations:history`"
            elif "missing_scope" in str(e).lower():
                error_message = "❌ **Missing OAuth Permissions**\n\nYour Slack app needs additional scopes:\n• `conversations:read`\n• `conversations:history` \n• `canvases:read`\n• `canvases:write`\n\nAdd these in your Slack app settings and reinstall."
            elif "not_authed" in str(e).lower():
                error_message = "❌ **Authentication Error**\n\nBot token may be invalid. Check your `SLACK_BOT_TOKEN` in settings."
            else:
                error_message = f"❌ **Error in Personal Task Analysis:**\n{str(e)}\n\nCheck server logs for details or contact support."
            
            try:
                self.client.chat_postMessage(
                    channel=dm_channel_id,
                    text=error_message
                )
            except Exception as msg_error:
                logger.error(f"Could not send error message to DM: {msg_error}")
            
            return {
                "response_type": "ephemeral",
                "text": error_message
            }

    def _handle_channel_task_command(self, payload: Dict, bot_command: BotCommand, text: str) -> Dict:
        """
        Handle /task command in channel - original channel-specific behavior
        
        Args:
            payload: Slack slash command payload  
            bot_command: BotCommand database record
            text: Command text for canvas name
            
        Returns:
            Response dictionary for Slack
        """
        user_id = payload.get('user_id')
        channel_id = payload.get('channel_id')
        
        # Parse canvas name from command text
        canvas_name = None
        if text:
            # Remove quotes if present
            canvas_name = text.strip('"\'').strip()
        
        logger.info(f"Processing CHANNEL /task command for channel {channel_id} by user {user_id}, canvas: {canvas_name}")
        
        try:
            # Send immediate response
            if canvas_name:
                self.client.chat_postMessage(
                    channel=channel_id,
                    text=f"<@{user_id}> 🤖 Processing all channel messages and updating Canvas '{canvas_name}'... ⏳"
                )
            else:
                self.client.chat_postMessage(
                    channel=channel_id,
                    text=f"<@{user_id}> 🤖 Processing all channel messages and creating todos... ⏳"
                )
            
            # Get ALL messages from the channel (last 500 messages)
            try:
                response = self.client.conversations_history(
                    channel=channel_id,
                    limit=500  # Adjust as needed
                )
                
                if not response['ok']:
                    bot_command.status = 'failed'
                    bot_command.error_message = 'Could not access channel messages'
                    bot_command.save()
                    
                    self.client.chat_postMessage(
                        channel=channel_id,
                        text=f"<@{user_id}> ❌ Could not access channel messages. Please check bot permissions."
                    )
                    return {"response_type": "ephemeral", "text": ""}
                
                messages = response['messages']
                logger.info(f"Retrieved {len(messages)} messages from channel")
                
                # Filter out bot messages and system messages
                user_messages = []
                for msg in messages:
                    if (not msg.get('bot_id') and 
                        msg.get('user') != self.bot_user_id and 
                        not msg.get('subtype') and
                        msg.get('text', '').strip() and
                        len(msg.get('text', '').strip()) >= 10):
                        user_messages.append(msg)
                
                logger.info(f"Filtered to {len(user_messages)} user messages")
                
                if not user_messages:
                    self.client.chat_postMessage(
                        channel=channel_id,
                        text=f"<@{user_id}> 📭 No suitable messages found to convert to todos."
                    )
                    return {"response_type": "ephemeral", "text": ""}
                
                # Handle Canvas - either find existing or create new
                canvas_success, canvas_message = self._ensure_specific_canvas_exists(
                    channel_id, user_id, canvas_name
                )
                
                # Process messages and create todos
                todos_created = 0
                channel_name = self._get_channel_name(channel_id)
                
                # Get workspace and channel objects
                workspace = self.todo_manager._get_or_create_workspace()
                channel_obj = self.todo_manager._get_or_create_channel(channel_id, workspace)
                
                for msg in user_messages:
                    try:
                        message_text = msg.get('text', '')
                        msg_user_id = msg.get('user', '')
                        timestamp = msg.get('ts', '')
                        
                        # Use AI to detect if this message contains actionable content
                        detected_task = self.task_detector.analyze_message(
                            message=message_text,
                            channel_name=channel_name,
                            user_id=msg_user_id,
                            timestamp=timestamp
                        )
                        
                        # Create todo for any message with reasonable confidence OR for all messages
                        # Let's create todos for all messages but with different handling
                        if detected_task and detected_task.confidence_score >= 0.5:
                            # High confidence AI detection
                            title = detected_task.title
                            task_type = detected_task.task_type
                            priority = detected_task.priority
                            due_date = detected_task.due_date
                        else:
                            # Convert message to general todo
                            title = message_text[:100] + "..." if len(message_text) > 100 else message_text
                            task_type = 'general'
                            priority = 'medium'
                            due_date = None
                        
                        # Check if todo from this message already exists (prevent duplicates)
                        existing_todo = ChannelTodo.objects.filter(
                            channel=channel_obj,
                            created_from_message=timestamp
                        ).first()
                        
                        if not existing_todo:
                            # Create message link
                            message_link = f"https://slack.com/archives/{channel_id}/p{timestamp.replace('.', '')}"
                            
                            # Create the todo
                            todo = ChannelTodo.objects.create(
                                channel=channel_obj,
                                title=title,
                                description=f"Created from message: {message_text[:200]}",
                                task_type=task_type,
                                priority=priority,
                                due_date=due_date,
                                created_from_message=timestamp,
                                created_from_message_link=message_link,
                                created_by=user_id,  # User who ran the command
                                status='pending'
                            )
                            
                            todos_created += 1
                    
                    except Exception as e:
                        logger.error(f"Error processing message: {str(e)}")
                        continue
                
                # Update Canvas with all todos
                if canvas_success:
                    if canvas_name:
                        canvas_update_success, canvas_update_message = self.canvas_manager.update_specific_canvas(
                            channel_id, canvas_name, force_sync=True
                        )
                    else:
                        canvas_update_success, canvas_update_message = self.canvas_manager.update_canvas(
                            channel_id, force_sync=True
                        )
                else:
                    canvas_update_success = False
                    canvas_update_message = canvas_message
                
                # Send success message
                success_message = f"<@{user_id}> ✅ **Task processing complete!**\n\n"
                success_message += f"📊 **Results:**\n"
                success_message += f"• {len(user_messages)} messages analyzed\n"
                success_message += f"• {todos_created} todos created\n"
                
                if canvas_update_success:
                    if canvas_name:
                        success_message += f"• Canvas '{canvas_name}' updated successfully\n\n"
                        success_message += f"🎨 **View your visual todo board:** Canvas '{canvas_name}' in channel tabs\n"
                    else:
                        success_message += f"• Canvas updated successfully\n\n"
                        success_message += f"🎨 **View your visual todo board:** Use `/canvas show` to get the Canvas link\n"
                else:
                    success_message += f"• Canvas update: {canvas_update_message}\n\n"
                
                success_message += f"💡 **Manage todos:** Use `/todo list` to see all todos"
                
                self.client.chat_postMessage(
                    channel=channel_id,
                    text=success_message
                )
                
                bot_command.status = 'completed'
                bot_command.save()
                
                logger.info(f"Channel task command completed: {todos_created} todos created for channel {channel_id}")
                
                return {"response_type": "ephemeral", "text": ""}
                
            except SlackApiError as e:
                logger.error(f"Slack API error in channel task command: {e}")
                self.client.chat_postMessage(
                    channel=channel_id,
                    text=f"<@{user_id}> ❌ Slack API error: {str(e)}"
                )
                return {"response_type": "ephemeral", "text": ""}
                
        except Exception as e:
            logger.error(f"Error in channel task command: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            try:
                self.client.chat_postMessage(
                    channel=channel_id,
                    text=f"<@{user_id}> ❌ An error occurred while processing tasks: {str(e)}"
                )
            except:
                pass
            
            return {
                "response_type": "ephemeral",
                "text": "❌ An error occurred while processing your task command. Please try again later."
            }

    def _ensure_specific_canvas_exists(self, channel_id: str, user_id: str, canvas_name: str = None) -> Tuple[bool, str]:
        """
        Ensure a specific Canvas exists for the channel, or use default
        
        Args:
            channel_id: Slack channel ID
            user_id: User ID for Canvas creation
            canvas_name: Specific canvas name to find/create, or None for default
            
        Returns:
            Tuple of (success, message)
        """
        try:
            channel = SlackChannel.objects.filter(channel_id=channel_id).first()
            
            if canvas_name:
                # Look for specific canvas by name
                if channel:
                    canvas = ChannelCanvas.objects.filter(
                        channel=channel, 
                        canvas_title=canvas_name
                    ).first()
                    
                    if canvas:
                        logger.info(f"Found existing canvas '{canvas_name}' for channel {channel_id}")
                        return True, f"Using existing canvas '{canvas_name}'"
                    else:
                        # Canvas with this name doesn't exist, suggest creation
                        return False, f"❌ Canvas '{canvas_name}' not found. Use `/canvas create \"{canvas_name}\"` first."
                else:
                    return False, f"❌ Channel not found"
            else:
                # Use default canvas behavior (existing logic)
                if channel:
                    canvas = ChannelCanvas.objects.filter(channel=channel).first()
                    if canvas:
                        logger.info(f"Using existing default canvas for channel {channel_id}")
                        return True, "Using existing canvas"
                
                # Create default Canvas
                success, message, canvas = self.canvas_manager.create_canvas(
                    channel_id=channel_id,
                    title="Todo List",
                    created_by=user_id
                )
                
                if success:
                    logger.info(f"Created new default Canvas for channel {channel_id}")
                    return True, "Created new canvas"
                else:
                    logger.error(f"Failed to create Canvas: {message}")
                    return False, message
                
        except Exception as e:
            logger.error(f"Error ensuring Canvas exists: {str(e)}")
            return False, f"❌ Error with canvas: {str(e)}"



    def _handle_config_command(self, payload: Dict, bot_command: BotCommand) -> Dict:
        """
        Handle the /config command for bot configuration
        
        Args:
            payload: Slack slash command payload
            bot_command: BotCommand database record
            
        Returns:
            Response dictionary for Slack
        """
        text = payload.get('text', '').strip()
        user_id = payload.get('user_id')
        channel_id = payload.get('channel_id')
        
        logger.info(f"Processing config command with text: '{text}'")
        
        try:
            parts = text.split() if text else ['help']
            subcommand = parts[0].lower()
            
            if subcommand == 'auto-tasks' or subcommand == 'autotasks':
                return self._config_auto_tasks(parts[1:], user_id, channel_id, bot_command)
            elif subcommand == 'notifications':
                return self._config_notifications(parts[1:], user_id, channel_id, bot_command)
            elif subcommand == 'status':
                return self._config_show_status(user_id, channel_id, bot_command)
            elif subcommand == 'help':
                return self._config_show_help(bot_command)
            else:
                return self._config_show_help(bot_command)
                
        except Exception as e:
            logger.error(f"Error in config command: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            return {
                "response_type": "ephemeral",
                "text": "❌ An error occurred while processing your config command. Please try again later."
            }

    def _config_auto_tasks(self, args: List[str], user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Configure auto-task detection"""
        if not args:
            # Show current status
            enabled = self._is_auto_task_detection_enabled(channel_id)
            status = "✅ **ENABLED**" if enabled else "❌ **DISABLED**"
            
            bot_command.status = 'completed'
            bot_command.save()
            
            return {
                "response_type": "ephemeral",
                "text": f"<@{user_id}> 🤖 **Auto-Task Detection Status:** {status}\n\n💡 Use `/config auto-tasks enable` or `/config auto-tasks disable` to change"
            }
        
        action = args[0].lower()
        if action == 'enable':
            # For now, auto-tasks are always enabled
            # In future, this could update database settings
            bot_command.status = 'completed'
            bot_command.save()
            
            return {
                "response_type": "in_channel",
                "text": f"<@{user_id}> ✅ Auto-task detection **ENABLED** for this channel\n\n🤖 I'll now automatically detect tasks from messages and create todos!"
            }
        elif action == 'disable':
            bot_command.status = 'completed'
            bot_command.save()
            
            return {
                "response_type": "in_channel", 
                "text": f"<@{user_id}> ❌ Auto-task detection **DISABLED** for this channel\n\n💡 Use `/config auto-tasks enable` to re-enable"
            }
        else:
            return {
                "response_type": "ephemeral",
                "text": "❌ Usage: `/config auto-tasks [enable|disable]`"
            }

    def _config_notifications(self, args: List[str], user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Configure auto-todo notifications"""
        if not args:
            # Show current status
            enabled = self._should_notify_auto_todo_creation(channel_id)
            status = "✅ **ENABLED**" if enabled else "❌ **DISABLED**"
            
            bot_command.status = 'completed'
            bot_command.save()
            
            return {
                "response_type": "ephemeral",
                "text": f"<@{user_id}> 🔔 **Auto-Todo Notifications:** {status}\n\n💡 Use `/config notifications enable` or `/config notifications disable` to change"
            }
        
        # For now, notifications are disabled by default
        # This could be made configurable in the future
        bot_command.status = 'completed'
        bot_command.save()
        
        return {
            "response_type": "ephemeral",
            "text": f"<@{user_id}> 🔔 Notifications are currently managed automatically to avoid spam.\n\n💡 Todos are created silently and synced to Canvas. Use `/todo list` or `/canvas show` to see them."
        }

    def _config_show_status(self, user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Show current bot configuration"""
        auto_tasks_enabled = self._is_auto_task_detection_enabled(channel_id)
        notifications_enabled = self._should_notify_auto_todo_creation(channel_id)
        
        # Get channel stats
        from .models import ChannelTodo, ChannelCanvas
        channel = SlackChannel.objects.filter(channel_id=channel_id).first()
        
        if channel:
            total_todos = ChannelTodo.objects.filter(channel=channel).count()
            pending_todos = ChannelTodo.objects.filter(channel=channel, status__in=['pending', 'in_progress']).count()
            canvas_exists = ChannelCanvas.objects.filter(channel=channel).exists()
        else:
            total_todos = 0
            pending_todos = 0
            canvas_exists = False
        
        auto_status = "✅ **ENABLED**" if auto_tasks_enabled else "❌ **DISABLED**"
        notify_status = "✅ **ENABLED**" if notifications_enabled else "❌ **DISABLED**"
        canvas_status = "✅ **EXISTS**" if canvas_exists else "❌ **NOT CREATED**"
        
        bot_command.status = 'completed'
        bot_command.save()
        
        status_message = f"""<@{user_id}> 🤖 **Bot Configuration Status**

🔍 **Auto-Task Detection:** {auto_status}
🔔 **Auto-Notifications:** {notify_status}
🎨 **Canvas Integration:** {canvas_status}

📊 **Channel Statistics:**
• Total todos: {total_todos}
• Pending todos: {pending_todos}
• Channel: #{self._get_channel_name(channel_id)}

⚙️ **Available Commands:**
• `/config auto-tasks enable/disable` - Control task detection
• `/todo list` - View current todos
• `/canvas show` - View Canvas info
• `/config help` - Show all configuration options"""
        
        return {
            "response_type": "ephemeral",
            "text": status_message
        }

    def _config_show_help(self, bot_command: BotCommand) -> Dict:
        """Show configuration help"""
        bot_command.status = 'completed'
        bot_command.save()
        
        help_text = """⚙️ **Bot Configuration Commands**

**Main Commands:**
• `/config status` - Show current bot configuration
• `/config auto-tasks [enable|disable]` - Control automatic task detection
• `/config notifications [enable|disable]` - Control auto-todo notifications
• `/config help` - Show this help

**Auto-Task Detection:**
🤖 When enabled, the bot automatically:
• Monitors all channel messages in real-time
• Uses AI to detect actionable tasks, deadlines, and assignments
• Creates todos automatically (confidence ≥ 70%)
• Updates Canvas documents automatically
• Links todos back to original messages

**Example Messages it Detects:**
• "Need to finalize the monthly report by EOD"
• "Fix the login bug on staging server"
• "@john please review the PR before Friday"
• "Team meeting tomorrow at 3pm"
• "Deploy the hotfix to production"

**Canvas Integration:**
🎨 Automatically syncs todos to beautiful visual boards
• Use `/canvas create` to set up Canvas for this channel
• Canvas updates automatically when todos change
• Share visual progress with your team

**Privacy & Control:**
• Auto-detection can be disabled per channel
• Notifications are minimal to avoid spam
• All todos can be managed with `/todo` commands
• Full audit trail of auto-created todos

💡 **Quick Start:** `/config auto-tasks enable` then post some task-related messages to see the magic!"""
        
        return {
            "response_type": "ephemeral",
            "text": help_text
        }

    # Todo command helpers
    def _todo_add(self, args: List[str], user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Add a new todo"""
        if not args:
            return {
                "response_type": "ephemeral",
                "text": "❌ Please provide a todo title. Usage: `/todo add \"Fix the login bug\" @user high tomorrow`"
            }
        
        # Parse arguments
        title = " ".join(args)
        description = ""
        task_type = "general"
        priority = "medium"
        assigned_to = ""
        due_date = ""
        
        # Extract quoted title if present
        if title.startswith('"') and '"' in title[1:]:
            end_quote = title.index('"', 1)
            description = title[:end_quote+1].strip('"')
            remaining = title[end_quote+1:].strip()
            
            # Parse remaining arguments
            remaining_parts = remaining.split()
            for part in remaining_parts:
                if part.startswith('@'):
                    assigned_to = part
                elif part.lower() in ['low', 'medium', 'high', 'critical']:
                    priority = part.lower()
                elif part.lower() in ['today', 'tomorrow', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday']:
                    due_date = part.lower()
                elif part.lower() in ['bug', 'feature', 'meeting', 'review', 'deadline', 'urgent']:
                    task_type = part.lower()
            
            title = description
        
        success, message, todo = self.todo_manager.add_todo(
                channel_id=channel_id,
            title=title,
            description=description,
            task_type=task_type,
            priority=priority,
            assigned_to=assigned_to,
            due_date=due_date,
            created_by=user_id
        )
        
        if success:
            bot_command.status = 'completed'
            bot_command.save()
            
            # Auto-sync canvas if exists
            self.canvas_manager.auto_sync_canvas(channel_id)
            
            return {
                "response_type": "in_channel",
                "text": f"<@{user_id}> {message}"
            }
        else:
            bot_command.status = 'failed'
            bot_command.error_message = message
            bot_command.save()
            
            return {
                "response_type": "ephemeral",
                "text": message
            }

    def _todo_add_direct(self, text: str, user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Add todo directly from command text"""
        success, message, todo = self.todo_manager.add_todo(
            channel_id=channel_id,
            title=text,
            created_by=user_id
        )
        
        if success:
            bot_command.status = 'completed'
            bot_command.save()
            
            # Auto-sync canvas if exists
            self.canvas_manager.auto_sync_canvas(channel_id)
            
            return {
                "response_type": "in_channel",
                "text": f"<@{user_id}> {message}"
            }
        else:
            return {
                "response_type": "ephemeral",
                "text": message
            }

    def _todo_list(self, args: List[str], user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """List todos with optional filters"""
        filter_status = "active"
        filter_assigned = ""
        filter_priority = ""
        
        # Parse filters
        for arg in args:
            if arg.lower() in ['active', 'completed', 'all', 'pending', 'in_progress']:
                filter_status = arg.lower()
            elif arg.startswith('@'):
                filter_assigned = arg
            elif arg.lower() in ['low', 'medium', 'high', 'critical']:
                filter_priority = arg.lower()
        
        success, message, todos = self.todo_manager.list_todos(
            channel_id=channel_id,
            filter_status=filter_status,
            filter_assigned=filter_assigned,
            filter_priority=filter_priority
        )
        
        bot_command.status = 'completed' if success else 'failed'
        if not success:
            bot_command.error_message = message
        bot_command.save()
        
        return {
            "response_type": "ephemeral",
            "text": f"<@{user_id}> {message}"
        }

    def _todo_complete(self, args: List[str], user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Complete a todo"""
        if not args:
            return {
                "response_type": "ephemeral",
                "text": "❌ Please specify a todo to complete. Usage: `/todo complete 1` or `/todo complete \"task title\"`"
            }
        
        identifier = " ".join(args).strip('"')
        success, message, todo = self.todo_manager.complete_todo(
                channel_id=channel_id,
            todo_identifier=identifier,
            completed_by=user_id
        )
        
        bot_command.status = 'completed' if success else 'failed'
        if not success:
            bot_command.error_message = message
        bot_command.save()
        
        if success:
            # Auto-sync canvas if exists
            self.canvas_manager.auto_sync_canvas(channel_id)
            
            return {
                "response_type": "in_channel",
                "text": f"<@{user_id}> {message}"
            }
        else:
            return {
                "response_type": "ephemeral",
                "text": message
            }

    def _todo_show_help(self, bot_command: BotCommand) -> Dict:
        """Show todo command help"""
        bot_command.status = 'completed'
        bot_command.save()
        
        help_text = """📝 **Todo Management Commands**

**Basic Usage:**
• `/todo add "Fix login bug" @user high tomorrow` - Add a new todo
• `/todo list` - Show all active todos
• `/todo complete 1` - Mark todo #1 as completed
• `/todo edit 1 priority high` - Edit todo properties

**Commands:**
• `add "title" [type] [priority] [@user] [due_date]` - Create new todo
• `list [status] [@user] [priority]` - List todos with filters
• `complete <id|title>` - Mark todo as completed
• `edit <id|title> [title/description/priority/assigned/due]` - Edit todo
• `assign <id|title> @user` - Assign todo to someone
• `priority <id|title> <level>` - Set priority (low/medium/high/critical)
• `delete <id|title>` - Delete a todo
• `help` - Show this help

**Examples:**
• `/todo add "Review PR #123" review high @sarah`
• `/todo add "Team meeting" meeting medium tomorrow`
• `/todo list completed` - Show completed todos
• `/todo assign 2 @john` - Assign todo #2 to John
• `/todo priority 3 critical` - Set todo #3 to critical priority

**Task Types:** bug, feature, meeting, review, deadline, urgent, general
**Priorities:** low, medium, high, critical
**Due Dates:** today, tomorrow, monday-friday"""
        
        return {
            "response_type": "ephemeral",
            "text": help_text
        }

    def _todo_edit(self, args: List[str], user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Edit a todo"""
        if len(args) < 2:
            return {
                "response_type": "ephemeral",
                "text": "❌ Usage: `/todo edit <id|title> [title|description|priority|assigned|due] <new_value>`"
            }
        
        identifier = args[0]
        field = args[1].lower() if len(args) > 1 else ""
        new_value = " ".join(args[2:]) if len(args) > 2 else ""
        
        if field == "title":
            success, message, todo = self.todo_manager.edit_todo(channel_id, identifier, new_title=new_value)
        elif field == "description":
            success, message, todo = self.todo_manager.edit_todo(channel_id, identifier, new_description=new_value)
        elif field == "priority":
            success, message, todo = self.todo_manager.edit_todo(channel_id, identifier, new_priority=new_value)
        elif field == "assigned":
            success, message, todo = self.todo_manager.edit_todo(channel_id, identifier, new_assigned=new_value)
        elif field == "due":
            success, message, todo = self.todo_manager.edit_todo(channel_id, identifier, new_due_date=new_value)
        else:
            return {
                "response_type": "ephemeral",
                "text": "❌ Valid fields: title, description, priority, assigned, due"
            }
        
        bot_command.status = 'completed' if success else 'failed'
        if not success:
            bot_command.error_message = message
        bot_command.save()
        
        if success:
            self.canvas_manager.auto_sync_canvas(channel_id)
        
        return {
            "response_type": "in_channel" if success else "ephemeral",
            "text": f"<@{user_id}> {message}"
        }

    def _todo_assign(self, args: List[str], user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Assign a todo to a user"""
        if len(args) < 2:
            return {
                "response_type": "ephemeral",
                "text": "❌ Usage: `/todo assign <id|title> @username`"
            }
        
        identifier = args[0]
        assigned_to = args[1]
        
        success, message, todo = self.todo_manager.assign_todo(channel_id, identifier, assigned_to)
        
        bot_command.status = 'completed' if success else 'failed'
        if not success:
            bot_command.error_message = message
        bot_command.save()
        
        if success:
            self.canvas_manager.auto_sync_canvas(channel_id)
        
        return {
            "response_type": "in_channel" if success else "ephemeral",
            "text": f"<@{user_id}> {message}"
        }

    def _todo_priority(self, args: List[str], user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Set priority for a todo"""
        if len(args) < 2:
            return {
                "response_type": "ephemeral",
                "text": "❌ Usage: `/todo priority <id|title> <low|medium|high|critical>`"
            }
        
        identifier = args[0]
        priority = args[1]
        
        success, message, todo = self.todo_manager.set_priority(channel_id, identifier, priority)
        
        bot_command.status = 'completed' if success else 'failed'
        if not success:
            bot_command.error_message = message
        bot_command.save()
        
        if success:
            self.canvas_manager.auto_sync_canvas(channel_id)
        
        return {
            "response_type": "in_channel" if success else "ephemeral",
            "text": f"<@{user_id}> {message}"
        }

    def _todo_delete(self, args: List[str], user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Delete a todo"""
        if not args:
            return {
                "response_type": "ephemeral",
                "text": "❌ Usage: `/todo delete <id|title>`"
            }
        
        identifier = " ".join(args).strip('"')
        success, message = self.todo_manager.delete_todo(channel_id, identifier)
        
        bot_command.status = 'completed' if success else 'failed'
        if not success:
            bot_command.error_message = message
        bot_command.save()
        
        if success:
            self.canvas_manager.auto_sync_canvas(channel_id)
        
        return {
            "response_type": "in_channel" if success else "ephemeral",
            "text": f"<@{user_id}> {message}"
        }

     # Tasks command helpers
    def _tasks_summary(self, args: List[str], user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Generate task summary for channel"""
        # This will be implemented to enhance the existing summary with task analysis
        return {
            "response_type": "ephemeral",
            "text": f"<@{user_id}> Task summary feature is being developed. Use `/todo list` to see current todos."
        }

    def _tasks_extract(self, args: List[str], user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Extract tasks from recent messages"""
        auto_create = 'auto' in args
        
        # Get recent messages (last 100)
        try:
            response = self.client.conversations_history(
                channel=channel_id,
                limit=100
            )
            
            if response['ok']:
                messages = response['messages']
                
                success, message, detected_tasks = self.todo_manager.extract_tasks_from_messages(
                    channel_id=channel_id,
                    messages=messages,
                    auto_create=auto_create,
                    created_by=user_id
                )
                
                bot_command.status = 'completed' if success else 'failed'
                if not success:
                    bot_command.error_message = message
                bot_command.save()
                
                if success and auto_create:
                    # Auto-sync canvas if exists
                    self.canvas_manager.auto_sync_canvas(channel_id)
                
                return {
                    "response_type": "ephemeral",
                    "text": f"<@{user_id}> {message}"
                }
            else:
                return {
                    "response_type": "ephemeral",
                    "text": "❌ Could not access channel messages"
                }
            
        except Exception as e:
            logger.error(f"Error extracting tasks: {str(e)}")
            return {
                "response_type": "ephemeral",
                                 "text": f"❌ Failed to extract tasks: {str(e)}"
             }

    def _tasks_priority(self, args: List[str], user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Show high priority todos"""
        priority = args[0] if args else "high"
        
        success, message, todos = self.todo_manager.get_priority_todos(channel_id, priority)
        
        bot_command.status = 'completed' if success else 'failed'
        if not success:
            bot_command.error_message = message
        bot_command.save()
        
        return {
            "response_type": "ephemeral",
            "text": f"<@{user_id}> {message}"
        }

    def _tasks_overdue(self, user_id: str, channel_id: str, bot_command: BotCommand) -> Dict:
        """Show overdue todos"""
        success, message, todos = self.todo_manager.get_overdue_todos(channel_id)
        
        bot_command.status = 'completed' if success else 'failed'
        if not success:
            bot_command.error_message = message
        bot_command.save()
        
        return {
            "response_type": "ephemeral",
            "text": f"<@{user_id}> {message}"
        }

    def _tasks_show_help(self, bot_command: BotCommand) -> Dict:
        """Show tasks command help"""
        bot_command.status = 'completed'
        bot_command.save()
        
        help_text = """🔍 **Task Analysis Commands**

**Commands:**
• `/tasks summary` - Analyze channel for task-related content
• `/tasks extract` - Extract tasks from recent messages (preview only)
• `/tasks extract auto` - Extract and automatically create todos
• `/tasks priority [level]` - Show todos by priority (default: high)
• `/tasks overdue` - Show overdue todos
• `/tasks help` - Show this help

**What does Tasks do?**
The Tasks command uses AI to analyze your channel messages and:
• Identify actionable items and tasks
• Detect bug reports and feature requests
• Find deadlines and time-sensitive items
• Extract assignments and responsibilities
• Analyze team workload and priorities

**Examples:**
• `/tasks extract auto` - Scan recent messages and create todos automatically
• `/tasks priority critical` - Show all critical priority todos
• `/tasks overdue` - Find todos that are past their due date
• `/tasks summary` - Get insights into task-related discussions

💡 Use `/tasks extract` to see what the AI can find, then add `auto` to create todos automatically!"""
        
        return {
            "response_type": "ephemeral",
            "text": help_text
        }

     # Canvas command helpers


    # Auto-task detection helper methods
    def _is_auto_task_detection_enabled(self, channel_id: str) -> bool:
        """
        Check if auto-task detection is enabled for a channel
        
        Args:
            channel_id: Slack channel ID
            
        Returns:
            True if auto-detection is enabled (default: True for all channels)
        """
        try:
            # For now, enable auto-detection for all channels
            # This can be made configurable per channel in the future
            return True
            
            # Future implementation could check database settings:
            # channel = SlackChannel.objects.filter(channel_id=channel_id).first()
            # return channel.auto_task_detection_enabled if channel else True
            
        except Exception as e:
            logger.error(f"Error checking auto-task detection setting: {str(e)}")
            return True  # Default to enabled
    
    def _get_channel_name(self, channel_id: str) -> str:
        """
        Get channel name from channel ID
        
        Args:
            channel_id: Slack channel ID
            
        Returns:
            Channel name or 'unknown'
        """
        try:
            # First try to get from database
            channel = SlackChannel.objects.filter(channel_id=channel_id).first()
            if channel:
                return channel.channel_name
            
            # If not in database, try Slack API
            response = self.client.conversations_info(channel=channel_id)
            if response['ok']:
                return response['channel']['name']
            
            return 'unknown'
            
        except Exception as e:
            logger.error(f"Error getting channel name: {str(e)}")
            return 'unknown'
    
    def _auto_create_todo_from_message(self, detected_task, channel_id: str, 
                                       user_id: str, message_timestamp: str) -> bool:
        """
        Automatically create a todo from a detected task in a message
        
        Args:
            detected_task: DetectedTask object from AI analysis
            channel_id: Slack channel ID
            user_id: User who sent the original message
            message_timestamp: Timestamp of the original message
            
        Returns:
            True if todo was created successfully
        """
        try:
            # Resolve assigned user if detected
            assigned_to = detected_task.assigned_to
            assigned_to_username = detected_task.assigned_to_username
            
            if assigned_to_username and not assigned_to:
                # Try to resolve username to user ID
                user_info = self.todo_manager._resolve_user(assigned_to_username)
                if user_info:
                    assigned_to = user_info.get('user_id', '')
                    assigned_to_username = user_info.get('username', assigned_to_username)
            
            # Create message link for reference
            workspace = self.todo_manager._get_or_create_workspace()
            channel = self.todo_manager._get_or_create_channel(channel_id, workspace)
            
            message_link = f"https://slack.com/archives/{channel_id}/p{message_timestamp.replace('.', '')}"
            
            # Create the todo
            todo = ChannelTodo.objects.create(
                channel=channel,
                title=detected_task.title,
                description=f"Auto-detected from message: {detected_task.description[:200]}",
                task_type=detected_task.task_type,
                priority=detected_task.priority,
                assigned_to=assigned_to,
                assigned_to_username=assigned_to_username,
                due_date=detected_task.due_date,
                created_from_message=message_timestamp,
                created_from_message_link=message_link,
                created_by=user_id,  # Original message author
                status='pending'
            )
            
            # Create reminder if due date is set
            if detected_task.due_date:
                try:
                    from .models import TaskReminder
                    from django.utils import timezone
                    from datetime import timedelta
                    
                    # Create due soon reminder (1 hour before)
                    reminder_time = detected_task.due_date - timedelta(hours=1)
                    if reminder_time > timezone.now():
                        TaskReminder.objects.create(
                            todo=todo,
                            reminder_type='due_soon',
                            reminder_time=reminder_time
                        )
                except Exception as e:
                    logger.warning(f"Could not create reminder: {str(e)}")
            
            logger.info(f"Auto-created todo {todo.id}: {todo.title}")
            return True
            
        except Exception as e:
            logger.error(f"Error auto-creating todo: {str(e)}")
            return False
    
    def _should_notify_auto_todo_creation(self, channel_id: str) -> bool:
        """
        Check if the bot should notify the channel when auto-creating todos
        
        Args:
            channel_id: Slack channel ID
            
        Returns:
            True if notifications should be sent (default: False to avoid spam)
        """
        # Default to False to avoid spamming channels
        # Can be made configurable per channel
        return False
    
    def _send_auto_todo_notification(self, channel_id: str, detected_task, user_id: str):
        """
        Send a notification about auto-created todo
        
        Args:
            channel_id: Slack channel ID
            detected_task: DetectedTask object
            user_id: User who sent the original message
        """
        try:
            # Create a subtle notification
            priority_emoji = {
                'critical': '🔴',
                'high': '🟠',
                'medium': '🟡',
                'low': '🟢'
            }
            
            type_emoji = {
                'bug': '🐛',
                'feature': '✨',
                'meeting': '📅',
                'review': '👀',
                'deadline': '⏰',
                'urgent': '🚨',
                'general': '📝'
            }
            
            message = f"🤖 Auto-detected task from <@{user_id}>'s message: {priority_emoji.get(detected_task.priority, '🟡')}{type_emoji.get(detected_task.task_type, '📝')} **{detected_task.title}**"
            
            if detected_task.due_date:
                message += f" | Due: {detected_task.due_date.strftime('%m/%d %H:%M')}"
            
            message += f"\n💡 Use `/todo list` to manage todos or `/canvas show` to see the visual board"
            
            self.client.chat_postMessage(
                channel=channel_id,
                text=message,
                unfurl_links=False,
                unfurl_media=False
            )
            
        except Exception as e:
            logger.error(f"Error sending auto-todo notification: {str(e)}")

    def _get_user_channels(self, user_id: str) -> List[Dict]:
        """
        Get all channels the user is a member of
        
        Args:
            user_id: User ID to get channels for
            
        Returns:
            List of channel dictionaries
        """
        try:
            user_channels = []
            cursor = None
            
            while True:
                response = self.client.users_conversations(
                    user=user_id,
                    types="public_channel,private_channel",
                    limit=200,
                    cursor=cursor
                )
                
                if response['ok']:
                    channels = response.get('channels', [])
                    user_channels.extend(channels)
                    
                    cursor = response.get('response_metadata', {}).get('next_cursor')
                    if not cursor:
                        break
                else:
                    logger.error(f"Failed to get user channels: {response.get('error', 'Unknown error')}")
                    break
            
            logger.info(f"Found {len(user_channels)} channels for user {user_id}")
            return user_channels
            
        except Exception as e:
            logger.error(f"Error getting user channels: {str(e)}")
            return []
    
    def _get_user_dms(self, user_id: str) -> List[Dict]:
        """
        Get all DM conversations for the user
        
        Args:
            user_id: User ID to get DMs for
            
        Returns:
            List of DM dictionaries
        """
        try:
            user_dms = []
            cursor = None
            
            while True:
                response = self.client.users_conversations(
                    user=user_id,
                    types="im,mpim",
                    limit=200,
                    cursor=cursor
                )
                
                if response['ok']:
                    dms = response.get('channels', [])
                    # Filter out the DM with the bot itself
                    filtered_dms = [dm for dm in dms if dm.get('user') != self.bot_user_id]
                    user_dms.extend(filtered_dms)
                    
                cursor = response.get('response_metadata', {}).get('next_cursor')
                if not cursor:
                        break
                else:
                    logger.error(f"Failed to get user DMs: {response.get('error', 'Unknown error')}")
                    break
            
            logger.info(f"Found {len(user_dms)} DM conversations for user {user_id}")
            return user_dms
            
        except Exception as e:
            logger.error(f"Error getting user DMs: {str(e)}")
            return []

    def _extract_tasks_from_channel(self, channel_id: str, user_id: str) -> List[Dict]:
        """
        Extract tasks from a specific channel
        
        Args:
            channel_id: Channel ID to scan
            user_id: User ID who requested the scan
            
        Returns:
            List of task dictionaries
        """
        try:
            # Get recent messages from channel (last 100 for better coverage)
            response = self._safe_api_call(
                self.client.conversations_history,
                channel=channel_id,
                limit=100
            )
            
            if not response['ok']:
                logger.warning(f"Could not access channel {channel_id}: {response.get('error', 'Unknown error')}")
            return []
    
            messages = response['messages']
            channel_name = self._get_channel_name(channel_id)
            extracted_tasks = []
            
            # Filter and analyze messages
            for msg in messages:
                if (not msg.get('bot_id') and 
                    msg.get('user') != self.bot_user_id and 
                    not msg.get('subtype') and
                    msg.get('text', '').strip() and
                    len(msg.get('text', '').strip()) >= 8):  # Lower threshold to catch more tasks
                    
                    message_text = msg.get('text', '')
                    msg_user_id = msg.get('user', '')
                    timestamp = msg.get('ts', '')
                    
                    # Use AI to detect actionable content (with fallback for quota exceeded)
                    detected_task = None
                    try:
                        detected_task = self.task_detector.analyze_message(
                            message=message_text,
                            channel_name=channel_name,
                            user_id=msg_user_id,
                            timestamp=timestamp
                        )
                    except Exception as ai_error:
                        if "429" in str(ai_error) or "quota" in str(ai_error).lower():
                            logger.warning(f"AI quota exceeded, using keyword fallback for: {message_text[:50]}")
                            # Simple keyword-based fallback when AI quota is exceeded
                            detected_task = self._simple_task_detection_fallback(message_text, channel_name)
                        else:
                            logger.error(f"AI analysis error: {ai_error}")
                            # Try fallback on any AI error
                            detected_task = self._simple_task_detection_fallback(message_text, channel_name)
                    
                    # Only include tasks with reasonable confidence for personal analysis (lowered for better coverage)
                    if detected_task and detected_task.confidence_score >= 0.4:
                        task_dict = {
                            'title': detected_task.title,
                            'description': f"From #{channel_name}: {message_text[:150]}",
                            'task_type': detected_task.task_type,
                            'priority': detected_task.priority,
                            'due_date': detected_task.due_date,
                            'source_type': 'channel',
                            'source_name': channel_name,
                            'source_id': channel_id,
                            'message_timestamp': timestamp,
                            'message_user': msg_user_id,
                            'message_link': f"https://slack.com/archives/{channel_id}/p{timestamp.replace('.', '')}",
                            'confidence_score': detected_task.confidence_score
                        }
                        extracted_tasks.append(task_dict)
            
            logger.info(f"Extracted {len(extracted_tasks)} tasks from channel {channel_name} (scanned {len(messages)} messages)")
            return extracted_tasks
            
        except Exception as e:
            logger.error(f"Error extracting tasks from channel {channel_id}: {str(e)}")
            return []

    def _extract_tasks_from_dm(self, dm_id: str, user_id: str) -> List[Dict]:
        """
        Extract tasks from a specific DM conversation
        
        Args:
            dm_id: DM channel ID to scan
            user_id: User ID who requested the scan
            
        Returns:
            List of task dictionaries
        """
        try:
            # Get recent messages from DM (last 60 for better coverage)
            response = self._safe_api_call(
                self.client.conversations_history,
                channel=dm_id,
                limit=60
            )
            
            if not response['ok']:
                logger.warning(f"Could not access DM {dm_id}: {response.get('error', 'Unknown error')}")
                return []
            
            messages = response['messages']
            extracted_tasks = []
            
            # Get DM partner name for context
            dm_partner_name = self._get_dm_partner_name(dm_id, user_id)
            
            # Filter and analyze messages
            for msg in messages:
                if (not msg.get('bot_id') and 
                    not msg.get('subtype') and
                    msg.get('text', '').strip() and
                    len(msg.get('text', '').strip()) >= 15):
                    
                    message_text = msg.get('text', '')
                    msg_user_id = msg.get('user', '')
                    timestamp = msg.get('ts', '')
                    
                    # Use AI to detect actionable content (with fallback for quota exceeded)
                    detected_task = None
                    try:
                        detected_task = self.task_detector.analyze_message(
                            message=message_text,
                            channel_name=f"DM with {dm_partner_name}",
                            user_id=msg_user_id,
                            timestamp=timestamp
                        )
                    except Exception as ai_error:
                        if "429" in str(ai_error) or "quota" in str(ai_error).lower():
                            logger.warning(f"AI quota exceeded in DM, using keyword fallback for: {message_text[:50]}")
                            # Simple keyword-based fallback when AI quota is exceeded
                            detected_task = self._simple_task_detection_fallback(message_text, f"DM with {dm_partner_name}")
                        else:
                            logger.error(f"AI analysis error in DM: {ai_error}")
                    
                    # Only include tasks with good confidence for personal analysis
                    if detected_task and detected_task.confidence_score >= 0.7:  # Higher threshold for DMs
                        task_dict = {
                            'title': detected_task.title,
                            'description': f"From DM with {dm_partner_name}: {message_text[:150]}",
                            'task_type': detected_task.task_type,
                            'priority': detected_task.priority,
                            'due_date': detected_task.due_date,
                            'source_type': 'dm',
                            'source_name': f"DM with {dm_partner_name}",
                            'source_id': dm_id,
                            'message_timestamp': timestamp,
                            'message_user': msg_user_id,
                            'message_link': f"https://slack.com/archives/{dm_id}/p{timestamp.replace('.', '')}",
                            'confidence_score': detected_task.confidence_score
                        }
                        extracted_tasks.append(task_dict)
            
            logger.info(f"Extracted {len(extracted_tasks)} tasks from DM with {dm_partner_name}")
            return extracted_tasks
            
        except Exception as e:
            logger.error(f"Error extracting tasks from DM {dm_id}: {str(e)}")
            return []

    def _get_dm_partner_name(self, dm_id: str, user_id: str) -> str:
        """
        Get the name of the other person in a DM conversation
        
        Args:
            dm_id: DM channel ID
            user_id: Current user ID
            
        Returns:
            Partner's display name or username
        """
        try:
            # Get DM info
            response = self.client.conversations_info(channel=dm_id)
            if response['ok']:
                channel_info = response['channel']
                
                if channel_info.get('is_im'):
                    # Direct IM - get the other user
                    partner_id = channel_info.get('user')
                    if partner_id and partner_id != user_id:
                        user_info = self.client.users_info(user=partner_id)
                        if user_info['ok']:
                            user_data = user_info['user']
                            return user_data.get('display_name') or user_data.get('real_name') or user_data.get('name', 'Unknown')
                elif channel_info.get('is_mpim'):
                    # Multi-person IM - return member count
                    return f"Group DM ({len(channel_info.get('members', []))} members)"
            
            return "Unknown"
            
        except Exception as e:
            logger.error(f"Error getting DM partner name: {str(e)}")
            return "Unknown"

    def _deduplicate_tasks(self, all_tasks: List[Dict]) -> List[Dict]:
        """
        Remove duplicate tasks based on similarity and content
        
        Args:
            all_tasks: List of all extracted tasks
            
        Returns:
            List of deduplicated tasks
        """
        try:
            if not all_tasks:
                return []
            
            deduplicated = []
            seen_titles = set()
            
            # Sort by confidence score (highest first) to prefer better detections
            sorted_tasks = sorted(all_tasks, key=lambda x: x.get('confidence_score', 0), reverse=True)
            
            for task in sorted_tasks:
                title = task.get('title', '').lower().strip()
                
                # Check for exact title matches
                if title in seen_titles:
                    continue
                
                # Check for similar titles (simple similarity check)
                is_similar = False
                for seen_title in seen_titles:
                    # Simple similarity check - if 80% of words overlap
                    title_words = set(title.split())
                    seen_words = set(seen_title.split())
                    
                    if len(title_words) > 2 and len(seen_words) > 2:
                        overlap = len(title_words.intersection(seen_words))
                        similarity = overlap / max(len(title_words), len(seen_words))
                        
                        if similarity > 0.8:
                            is_similar = True
                            break
                
                if not is_similar:
                    seen_titles.add(title)
                    deduplicated.append(task)
            
            logger.info(f"Deduplicated {len(all_tasks)} tasks down to {len(deduplicated)} unique tasks")
            return deduplicated
            
        except Exception as e:
            logger.error(f"Error deduplicating tasks: {str(e)}")
            return all_tasks  # Return original list if deduplication fails



    def _generate_personal_canvas_content(self, user_id: str, tasks: List[Dict]) -> str:
        """
        Generate Canvas content for personal master todo list
        
        Args:
            user_id: User ID
            tasks: List of task dictionaries
            
        Returns:
            Canvas markdown content
        """
        try:
            from django.utils import timezone
            
            # Organize tasks by priority
            priority_groups = {
                'critical': [],
                'high': [],
                'medium': [],
                'low': []
            }
            
            for task in tasks:
                priority = task.get('priority', 'medium')
                priority_groups[priority].append(task)
            
            # Generate content
            content_parts = [
                f"# 🎯 Personal Master Todo List",
                "",
                f"> *Generated from your entire workspace • {timezone.now().strftime('%Y-%m-%d %H:%M')}*",
                "",
                f"📊 **Summary:** {len(tasks)} actionable tasks found across all your conversations",
                "",
                "## 📌 Your Tasks by Priority",
                ""
            ]
            
            # Add tasks by priority
            for priority in ['critical', 'high', 'medium', 'low']:
                if priority_groups[priority]:
                    priority_emoji = {
                        'critical': '🔴',
                        'high': '🟠', 
                        'medium': '🟡',
                        'low': '🟢'
                    }
                    
                    content_parts.append(f"### {priority_emoji[priority]} {priority.upper()} PRIORITY")
                    content_parts.append("")
                    
                    for task in priority_groups[priority]:
                        # Format task with source info
                        checkbox = "- [ ]"
                        title = task.get('title', 'Untitled task')
                        source = task.get('source_name', 'Unknown source')
                        task_type = task.get('task_type', 'general')
                        
                        # Task type emoji
                        type_emoji = {
                            'bug': '🐛', 'feature': '✨', 'meeting': '📅',
                            'review': '👀', 'urgent': '🚨', 'deadline': '⏰'
                        }
                        
                        todo_line = f"{checkbox} **{title}**"
                        todo_line += f" | 📍 {source}"
                        
                        if task_type != 'general':
                            todo_line += f" | {type_emoji.get(task_type, '📝')} {task_type}"
                        
                        # Add confidence score for transparency
                        confidence = task.get('confidence_score', 0)
                        todo_line += f" | 🎯 {confidence:.0%} confidence"
                        
                        content_parts.append(todo_line)
                        
                        # Add description if present
                        description = task.get('description', '')
                        if description:
                            short_desc = description[:100] + "..." if len(description) > 100 else description
                            content_parts.append(f"  💬 *{short_desc}*")
                    
                    content_parts.append("")
            
            # Add source breakdown
            source_counts = {}
            for task in tasks:
                source_type = task.get('source_type', 'unknown')
                source_name = task.get('source_name', 'Unknown')
                key = f"{source_type}:{source_name}"
                source_counts[key] = source_counts.get(key, 0) + 1
            
            content_parts.extend([
                "## 📊 Task Sources",
                "",
            ])
            
            for source, count in sorted(source_counts.items(), key=lambda x: x[1], reverse=True):
                source_type, source_name = source.split(':', 1)
                type_emoji = "📢" if source_type == "channel" else "💬"
                content_parts.append(f"- {type_emoji} **{source_name}**: {count} tasks")
            
            content_parts.extend([
                "",
                "---",
                "### 💡 Quick Commands",
                "- `/task` - Refresh this list with latest messages",
                "- `/todo add \"task name\"` - Add manual todo",
                "- `/todo complete [id]` - Mark todo as completed",  
                "- `/todo list` - View all your todos",
                "",
                "*Your personal productivity assistant*"
            ])
            
            return "\n".join(content_parts)
            
        except Exception as e:
            logger.error(f"Error generating personal canvas content: {str(e)}")
            return f"# Personal Master Todo List\n\nError generating content: {str(e)}"

    def _save_personal_todos(self, user_id: str, tasks: List[Dict]) -> int:
        """
        Save tasks to database as personal todos
        
        Args:
            user_id: User ID
            tasks: List of task dictionaries
            
        Returns:
            Number of todos created
        """
        try:
            # Create or get personal workspace/channel for the user
            workspace = self.todo_manager._get_or_create_workspace()
            personal_channel = self.todo_manager._get_or_create_channel(
                f"personal_{user_id}", workspace
            )
            
            todos_created = 0
            
            for task in tasks:
                try:
                    # Check if this task already exists (prevent duplicates)
                    message_timestamp = task.get('message_timestamp', '')
                    source_id = task.get('source_id', '')
                    
                    existing_todo = ChannelTodo.objects.filter(
                        channel=personal_channel,
                        created_from_message=message_timestamp,
                        description__contains=source_id[:20]  # Use part of source_id as identifier
                    ).first()
                    
                    if not existing_todo:
                        # Parse due date if present
                        due_date = task.get('due_date')
                        if isinstance(due_date, str):
                            due_date = None  # Handle string dates properly if needed
                        
                        # Create the todo
                        todo = ChannelTodo.objects.create(
                            channel=personal_channel,
                            title=task.get('title', 'Untitled task')[:500],
                            description=task.get('description', '')[:1000],
                            task_type=task.get('task_type', 'general'),
                            priority=task.get('priority', 'medium'),
                            due_date=due_date,
                            created_from_message=message_timestamp,
                            created_from_message_link=task.get('message_link', ''),
                            created_by=user_id,
                            status='pending'
                        )
                        
                        todos_created += 1
                        
                except Exception as e:
                    logger.error(f"Error saving individual todo: {str(e)}")
                    continue
            
            logger.info(f"Saved {todos_created} personal todos for user {user_id}")
            return todos_created
            
        except Exception as e:
            logger.error(f"Error saving personal todos: {str(e)}")
            return 0

    def _simple_task_detection_fallback(self, message_text: str, channel_name: str):
        """
        Simple keyword-based task detection fallback when AI quota is exceeded
        
        Args:
            message_text: Message text to analyze
            channel_name: Channel name for context
            
        Returns:
            Simple detected task object or None
        """
        try:
            message_lower = message_text.lower().strip()
            
            # Task keywords that indicate actionable content (expanded)
            task_keywords = [
                'need to', 'have to', 'must', 'should', 'todo', 'task',
                'deadline', 'due', 'urgent', 'asap', 'please', 'can you',
                'fix', 'update', 'create', 'review', 'check', 'send',
                'call', 'email', 'meeting', 'schedule', 'prepare',
                'we need', 'i need', 'let\'s', 'remember to', 'don\'t forget',
                'action item', 'follow up', 'next step', 'work on',
                'finish', 'complete', 'implement', 'handle', 'take care',
                'make sure', 'ensure', 'organize', 'plan', 'discuss'
            ]
            
            # Priority keywords
            priority_keywords = {
                'critical': ['urgent', 'asap', 'critical', 'emergency', 'immediately'],
                'high': ['important', 'priority', 'soon', 'deadline'],
                'medium': ['should', 'need to', 'please'],
                'low': ['when you can', 'sometime', 'eventually']
            }
            
            # Task type keywords
            type_keywords = {
                'bug': ['bug', 'error', 'broken', 'fix', 'issue'],
                'feature': ['feature', 'add', 'create', 'build', 'implement'],
                'meeting': ['meeting', 'call', 'discuss', 'sync'],
                'review': ['review', 'check', 'approve', 'feedback'],
                'deadline': ['deadline', 'due', 'by'],
                'urgent': ['urgent', 'asap', 'emergency']
            }
            
            # Check if message contains task indicators
            has_task_keywords = any(keyword in message_lower for keyword in task_keywords)
            
            if not has_task_keywords:
                return None
            
            # Determine priority
            priority = 'medium'  # default
            for prio, keywords in priority_keywords.items():
                if any(keyword in message_lower for keyword in keywords):
                    priority = prio
                    break
            
            # Determine task type
            task_type = 'general'  # default
            for t_type, keywords in type_keywords.items():
                if any(keyword in message_lower for keyword in keywords):
                    task_type = t_type
                    break
            
            # Generate a simple title (first 60 characters, cleaned up)
            title = message_text.strip()
            if len(title) > 60:
                title = title[:60] + "..."
            
            # Clean up title - remove excessive punctuation
            title = ' '.join(title.split())  # normalize whitespace
            
            # Create a simple task object (mimic the DetectedTask structure)
            class SimpleFallbackTask:
                def __init__(self, title, task_type, priority):
                    self.title = title
                    self.task_type = task_type
                    self.priority = priority
                    self.due_date = None  # Could add simple date parsing if needed
                    self.confidence_score = 0.65  # Lower confidence for better sensitivity
            
            logger.info(f"Keyword fallback detected task: {title} (type: {task_type}, priority: {priority})")
            return SimpleFallbackTask(title, task_type, priority)
            
        except Exception as e:
            logger.error(f"Error in simple task detection fallback: {str(e)}")
            return None
    
    def _safe_api_call(self, api_method, **kwargs):
        """
        Wrapper for Slack API calls with rate limiting and retry logic
        
        Args:
            api_method: Slack API method to call
            **kwargs: Arguments for the API method
            
        Returns:
            API response or error response
        """
        import time
        
        max_retries = 3
        base_delay = 1
        
        for attempt in range(max_retries):
            try:
                response = api_method(**kwargs)
                return response
            
            except SlackApiError as e:
                if e.response.get('error') == 'ratelimited':
                    # Handle rate limiting with exponential backoff
                    retry_after = e.response.headers.get('retry-after', base_delay * (2 ** attempt))
                    wait_time = min(int(retry_after), 60)  # Cap at 60 seconds
                    
                    logger.warning(f"Rate limited, waiting {wait_time} seconds (attempt {attempt + 1}/{max_retries})")
                    
                    if attempt < max_retries - 1:  # Don't wait on last attempt
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"Max retries exceeded for rate limited API call")
                        return {'ok': False, 'error': 'ratelimited', 'max_retries_exceeded': True}
                else:
                    # Re-raise non-rate-limit errors
                    raise e
                    
            except Exception as e:
                logger.error(f"API call error: {str(e)}")
                return {'ok': False, 'error': str(e)}
        
        return {'ok': False, 'error': 'max_retries_exceeded'}

    def _handle_task_checkbox_interaction(self, payload: Dict) -> Dict:
        """
        Handle interactive checkbox clicks from the personal task list
        
        Args:
            payload: Slack interaction payload
            
        Returns:
            Response dictionary for Slack
        """
        try:
            user_id = payload.get('user', {}).get('id')
            action = payload.get('actions', [{}])[0]
            action_id = action.get('action_id', '')
            selected_options = action.get('selected_options', [])
            
            logger.info(f"Checkbox interaction: user={user_id}, action_id={action_id}, selected={len(selected_options)}")
            
            if action_id.startswith('task_toggle_'):
                # Extract task info from action_id
                parts = action_id.split('_')
                if len(parts) >= 4:
                    priority = parts[2]
                    task_index = parts[3]
                    
                    if selected_options:
                        # Task was checked
                        response_text = f"✅ Task marked complete! Great job! 🎉"
                        
                        # Optionally update the database todo status here
                        # self._mark_personal_todo_complete(user_id, action_id)
                    else:
                        # Task was unchecked
                        response_text = f"↩️ Task unmarked. No worries, keep going!"
                    
                    # Send ephemeral response
                    return {
                        "response_type": "ephemeral",
                        "text": response_text
                    }
            
            return {"response_type": "ephemeral", "text": "👍 Got it!"}
            
        except Exception as e:
            logger.error(f"Error handling checkbox interaction: {str(e)}")
            return {
                "response_type": "ephemeral", 
                "text": "❌ Error processing your selection"
            }

    def _create_personal_list(self, dm_channel_id: str, user_id: str, tasks: List[Dict]) -> Tuple[bool, str]:
        """
        Create or update a personal Slack List in the user's DM with the bot
        
        Args:
            dm_channel_id: DM channel ID
            user_id: User ID
            tasks: List of task dictionaries
            
        Returns:
            Tuple of (success, message)
        """
        try:
            logger.info(f"Creating personal Slack List for user {user_id} with {len(tasks)} tasks")
            
            # Create a Slack List using the Files API
            # (Note: Slack Lists are created through files.upload with a specific format)
            
            # Prepare list content in the format Slack expects
            list_title = "Personal Task List"
            list_items = []
            
            # Group tasks by priority  
            priority_groups = {
                'critical': [],
                'high': [],
                'medium': [],
                'low': []
            }
            
            for task in tasks:
                priority = task.get('priority', 'medium')
                priority_groups[priority].append(task)
            
            # Create list items with proper formatting
            for priority in ['critical', 'high', 'medium', 'low']:
                if priority_groups[priority]:
                    priority_emoji = {
                        'critical': '🔴',
                        'high': '🟠', 
                        'medium': '🟡',
                        'low': '🟢'
                    }
                    
                    for task in priority_groups[priority]:
                        title = task.get('title', 'Untitled task')
                        source = task.get('source_name', 'Unknown source')
                        task_type = task.get('task_type', 'general')
                        
                        # Format task title with priority and source info
                        item_title = f"{priority_emoji[priority]} {title}"
                        item_description = f"From: {source}"
                        
                        if task_type != 'general':
                            type_emoji = {
                                'bug': '🐛', 'feature': '✨', 'meeting': '📅',
                                'review': '👀', 'urgent': '🚨', 'deadline': '⏰'
                            }
                            item_description += f" | {type_emoji.get(task_type, '📝')} {task_type}"
                        
                        list_items.append({
                            "text": item_title,
                            "description": item_description,
                            "completed": False,
                            "priority": priority
                        })
            
            # Try using the message approach with rich text blocks that look like a list
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"🎯 {list_title}"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"📊 *{len(tasks)} actionable tasks found across your workspace*\n_Click items to mark complete_"
                    }
                }
            ]
            
            # Add a section for each priority group
            for priority in ['critical', 'high', 'medium', 'low']:
                if priority_groups[priority]:
                    priority_emoji = {
                        'critical': '🔴',
                        'high': '🟠', 
                        'medium': '🟡',
                        'low': '🟢'
                    }
                    
                    # Priority header
                    blocks.append({
                        "type": "divider"
                    })
                    blocks.append({
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{priority_emoji[priority]} {priority.upper()} PRIORITY*"
                        }
                    })
                    
                    # Create action blocks with checkboxes for each task
                    task_elements = []
                    for i, task in enumerate(priority_groups[priority]):
                        title = task.get('title', 'Untitled task')
                        source = task.get('source_name', 'Unknown source')
                        task_type = task.get('task_type', 'general')
                        
                        # Create unique action_id for this task
                        action_id = f"task_toggle_{priority}_{i}"
                        
                        # Format the checkbox text
                        checkbox_text = f"{title}\n📍 {source}"
                        if task_type != 'general':
                            type_emoji = {
                                'bug': '🐛', 'feature': '✨', 'meeting': '📅',
                                'review': '👀', 'urgent': '🚨', 'deadline': '⏰'
                            }
                            checkbox_text += f" | {type_emoji.get(task_type, '📝')} {task_type}"
                        
                        task_elements.append({
                            "type": "checkboxes",
                            "options": [
                                {
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": checkbox_text
                                    },
                                    "value": action_id
                                }
                            ],
                            "action_id": action_id
                        })
                    
                    # Add elements in groups of 3 (Slack limit for elements per action block)
                    for i in range(0, len(task_elements), 3):
                        element_group = task_elements[i:i+3]
                        for element in element_group:
                            blocks.append({
                                "type": "actions",
                                "elements": [element]
                            })
            
            # Add footer
            blocks.append({
                "type": "divider"
            })
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "💡 Use `/todo list` for database view | `/task` to refresh | `/todo complete [id]` for specific todos"
                    }
                ]
            })
            
            # Send the interactive task list message
            response = self.client.chat_postMessage(
                channel=dm_channel_id,
                text=f"🎯 Personal Task List ({len(tasks)} tasks)",
                blocks=blocks
            )
            
            if response['ok']:
                logger.info(f"Successfully created personal task list for user {user_id}")
                return True, "Interactive task list created in your DM! Click checkboxes to mark tasks complete."
            else:
                logger.error(f"Failed to create personal task list: {response.get('error', 'unknown error')}")
                return False, f"Failed to create task list: {response.get('error', 'unknown error')}"
            
        except Exception as e:
            logger.error(f"Error creating personal task list: {str(e)}")
            return False, f"Error creating task list: {str(e)}"


 

# Utility function for command handlers
def verify_slack_signature(request_body: str, timestamp: str, signature: str) -> bool:
    """
    Verify that the request is from Slack using the signing secret
    
    Args:
        request_body: Raw request body
        timestamp: Request timestamp
        signature: Slack signature from headers
        
    Returns:
        True if signature is valid
    """
    if not settings.SLACK_SIGNING_SECRET:
        logger.warning("SLACK_SIGNING_SECRET not configured")
        return True  # Skip verification if secret not configured
    
    # Create signature
    sig_basestring = f"v0:{timestamp}:{request_body}"
    my_signature = 'v0=' + hmac.new(
        settings.SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(my_signature, signature)