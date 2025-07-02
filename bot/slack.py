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
    ConversationContext, BotCommand, ChatbotInteraction
)
from .summarizer import (
    ChannelSummarizer, filter_messages_by_timeframe, extract_channel_name_from_command, 
    extract_unread_command_details, extract_thread_command_details, parse_message_link, 
    is_thread_command, extract_category_command_details, is_category_command
)
from .intent_classifier import IntentClassifier, ChatbotResponder
from .category_manager import CategoryManager

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
        Process message events (for future use with follow-up questions)
        
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
            
            # For now, just log the event and return
            logger.info(f"Received message event from user {event.get('user')} in channel {event.get('channel')}")
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