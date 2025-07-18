"""
Canvas Management Module
Handles Slack Canvas integration for visual todo lists and project management
"""
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from django.conf import settings
from django.utils import timezone

from .models import ChannelTodo, SlackChannel, ChannelCanvas, SlackWorkspace

logger = logging.getLogger(__name__)


class CanvasManager:
    """
    Manages Slack Canvas documents for todo lists and project tracking
    """
    
    def __init__(self, slack_client: WebClient):
        """Initialize the canvas manager with Slack client"""
        self.client = slack_client
    
    def create_canvas(self, channel_id: str, title: str = "Todo List", 
                      created_by: str = "") -> Tuple[bool, str, Optional[ChannelCanvas]]:
        """
        Create a new Canvas document for a channel
        
        Args:
            channel_id: Slack channel ID
            title: Canvas title
            created_by: User ID who created the canvas
            
        Returns:
            Tuple of (success, message, canvas_object)
        """
        try:
            # Check if canvas already exists for this channel
            workspace = self._get_or_create_workspace()
            channel = self._get_or_create_channel(channel_id, workspace)
            
            existing_canvas = ChannelCanvas.objects.filter(channel=channel).first()
            if existing_canvas:
                return False, f"Canvas already exists for #{channel.channel_name}: {existing_canvas.canvas_url}", existing_canvas
            
            # Create initial canvas content
            canvas_content = self._generate_canvas_content(channel, title)
            
            # Create canvas via modern Canvas API
            try:
                # Use the new conversations.canvases.create API for channel canvases
                response = self.client.api_call(
                    "conversations.canvases.create",
                    json={
                        "channel_id": channel_id,
                        "document_content": {
                            "type": "markdown",
                            "markdown": canvas_content
                        }
                    }
                )
                
                if response['ok']:
                    canvas_id = response['canvas_id']
                    # Get canvas info to build URL
                    canvas_url = f"https://slack.com/canvas/{canvas_id}"
                    
                    # Save to database
                    canvas = ChannelCanvas.objects.create(
                        channel=channel,
                        canvas_id=canvas_id,
                        canvas_url=canvas_url,
                        canvas_title=title,
                        total_todos=0,
                        pending_todos=0,
                        last_sync_at=timezone.now()
                    )
                    
                    logger.info(f"Created canvas for #{channel.channel_name}: {canvas_id}")
                    
                    return True, f"✅ Canvas created successfully: {canvas_url}", canvas
                else:
                    return False, f"❌ Failed to create canvas: {response.get('error', 'Unknown error')}", None
                    
            except SlackApiError as e:
                logger.error(f"Slack API error creating canvas: {e}")
                return False, f"❌ Slack API error: {str(e)}", None
            
        except Exception as e:
            logger.error(f"Error creating canvas: {str(e)}")
            return False, f"❌ Failed to create canvas: {str(e)}", None
    
    def update_canvas(self, channel_id: str, force_sync: bool = False) -> Tuple[bool, str]:
        """
        Update existing Canvas with current todo list
        
        Args:
            channel_id: Slack channel ID
            force_sync: Force update even if not needed
            
        Returns:
            Tuple of (success, message)
        """
        try:
            # Get canvas info
            channel = SlackChannel.objects.filter(channel_id=channel_id).first()
            if not channel:
                return False, "❌ Channel not found"
            
            canvas = ChannelCanvas.objects.filter(channel=channel).first()
            if not canvas:
                return False, f"❌ No canvas found for #{channel.channel_name}. Use `/canvas create` first."
            
            # Check if sync is needed
            if not force_sync and not canvas.needs_sync():
                return True, f"✅ Canvas is already up to date: {canvas.canvas_url}"
            
            # Generate updated canvas content
            canvas_content = self._generate_canvas_content(channel, canvas.canvas_title)
            
            # Update canvas via modern Canvas API
            try:
                response = self.client.api_call(
                    "canvases.edit",
                    json={
                        "canvas_id": canvas.canvas_id,
                        "changes": [{
                            "operation": "replace",
                            "document_content": {
                                "type": "markdown",
                                "markdown": canvas_content
                            }
                        }]
                    }
                )
                
                if response['ok']:
                    # Update database record
                    canvas.last_sync_at = timezone.now()
                    canvas.total_todos = ChannelTodo.objects.filter(channel=channel).count()
                    canvas.pending_todos = ChannelTodo.objects.filter(
                        channel=channel, 
                        status__in=['pending', 'in_progress']
                    ).count()
                    canvas.save()
                    
                    logger.info(f"Updated canvas for #{channel.channel_name}")
                    
                    return True, f"✅ Canvas updated successfully: {canvas.canvas_url}"
                else:
                    return False, f"❌ Failed to update canvas: {response.get('error', 'Unknown error')}"
                    
            except SlackApiError as e:
                logger.error(f"Slack API error updating canvas: {e}")
                return False, f"❌ Slack API error: {str(e)}"
            
        except Exception as e:
            logger.error(f"Error updating canvas: {str(e)}")
            return False, f"❌ Failed to update canvas: {str(e)}"

    def update_specific_canvas(self, channel_id: str, canvas_title: str, force_sync: bool = False) -> Tuple[bool, str]:
        """
        Update a specific Canvas by title with current todo list
        
        Args:
            channel_id: Slack channel ID
            canvas_title: Title of the specific canvas to update
            force_sync: Force update even if not needed
            
        Returns:
            Tuple of (success, message)
        """
        try:
            # Get canvas info by title
            channel = SlackChannel.objects.filter(channel_id=channel_id).first()
            if not channel:
                return False, "❌ Channel not found"
            
            canvas = ChannelCanvas.objects.filter(
                channel=channel, 
                canvas_title=canvas_title
            ).first()
            
            if not canvas:
                return False, f"❌ Canvas '{canvas_title}' not found for #{channel.channel_name}. Use `/canvas create \"{canvas_title}\"` first."
            
            # Check if sync is needed
            if not force_sync and not canvas.needs_sync():
                return True, f"✅ Canvas '{canvas_title}' is already up to date: {canvas.canvas_url}"
            
            # Generate updated canvas content
            canvas_content = self._generate_canvas_content(channel, canvas_title)
            
            # Update canvas via modern Canvas API
            try:
                response = self.client.api_call(
                    "canvases.edit",
                    json={
                        "canvas_id": canvas.canvas_id,
                        "changes": [{
                            "operation": "replace",
                            "document_content": {
                                "type": "markdown",
                                "markdown": canvas_content
                            }
                        }]
                    }
                )
                
                if response['ok']:
                    # Update database record
                    canvas.last_sync_at = timezone.now()
                    canvas.total_todos = ChannelTodo.objects.filter(channel=channel).count()
                    canvas.pending_todos = ChannelTodo.objects.filter(
                        channel=channel, 
                        status__in=['pending', 'in_progress']
                    ).count()
                    canvas.save()
                    
                    logger.info(f"Updated canvas '{canvas_title}' for #{channel.channel_name}")
                    
                    return True, f"✅ Canvas '{canvas_title}' updated successfully: {canvas.canvas_url}"
                else:
                    return False, f"❌ Failed to update canvas '{canvas_title}': {response.get('error', 'Unknown error')}"
                    
            except SlackApiError as e:
                logger.error(f"Slack API error updating canvas '{canvas_title}': {e}")
                return False, f"❌ Slack API error: {str(e)}"
            
        except Exception as e:
            logger.error(f"Error updating canvas '{canvas_title}': {str(e)}")
            return False, f"❌ Failed to update canvas '{canvas_title}': {str(e)}"
    
    def show_canvas_info(self, channel_id: str) -> Tuple[bool, str]:
        """
        Show canvas information and link
        
        Args:
            channel_id: Slack channel ID
            
        Returns:
            Tuple of (success, formatted_message)
        """
        try:
            channel = SlackChannel.objects.filter(channel_id=channel_id).first()
            if not channel:
                return False, "❌ Channel not found"
            
            canvas = ChannelCanvas.objects.filter(channel=channel).first()
            if not canvas:
                return True, f"📋 No canvas found for #{channel.channel_name}\n\n💡 Use `/canvas create` to create one!"
            
            # Get current todo stats
            total_todos = ChannelTodo.objects.filter(channel=channel).count()
            pending_todos = ChannelTodo.objects.filter(
                channel=channel, 
                status__in=['pending', 'in_progress']
            ).count()
            completed_todos = ChannelTodo.objects.filter(
                channel=channel, 
                status='completed'
            ).count()
            
            # Check sync status
            sync_status = "✅ Up to date" if not canvas.needs_sync() else "⚠️ Needs sync"
            
            message = f"""📋 **Canvas Information for #{channel.channel_name}**

🎨 **Canvas:** {canvas.canvas_title}
🔗 **Link:** {canvas.canvas_url}

📊 **Todo Statistics:**
• Total todos: {total_todos}
• Pending: {pending_todos}
• Completed: {completed_todos}
• Completion rate: {round((completed_todos / max(total_todos, 1)) * 100, 1)}%

🔄 **Sync Status:** {sync_status}
🕐 **Last updated:** {canvas.last_sync_at.strftime('%Y-%m-%d %H:%M') if canvas.last_sync_at else 'Never'}

💡 **Commands:**
• `/canvas update` - Sync latest todos to canvas
• `/canvas create` - Create new canvas (if none exists)"""

            return True, message
            
        except Exception as e:
            logger.error(f"Error showing canvas info: {str(e)}")
            return False, f"❌ Failed to get canvas info: {str(e)}"
    
    def auto_sync_canvas(self, channel_id: str) -> bool:
        """
        Automatically sync canvas when todos are updated
        
        Args:
            channel_id: Slack channel ID
            
        Returns:
            True if sync was successful or not needed
        """
        try:
            channel = SlackChannel.objects.filter(channel_id=channel_id).first()
            if not channel:
                return False
            
            canvas = ChannelCanvas.objects.filter(channel=channel).first()
            if not canvas:
                return True  # No canvas to sync, that's ok
            
            if canvas.needs_sync():
                success, message = self.update_canvas(channel_id)
                return success
            
            return True  # Already up to date
            
        except Exception as e:
            logger.error(f"Error in auto sync: {str(e)}")
            return False
    
    def _generate_canvas_content(self, channel: SlackChannel, canvas_title: str = None) -> str:
        """
        Generate Canvas document content with todo lists
        
        Args:
            channel: SlackChannel object
            
        Returns:
            Canvas document content as string
        """
        try:
            # Get todos organized by status and priority
            pending_todos = ChannelTodo.objects.filter(
                channel=channel,
                status__in=['pending', 'in_progress']
            ).order_by('-priority', '-created_at')
            
            completed_todos = ChannelTodo.objects.filter(
                channel=channel,
                status='completed'
            ).order_by('-completed_at')[:10]  # Show last 10 completed
            
            # Generate modern Canvas-compatible markdown content
            content_parts = [
                f"# 📋 Todo List - #{channel.channel_name}",
                "",
                f"> *Last updated: {timezone.now().strftime('%Y-%m-%d %H:%M')}*",
                "",
                "## 📌 Pending Tasks",
                ""
            ]
            
            if pending_todos.exists():
                # Group by priority
                priority_groups = {
                    'critical': [],
                    'high': [],
                    'medium': [],
                    'low': []
                }
                
                for todo in pending_todos:
                    priority_groups[todo.priority].append(todo)
                
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
                        
                        for todo in priority_groups[priority]:
                            # Format todo item with proper checkbox syntax
                            if todo.status == 'completed':
                                checkbox = "- [x]"  # Checked checkbox
                            elif todo.status == 'in_progress':
                                checkbox = "- [ ] ⏳"  # Unchecked with in-progress indicator
                            else:
                                checkbox = "- [ ]"  # Unchecked checkbox
                            
                            todo_line = f"{checkbox} **{todo.title}**"
                            
                            # Add details
                            details = []
                            if todo.assigned_to_username:
                                details.append(f"@{todo.assigned_to_username}")
                            
                            if todo.due_date:
                                due_str = todo.due_date.strftime('%m/%d %H:%M')
                                if todo.is_overdue():
                                    details.append(f"🚨 **OVERDUE** {due_str}")
                                else:
                                    details.append(f"📅 Due: {due_str}")
                            
                            if todo.task_type != 'general':
                                type_emoji = {
                                    'bug': '🐛', 'feature': '✨', 'meeting': '📅',
                                    'review': '👀', 'urgent': '🚨'
                                }
                                details.append(f"{type_emoji.get(todo.task_type, '📝')} {todo.task_type}")
                            
                            if details:
                                todo_line += f" | {' | '.join(details)}"
                            
                            content_parts.append(todo_line)
                            
                            # Add description if present
                            if todo.description:
                                desc = todo.description[:150] + "..." if len(todo.description) > 150 else todo.description
                                content_parts.append(f"  💬 *{desc}*")
                        
                        content_parts.append("")
            else:
                content_parts.extend([
                    "🎉 **No pending tasks!**",
                    "*All caught up - great work!*",
                    ""
                ])
            
            # Add completed section
            if completed_todos.exists():
                content_parts.extend([
                    "## ✅ Recently Completed",
                    ""
                ])
                
                for todo in completed_todos:
                    completed_line = f"- [x] ~~{todo.title}~~"
                    if todo.completed_by:
                        completed_line += f" | Completed by @{todo.completed_by}"
                    if todo.completed_at:
                        completed_line += f" | {todo.completed_at.strftime('%m/%d')}"
                    
                    content_parts.append(completed_line)
                
                content_parts.append("")
            
            # Add statistics
            total_todos = ChannelTodo.objects.filter(channel=channel).count()
            pending_count = pending_todos.count()
            completed_count = ChannelTodo.objects.filter(channel=channel, status='completed').count()
            
            content_parts.extend([
                "## 📊 Statistics",
                "",
                f"- **Total todos:** {total_todos}",
                f"- **Pending:** {pending_count}",
                f"- **Completed:** {completed_count}",
                f"- **Completion rate:** {round((completed_count / max(total_todos, 1)) * 100, 1)}%",
                "",
                "---",
                "### 💡 Quick Commands",
                f"- `/task \"{canvas_title or 'Todo List'}\"` - Update this specific canvas with channel messages",
                "- `/todo add \"task name\"` - Add individual todo",
                "- `/todo complete [id]` - Mark todo as completed",  
                "- `/canvas update` - Force sync todos to canvas",
                "",
                "*Managed by @betasummarizer bot*"
            ])
            
            return "\n".join(content_parts)
            
        except Exception as e:
            logger.error(f"Error generating canvas content: {str(e)}")
            return f"# Todo List - #{channel.channel_name}\n\nError generating content: {str(e)}"
    
    def delete_canvas(self, channel_id: str) -> Tuple[bool, str]:
        """
        Delete canvas for a channel
        
        Args:
            channel_id: Slack channel ID
            
        Returns:
            Tuple of (success, message)
        """
        try:
            channel = SlackChannel.objects.filter(channel_id=channel_id).first()
            if not channel:
                return False, "❌ Channel not found"
            
            canvas = ChannelCanvas.objects.filter(channel=channel).first()
            if not canvas:
                return False, f"❌ No canvas found for #{channel.channel_name}"
            
            # Delete from Slack using Canvas API
            try:
                self.client.api_call(
                    "canvases.delete",
                    json={"canvas_id": canvas.canvas_id}
                )
            except SlackApiError as e:
                logger.warning(f"Could not delete canvas from Slack: {e}")
            
            # Delete from database
            canvas_title = canvas.canvas_title
            canvas.delete()
            
            logger.info(f"Deleted canvas for #{channel.channel_name}")
            
            return True, f"🗑️ Deleted canvas: {canvas_title}"
            
        except Exception as e:
            logger.error(f"Error deleting canvas: {str(e)}")
            return False, f"❌ Failed to delete canvas: {str(e)}"
    
    def get_canvas_stats(self) -> Tuple[bool, str]:
        """
        Get statistics about all canvas documents
        
        Returns:
            Tuple of (success, formatted_message)
        """
        try:
            total_canvases = ChannelCanvas.objects.count()
            
            if total_canvases == 0:
                return True, "📋 No canvas documents found\n\n💡 Use `/canvas create` to create your first one!"
            
            # Get active canvas stats
            canvases_needing_sync = ChannelCanvas.objects.filter(
                last_sync_at__lt=timezone.now() - timezone.timedelta(hours=1)
            ).count()
            
            total_todos_across_canvases = sum(
                canvas.total_todos for canvas in ChannelCanvas.objects.all()
            )
            
            pending_todos_across_canvases = sum(
                canvas.pending_todos for canvas in ChannelCanvas.objects.all()
            )
            
            message = f"""📊 **Canvas Statistics**

📋 **Total canvas documents:** {total_canvases}
⚠️ **Needing sync:** {canvases_needing_sync}
📝 **Total todos across all canvases:** {total_todos_across_canvases}
⏳ **Pending todos:** {pending_todos_across_canvases}
✅ **Completed todos:** {total_todos_across_canvases - pending_todos_across_canvases}

💡 **Canvas helps you:**
• Visualize todos in a beautiful format
• Share project status with your team
• Track progress across channels
• Keep everyone aligned on priorities"""
            
            return True, message
            
        except Exception as e:
            logger.error(f"Error getting canvas stats: {str(e)}")
            return False, f"❌ Failed to get canvas stats: {str(e)}"
    
    def _get_or_create_workspace(self) -> SlackWorkspace:
        """Get or create the default workspace"""
        workspace, _ = SlackWorkspace.objects.get_or_create(
            workspace_id="default",
            defaults={'workspace_name': 'Default Workspace'}
        )
        return workspace
    
    def _get_or_create_channel(self, channel_id: str, workspace: SlackWorkspace) -> SlackChannel:
        """Get or create a SlackChannel object"""
        try:
            # Try to get channel info from Slack API
            response = self.client.conversations_info(channel=channel_id)
            if response['ok']:
                channel_info = response['channel']
                channel_name = channel_info.get('name', 'unknown')
                is_private = channel_info.get('is_private', False)
            else:
                channel_name = 'unknown'
                is_private = False
        except SlackApiError:
            channel_name = 'unknown'
            is_private = False
        
        channel, created = SlackChannel.objects.get_or_create(
            workspace=workspace,
            channel_id=channel_id,
            defaults={
                'channel_name': channel_name,
                'is_private': is_private
            }
        )
        
        return channel 