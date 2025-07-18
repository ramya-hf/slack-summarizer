"""
Todo Management Module
Handles CRUD operations for channel todos, task assignments, and priority management
"""
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from django.conf import settings
from django.utils import timezone
from django.db.models import Q, Count

from .models import (
    ChannelTodo, SlackChannel, SlackWorkspace, TaskSummary, 
    TaskReminder, ChannelCanvas
)
from .task_detector import TaskDetector, DetectedTask

logger = logging.getLogger(__name__)


class TodoManager:
    """
    Manages todo operations including creation, editing, completion, and assignments
    """
    
    def __init__(self, slack_client: WebClient):
        """Initialize the todo manager with Slack client"""
        self.client = slack_client
        self.task_detector = TaskDetector()
    
    def add_todo(self, channel_id: str, title: str, description: str = "", 
                 task_type: str = "general", priority: str = "medium", 
                 assigned_to: str = "", due_date: str = "", 
                 created_by: str = "") -> Tuple[bool, str, Optional[ChannelTodo]]:
        """
        Add a new todo to a channel
        
        Args:
            channel_id: Slack channel ID
            title: Todo title
            description: Todo description
            task_type: Type of task (bug, feature, meeting, etc.)
            priority: Priority level (low, medium, high, critical)
            assigned_to: User ID to assign to
            due_date: Due date string (e.g., "tomorrow", "friday", "2024-01-15")
            created_by: User ID who created the todo
            
        Returns:
            Tuple of (success, message, todo_object)
        """
        try:
            # Get or create the channel
            workspace = self._get_or_create_workspace()
            channel = self._get_or_create_channel(channel_id, workspace)
            
            # Parse due date
            parsed_due_date = self._parse_due_date(due_date) if due_date else None
            
            # Resolve assigned user if provided
            assigned_username = ""
            if assigned_to:
                if assigned_to.startswith('@'):
                    assigned_to = assigned_to[1:]  # Remove @ symbol
                
                # Try to resolve username to user ID
                user_info = self._resolve_user(assigned_to)
                if user_info:
                    assigned_to = user_info.get('user_id', assigned_to)
                    assigned_username = user_info.get('username', assigned_to)
            
            # Create the todo
            todo = ChannelTodo.objects.create(
                channel=channel,
                title=title[:500],  # Ensure title fits in database
                description=description,
                task_type=task_type,
                priority=priority,
                assigned_to=assigned_to,
                assigned_to_username=assigned_username,
                due_date=parsed_due_date,
                created_by=created_by,
                status='pending'
            )
            
            # Create reminder if due date is set
            if parsed_due_date:
                self._create_reminder(todo, parsed_due_date)
            
            success_message = f"✅ Todo created: **{title}**"
            if assigned_username:
                success_message += f" | Assigned to: @{assigned_username}"
            if parsed_due_date:
                success_message += f" | Due: {parsed_due_date.strftime('%m/%d %H:%M')}"
            
            logger.info(f"Created todo: {title} in #{channel.channel_name} by {created_by}")
            
            return True, success_message, todo
            
        except Exception as e:
            logger.error(f"Error adding todo: {str(e)}")
            return False, f"❌ Failed to create todo: {str(e)}", None
    
    def list_todos(self, channel_id: str, filter_status: str = "active", 
                   filter_assigned: str = "", filter_priority: str = "") -> Tuple[bool, str, List[ChannelTodo]]:
        """
        List todos for a channel with filtering options
        
        Args:
            channel_id: Slack channel ID
            filter_status: Filter by status ("active", "completed", "all")
            filter_assigned: Filter by assigned user
            filter_priority: Filter by priority level
            
        Returns:
            Tuple of (success, formatted_message, todos_list)
        """
        try:
            # Get the channel
            channel = SlackChannel.objects.filter(channel_id=channel_id).first()
            if not channel:
                return False, "❌ Channel not found", []
            
            # Build query
            query = Q(channel=channel)
            
            if filter_status == "active":
                query &= Q(status__in=['pending', 'in_progress'])
            elif filter_status == "completed":
                query &= Q(status='completed')
            elif filter_status != "all":
                query &= Q(status=filter_status)
            
            if filter_assigned:
                if filter_assigned.startswith('@'):
                    filter_assigned = filter_assigned[1:]
                query &= Q(assigned_to_username__icontains=filter_assigned)
            
            if filter_priority:
                query &= Q(priority=filter_priority)
            
            # Get todos
            todos = ChannelTodo.objects.filter(query).order_by('-priority', '-created_at')
            
            if not todos.exists():
                return True, f"📋 No todos found in #{channel.channel_name}", []
            
            # Format message
            message_parts = [f"📋 **Todos for #{channel.channel_name}**"]
            
            if filter_status != "all":
                message_parts.append(f"*Filter: {filter_status.title()} tasks*")
            
            message_parts.append("")  # Empty line
            
            # Group by priority
            priority_groups = {
                'critical': [],
                'high': [],
                'medium': [],
                'low': []
            }
            
            for todo in todos:
                priority_groups[todo.priority].append(todo)
            
            todo_count = 0
            for priority in ['critical', 'high', 'medium', 'low']:
                if priority_groups[priority]:
                    priority_emoji = {'critical': '🔴', 'high': '🟠', 'medium': '🟡', 'low': '🟢'}
                    message_parts.append(f"**{priority_emoji[priority]} {priority.upper()} PRIORITY**")
                    
                    for todo in priority_groups[priority]:
                        todo_count += 1
                        todo_line = f"{todo_count}. {todo.to_slack_format()}"
                        
                        if todo.description:
                            # Truncate long descriptions
                            desc = todo.description[:100] + "..." if len(todo.description) > 100 else todo.description
                            todo_line += f"\n   💬 {desc}"
                        
                        if todo.is_overdue():
                            todo_line += " 🚨 **OVERDUE**"
                        
                        message_parts.append(todo_line)
                    
                    message_parts.append("")  # Empty line between priorities
            
            # Add summary
            message_parts.append(f"📊 **Summary:** {todo_count} todos shown")
            
            return True, "\n".join(message_parts), list(todos)
            
        except Exception as e:
            logger.error(f"Error listing todos: {str(e)}")
            return False, f"❌ Failed to list todos: {str(e)}", []
    
    def complete_todo(self, channel_id: str, todo_identifier: str, 
                      completed_by: str = "") -> Tuple[bool, str, Optional[ChannelTodo]]:
        """
        Mark a todo as completed
        
        Args:
            channel_id: Slack channel ID
            todo_identifier: Todo ID or partial title to match
            completed_by: User ID who completed the todo
            
        Returns:
            Tuple of (success, message, todo_object)
        """
        try:
            # Find the todo
            todo = self._find_todo(channel_id, todo_identifier)
            if not todo:
                return False, f"❌ Todo not found: {todo_identifier}", None
            
            if todo.status == 'completed':
                return False, f"✅ Todo is already completed: {todo.title}", todo
            
            # Mark as completed
            todo.status = 'completed'
            todo.completed_at = timezone.now()
            todo.completed_by = completed_by
            todo.save()
            
            success_message = f"✅ Completed: **{todo.title}**"
            if todo.assigned_to_username:
                success_message += f" (was assigned to @{todo.assigned_to_username})"
            
            logger.info(f"Completed todo: {todo.title} by {completed_by}")
            
            return True, success_message, todo
            
        except Exception as e:
            logger.error(f"Error completing todo: {str(e)}")
            return False, f"❌ Failed to complete todo: {str(e)}", None
    
    def edit_todo(self, channel_id: str, todo_identifier: str, 
                  new_title: str = "", new_description: str = "", 
                  new_priority: str = "", new_assigned: str = "", 
                  new_due_date: str = "") -> Tuple[bool, str, Optional[ChannelTodo]]:
        """
        Edit an existing todo
        
        Args:
            channel_id: Slack channel ID
            todo_identifier: Todo ID or partial title to match
            new_title: New title (optional)
            new_description: New description (optional)
            new_priority: New priority (optional)
            new_assigned: New assignee (optional)
            new_due_date: New due date (optional)
            
        Returns:
            Tuple of (success, message, todo_object)
        """
        try:
            # Find the todo
            todo = self._find_todo(channel_id, todo_identifier)
            if not todo:
                return False, f"❌ Todo not found: {todo_identifier}", None
            
            # Track changes
            changes = []
            
            if new_title and new_title != todo.title:
                changes.append(f"Title: '{todo.title}' → '{new_title}'")
                todo.title = new_title[:500]
            
            if new_description != todo.description:  # Allow empty string to clear description
                if new_description:
                    changes.append(f"Description updated")
                else:
                    changes.append(f"Description cleared")
                todo.description = new_description
            
            if new_priority and new_priority != todo.priority:
                if new_priority in ['low', 'medium', 'high', 'critical']:
                    changes.append(f"Priority: {todo.priority} → {new_priority}")
                    todo.priority = new_priority
                else:
                    return False, f"❌ Invalid priority: {new_priority}. Use: low, medium, high, critical", None
            
            if new_assigned:
                if new_assigned.startswith('@'):
                    new_assigned = new_assigned[1:]
                
                # Resolve user
                user_info = self._resolve_user(new_assigned)
                if user_info:
                    new_user_id = user_info.get('user_id', new_assigned)
                    new_username = user_info.get('username', new_assigned)
                    
                    if new_user_id != todo.assigned_to:
                        old_assignee = f"@{todo.assigned_to_username}" if todo.assigned_to_username else "Unassigned"
                        changes.append(f"Assigned: {old_assignee} → @{new_username}")
                        todo.assigned_to = new_user_id
                        todo.assigned_to_username = new_username
            
            if new_due_date:
                parsed_due_date = self._parse_due_date(new_due_date)
                if parsed_due_date != todo.due_date:
                    old_due = todo.due_date.strftime('%m/%d %H:%M') if todo.due_date else "No due date"
                    new_due = parsed_due_date.strftime('%m/%d %H:%M') if parsed_due_date else "No due date"
                    changes.append(f"Due date: {old_due} → {new_due}")
                    todo.due_date = parsed_due_date
                    
                    # Update or create reminder
                    if parsed_due_date:
                        self._create_reminder(todo, parsed_due_date)
            
            if not changes:
                return False, f"ℹ️ No changes made to: {todo.title}", todo
            
            todo.save()
            
            success_message = f"✏️ Updated: **{todo.title}**\n🔄 Changes:\n• " + "\n• ".join(changes)
            
            logger.info(f"Edited todo: {todo.title} - {len(changes)} changes")
            
            return True, success_message, todo
            
        except Exception as e:
            logger.error(f"Error editing todo: {str(e)}")
            return False, f"❌ Failed to edit todo: {str(e)}", None
    
    def assign_todo(self, channel_id: str, todo_identifier: str, 
                    assigned_to: str) -> Tuple[bool, str, Optional[ChannelTodo]]:
        """
        Assign a todo to a user
        
        Args:
            channel_id: Slack channel ID
            todo_identifier: Todo ID or partial title to match
            assigned_to: Username or user ID to assign to
            
        Returns:
            Tuple of (success, message, todo_object)
        """
        return self.edit_todo(channel_id, todo_identifier, new_assigned=assigned_to)
    
    def set_priority(self, channel_id: str, todo_identifier: str, 
                     priority: str) -> Tuple[bool, str, Optional[ChannelTodo]]:
        """
        Set priority for a todo
        
        Args:
            channel_id: Slack channel ID
            todo_identifier: Todo ID or partial title to match
            priority: Priority level (low, medium, high, critical)
            
        Returns:
            Tuple of (success, message, todo_object)
        """
        return self.edit_todo(channel_id, todo_identifier, new_priority=priority)
    
    def delete_todo(self, channel_id: str, todo_identifier: str) -> Tuple[bool, str]:
        """
        Delete a todo
        
        Args:
            channel_id: Slack channel ID
            todo_identifier: Todo ID or partial title to match
            
        Returns:
            Tuple of (success, message)
        """
        try:
            # Find the todo
            todo = self._find_todo(channel_id, todo_identifier)
            if not todo:
                return False, f"❌ Todo not found: {todo_identifier}"
            
            todo_title = todo.title
            todo.delete()
            
            logger.info(f"Deleted todo: {todo_title}")
            
            return True, f"🗑️ Deleted: **{todo_title}**"
            
        except Exception as e:
            logger.error(f"Error deleting todo: {str(e)}")
            return False, f"❌ Failed to delete todo: {str(e)}"
    
    def extract_tasks_from_messages(self, channel_id: str, messages: List[Dict], 
                                    auto_create: bool = False, 
                                    created_by: str = "") -> Tuple[bool, str, List[DetectedTask]]:
        """
        Extract tasks from channel messages using AI detection
        
        Args:
            channel_id: Slack channel ID
            messages: List of message dictionaries
            auto_create: Whether to automatically create todos from detected tasks
            created_by: User ID who requested the extraction
            
        Returns:
            Tuple of (success, formatted_message, detected_tasks)
        """
        try:
            # Get channel info
            channel = SlackChannel.objects.filter(channel_id=channel_id).first()
            channel_name = channel.channel_name if channel else "unknown"
            
            # Detect tasks in messages
            detected_tasks = self.task_detector.batch_analyze_messages(messages, channel_name)
            
            if not detected_tasks:
                return True, "📝 No actionable tasks detected in the analyzed messages.", []
            
            created_todos = []
            if auto_create and channel:
                workspace = self._get_or_create_workspace()
                channel_obj = self._get_or_create_channel(channel_id, workspace)
                
                for detected_task in detected_tasks:
                    try:
                        todo = self.task_detector.create_todo_from_detection(
                            detected_task, channel_obj, created_by
                        )
                        created_todos.append(todo)
                    except Exception as e:
                        logger.error(f"Error creating todo from detection: {str(e)}")
            
            # Format response
            message_parts = [f"🔍 **Task Detection Results for #{channel_name}**"]
            message_parts.append(f"📊 Found {len(detected_tasks)} potential tasks")
            
            if auto_create:
                message_parts.append(f"✅ Created {len(created_todos)} todos automatically")
            
            message_parts.append("")
            
            # Group by task type
            task_groups = {}
            for task in detected_tasks:
                task_type = task.task_type
                if task_type not in task_groups:
                    task_groups[task_type] = []
                task_groups[task_type].append(task)
            
            # Display detected tasks
            for task_type, tasks in task_groups.items():
                type_emoji = {
                    'bug': '🐛', 'feature': '✨', 'meeting': '📅',
                    'review': '👀', 'deadline': '⏰', 'urgent': '🚨', 'general': '📝'
                }
                
                message_parts.append(f"**{type_emoji.get(task_type, '📝')} {task_type.upper()} TASKS**")
                
                for i, task in enumerate(tasks, 1):
                    confidence_bar = "🟢" if task.confidence_score >= 0.8 else "🟡" if task.confidence_score >= 0.6 else "🟠"
                    
                    task_line = f"{i}. {confidence_bar} **{task.title}**"
                    if task.priority != 'medium':
                        priority_emoji = {'critical': '🔴', 'high': '🟠', 'low': '🟢'}
                        task_line += f" {priority_emoji.get(task.priority, '')} {task.priority.upper()}"
                    
                    if task.assigned_to_username:
                        task_line += f" | @{task.assigned_to_username}"
                    
                    if task.due_date:
                        task_line += f" | Due: {task.due_date.strftime('%m/%d')}"
                    
                    message_parts.append(task_line)
                    
                    if task.description and task.description != task.title:
                        desc = task.description[:100] + "..." if len(task.description) > 100 else task.description
                        message_parts.append(f"   💬 {desc}")
                
                message_parts.append("")
            
            # Add instructions
            if not auto_create:
                message_parts.append("💡 Use `/tasks extract auto` to automatically create todos from detected tasks")
            
            return True, "\n".join(message_parts), detected_tasks
            
        except Exception as e:
            logger.error(f"Error extracting tasks from messages: {str(e)}")
            return False, f"❌ Failed to extract tasks: {str(e)}", []
    
    def get_priority_todos(self, channel_id: str, priority: str = "high") -> Tuple[bool, str, List[ChannelTodo]]:
        """
        Get todos by priority level
        
        Args:
            channel_id: Slack channel ID
            priority: Priority level to filter by
            
        Returns:
            Tuple of (success, formatted_message, todos_list)
        """
        return self.list_todos(channel_id, filter_status="active", filter_priority=priority)
    
    def get_overdue_todos(self, channel_id: str = "") -> Tuple[bool, str, List[ChannelTodo]]:
        """
        Get overdue todos for a channel or all channels
        
        Args:
            channel_id: Slack channel ID (optional, if empty gets all channels)
            
        Returns:
            Tuple of (success, formatted_message, todos_list)
        """
        try:
            query = Q(
                due_date__lt=timezone.now(),
                status__in=['pending', 'in_progress']
            )
            
            if channel_id:
                channel = SlackChannel.objects.filter(channel_id=channel_id).first()
                if not channel:
                    return False, "❌ Channel not found", []
                query &= Q(channel=channel)
            
            overdue_todos = ChannelTodo.objects.filter(query).order_by('due_date')
            
            if not overdue_todos.exists():
                scope = f"#{channel.channel_name}" if channel_id else "all channels"
                return True, f"✅ No overdue todos in {scope}", []
            
            # Format message
            message_parts = [f"🚨 **Overdue Todos**"]
            if channel_id:
                message_parts.append(f"Channel: #{channel.channel_name}")
            
            message_parts.append("")
            
            for i, todo in enumerate(overdue_todos, 1):
                days_overdue = (timezone.now() - todo.due_date).days
                
                todo_line = f"{i}. 🚨 **{todo.title}**"
                todo_line += f" | Due: {todo.due_date.strftime('%m/%d %H:%M')}"
                todo_line += f" | **{days_overdue} days overdue**"
                
                if todo.assigned_to_username:
                    todo_line += f" | @{todo.assigned_to_username}"
                
                if not channel_id:
                    todo_line += f" | #{todo.channel.channel_name}"
                
                message_parts.append(todo_line)
            
            message_parts.append(f"\n📊 Total overdue: {len(overdue_todos)}")
            
            return True, "\n".join(message_parts), list(overdue_todos)
            
        except Exception as e:
            logger.error(f"Error getting overdue todos: {str(e)}")
            return False, f"❌ Failed to get overdue todos: {str(e)}", []
    
    def _find_todo(self, channel_id: str, identifier: str) -> Optional[ChannelTodo]:
        """
        Find a todo by ID or partial title match
        
        Args:
            channel_id: Slack channel ID
            identifier: Todo ID or partial title
            
        Returns:
            ChannelTodo object if found, None otherwise
        """
        try:
            channel = SlackChannel.objects.filter(channel_id=channel_id).first()
            if not channel:
                return None
            
            # Try to find by ID first
            if identifier.isdigit():
                todos = list(ChannelTodo.objects.filter(
                    channel=channel,
                    status__in=['pending', 'in_progress', 'completed']
                ).order_by('-priority', '-created_at'))
                
                todo_index = int(identifier) - 1  # Convert to 0-based index
                if 0 <= todo_index < len(todos):
                    return todos[todo_index]
            
            # Try to find by title match
            todos = ChannelTodo.objects.filter(
                channel=channel,
                title__icontains=identifier,
                status__in=['pending', 'in_progress', 'completed']
            ).order_by('-priority', '-created_at')
            
            return todos.first()
            
        except Exception as e:
            logger.error(f"Error finding todo: {str(e)}")
            return None
    
    def _resolve_user(self, username_or_id: str) -> Optional[Dict[str, str]]:
        """
        Resolve username to user ID using Slack API
        
        Args:
            username_or_id: Username or user ID
            
        Returns:
            Dictionary with user_id and username if found
        """
        try:
            # If it looks like a user ID, try to get user info
            if username_or_id.startswith('U'):
                response = self.client.users_info(user=username_or_id)
                if response['ok']:
                    user = response['user']
                    return {
                        'user_id': user['id'],
                        'username': user.get('name', user.get('real_name', username_or_id))
                    }
            
            # Try to find user by username
            response = self.client.users_list()
            if response['ok']:
                for user in response['members']:
                    if (user.get('name', '').lower() == username_or_id.lower() or
                        user.get('real_name', '').lower() == username_or_id.lower()):
                        return {
                            'user_id': user['id'],
                            'username': user.get('name', user.get('real_name', username_or_id))
                        }
            
            # Return as-is if not found
            return {
                'user_id': username_or_id,
                'username': username_or_id
            }
            
        except SlackApiError as e:
            logger.warning(f"Error resolving user {username_or_id}: {e}")
            return {
                'user_id': username_or_id,
                'username': username_or_id
            }
    
    def _parse_due_date(self, date_string: str) -> Optional[datetime]:
        """
        Parse due date string into datetime object
        
        Args:
            date_string: Date string like "tomorrow", "friday", "2024-01-15"
            
        Returns:
            datetime object if parsed successfully
        """
        try:
            # Use the task detector's date parsing logic
            return self.task_detector._extract_due_date(f"due {date_string}")
        except Exception as e:
            logger.error(f"Error parsing due date '{date_string}': {str(e)}")
            return None
    
    def _create_reminder(self, todo: ChannelTodo, due_date: datetime):
        """
        Create reminder for a todo based on due date
        
        Args:
            todo: ChannelTodo object
            due_date: Due date for the todo
        """
        try:
            # Delete existing reminders for this todo
            TaskReminder.objects.filter(todo=todo).delete()
            
            # Create due soon reminder (1 hour before)
            reminder_time = due_date - timedelta(hours=1)
            if reminder_time > timezone.now():
                TaskReminder.objects.create(
                    todo=todo,
                    reminder_type='due_soon',
                    reminder_time=reminder_time
                )
            
            # Create overdue reminder (1 hour after due)
            overdue_time = due_date + timedelta(hours=1)
            TaskReminder.objects.create(
                todo=todo,
                reminder_type='overdue',
                reminder_time=overdue_time
            )
            
        except Exception as e:
            logger.error(f"Error creating reminder: {str(e)}")
    
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