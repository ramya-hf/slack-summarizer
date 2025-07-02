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
    
    def _get_available_channels(self) -> List[Dict]:
        """Get list of available channels for category creation"""
        try:
            response = self.client.conversations_list(
                types="public_channel,private_channel",
                limit=1000,
                exclude_archived=True
            )
            
            channels = response.get('channels', [])
            
            # Filter out channels that might not be suitable
            available_channels = []
            for channel in channels:
                if (not channel.get('is_archived', False) and 
                    not channel.get('is_general', False) and  # Usually exclude #general
                    channel.get('is_member', True)):  # Bot must be a member
                    available_channels.append({
                        'id': channel['id'],
                        'name': channel['name'],
                        'is_private': channel.get('is_private', False)
                    })
            
            # Sort by name for better UX
            available_channels.sort(key=lambda x: x['name'])
            
            return available_channels
            
        except SlackApiError as e:
            logger.error(f"Error getting available channels: {e}")
            return []

    def open_edit_category_modal(self, trigger_id: str, user_id: str, category_id: int) -> bool:
        """
        Open modal for editing category details
        
        Args:
            trigger_id: Slack trigger ID for modal
            user_id: User requesting the modal
            category_id: ID of category to edit
            
        Returns:
            True if modal opened successfully
        """
        try:
            # Get the category
            category = ChannelCategory.objects.get(id=category_id)
            
            # Get current channels in category
            current_channels = category.get_channels()
            channel_names = [f"#{ch.channel_name}" for ch in current_channels]
            
            modal_view = {
                "type": "modal",
                "callback_id": "edit_category_modal",
                "title": {
                    "type": "plain_text",
                    "text": "Edit Category"
                },
                "submit": {
                    "type": "plain_text",
                    "text": "Save Changes"
                },
                "close": {
                    "type": "plain_text",
                    "text": "Cancel"
                },
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Editing category: {category.name}*\n\nCurrent channels: {', '.join(channel_names)}"
                        }
                    },
                    {
                        "type": "divider"
                    },
                    {
                        "type": "input",
                        "block_id": "category_name",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "name_input",
                            "initial_value": category.name,
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
                            "initial_value": category.description or "",
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
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "💡 *Use 'Manage Channels' from the main menu to add or remove channels from this category.*"
                        }
                    }
                ],
                "private_metadata": json.dumps({
                    "user_id": user_id,
                    "category_id": category_id,
                    "original_name": category.name
                })
            }
            
            response = self.client.views_open(
                trigger_id=trigger_id,
                view=modal_view
            )
            
            logger.info(f"Edit category modal opened for category {category_id} by user {user_id}")
            return True
            
        except ChannelCategory.DoesNotExist:
            logger.error(f"Category {category_id} not found")
            self._send_message_to_user(user_id, "❌ Category not found.")
            return False
        except SlackApiError as e:
            logger.error(f"Error opening edit category modal: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error opening edit category modal: {str(e)}")
            return False

    def handle_edit_category_submission(self, payload: Dict) -> Dict:
        """
        Handle edit category modal submission
        
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
            category_id = private_metadata.get('category_id')
            original_name = private_metadata.get('original_name')
            
            # Validate required data
            if not all([user_id, category_id]):
                return self._create_error_response("Missing required information")
            
            # Get form values
            new_name = values.get('category_name', {}).get('name_input', {}).get('value', '').strip()
            new_description = values.get('category_description', {}).get('description_input', {}).get('value', '').strip()
            
            if not new_name:
                return self._create_error_response("Category name is required")
            
            # Get the category
            category = ChannelCategory.objects.get(id=category_id)
            workspace = category.workspace
            
            # Check if new name conflicts with existing category (if name changed)
            if new_name != original_name:
                if ChannelCategory.objects.filter(workspace=workspace, name=new_name).exclude(id=category_id).exists():
                    return self._create_error_response(f"Category name '{new_name}' already exists")
            
            # Update the category
            old_name = category.name
            old_description = category.description
            
            category.name = new_name
            category.description = new_description
            category.updated_at = timezone.now()
            category.save()
            
            # Build success message
            changes = []
            if old_name != new_name:
                changes.append(f"Name: '{old_name}' → '{new_name}'")
            
            if old_description != new_description:
                if old_description:
                    changes.append(f"Description updated")
                else:
                    changes.append(f"Description added")
            
            if changes:
                success_message = f"✅ Category updated successfully!\n\n🔄 Changes made:\n• " + "\n• ".join(changes)
            else:
                success_message = f"✅ Category '{new_name}' saved (no changes detected)."
            
            success_message += f"\n\n💡 Use `/category list` to see all your categories."
            
            logger.info(f"Category {category_id} updated by user {user_id}. Changes: {len(changes)}")
            
            # Send success notification
            self._send_message_to_user(user_id, success_message)
            
            # Return proper modal close response
            return {"response_action": "clear"}
            
        except ChannelCategory.DoesNotExist:
            logger.error(f"Category {category_id} not found during edit")
            return self._create_error_response("Category not found")
        except Exception as e:
            logger.error(f"Error handling edit category submission: {str(e)}")
            return self._create_error_response(f"Failed to update category: {str(e)}")
    
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
                    'subtype' not in message):
                    filtered_messages.append(message)
            
            return filtered_messages
            
        except SlackApiError as e:
            logger.error(f"Error getting messages from channel {channel_id}: {e}")
            return []

    def show_help(self, user_id: str, channel_id: str) -> bool:
        """
        Show category management help
        
        Args:
            user_id: User requesting help
            channel_id: Channel to send the response to
            
        Returns:
            True if successful
        """
        try:
            help_message = f"""<@{user_id}> 📚 *Category Management Help*

**Available Commands:**

🆕 `/category create`
   Create a new category with 2-5 channels

📋 `/category list`
   View all categories with management options

❓ `/category help`
   Show this help message

**What are Categories?**
Categories let you group related channels together for:
• 📊 Cross-channel summaries and insights
• 🔍 Better organization of your workspace
• 📈 Team collaboration analysis

**Category Features:**
• ✅ Group 2-5 related channels
• 📝 Add descriptions to explain the category
• 📊 Generate AI summaries across all channels
• ➕ Add or remove channels anytime
• ✏️ Edit category details
• 🗑️ Delete categories when no longer needed

**Example Categories:**
• *Development Team* - #dev-general, #code-review, #deployment
• *Marketing* - #marketing-general, #campaigns, #social-media
• *Support* - #customer-support, #bug-reports, #feature-requests

💡 *Tip: Use categories to get insights into how different teams collaborate and what topics are trending across related channels!*"""

            self._send_message(channel_id, help_message)
            return True
            
        except Exception as e:
            logger.error(f"Error showing category help: {str(e)}")
            return False

    def handle_category_action(self, payload: Dict) -> bool:
        """
        Handle category action from overflow menu
        
        Args:
            payload: Slack action payload
            
        Returns:
            True if successful
        """
        try:
            action = payload.get('actions', [{}])[0]
            action_value = action.get('selected_option', {}).get('value', '')
            user_id = payload.get('user', {}).get('id')
            channel_id = payload.get('channel', {}).get('id')
            trigger_id = payload.get('trigger_id')
            
            if not action_value or not user_id or not channel_id:
                logger.warning(f"Missing required data in category action: {action_value}, {user_id}, {channel_id}")
                return False
            
            # Parse action - handle both old and new format
            action_parts = action_value.split('_')
            if len(action_parts) < 2:
                logger.error(f"Invalid action format: {action_value}")
                return False
            
            # Handle different action formats
            if action_parts[0] == 'summarize':
                category_id = int(action_parts[1])
                return self.generate_category_summary(category_id, user_id, channel_id)
            elif action_parts[0] == 'add' and len(action_parts) >= 3 and action_parts[1] == 'channels':
                category_id = int(action_parts[2])
                return self.open_manage_channels_modal(trigger_id, user_id, category_id)
            elif action_parts[0] == 'edit':
                category_id = int(action_parts[1])
                return self.open_edit_category_modal(trigger_id, user_id, category_id)
            elif action_parts[0] == 'delete':
                category_id = int(action_parts[1])
                return self._delete_category(category_id, user_id, channel_id)
            else:
                logger.warning(f"Unknown action type: {action_value}")
                return False
            
        except ValueError as e:
            logger.error(f"Invalid category ID in action: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"Error handling category action: {str(e)}")
            return False

    def open_manage_channels_modal(self, trigger_id: str, user_id: str, category_id: int) -> bool:
        """
        Open modal for managing channels in a category (add/remove)
        
        Args:
            trigger_id: Slack trigger ID for modal
            user_id: User requesting the modal
            category_id: ID of category to manage
            
        Returns:
            True if modal opened successfully
        """
        try:
            # Get the category
            category = ChannelCategory.objects.get(id=category_id)
            
            # Get currently assigned channels
            current_category_channels = CategoryChannel.objects.filter(category=category).select_related('channel')
            current_channel_ids = set(cc.channel.channel_id for cc in current_category_channels)
            
            # Get all available channels
            all_channels = self._get_available_channels()
            
            # Separate current channels and available channels
            current_channels = []
            available_channels = []
            
            for channel in all_channels:
                if channel['id'] in current_channel_ids:
                    current_channels.append(channel)
                else:
                    available_channels.append(channel)
            
            # Create options for channels to add
            add_channel_options = []
            for channel in available_channels:
                add_channel_options.append({
                    "text": {
                        "type": "plain_text",
                        "text": f"#{channel['name']}"
                    },
                    "value": f"{channel['id']}|{channel['name']}"
                })
            
            # Create options for channels to remove (must keep at least 2)
            remove_channel_options = []
            can_remove = len(current_channels) > 2
            
            if can_remove:
                for channel in current_channels:
                    remove_channel_options.append({
                        "text": {
                            "type": "plain_text",
                            "text": f"#{channel['name']}"
                        },
                        "value": f"{channel['id']}|{channel['name']}"
                    })
            
            # Limit options to Slack's 100 limit
            if len(add_channel_options) > 100:
                add_channel_options = add_channel_options[:100]
            if len(remove_channel_options) > 100:
                remove_channel_options = remove_channel_options[:100]
            
            # Calculate constraints
            current_count = len(current_channels)
            max_additional = min(5 - current_count, len(available_channels))
            max_removable = max(0, current_count - 2)
            
            # Build modal blocks
            modal_blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Managing channels for: {category.name}*\n\n📊 Current: {current_count}/5 channels\n📋 Channels: {', '.join([f'#{ch.channel_name}' for ch in category.get_channels()])}"
                    }
                },
                {
                    "type": "divider"
                }
            ]
            
            # Add channels section
            if add_channel_options and max_additional > 0:
                modal_blocks.extend([
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*➕ Add Channels* (up to {max_additional} more)"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "channels_to_add",
                        "element": {
                            "type": "multi_static_select",
                            "action_id": "add_channels_select",
                            "placeholder": {
                                "type": "plain_text",
                                "text": f"Select channels to add (max {max_additional})"
                            },
                            "options": add_channel_options
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Channels to Add"
                        },
                        "optional": True
                    }
                ])
            elif current_count >= 5:
                modal_blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "➕ *Cannot add more channels* - Category already has maximum of 5 channels"
                    }
                })
            elif not add_channel_options:
                modal_blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "➕ *No additional channels available* - All accessible channels are already in categories"
                    }
                })
            
            # Remove channels section
            if remove_channel_options and can_remove:
                modal_blocks.extend([
                    {
                        "type": "divider"
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*➖ Remove Channels* (can remove up to {max_removable})"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "channels_to_remove",
                        "element": {
                            "type": "multi_static_select",
                            "action_id": "remove_channels_select",
                            "placeholder": {
                                "type": "plain_text",
                                "text": f"Select channels to remove (max {max_removable})"
                            },
                            "options": remove_channel_options
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Channels to Remove"
                        },
                        "optional": True
                    }
                ])
            else:
                modal_blocks.extend([
                    {
                        "type": "divider"
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "➖ *Cannot remove channels* - Categories must have at least 2 channels"
                        }
                    }
                ])
            
            # Add info section
            modal_blocks.extend([
                {
                    "type": "divider"
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "💡 *Rules:*\n• Categories must have 2-5 channels\n• Select channels to add, remove, or both\n• Changes will be applied when you click 'Save Changes'"
                    }
                }
            ])
            
            modal_view = {
                "type": "modal",
                "callback_id": "manage_channels_modal",
                "title": {
                    "type": "plain_text",
                    "text": "Manage Channels"
                },
                "submit": {
                    "type": "plain_text",
                    "text": "Save Changes"
                },
                "close": {
                    "type": "plain_text",
                    "text": "Cancel"
                },
                "blocks": modal_blocks,
                "private_metadata": json.dumps({
                    "user_id": user_id,
                    "category_id": category_id,
                    "current_count": current_count,
                    "max_additional": max_additional,
                    "max_removable": max_removable
                })
            }
            
            response = self.client.views_open(
                trigger_id=trigger_id,
                view=modal_view
            )
            
            logger.info(f"Manage channels modal opened for category {category_id} by user {user_id}")
            return True
            
        except ChannelCategory.DoesNotExist:
            logger.error(f"Category {category_id} not found")
            self._send_message_to_user(user_id, "❌ Category not found.")
            return False
        except SlackApiError as e:
            logger.error(f"Error opening manage channels modal: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error opening manage channels modal: {str(e)}")
            return False

    def handle_manage_channels_submission(self, payload: Dict) -> Dict:
        """
        Handle manage channels modal submission (add/remove channels)
        
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
            category_id = private_metadata.get('category_id')
            current_count = private_metadata.get('current_count', 0)
            max_additional = private_metadata.get('max_additional', 0)
            max_removable = private_metadata.get('max_removable', 0)
            
            # Validate required data
            if not all([user_id, category_id]):
                return self._create_error_response("Missing required information")
            
            # Get selected channels to add and remove
            channels_to_add = values.get('channels_to_add', {}).get('add_channels_select', {}).get('selected_options', [])
            channels_to_remove = values.get('channels_to_remove', {}).get('remove_channels_select', {}).get('selected_options', [])
            
            # Validate that at least one action is selected
            if not channels_to_add and not channels_to_remove:
                return self._create_error_response("Please select channels to add or remove, or click Cancel")
            
            # Validate add constraints
            if len(channels_to_add) > max_additional:
                return self._create_error_response(f"You can only add {max_additional} more channels")
            
            # Validate remove constraints
            if len(channels_to_remove) > max_removable:
                return self._create_error_response(f"You can only remove {max_removable} channels")
            
            # Check final count constraint
            final_count = current_count + len(channels_to_add) - len(channels_to_remove)
            if final_count < 2:
                return self._create_error_response("Categories must have at least 2 channels")
            if final_count > 5:
                return self._create_error_response("Categories cannot have more than 5 channels")
            
            # Get the category
            category = ChannelCategory.objects.get(id=category_id)
            workspace = category.workspace
            
            # Track results
            channels_added = []
            channels_removed = []
            channels_failed = []
            
            # Process removals first
            for channel_option in channels_to_remove:
                try:
                    channel_id, channel_name = channel_option['value'].split('|', 1)
                    
                    # Find and remove the CategoryChannel link
                    slack_channel = SlackChannel.objects.get(workspace=workspace, channel_id=channel_id)
                    category_channel = CategoryChannel.objects.filter(category=category, channel=slack_channel).first()
                    
                    if category_channel:
                        category_channel.delete()
                        channels_removed.append(f"#{channel_name}")
                        logger.info(f"Removed channel {channel_name} from category {category.name}")
                    else:
                        channels_failed.append(f"#{channel_name} (not in category)")
                        
                except Exception as e:
                    logger.error(f"Error removing channel: {str(e)}")
                    channels_failed.append(f"#{channel_option.get('text', {}).get('text', 'unknown')} (remove error)")
                    continue
            
            # Process additions
            for channel_option in channels_to_add:
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
                    
                    # Check if channel is already in this category
                    if CategoryChannel.objects.filter(category=category, channel=slack_channel).exists():
                        channels_failed.append(f"#{channel_name} (already in category)")
                        continue
                    
                    # Create the link
                    CategoryChannel.objects.create(
                        category=category,
                        channel=slack_channel,
                        added_by_user=user_id
                    )
                    
                    channels_added.append(f"#{channel_name}")
                    logger.info(f"Added channel {channel_name} to category {category.name}")
                    
                except Exception as e:
                    logger.error(f"Error adding channel: {str(e)}")
                    channels_failed.append(f"#{channel_option.get('text', {}).get('text', 'unknown')} (add error)")
                    continue
            
            # Build success message
            success_parts = [f"✅ *Category '{category.name}' updated successfully!*\n"]
            
            if channels_added:
                success_parts.append(f"➕ **Added** ({len(channels_added)}): {', '.join(channels_added)}")
            
            if channels_removed:
                success_parts.append(f"➖ **Removed** ({len(channels_removed)}): {', '.join(channels_removed)}")
            
            if channels_failed:
                success_parts.append(f"⚠️ **Failed**: {', '.join(channels_failed)}")
            
            # Add current status
            updated_category = ChannelCategory.objects.get(id=category_id)  # Refresh from DB
            current_channels = [f"#{ch.channel_name}" for ch in updated_category.get_channels()]
            success_parts.append(f"\n📋 **Current channels** ({len(current_channels)}/5): {', '.join(current_channels)}")
            
            success_message = "\n".join(success_parts)
            
            logger.info(f"Manage channels completed for category {category_id} by user {user_id}. Added: {len(channels_added)}, Removed: {len(channels_removed)}, Failed: {len(channels_failed)}")
            
            # Send success notification
            self._send_message_to_user(user_id, success_message)
            
            # Return proper modal close response
            return {"response_action": "clear"}
            
        except ChannelCategory.DoesNotExist:
            logger.error(f"Category {category_id} not found during manage channels")
            return self._create_error_response("Category not found")
        except Exception as e:
            logger.error(f"Error handling manage channels submission: {str(e)}")
            return self._create_error_response(f"Failed to manage channels: {str(e)}")

    def _delete_category(self, category_id: int, user_id: str, channel_id: str) -> bool:
        """Delete a category"""
        try:
            category = ChannelCategory.objects.get(id=category_id)
            category_name = category.name
            channels_count = category.get_channels_count()
            
            # Delete the category (this will cascade delete CategoryChannel entries)
            category.delete()
            
            self._send_message(
                channel_id,
                f"<@{user_id}> ✅ Category '{category_name}' and its {channels_count} channel associations have been deleted successfully."
            )
            
            logger.info(f"Category '{category_name}' (ID: {category_id}) deleted by user {user_id}")
            return True
            
        except ChannelCategory.DoesNotExist:
            self._send_message(
                channel_id,
                f"<@{user_id}> ❌ Category not found."
            )
            return False
        except Exception as e:
            logger.error(f"Error deleting category: {str(e)}")
            self._send_message(
                channel_id,
                f"<@{user_id}> ❌ Failed to delete category: {str(e)}"
            )
            return False

    def _send_message_to_user(self, user_id: str, message: str):
        """
        Send a message to user via DM or fallback method
        
        Args:
            user_id: User ID to send message to
            message: Message text
        """
        try:
            # Try to send a DM to the user first
            try:
                dm_response = self.client.conversations_open(users=user_id)
                dm_channel_id = dm_response['channel']['id']
                
                self.client.chat_postMessage(
                    channel=dm_channel_id,
                    text=message,
                    unfurl_links=False,
                    unfurl_media=False
                )
                
                logger.info(f"Message sent via DM to user {user_id}")
                
            except SlackApiError as dm_error:
                logger.warning(f"Could not send DM to user {user_id}: {dm_error}")
                # Could implement additional fallback methods here
                
        except Exception as e:
            logger.error(f"Error sending message to user: {str(e)}")