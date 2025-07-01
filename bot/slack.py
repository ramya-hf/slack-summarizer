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
    is_thread_command
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
        
        # Log the command
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
                return self._handle_unknown_command(command)
                
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
        Handle the /summary command and its variations including unread and thread
        
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
                # Check if it's a thread command first
                if is_thread_command(f"/summary {text}"):
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
                        # Invalid channel format
                        bot_command.status = 'failed'
                        bot_command.error_message = 'Invalid command format'
                        bot_command.save()
                        
                        self._send_error_message(
                            channel_id, 
                            "❌ Please specify a valid command. Examples:\n• `/summary general` - Regular summary\n• `/summary unread general` - Unread messages summary\n• `/summary thread latest general` - Latest thread summary\n• `/summary thread <message-link>` - Specific thread summary"
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
    
    def _send_acknowledgment_message(self, channel_id: str, user_id: str, custom_message: str = "Your summary is getting generated ⏳"):
        """Send acknowledgment message to user"""
        try:
            self.client.chat_postMessage(
                channel=channel_id,
                text=f"<@{user_id}> {custom_message}",
                unfurl_links=False,
                unfurl_media=False
            )
        except SlackApiError as e:
            logger.error(f"Failed to send acknowledgment: {e}")
    
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
                workspace_id="default",  # You might want to get actual workspace ID
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
    
    def _process_unread_channel_summary(self, channel_name: str, response_channel_id: str, user_id: str, bot_command: BotCommand):
        """
        Process unread summary for a specific channel
        
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
            
            # Get unread messages from the channel
            unread_messages, total_unread_count = self._get_unread_messages(channel_id, user_id)
            
            # Update bot command status
            bot_command.status = 'processing'
            bot_command.save()
            
            # Generate unread summary
            summary = self.summarizer.generate_unread_summary(unread_messages, channel_name, total_unread_count)
            
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
            
            # Create a custom timeframe description for unread messages
            timeframe_desc = f"Unread messages ({total_unread_count} total)"
            
            channel_summary = ChannelSummary.objects.create(
                channel=slack_channel,
                summary_text=summary,
                messages_count=len(unread_messages),
                timeframe=timeframe_desc,
                timeframe_hours=-1,  # Special indicator for unread
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
                        'summary_text': summary,
                        'summary_type': 'unread',
                        'total_unread_count': total_unread_count
                    }),
                    'last_summary': channel_summary
                }
            )
            
            # Send the summary
            self._send_unread_summary_message(response_channel_id, summary, user_id, total_unread_count)
            
            # Update user's read status after sending summary
            self._update_user_read_status(channel_id, user_id)
            
            # Update command status
            execution_time = time.time() - start_time
            bot_command.status = 'completed'
            bot_command.execution_time = execution_time
            bot_command.save()
            
        except Exception as e:
            logger.error(f"Error processing unread channel summary: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            self._send_error_message(
                response_channel_id,
                f"❌ Failed to generate unread summary for `#{channel_name}`. Error: {str(e)}"
            )

    def _process_unread_current_channel_summary(self, channel_id: str, user_id: str, bot_command: BotCommand):
        """
        Process unread summary for the current channel
        
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
            
            # Get unread messages from the current channel
            unread_messages, total_unread_count = self._get_unread_messages(channel_id, user_id)
            
            # Update bot command status
            bot_command.status = 'processing'
            bot_command.save()
            
            # Generate unread summary
            summary = self.summarizer.generate_unread_summary(unread_messages, channel_name, total_unread_count)
            
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
            
            # Create a custom timeframe description for unread messages
            timeframe_desc = f"Unread messages ({total_unread_count} total)"
            
            channel_summary = ChannelSummary.objects.create(
                channel=slack_channel,
                summary_text=summary,
                messages_count=len(unread_messages),
                timeframe=timeframe_desc,
                timeframe_hours=-1,  # Special indicator for unread
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
                        'summary_type': 'unread',
                        'total_unread_count': total_unread_count
                    }),
                    'last_summary': channel_summary
                }
            )
            
            # Send the summary
            self._send_unread_summary_message(channel_id, summary, user_id, total_unread_count)
            
            # Update user's read status after sending summary
            self._update_user_read_status(channel_id, user_id)
            
            # Update command status
            execution_time = time.time() - start_time
            bot_command.status = 'completed'
            bot_command.execution_time = execution_time
            bot_command.save()
            
        except Exception as e:
            logger.error(f"Error processing unread current channel summary: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            self._send_error_message(
                channel_id,
                f"❌ Failed to generate unread summary. Error: {str(e)}"
            )
    
    def _get_unread_messages(self, channel_id: str, user_id: str) -> Tuple[List[Dict], int]:
        """
        Get unread messages for a user in a specific channel
        
        Args:
            channel_id: Slack channel ID
            user_id: User ID to get unread messages for
            
        Returns:
            Tuple of (filtered_unread_messages, total_unread_count)
        """
        try:
            # Get user's last read timestamp for this channel
            last_read_ts = self._get_user_last_read_timestamp(channel_id, user_id)
            
            # Get all messages since last read
            messages = []
            cursor = None
            total_count = 0
            
            while True:
                response = self.client.conversations_history(
                    channel=channel_id,
                    oldest=str(last_read_ts) if last_read_ts else "0",
                    limit=200,
                    cursor=cursor
                )
                
                channel_messages = response.get('messages', [])
                messages.extend(channel_messages)
                total_count += len(channel_messages)
                
                # Check if there are more messages
                cursor = response.get('response_metadata', {}).get('next_cursor')
                if not cursor:
                    break
            
            # Filter out bot messages, system messages, and the user's own messages
            filtered_messages = []
            for message in messages:
                if (message.get('type') == 'message' and 
                    'bot_id' not in message and 
                    message.get('user') != self.bot_user_id and
                    message.get('user') != user_id and  # Exclude user's own messages
                    'subtype' not in message):
                    filtered_messages.append(message)
            
            return filtered_messages, total_count
            
        except SlackApiError as e:
            logger.error(f"Error getting unread messages from channel {channel_id}: {e}")
            return [], 0

    def _get_user_last_read_timestamp(self, channel_id: str, user_id: str) -> Optional[float]:
        """
        Get the last read timestamp for a user in a channel
        
        Args:
            channel_id: Slack channel ID
            user_id: User ID
            
        Returns:
            Last read timestamp or None if not found
        """
        try:
            # First try to get from our database
            from .models import UserReadStatus
            read_status = UserReadStatus.objects.filter(
                user_id=user_id,
                channel_id=channel_id
            ).first()
            
            if read_status:
                return float(read_status.last_read_ts)
            
            # If not in database, try to get from Slack API
            # Note: This requires the channels:read scope and may not always be available
            try:
                response = self.client.conversations_info(
                    channel=channel_id,
                    include_num_members=True
                )
                
                # Try to get user's conversation state
                # This is a fallback and may not always work depending on Slack API permissions
                return None  # Default to treating all messages as unread for first-time users
                
            except SlackApiError:
                logger.warning(f"Could not get read status for user {user_id} in channel {channel_id}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting last read timestamp: {str(e)}")
            return None

    def _update_user_read_status(self, channel_id: str, user_id: str):
        """
        Update user's read status for a channel to mark messages as read
        
        Args:
            channel_id: Slack channel ID
            user_id: User ID
        """
        try:
            from .models import UserReadStatus
            current_timestamp = str(timezone.now().timestamp())
            
            UserReadStatus.objects.update_or_create(
                user_id=user_id,
                channel_id=channel_id,
                defaults={'last_read_ts': current_timestamp}
            )
            
        except Exception as e:
            logger.error(f"Error updating read status: {str(e)}")

    def _send_unread_summary_message(self, channel_id: str, summary: str, user_id: str, total_unread_count: int):
        """Send the unread summary message to the channel"""
        try:
            # Create a formatted message with the unread summary
            if total_unread_count == 0:
                header = f"<@{user_id}> 🎉 You're all caught up! No unread messages found.\n\n"
            else:
                header = f"<@{user_id}> Here's your unread messages summary ({total_unread_count} total unread):\n\n"
            
            formatted_message = f"{header}```\n{summary}\n```\n\n💬 *Ask me any follow-up questions about this summary!*"
            
            self.client.chat_postMessage(
                channel=channel_id,
                text=formatted_message,
                unfurl_links=False,
                unfurl_media=False
            )
        except SlackApiError as e:
            logger.error(f"Failed to send unread summary message: {e}")

    def _process_latest_thread_summary(self, channel_name: str, response_channel_id: str, user_id: str, bot_command: BotCommand):
        """
        Process summary for the latest thread in a specific channel
        
        Args:
            channel_name: Name of the channel to find latest thread in
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
            
            # Find the latest thread in the channel
            latest_thread_ts = self._get_latest_thread_timestamp(channel_id)
            if not latest_thread_ts:
                bot_command.status = 'failed'
                bot_command.error_message = f'No threads found in #{channel_name}'
                bot_command.save()
                
                self._send_error_message(
                    response_channel_id,
                    f"❌ No threads found in `#{channel_name}`."
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
                    'is_private': channel_info.get('is_private', False)
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
                channel_id=response_channel_id,
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
            self._send_thread_summary_message(response_channel_id, summary, user_id, f"#{channel_name}", "latest thread")
            
            # Update command status
            execution_time = time.time() - start_time
            bot_command.status = 'completed'
            bot_command.execution_time = execution_time
            bot_command.save()
            
        except Exception as e:
            logger.error(f"Error processing latest thread summary: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            self._send_error_message(
                response_channel_id,
                f"❌ Failed to generate latest thread summary for `#{channel_name}`. Error: {str(e)}"
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
            
        except Exception as e:
            logger.error(f"Error processing current channel latest thread summary: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            self._send_error_message(
                channel_id,
                f"❌ Failed to generate latest thread summary. Error: {str(e)}"
            )
    
    def _process_specific_thread_summary(self, message_link: str, message_ts: str, response_channel_id: str, user_id: str, bot_command: BotCommand):
        """
        Process summary for a specific thread identified by message link
        
        Args:
            message_link: Slack message link
            message_ts: Parsed message timestamp
            response_channel_id: Channel to send the response to
            user_id: User who requested the summary
            bot_command: Database record for this command
        """
        start_time = time.time()
        
        try:
            # Parse the message link to get channel ID
            channel_id, _ = parse_message_link(message_link)
            if not channel_id:
                bot_command.status = 'failed'
                bot_command.error_message = 'Invalid message link format'
                bot_command.save()
                
                self._send_error_message(
                    response_channel_id,
                    "❌ Invalid message link format. Please provide a valid Slack message link."
                )
                return
            
            # Get channel info
            channel_info = self._get_channel_info_by_id(channel_id)
            channel_name = channel_info.get('name', 'unknown-channel') if channel_info else 'unknown-channel'
            
            # Check if the message has replies (is a thread)
            if not self._message_has_replies(channel_id, message_ts):
                bot_command.status = 'failed'
                bot_command.error_message = 'Message has no thread replies'
                bot_command.save()
                
                self._send_error_message(
                    response_channel_id,
                    "❌ The specified message has no thread replies to summarize."
                )
                return
            
            # Get thread messages
            thread_messages = self._get_thread_messages(channel_id, message_ts)
            
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
                timeframe=f"Specific thread in #{channel_name}",
                timeframe_hours=0,  # Special indicator for thread
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
                        'summary_text': summary,
                        'summary_type': 'thread_specific',
                        'thread_ts': message_ts,
                        'message_link': message_link
                    }),
                    'last_summary': channel_summary
                }
            )
            
            # Send the summary
            self._send_thread_summary_message(response_channel_id, summary, user_id, f"#{channel_name}", "specific thread")
            
            # Update command status
            execution_time = time.time() - start_time
            bot_command.status = 'completed'
            bot_command.execution_time = execution_time
            bot_command.save()
            
        except Exception as e:
            logger.error(f"Error processing specific thread summary: {str(e)}")
            bot_command.status = 'failed'
            bot_command.error_message = str(e)
            bot_command.save()
            
            self._send_error_message(
                response_channel_id,
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
            formatted_message = f"<@{user_id}> Here's your {thread_type} summary for {channel_context}:\n\n```\n{summary}\n```\n\n💬 *Ask me any follow-up questions about this thread summary!*"
            
            self.client.chat_postMessage(
                channel=channel_id,
                text=formatted_message,
                unfurl_links=False,
                unfurl_media=False
            )
        except SlackApiError as e:
            logger.error(f"Failed to send thread summary message: {e}")
    
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
        
        try:
            if text == 'create':
                # Open modal for category creation
                if self.category_manager.create_category_modal(trigger_id, user_id):
                    bot_command.status = 'completed'
                    bot_command.save()
                    
                    # Send immediate response about modal opening
                    self._send_acknowledgment_message(channel_id, user_id, "Category creation form is opening...")
                    
                    return {
                        "response_type": "ephemeral",
                        "text": "📝 Category creation form opened! Fill out the details and click Create."
                    }
                else:
                    bot_command.status = 'failed'
                    bot_command.error_message = 'Failed to open modal'
                    bot_command.save()
                    return {
                        "response_type": "ephemeral",
                        "text": "❌ Failed to open category creation form. Please ensure the bot has proper permissions and try again."
                    }
            
            elif text == 'list':
                # List all categories
                self.category_manager.list_categories(user_id, channel_id)
                bot_command.status = 'completed'
                bot_command.save()
                return {
                    "response_type": "ephemeral",
                    "text": "📋 Loading your categories..."
                }
            
            elif text == 'help' or text == '':
                # Show help
                self.category_manager.show_help(user_id, channel_id)
                bot_command.status = 'completed'
                bot_command.save()
                return {
                    "response_type": "ephemeral",
                    "text": "📚 Category help is on the way..."
                }
            
            else:
                # Unknown subcommand
                bot_command.status = 'failed'
                bot_command.error_message = f'Unknown subcommand: {text}'
                bot_command.save()
                return {
                    "response_type": "ephemeral",
                    "text": f"❓ Unknown subcommand `{text}`. Available commands:\n• `/category create` - Create new category\n• `/category list` - List all categories\n• `/category help` - Show detailed help"
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
    
    def handle_category_creation_success(self, user_id: str, channel_id: str, category_name: str, channels_added: list, description: str = ""):
        """
        Handle successful category creation by sending confirmation message
        
        Args:
            user_id: User who created the category
            channel_id: Channel to send the message to
            category_name: Name of the created category
            channels_added: List of channel names added
            description: Category description
        """
        try:
            success_message = (
                f"<@{user_id}> ✅ *Category '{category_name}' created successfully!*\n\n"
                f"📝 *Description:* {description or 'No description provided'}\n"
                f"📋 *Channels:* {', '.join(channels_added)}\n\n"
                f"💡 Use `/category list` to manage your categories or `/category help` for more options."
            )
            
            self.client.chat_postMessage(
                channel=channel_id,
                text=success_message,
                unfurl_links=False,
                unfurl_media=False
            )
            
        except SlackApiError as e:
            logger.error(f"Failed to send category creation success message: {e}")

    def process_message_event(self, event_data: Dict) -> bool:
        """
        Enhanced message event processing with natural language understanding
        
        Args:
            event_data: Slack event data
            
        Returns:
            True if message was processed, False otherwise
        """
        event = event_data.get('event', {})
        
        # Ignore bot messages and system messages
        if (event.get('bot_id') or 
            event.get('user') == self.bot_user_id or
            event.get('subtype')):
            return False
        
        user_id = event.get('user')
        channel_id = event.get('channel')
        text = event.get('text', '').strip()
        
        if not all([user_id, channel_id, text]):
            return False
        
        # Check if bot is mentioned or if this is a DM
        is_mentioned = f'<@{self.bot_user_id}>' in text
        is_dm = channel_id.startswith('D')  # Direct message channels start with 'D'
        
        # Remove bot mention from text for processing
        if is_mentioned:
            text = text.replace(f'<@{self.bot_user_id}>', '').strip()
        
        # Process if bot is mentioned or in DM
        if is_mentioned or is_dm:
            return self._process_natural_language_message(user_id, channel_id, text, event_data)
        
        # Check for follow-up questions (existing functionality)
        try:
            context = ConversationContext.objects.filter(
                user_id=user_id,
                channel_id=channel_id,
                context_type__in=['summary', 'chat'],
                updated_at__gte=timezone.now() - timedelta(hours=2)  # Context expires after 2 hours
            ).first()
            
            if context and self._is_followup_question(text):
                self._handle_followup_question(context, text, channel_id, user_id)
                return True
                
        except Exception as e:
            logger.error(f"Error processing message event: {str(e)}")
        
        return False
    
    def _process_natural_language_message(self, user_id: str, channel_id: str, text: str, event_data: Dict) -> bool:
        """
        Process natural language messages using intent classification
        
        Args:
            user_id: User ID
            channel_id: Channel ID
            text: Message text (with mentions removed)
            event_data: Full event data
            
        Returns:
            True if message was processed
        """
        start_time = time.time()
        
        try:
            # Classify the intent
            classification = self.intent_classifier.classify_intent(text, user_id)
            intent = classification['intent']
            confidence = classification['confidence']
            parameters = classification.get('parameters', {})
            
            # Log the interaction
            interaction = ChatbotInteraction.objects.create(
                user_id=user_id,
                channel_id=channel_id,
                message_type='natural_language',
                user_message=text,
                intent_classified=intent,
                confidence_score=confidence,
                processing_time=time.time() - start_time
            )
            interaction.set_extracted_parameters(parameters)
            
            # Handle different intents
            if intent == 'summary_request':
                return self._handle_natural_summary_request(user_id, channel_id, parameters, interaction)
            elif intent == 'help_request':
                return self._handle_help_request(user_id, channel_id, interaction)
            elif intent == 'greeting':
                return self._handle_greeting(user_id, channel_id, text, interaction)
            elif intent == 'status_check':
                return self._handle_status_check(user_id, channel_id, interaction)
            else:  # general_chat
                return self._handle_general_chat(user_id, channel_id, text, interaction)
                
        except Exception as e:
            logger.error(f"Error processing natural language message: {str(e)}")
            self._send_error_message(channel_id, f"<@{user_id}> I encountered an error processing your message. Please try again.")
            return False
    
    def _handle_natural_summary_request(self, user_id: str, channel_id: str, parameters: Dict, interaction: ChatbotInteraction) -> bool:
        """Handle natural language summary requests"""
        try:
            target_channel = parameters.get('channel_name')
            timeframe_hours = parameters.get('timeframe_hours', 24)
            timeframe_text = parameters.get('timeframe_text', 'Last 24 hours')
            
            # Send acknowledgment
            if target_channel:
                ack_message = f"<@{user_id}> Getting summary for #{target_channel} ({timeframe_text}) ⏳"
            else:
                ack_message = f"<@{user_id}> Getting summary for this channel ({timeframe_text}) ⏳"
            
            self.client.chat_postMessage(
                channel=channel_id,
                text=ack_message,
                unfurl_links=False,
                unfurl_media=False
            )
            
            # Process the summary
            if target_channel:
                self._process_natural_channel_summary(target_channel, channel_id, user_id, timeframe_hours, interaction)
            else:
                self._process_natural_current_channel_summary(channel_id, user_id, timeframe_hours, interaction)
            
            return True
            
        except Exception as e:
            logger.error(f"Error handling natural summary request: {str(e)}")
            interaction.bot_response = f"Error: {str(e)}"
            interaction.save()
            return False
    
    def _process_natural_channel_summary(self, channel_name: str, response_channel_id: str, user_id: str, timeframe_hours: int, interaction: ChatbotInteraction):
        """Process natural language channel summary request"""
        start_time = time.time()
        
        try:
            # Find the channel
            channel_info = self._get_channel_info(channel_name)
            if not channel_info:
                error_msg = f"❌ Channel `#{channel_name}` not found or I don't have access to it."
                self._send_error_message(response_channel_id, f"<@{user_id}> {error_msg}")
                interaction.bot_response = error_msg
                interaction.save()
                return
            
            channel_id = channel_info['id']
            
            # Get messages from the channel
            messages = self._get_channel_messages(channel_id, hours=timeframe_hours)
            
            # Filter to specified timeframe
            recent_messages = filter_messages_by_timeframe(messages, hours=timeframe_hours)
            
            # Generate summary
            summary = self.summarizer.generate_summary(recent_messages, channel_name, timeframe_hours)
            
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
            
            # Update interaction
            execution_time = time.time() - start_time
            interaction.bot_response = "Summary generated successfully"
            interaction.processing_time = execution_time
            interaction.save()
            
        except Exception as e:
            logger.error(f"Error processing natural channel summary: {str(e)}")
            error_msg = f"❌ Failed to generate summary for `#{channel_name}`. Error: {str(e)}"
            self._send_error_message(response_channel_id, f"<@{user_id}> {error_msg}")
            interaction.bot_response = error_msg
            interaction.save()
    
    def _process_natural_current_channel_summary(self, channel_id: str, user_id: str, timeframe_hours: int, interaction: ChatbotInteraction):
        """Process natural language current channel summary request"""
        start_time = time.time()
        
        try:
            # Get channel info
            channel_info = self._get_channel_info_by_id(channel_id)
            channel_name = channel_info.get('name', 'current-channel') if channel_info else 'current-channel'
            
            # Get messages from the current channel
            messages = self._get_channel_messages(channel_id, hours=timeframe_hours)
            
            # Filter to specified timeframe
            recent_messages = filter_messages_by_timeframe(messages, hours=timeframe_hours)
            
            # Generate summary
            summary = self.summarizer.generate_summary(recent_messages, channel_name, timeframe_hours)
            
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
            
            # Update interaction
            execution_time = time.time() - start_time
            interaction.bot_response = "Summary generated successfully"
            interaction.processing_time = execution_time
            interaction.save()
            
        except Exception as e:
            logger.error(f"Error processing natural current channel summary: {str(e)}")
            error_msg = f"❌ Failed to generate summary. Error: {str(e)}"
            self._send_error_message(channel_id, f"<@{user_id}> {error_msg}")
            interaction.bot_response = error_msg
            interaction.save()
    
    def _handle_help_request(self, user_id: str, channel_id: str, interaction: ChatbotInteraction) -> bool:
        """Handle help requests"""
        try:
            help_response = self.responder.generate_help_response()
            
            self.client.chat_postMessage(
                channel=channel_id,
                text=f"<@{user_id}> {help_response}",
                unfurl_links=False,
                unfurl_media=False
            )
            
            interaction.bot_response = help_response
            interaction.save()
            return True
            
        except Exception as e:
            logger.error(f"Error handling help request: {str(e)}")
            return False
    
    def _handle_greeting(self, user_id: str, channel_id: str, message: str, interaction: ChatbotInteraction) -> bool:
        """Handle greetings"""
        try:
            greeting_response = self.responder.generate_greeting_response(message)
            
            self.client.chat_postMessage(
                channel=channel_id,
                text=f"<@{user_id}> {greeting_response}",
                unfurl_links=False,
                unfurl_media=False
            )
            
            interaction.bot_response = greeting_response
            interaction.save()
            return True
            
        except Exception as e:
            logger.error(f"Error handling greeting: {str(e)}")
            return False
    
    def _handle_status_check(self, user_id: str, channel_id: str, interaction: ChatbotInteraction) -> bool:
        """Handle status check requests"""
        try:
            status_response = self.responder.generate_status_response()
            
            self.client.chat_postMessage(
                channel=channel_id,
                text=f"<@{user_id}> {status_response}",
                unfurl_links=False,
                unfurl_media=False
            )
            
            interaction.bot_response = status_response
            interaction.save()
            return True
            
        except Exception as e:
            logger.error(f"Error handling status check: {str(e)}")
            return False
    
    def _handle_general_chat(self, user_id: str, channel_id: str, message: str, interaction: ChatbotInteraction) -> bool:
        """Handle general chat messages"""
        try:
            chat_response = self.responder.generate_general_chat_response(message)
            
            self.client.chat_postMessage(
                channel=channel_id,
                text=f"<@{user_id}> {chat_response}",
                unfurl_links=False,
                unfurl_media=False
            )
            
            # Store conversation context for potential follow-ups
            ConversationContext.objects.update_or_create(
                user_id=user_id,
                channel_id=channel_id,
                defaults={
                    'context_type': 'chat',
                    'context_data': json.dumps({
                        'last_message': message,
                        'last_response': chat_response,
                        'interaction_type': 'general_chat'
                    }),
                    'last_interaction_type': 'general_chat'
                }
            )
            
            interaction.bot_response = chat_response
            interaction.save()
            return True
            
        except Exception as e:
            logger.error(f"Error handling general chat: {str(e)}")
            return False
    
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
    
    def _is_followup_question(self, text: str) -> bool:
        """
        Check if a message is likely a follow-up question
        
        Args:
            text: Message text
            
        Returns:
            True if it seems like a follow-up question
        """
        followup_indicators = [
            'what', 'who', 'when', 'where', 'why', 'how',
            'can you', 'could you', 'tell me', 'explain',
            'more details', 'elaborate', 'expand',
            '?'  # Questions usually end with question marks
        ]
        
        text_lower = text.lower()
        return any(indicator in text_lower for indicator in followup_indicators)
    
    def _handle_followup_question(self, context: ConversationContext, question: str, channel_id: str, user_id: str):
        """
        Handle follow-up questions about previous summaries or conversations
        
        Args:
            context: Conversation context
            question: Follow-up question
            channel_id: Channel ID
            user_id: User ID
        """
        try:
            context_data = context.get_context_data()
            
            if context.context_type == 'summary':
                summary_text = context_data.get('summary_text', '')
                channel_name = context_data.get('summarized_channel', 'the channel')
                
                # Generate a response based on the question and summary context
                response = self.responder.generate_followup_response(question, summary_text, channel_name)
                
                # Log the interaction
                ChatbotInteraction.objects.create(
                    user_id=user_id,
                    channel_id=channel_id,
                    message_type='followup',
                    user_message=question,
                    bot_response=response,
                    intent_classified='followup_question'
                )
                
            elif context.context_type == 'chat':
                last_message = context_data.get('last_message', '')
                last_response = context_data.get('last_response', '')
                
                # Generate a contextual response for general chat follow-ups
                response = self.responder.generate_chat_followup_response(question, last_message, last_response)
                
                # Log the interaction
                ChatbotInteraction.objects.create(
                    user_id=user_id,
                    channel_id=channel_id,
                    message_type='followup',
                    user_message=question,
                    bot_response=response,
                    intent_classified='chat_followup'
                )
            else:
                response = "I'm not sure what you're asking about. Could you provide more context?"
            
            # Send the response
            self.client.chat_postMessage(
                channel=channel_id,
                text=f"<@{user_id}> {response}",
                unfurl_links=False,
                unfurl_media=False
            )
            
            # Update context
            context.updated_at = timezone.now()
            context.save()
            
        except Exception as e:
            logger.error(f"Error handling follow-up question: {str(e)}")
            self._send_error_message(
                channel_id,
                f"<@{user_id}> I encountered an error processing your follow-up question. Please try again."
            )

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