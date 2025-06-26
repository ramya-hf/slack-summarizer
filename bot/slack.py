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
    ConversationContext, BotCommand
)
from .summarizer import ChannelSummarizer, filter_messages_by_timeframe, extract_channel_name_from_command

logger = logging.getLogger(__name__)


class SlackBotHandler:
    """
    Main handler for Slack bot operations
    """
    
    def __init__(self):
        """Initialize the Slack bot with API credentials"""
        if not settings.SLACK_BOT_TOKEN:
            raise ValueError("SLACK_BOT_TOKEN not found in settings")
        
        self.client = WebClient(token=settings.SLACK_BOT_TOKEN)
        self.summarizer = ChannelSummarizer()
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
        Handle the /summary command and its variations
        
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
                # Extract channel name from command
                target_channel = extract_channel_name_from_command(f"/summary {text}")
                if target_channel:
                    self._process_channel_summary(target_channel, channel_id, user_id, bot_command)
                else:
                    # Invalid channel format
                    bot_command.status = 'failed'
                    bot_command.error_message = 'Invalid channel format'
                    bot_command.save()
                    
                    self._send_error_message(
                        channel_id, 
                        "❌ Please specify a valid channel name. Example: `/summary general`"
                    )
            else:
                # Summarize current channel
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
    
    def _get_channel_info(self, channel_name: str) -> Optional[Dict]:
        """Get channel information by name"""
        try:
            # Try public channels first
            response = self.client.conversations_list(types="public_channel")
            for channel in response.get('channels', []):
                if channel.get('name') == channel_name:
                    return channel
            
            # Try private channels if user has access
            try:
                response = self.client.conversations_list(types="private_channel")
                for channel in response.get('channels', []):
                    if channel.get('name') == channel_name:
                        return channel
            except SlackApiError:
                pass  # User might not have access to private channels
            
            return None
        except SlackApiError as e:
            logger.error(f"Error getting channel info for {channel_name}: {e}")
            return None
    
    def _get_channel_info_by_id(self, channel_id: str) -> Optional[Dict]:
        """Get channel information by ID"""
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
            # Calculate timestamp for 24 hours ago
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
    
    def _handle_unknown_command(self, command: str) -> Dict:
        """Handle unknown commands"""
        return {
            "response_type": "ephemeral",
            "text": f"❓ Unknown command `{command}`. Available commands:\n• `/summary` - Summarize current channel\n• `/summary [channel-name]` - Summarize specific channel"
        }
    
    def process_message_event(self, event_data: Dict) -> bool:
        """
        Process incoming message events for follow-up questions
        
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
        
        # Check if user has recent conversation context
        try:
            context = ConversationContext.objects.filter(
                user_id=user_id,
                channel_id=channel_id,
                context_type='summary',
                updated_at__gte=timezone.now() - timedelta(hours=2)  # Context expires after 2 hours
            ).first()
            
            if context and self._is_followup_question(text):
                self._handle_followup_question(context, text, channel_id, user_id)
                return True
                
        except Exception as e:
            logger.error(f"Error processing message event: {str(e)}")
        
        return False
    
    def _is_followup_question(self, text: str) -> bool:
        """
        Determine if a message is likely a follow-up question about a summary
        
        Args:
            text: Message text
            
        Returns:
            True if likely a follow-up question
        """
        question_indicators = [
            '?', 'what', 'who', 'when', 'where', 'why', 'how',
            'explain', 'tell me', 'can you', 'details', 'more info',
            'elaborate', 'clarify', 'summary'
        ]
        
        text_lower = text.lower()
        return any(indicator in text_lower for indicator in question_indicators)
    
    def _handle_followup_question(self, context: ConversationContext, question: str, channel_id: str, user_id: str):
        """
        Handle follow-up questions about summaries
        
        Args:
            context: Conversation context
            question: User's question
            channel_id: Channel ID
            user_id: User ID
        """
        try:
            context_data = context.get_context_data()
            summary_text = context_data.get('summary_text', '')
            summarized_channel = context_data.get('summarized_channel', 'channel')
            
            if not summary_text:
                return
            
            # Generate response using AI
            response = self.summarizer.generate_followup_response(
                question=question,
                summary_context=summary_text,
                channel_name=summarized_channel
            )
            
            # Send response
            formatted_response = f"<@{user_id}> {response}"
            
            self.client.chat_postMessage(
                channel=channel_id,
                text=formatted_response,
                unfurl_links=False,
                unfurl_media=False
            )
            
            # Update context timestamp
            context.save()  # This updates updated_at automatically
            
        except Exception as e:
            logger.error(f"Error handling follow-up question: {str(e)}")


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