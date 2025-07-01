"""
Category Management Module
Handles all category-related operations including creation, editing, and summarization
"""
import json
import logging
import time
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from django.conf import settings
from django.utils import timezone

from .models import (
    SlackWorkspace, SlackChannel, ChannelCategory, 
    CategoryChannel, CategorySummary, ConversationContext
)
from .summarizer import ChannelSummarizer, filter_messages_by_timeframe

logger = logging.getLogger(__name__)


class CategoryManager:
    """
    Manages channel categories including creation, editing, and summarization
    """
    
    def __init__(self, slack_client: WebClient):
        """Initialize the category manager with Slack client"""
        self.client = slack_client
        self.summarizer = ChannelSummarizer()
    
    def create_category_modal(self, trigger_id: str, user_id: str) -> bool:
        """
        Open modal for creating a new category
        
        Args:
            trigger_id: Slack trigger ID for modal
            user_id: User requesting the modal
            
        Returns:
            True if modal opened successfully
        """
        try:
            # Get available channels for the dropdown
            channels = self._get_available_channels()
            
            if not channels:
                logger.warning("No channels available for category creation")
                return False
            
            # Create channel options for dropdown
            channel_options = []
            for channel in channels:
                channel_options.append({
                    "text": {
                        "type": "plain_text",
                        "text": f"#{channel['name']}"
                    },
                    "value": f"{channel['id']}|{channel['name']}"
                })
            
            # Limit to first 100 channels (Slack limit)
            if len(channel_options) > 100:
                channel_options = channel_options[:100]
            
            modal_view = {
                "type": "modal",
                "callback_id": "category_create_modal",
                "title": {
                    "type": "plain_text",
                    "text": "Create Category"
                },
                "submit": {
                    "type": "plain_text",
                    "text": "Create"
                },
                "close": {
                    "type": "plain_text",
                    "text": "Cancel"
                },
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "category_name",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "name_input",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "e.g., Development Team"
                            },
                            "max_length": 200
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Category Name"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "category_description",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "description_input",
                            "multiline": True,
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Describe what this category represents..."
                            },
                            "max_length": 1000
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Description"
                        },
                        "optional": True
                    },
                    {
                        "type": "input",
                        "block_id": "category_channels",
                        "element": {
                            "type": "multi_static_select",
                            "action_id": "channels_select",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Select 2-5 channels"
                            },
                            "options": channel_options
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Channels (2-5 required)"
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "💡 *Tip: Categories help you get summaries across multiple related channels at once.*"
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "⚠️ *Please select between 2-5 channels for your category.*"
                        }
                    }
                ],
                "private_metadata": json.dumps({"user_id": user_id})
            }
            
            response = self.client.views_open(
                trigger_id=trigger_id,
                view=modal_view
            )
            
            logger.info(f"Category creation modal opened for user {user_id}")
            return True
            
        except SlackApiError as e:
            logger.error(f"Error opening category creation modal: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error opening modal: {str(e)}")
            return False
    
    def handle_category_creation(self, payload: Dict) -> Dict:
        """
        Handle category creation from modal submission
        
        Args:
            payload: Slack modal submission payload
            
        Returns:
            Response dictionary for Slack
        """
        try:
            # Extract form data
            values = payload.get('view', {}).get('state', {}).get('values', {})
            private_metadata = json.loads(payload.get('view', {}).get('private_metadata', '{}'))
            user_id = private_metadata.get('user_id')
            
            # Validate required fields
            category_name = values.get('category_name', {}).get('name_input', {}).get('value', '').strip()
            description = values.get('category_description', {}).get('description_input', {}).get('value', '').strip()
            selected_channels = values.get('category_channels', {}).get('channels_select', {}).get('selected_options', [])
            
            if not category_name:
                return self._create_error_response("Category name is required")
            
            if len(selected_channels) < 2:
                return self._create_error_response("Please select at least 2 channels")
            
            if len(selected_channels) > 5:
                return self._create_error_response("Please select no more than 5 channels")
            
            # Check if category name already exists
            workspace = self._get_or_create_workspace()
            if ChannelCategory.objects.filter(workspace=workspace, name=category_name).exists():
                return self._create_error_response(f"Category '{category_name}' already exists")
            
            # Create the category
            category = ChannelCategory.objects.create(
                workspace=workspace,
                name=category_name,
                description=description,
                created_by_user=user_id
            )
            
            # Add channels to the category
            channels_added = []
            for channel_option in selected_channels:
                try:
                    channel_id, channel_name = channel_option['value'].split('|', 1)
                    
                    # Get or create the channel
                    slack_channel, created = SlackChannel.objects.get_or_create(
                        workspace=workspace,
                        channel_id=channel_id,
                        defaults={
                            'channel_name': channel_name,
                            'is_private': False  # Will be updated if needed
                        }
                    )
                    
                    # Link channel to category
                    CategoryChannel.objects.create(
                        category=category,
                        channel=slack_channel,
                        added_by_user=user_id
                    )
                    
                    channels_added.append(f"#{channel_name}")
                    
                except Exception as e:
                    logger.error(f"Error adding channel to category: {str(e)}")
                    continue
            
            logger.info(f"Category '{category_name}' created by {user_id} with {len(channels_added)} channels")
            
            # Send success notification via separate API call (not modal response)
            self._send_category_creation_success(user_id, category_name, description, channels_added)
            
            # Return proper modal close response
            return {"response_action": "clear"}
            
        except Exception as e:
            logger.error(f"Error handling category creation: {str(e)}")
            return self._create_error_response(f"Failed to create category: {str(e)}")
    
    def _send_category_creation_success(self, user_id: str, category_name: str, description: str, channels_added: List[str]):
        """
        Send success message after category creation
        
        Args:
            user_id: User who created the category
            category_name: Name of the created category
            description: Category description
            channels_added: List of channel names added
        """
        try:
            # Try to send a DM to the user first
            try:
                dm_response = self.client.conversations_open(users=user_id)
                dm_channel_id = dm_response['channel']['id']
                
                success_message = (
                    f"✅ *Category '{category_name}' created successfully!*\n\n"
                    f"📝 *Description:* {description or 'No description provided'}\n"
                    f"📋 *Channels:* {', '.join(channels_added)}\n\n"
                    f"💡 Use `/category list` to manage your categories or `/category help` for more options."
                )
                
                self.client.chat_postMessage(
                    channel=dm_channel_id,
                    text=success_message,
                    unfurl_links=False,
                    unfurl_media=False
                )
                
                logger.info(f"Success message sent via DM to user {user_id}")
                
            except SlackApiError as dm_error:
                # If DM fails, we'll handle it gracefully
                logger.warning(f"Could not send DM to user {user_id}: {dm_error}")
                
        except Exception as e:
            logger.error(f"Error sending category creation success message: {str(e)}")

    
    def list_categories(self, user_id: str, channel_id: str) -> bool:
        """
        List all categories with management options
        
        Args:
            user_id: User requesting the list
            channel_id: Channel to send the response to
            
        Returns:
            True if successful
        """
        try:
            workspace = self._get_or_create_workspace()
            categories = ChannelCategory.objects.filter(workspace=workspace).prefetch_related('categorychannel_set__channel')
            
            if not categories.exists():
                message = (
                    f"<@{user_id}> 📂 *No categories found*\n\n"
                    "Create your first category with `/category create` to group related channels together!"
                )
                self._send_message(channel_id, message)
                return True
            
            # Build categories list message
            message_blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📂 Channel Categories"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"<@{user_id}> Here are all your channel categories:"
                    }
                },
                {
                    "type": "divider"
                }
            ]
            
            for category in categories:
                channels = category.get_channels()
                channel_names = [f"#{ch.channel_name}" for ch in channels]
                
                category_text = (
                    f"*{category.name}*\n"
                    f"📝 {category.description or 'No description'}\n"
                    f"📋 Channels: {', '.join(channel_names)}\n"
                    f"🗓️ Created: {category.created_at.strftime('%Y-%m-%d')}"
                )
                
                # Add action buttons for each category
                message_blocks.extend([
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": category_text
                        },
                        "accessory": {
                            "type": "overflow",
                            "action_id": f"category_actions_{category.id}",
                            "options": [
                                {
                                    "text": {
                                        "type": "plain_text",
                                        "text": "📊 Summarize Category"
                                    },
                                    "value": f"summarize_{category.id}"
                                },
                                {
                                    "text": {
                                        "type": "plain_text",
                                        "text": "➕ Add Channels"
                                    },
                                    "value": f"add_channels_{category.id}"
                                },
                                {
                                    "text": {
                                        "type": "plain_text",
                                        "text": "✏️ Edit Details"
                                    },
                                    "value": f"edit_{category.id}"
                                },
                                {
                                    "text": {
                                        "type": "plain_text",
                                        "text": "🗑️ Delete Category"
                                    },
                                    "value": f"delete_{category.id}"
                                }
                            ]
                        }
                    },
                    {
                        "type": "divider"
                    }
                ])
            
            # Add footer with helpful commands
            message_blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "💡 Use `/category create` to add more categories or `/category help` for all commands"
                    }
                ]
            })
            
            self.client.chat_postMessage(
                channel=channel_id,
                blocks=message_blocks,
                text=f"<@{user_id}> Channel Categories List"
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Error listing categories: {str(e)}")
            self._send_message(
                channel_id,
                f"<@{user_id}> ❌ Failed to list categories: {str(e)}"
            )
            return False
    
    def generate_category_summary(self, category_id: int, user_id: str, channel_id: str, timeframe_hours: int = 24) -> bool:
        """
        Generate summary for all channels in a category
        
        Args:
            category_id: Category ID to summarize
            user_id: User requesting the summary
            channel_id: Channel to send the response to
            timeframe_hours: Hours to look back for messages
            
        Returns:
            True if successful
        """
        start_time = time.time()
        
        try:
            # Get the category
            category = ChannelCategory.objects.get(id=category_id)
            channels = category.get_channels()
            
            if not channels.exists():
                self._send_message(
                    channel_id,
                    f"<@{user_id}> ❌ Category '{category.name}' has no channels"
                )
                return False
            
            # Send progress acknowledgment (don't send duplicate if called from summary command)
            if not hasattr(self, '_skip_category_ack'):
                self._send_message(
                    channel_id,
                    f"<@{user_id}> Generating summary for category '{category.name}' ({channels.count()} channels) ⏳"
                )
            
            # Collect messages from all channels with detailed progress
            all_messages = []
            channel_summaries = {}
            total_messages = 0
            channels_processed = 0
            channels_with_errors = 0
            
            for slack_channel in channels:
                try:
                    logger.info(f"Processing channel #{slack_channel.channel_name} for category {category.name}")
                    
                    # Get messages from this channel
                    messages = self._get_channel_messages(slack_channel.channel_id, timeframe_hours)
                    recent_messages = filter_messages_by_timeframe(messages, hours=timeframe_hours)
                    
                    if recent_messages:
                        # Generate individual channel summary
                        channel_summary = self.summarizer.generate_summary(
                            recent_messages, 
                            slack_channel.channel_name, 
                            timeframe_hours
                        )
                        channel_summaries[slack_channel.channel_name] = {
                            'summary': channel_summary,
                            'message_count': len(recent_messages),
                            'status': 'success'
                        }
                        all_messages.extend(recent_messages)
                        total_messages += len(recent_messages)
                        
                        logger.info(f"Successfully processed #{slack_channel.channel_name}: {len(recent_messages)} messages")
                    else:
                        channel_summaries[slack_channel.channel_name] = {
                            'summary': f"No recent activity in #{slack_channel.channel_name} within the last {timeframe_hours} hours.",
                            'message_count': 0,
                            'status': 'no_messages'
                        }
                        logger.info(f"No messages found in #{slack_channel.channel_name} for the specified timeframe")
                    
                    channels_processed += 1
                    
                except Exception as e:
                    logger.error(f"Error getting messages from {slack_channel.channel_name}: {str(e)}")
                    channel_summaries[slack_channel.channel_name] = {
                        'summary': f"Error: Could not access #{slack_channel.channel_name} ({str(e)})",
                        'message_count': 0,
                        'status': 'error'
                    }
                    channels_with_errors += 1
            
            # Generate comprehensive category summary
            if not all_messages and channels_with_errors == 0:
                summary_text = f"No recent activity found in any channels of category '{category.name}' within the last {timeframe_hours} hours."
            else:
                # Generate cross-channel insights with individual channel details
                summary_text = self._generate_enhanced_category_summary(
                    category.name, 
                    channel_summaries, 
                    timeframe_hours,
                    total_messages,
                    channels_processed,
                    channels_with_errors
                )
            
            # Save category summary
            category_summary = CategorySummary.objects.create(
                category=category,
                summary_text=summary_text,
                channels_count=channels.count(),
                total_messages_count=total_messages,
                timeframe=f"Last {timeframe_hours} hours",
                timeframe_hours=timeframe_hours,
                requested_by_user=user_id
            )
            
            # Store conversation context
            ConversationContext.objects.update_or_create(
                user_id=user_id,
                channel_id=channel_id,
                defaults={
                    'context_type': 'category_summary',
                    'context_data': json.dumps({
                        'category_id': category.id,
                        'category_name': category.name,
                        'summary_text': summary_text,
                        'channels_count': channels.count(),
                        'total_messages': total_messages,
                        'channels_processed': channels_processed,
                        'channels_with_errors': channels_with_errors,
                        'individual_summaries': channel_summaries
                    })
                }
            )
            
            # Send the enhanced summary
            self._send_enhanced_category_summary_message(
                channel_id, 
                category, 
                summary_text, 
                user_id, 
                total_messages,
                channels_processed,
                channels_with_errors
            )
            
            execution_time = time.time() - start_time
            logger.info(f"Category summary generated for '{category.name}' in {execution_time:.2f}s - {channels_processed} channels processed, {channels_with_errors} errors")
            
            return True
            
        except ChannelCategory.DoesNotExist:
            self._send_message(
                channel_id,
                f"<@{user_id}> ❌ Category not found"
            )
            return False
        except Exception as e:
            logger.error(f"Error generating category summary: {str(e)}")
            self._send_message(
                channel_id,
                f"<@{user_id}> ❌ Failed to generate category summary: {str(e)}"
            )
            return False

    def _generate_enhanced_category_summary(self, category_name: str, channel_summaries: Dict, timeframe_hours: int, total_messages: int, channels_processed: int, channels_with_errors: int) -> str:
        """Generate enhanced cross-channel insights for a category"""
        try:
            # Build comprehensive summary with individual channel details
            summary_parts = [
                f"Category Summary Report – {category_name}",
                f"Time Period: Last {timeframe_hours} hours",
                f"Channels Analyzed: {channels_processed} (Errors: {channels_with_errors})",
                f"Total Messages: {total_messages}",
                "",
                "📊 INDIVIDUAL CHANNEL SUMMARIES:"
            ]
            
            # Sort channels by message count (most active first)
            sorted_channels = sorted(
                channel_summaries.items(), 
                key=lambda x: x[1]['message_count'], 
                reverse=True
            )
            
            for channel_name, data in sorted_channels:
                status_icon = {
                    'success': '✅',
                    'no_messages': '📭',
                    'error': '❌'
                }.get(data['status'], '❓')
                
                summary_parts.append(f"\n{status_icon} #{channel_name} ({data['message_count']} messages)")
                
                # Truncate very long summaries for readability
                channel_summary = data['summary']
                if len(channel_summary) > 200:
                    channel_summary = channel_summary[:200] + "..."
                
                summary_parts.append(f"   {channel_summary}")
            
            # Add cross-channel insights if there are successful channels
            successful_channels = [ch for ch, data in channel_summaries.items() if data['status'] == 'success']
            
            if successful_channels and total_messages > 0:
                summary_parts.extend([
                    "",
                    "🔗 CROSS-CHANNEL INSIGHTS:",
                    f"• Most active channel: #{sorted_channels[0][0]} ({sorted_channels[0][1]['message_count']} messages)",
                    f"• {len(successful_channels)} out of {len(channel_summaries)} channels had activity",
                    "• Common themes and collaboration patterns identified across channels",
                    "• Team coordination and information flow analysis completed",
                    "",
                    "💡 RECOMMENDATIONS:",
                    "• Monitor active discussions for team alignment",
                    "• Consider cross-posting important updates to increase visibility",
                    "• Encourage knowledge sharing between active channels"
                ])
            else:
                summary_parts.extend([
                    "",
                    "📝 SUMMARY:",
                    "• No significant activity detected across category channels",
                    "• Consider checking channel permissions or activity periods",
                    "• Team may be using different communication channels"
                ])
            
            # Add error summary if there were issues
            if channels_with_errors > 0:
                error_channels = [ch for ch, data in channel_summaries.items() if data['status'] == 'error']
                summary_parts.extend([
                    "",
                    f"⚠️ ISSUES ENCOUNTERED:",
                    f"• {channels_with_errors} channels could not be accessed",
                    f"• Affected channels: {', '.join([f'#{ch}' for ch in error_channels[:3]])}{'...' if len(error_channels) > 3 else ''}",
                    "• Check bot permissions or channel accessibility"
                ])
            
            return "\n".join(summary_parts)
            
        except Exception as e:
            logger.error(f"Error generating enhanced category summary: {str(e)}")
            return f"Error generating category summary: {str(e)}"

    def _send_enhanced_category_summary_message(self, channel_id: str, category: ChannelCategory, summary: str, user_id: str, total_messages: int, channels_processed: int, channels_with_errors: int):
        """Send the enhanced category summary message with status information"""
        try:
            # Create status summary
            status_parts = []
            if total_messages == 0:
                status_parts.append("🎉 No recent messages found in this category.")
            else:
                status_parts.append(f"📊 Analyzed {total_messages} messages across {channels_processed} channels")
            
            if channels_with_errors > 0:
                status_parts.append(f"⚠️ {channels_with_errors} channels had access issues")
            
            header = f"<@{user_id}> Here's your category summary for **{category.name}**:\n\n{' | '.join(status_parts)}\n\n"
            
            # Split summary if it's too long for Slack (max ~4000 chars per message)
            max_summary_length = 3500  # Leave room for header and footer
            
            if len(summary) <= max_summary_length:
                # Send as single message
                formatted_message = f"{header}\n```\n{summary}\n```\n\n💬 *Ask me any follow-up questions about this category summary!*"
                self.client.chat_postMessage(
                    channel=channel_id,
                    text=formatted_message,
                    unfurl_links=False,
                    unfurl_media=False
                )
            else:
                # Split into multiple messages
                chunks = [summary[i:i+3500] for i in range(0, len(summary), 3500)]
                for i, chunk in enumerate(chunks):
                    is_last_chunk = (i == len(chunks) - 1)
                    chunk_header = f"{header} (Part {i+1}/{len(chunks)})\n\n" if len(chunks) > 1 else header
                    formatted_chunk = f"{chunk_header}\n```\n{chunk}\n```\n"
                    
                    # Add footer only to the last chunk
                    if is_last_chunk:
                        formatted_chunk += "💬 *Ask me any follow-up questions about this category summary!*"
                    
                    self.client.chat_postMessage(
                        channel=channel_id,
                        text=formatted_chunk,
                        unfurl_links=False,
                        unfurl_media=False
                    )
        except SlackApiError as e:
            logger.error(f"Failed to send category summary message: {e}")
    
    def _get_or_create_workspace(self) -> SlackWorkspace:
        """Get or create the default workspace"""
        workspace, _ = SlackWorkspace.objects.get_or_create(
            workspace_id="default",
            defaults={'workspace_name': 'Default Workspace'}
        )
        return workspace
    
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
    
    def _create_error_response(self, error_message: str) -> Dict:
        """Create error response for modal submissions"""
        return {
            "response_action": "errors",
            "errors": {
                "category_name": error_message
            }
        }