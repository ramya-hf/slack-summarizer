"""
Task Detection Module
Uses AI to analyze Slack messages and identify actionable tasks, bugs, deadlines, and todo items
"""
import json
import logging
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

import google.generativeai as genai
from django.conf import settings
from django.utils import timezone

from .models import ChannelTodo, SlackChannel

logger = logging.getLogger(__name__)


@dataclass
class DetectedTask:
    """Data class for detected tasks"""
    title: str
    description: str
    task_type: str
    priority: str
    assigned_to: str = ""
    assigned_to_username: str = ""
    due_date: Optional[datetime] = None
    confidence_score: float = 0.0
    original_message: str = ""
    message_timestamp: str = ""
    message_link: str = ""


class TaskDetector:
    """
    AI-powered task detection from Slack messages
    """
    
    def __init__(self):
        """Initialize the task detector with AI configuration"""
        if not settings.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY not found in settings")
        
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        
        # Task detection patterns and keywords
        self.task_keywords = {
            'bug': ['bug', 'error', 'issue', 'broken', 'fix', 'crash', 'problem', 'not working'],
            'feature': ['feature', 'add', 'implement', 'create', 'build', 'develop', 'new'],
            'meeting': ['meeting', 'standup', 'call', 'sync', 'demo', 'review meeting'],
            'review': ['review', 'PR', 'pull request', 'code review', 'check', 'approve'],
            'deadline': ['deadline', 'due', 'by', 'urgent', 'asap', 'today', 'tomorrow', 'friday'],
            'urgent': ['urgent', 'critical', 'emergency', 'immediately', 'asap', 'high priority'],
            'general': ['todo', 'task', 'need to', 'should', 'must', 'have to', 'remember to']
        }
        
        self.priority_keywords = {
            'critical': ['critical', 'emergency', 'urgent', 'asap', 'immediately', 'breaking'],
            'high': ['high priority', 'important', 'urgent', 'soon', 'today'],
            'medium': ['medium', 'normal', 'standard', 'regular'],
            'low': ['low priority', 'later', 'when possible', 'nice to have', 'optional']
        }
        
        self.assignment_patterns = [
            r'@(\w+)',  # @username
            r'(\w+)\s+(?:please|can you|could you)',  # "john please" or "can you sarah"
            r'assign(?:ed)?\s+to\s+(\w+)',  # "assigned to john"
            r'(\w+)\s+should\s+(?:handle|do|take)',  # "john should handle"
        ]
        
        self.time_patterns = [
            r'(?:by|due|deadline)\s+(\w+day|\d{1,2}/\d{1,2}|\d{1,2}:\d{2})',
            r'(?:today|tomorrow|next week|this week)',
            r'\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)',
            r'(?:morning|afternoon|evening|tonight)',
        ]
    
    def analyze_message(self, message: str, channel_name: str = "", user_id: str = "", timestamp: str = "") -> Optional[DetectedTask]:
        """
        Analyze a single message for task content
        
        Args:
            message: The message text to analyze
            channel_name: Name of the channel
            user_id: User ID who sent the message
            timestamp: Message timestamp
            
        Returns:
            DetectedTask if task is detected, None otherwise
        """
        try:
            # Quick pre-filtering to avoid unnecessary AI calls
            if not self._is_potentially_task_related(message):
                return None
            
            # Use AI to analyze the message
            task_data = self._ai_analyze_message(message, channel_name)
            
            if not task_data or not task_data.get('is_task', False):
                return None
            
            # Extract additional details using patterns
            assigned_user = self._extract_assigned_user(message)
            due_date = self._extract_due_date(message)
            
            # Create detected task
            detected_task = DetectedTask(
                title=task_data.get('title', message[:100]),
                description=task_data.get('description', message),
                task_type=task_data.get('task_type', 'general'),
                priority=task_data.get('priority', 'medium'),
                assigned_to=assigned_user.get('user_id', ''),
                assigned_to_username=assigned_user.get('username', ''),
                due_date=due_date,
                confidence_score=task_data.get('confidence', 0.0),
                original_message=message,
                message_timestamp=timestamp,
                message_link=""  # Will be set by caller if needed
            )
            
            return detected_task
            
        except Exception as e:
            logger.error(f"Error analyzing message for tasks: {str(e)}")
            return None
    
    def batch_analyze_messages(self, messages: List[Dict], channel_name: str = "") -> List[DetectedTask]:
        """
        Analyze multiple messages for tasks
        
        Args:
            messages: List of message dictionaries
            channel_name: Name of the channel
            
        Returns:
            List of detected tasks
        """
        detected_tasks = []
        
        for message_data in messages:
            message_text = message_data.get('text', '')
            user_id = message_data.get('user', '')
            timestamp = message_data.get('ts', '')
            
            if not message_text or len(message_text.strip()) < 10:
                continue
            
            detected_task = self.analyze_message(
                message=message_text,
                channel_name=channel_name,
                user_id=user_id,
                timestamp=timestamp
            )
            
            if detected_task:
                detected_tasks.append(detected_task)
        
        return detected_tasks
    
    def _is_potentially_task_related(self, message: str) -> bool:
        """
        Quick check if message might contain task-related content
        
        Args:
            message: Message text to check
            
        Returns:
            True if message might contain tasks
        """
        message_lower = message.lower()
        
        # Check for task indicators
        task_indicators = [
            # Action words
            'fix', 'add', 'create', 'implement', 'build', 'develop', 'update', 'change',
            'review', 'check', 'test', 'deploy', 'merge', 'approve', 'investigate',
            'schedule', 'plan', 'organize', 'prepare', 'setup', 'configure',
            
            # Task-related phrases
            'need to', 'should', 'must', 'have to', 'todo', 'task', 'action item',
            'follow up', 'make sure', 'don\'t forget', 'remember to',
            
            # Issue indicators
            'bug', 'error', 'issue', 'problem', 'broken', 'not working',
            
            # Time indicators
            'deadline', 'due', 'by', 'urgent', 'asap', 'today', 'tomorrow',
            'this week', 'next week', 'monday', 'friday',
            
            # Assignment indicators
            '@', 'assign', 'responsible', 'owner', 'please', 'can you', 'could you'
        ]
        
        # Check if message contains any task indicators
        return any(indicator in message_lower for indicator in task_indicators)
    
    def _ai_analyze_message(self, message: str, channel_name: str = "") -> Dict:
        """
        Use AI to analyze message for task content
        
        Args:
            message: Message to analyze
            channel_name: Channel context
            
        Returns:
            Dictionary with task analysis results
        """
        try:
            prompt = f"""
            Analyze this Slack message from #{channel_name} and determine if it contains an actionable task, bug report, or todo item.

            Message: "{message}"

            Please respond with a JSON object containing:
            {{
                "is_task": boolean,  // true if this message contains an actionable item
                "confidence": float,  // confidence score 0.0-1.0
                "title": "string",   // short title for the task (max 100 chars)
                "description": "string",  // detailed description
                "task_type": "string",    // one of: bug, feature, meeting, review, deadline, urgent, general
                "priority": "string",     // one of: low, medium, high, critical
                "reasoning": "string"     // why you classified it this way
            }}

            Task types:
            - bug: Bug reports, errors, broken functionality
            - feature: New features, enhancements, development requests
            - meeting: Meetings, calls, scheduled events
            - review: Code reviews, document reviews, approvals needed
            - deadline: Items with specific deadlines or time constraints
            - urgent: Urgent or critical items needing immediate attention
            - general: General tasks, todos, or action items

            Priority levels:
            - critical: Urgent, breaking issues, immediate action required
            - high: Important items, should be done soon
            - medium: Standard priority items
            - low: Nice to have, can be done later

            Only mark as task if the message clearly indicates something actionable that needs to be done.
            Questions, discussions, or informational messages should not be marked as tasks.
            """
            
            response = self.model.generate_content(prompt)
            
            if not response or not response.text:
                return {}
            
            # Try to parse JSON response
            try:
                # Clean up the response text
                response_text = response.text.strip()
                
                # Extract JSON from response (handle cases where AI adds extra text)
                if '```json' in response_text:
                    start = response_text.find('```json') + 7
                    end = response_text.find('```', start)
                    response_text = response_text[start:end].strip()
                elif '{' in response_text and '}' in response_text:
                    start = response_text.find('{')
                    end = response_text.rfind('}') + 1
                    response_text = response_text[start:end]
                
                task_data = json.loads(response_text)
                
                # Validate required fields
                if not isinstance(task_data.get('is_task'), bool):
                    return {}
                
                return task_data
                
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse AI response as JSON: {str(e)}")
                logger.warning(f"AI Response: {response.text}")
                return {}
            
        except Exception as e:
            logger.error(f"Error in AI message analysis: {str(e)}")
            return {}
    
    def _extract_assigned_user(self, message: str) -> Dict[str, str]:
        """
        Extract assigned user from message using patterns
        
        Args:
            message: Message text
            
        Returns:
            Dictionary with user_id and username
        """
        for pattern in self.assignment_patterns:
            matches = re.findall(pattern, message, re.IGNORECASE)
            if matches:
                username = matches[0].strip()
                # Remove @ if present
                if username.startswith('@'):
                    username = username[1:]
                
                return {
                    'username': username,
                    'user_id': ''  # Will need to be resolved by caller
                }
        
        return {'username': '', 'user_id': ''}
    
    def _extract_due_date(self, message: str) -> Optional[datetime]:
        """
        Extract due date from message using patterns
        
        Args:
            message: Message text
            
        Returns:
            datetime object if due date found, None otherwise
        """
        message_lower = message.lower()
        now = timezone.now()
        
        # Simple date extractions
        if 'today' in message_lower:
            return now.replace(hour=17, minute=0, second=0, microsecond=0)  # 5 PM today
        elif 'tomorrow' in message_lower:
            return (now + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
        elif 'this week' in message_lower or 'end of week' in message_lower:
            days_until_friday = 4 - now.weekday()  # Friday is 4
            if days_until_friday <= 0:
                days_until_friday += 7  # Next Friday
            return (now + timedelta(days=days_until_friday)).replace(hour=17, minute=0, second=0, microsecond=0)
        elif 'next week' in message_lower:
            days_until_next_monday = 7 - now.weekday()  # Monday is 0
            return (now + timedelta(days=days_until_next_monday)).replace(hour=9, minute=0, second=0, microsecond=0)
        elif 'monday' in message_lower:
            days_until_monday = (7 - now.weekday()) % 7
            if days_until_monday == 0:  # Today is Monday
                days_until_monday = 7  # Next Monday
            return (now + timedelta(days=days_until_monday)).replace(hour=9, minute=0, second=0, microsecond=0)
        elif 'friday' in message_lower:
            days_until_friday = (4 - now.weekday()) % 7
            if days_until_friday == 0:  # Today is Friday
                days_until_friday = 7  # Next Friday
            return (now + timedelta(days=days_until_friday)).replace(hour=17, minute=0, second=0, microsecond=0)
        
        # Try to extract specific times
        time_matches = re.findall(r'(\d{1,2}):(\d{2})\s*(am|pm|AM|PM)?', message)
        if time_matches:
            try:
                hour, minute, meridiem = time_matches[0]
                hour = int(hour)
                minute = int(minute)
                
                if meridiem and meridiem.lower() == 'pm' and hour != 12:
                    hour += 12
                elif meridiem and meridiem.lower() == 'am' and hour == 12:
                    hour = 0
                
                # Assume today if just time is given
                due_date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                
                # If time has passed today, assume tomorrow
                if due_date <= now:
                    due_date += timedelta(days=1)
                
                return due_date
                
            except (ValueError, IndexError):
                pass
        
        return None
    
    def create_todo_from_detection(self, detected_task: DetectedTask, channel: SlackChannel, created_by: str) -> ChannelTodo:
        """
        Create a ChannelTodo from a DetectedTask
        
        Args:
            detected_task: The detected task data
            channel: SlackChannel instance
            created_by: User ID who created the task
            
        Returns:
            Created ChannelTodo instance
        """
        todo = ChannelTodo.objects.create(
            channel=channel,
            title=detected_task.title,
            description=detected_task.description,
            task_type=detected_task.task_type,
            priority=detected_task.priority,
            assigned_to=detected_task.assigned_to,
            assigned_to_username=detected_task.assigned_to_username,
            due_date=detected_task.due_date,
            created_from_message=detected_task.message_timestamp,
            created_from_message_link=detected_task.message_link,
            created_by=created_by,
            status='pending'
        )
        
        logger.info(f"Created todo from message: {todo.title} in #{channel.channel_name}")
        return todo
    
    def get_task_statistics(self, messages: List[Dict]) -> Dict:
        """
        Get statistics about task content in messages
        
        Args:
            messages: List of message dictionaries
            
        Returns:
            Dictionary with task statistics
        """
        total_messages = len(messages)
        task_messages = 0
        task_types = {}
        priorities = {}
        
        for message_data in messages:
            message_text = message_data.get('text', '')
            
            if self._is_potentially_task_related(message_text):
                task_messages += 1
                
                # Quick classification for stats
                for task_type, keywords in self.task_keywords.items():
                    if any(keyword in message_text.lower() for keyword in keywords):
                        task_types[task_type] = task_types.get(task_type, 0) + 1
                        break
                
                for priority, keywords in self.priority_keywords.items():
                    if any(keyword in message_text.lower() for keyword in keywords):
                        priorities[priority] = priorities.get(priority, 0) + 1
                        break
        
        return {
            'total_messages': total_messages,
            'task_related_messages': task_messages,
            'task_percentage': round((task_messages / max(total_messages, 1)) * 100, 1),
            'task_types': task_types,
            'priorities': priorities
        } 