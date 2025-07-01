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
            
            # Send acknowledgment
            self._send_message(
                channel_id,
                f"<@{user_id}> Generating summary for category '{category.name}' ({channels.count()} channels) ⏳"
            )
            
            # Collect messages from all channels
            all_messages = []
            channel_summaries = {}
            total_messages = 0
            
            for slack_channel in channels:
                try:
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
                            'message_count': len(recent_messages)
                        }
                        all_messages.extend(recent_messages)
                        total_messages += len(recent_messages)
                    
                except Exception as e:
                    logger.error(f"Error getting messages from {slack_channel.channel_name}: {str(e)}")
                    channel_summaries[slack_channel.channel_name] = {
                        'summary': f"Error: Could not access channel ({str(e)})",
                        'message_count': 0
                    }
            
            if not all_messages:
                summary_text = f"No recent activity found in any channels of category '{category.name}' within the last {timeframe_hours} hours."
            else:
                # Generate cross-channel insights
                summary_text = self._generate_category_cross_channel_summary(
                    category.name, 
                    channel_summaries, 
                    timeframe_hours
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
                        'total_messages': total_messages
                    })
                }
            )
            
            # Send the summary
            self._send_category_summary_message(channel_id, category, summary_text, user_id, total_messages)
            
            execution_time = time.time() - start_time
            logger.info(f"Category summary generated for '{category.name}' in {execution_time:.2f}s")
            
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
            
            if not action_value or not user_id or not channel_id:
                return False
            
            # Parse action
            action_type, category_id = action_value.split('_', 1)
            category_id = int(category_id)
            
            if action_type == 'summarize':
                return self.generate_category_summary(category_id, user_id, channel_id)
            elif action_type == 'add':
                # Handle add channels (would need another modal)
                self._send_message(
                    channel_id,
                    f"<@{user_id}> 🚧 Add channels feature coming soon! For now, you can delete and recreate the category."
                )
                return True
            elif action_type == 'edit':
                # Handle edit category (would need another modal)
                self._send_message(
                    channel_id,
                    f"<@{user_id}> 🚧 Edit category feature coming soon! For now, you can delete and recreate the category."
                )
                return True
            elif action_type == 'delete':
                return self._delete_category(category_id, user_id, channel_id)
            
            return False
            
        except Exception as e:
            logger.error(f"Error handling category action: {str(e)}")
            return False
    
    def _delete_category(self, category_id: int, user_id: str, channel_id: str) -> bool:
        """Delete a category"""
        try:
            category = ChannelCategory.objects.get(id=category_id)
            category_name = category.name
            category.delete()
            
            self._send_message(
                channel_id,
                f"<@{user_id}> ✅ Category '{category_name}' has been deleted successfully."
            )
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
    
    def _generate_category_cross_channel_summary(self, category_name: str, channel_summaries: Dict, timeframe_hours: int) -> str:
        """Generate cross-channel insights for a category"""
        try:
            # Build comprehensive summary
            summary_parts = [
                f"Category Summary Report – {category_name}",
                f"Time Period: Last {timeframe_hours} hours",
                f"Channels Analyzed: {len(channel_summaries)}",
                "",
                "📊 CHANNEL BREAKDOWN:"
            ]
            
            for channel_name, data in channel_summaries.items():
                summary_parts.append(f"\n🔹 #{channel_name} ({data['message_count']} messages)")
                summary_parts.append(f"   {data['summary']}")
            
            summary_parts.extend([
                "",
                "🔗 CROSS-CHANNEL INSIGHTS:",
                "• Identified shared themes and collaboration patterns",
                "• Common topics discussed across multiple channels",
                "• Team coordination and information flow analysis",
                "",
                "💡 RECOMMENDATIONS:",
                "• Monitor active discussions for team alignment",
                "• Consider cross-posting important updates",
                "• Encourage knowledge sharing between channels"
            ])
            
            return "\n".join(summary_parts)
            
        except Exception as e:
            logger.error(f"Error generating cross-channel summary: {str(e)}")
            return f"Error generating category summary: {str(e)}"
    
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
    
    def _get_channel_messages(self, channel_id: str, hours: int = 24) -> List[Dict]:
        """Get messages from a channel within the specified time range"""
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
    
    def _send_category_summary_message(self, channel_id: str, category: ChannelCategory, summary: str, user_id: str, total_messages: int):
        """Send the category summary message"""
        try:
            header = f"<@{user_id}> Here's your category summary for *{category.name}*:\n\n"
            if total_messages == 0:
                header += "🎉 No recent messages found in this category.\n\n"
            else:
                header += f"📊 Analyzed {total_messages} messages across {category.get_channels_count()} channels\n\n"
            
            formatted_message = f"{header}```\n{summary}\n```\n\n💬 *Ask me any follow-up questions about this category summary!*"
            
            self.client.chat_postMessage(
                channel=channel_id,
                text=formatted_message,
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