"""
Slack Channel Message Summarizer using Google Gemini AI
"""
import google.generativeai as genai
from datetime import datetime, timedelta
from django.conf import settings
import logging
import re
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class ChannelSummarizer:
    """
    Handles summarization of Slack channel messages using Google Gemini AI
    """
    
    def __init__(self):
        """Initialize the summarizer with Gemini API configuration"""
        if not settings.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY not found in settings")
        
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        
        # Configure generation parameters for consistent output
        self.generation_config = genai.types.GenerationConfig(
            temperature=0.3,  # Lower temperature for more consistent formatting
            top_p=0.8,
            top_k=40,
            max_output_tokens=2048,
        )
    
    def format_messages_for_analysis(self, messages: List[Dict]) -> str:
        """
        Format Slack messages for AI analysis
        
        Args:
            messages: List of message dictionaries from Slack API
            
        Returns:
            Formatted string of messages for analysis
        """
        formatted_messages = []
        
        for message in messages:
            timestamp = datetime.fromtimestamp(float(message.get('ts', 0)))
            user = message.get('user', 'Unknown')
            text = message.get('text', '')
            
            # Clean up Slack formatting
            text = self._clean_slack_formatting(text)
            
            if text.strip():  # Only include non-empty messages
                formatted_message = f"[{timestamp.strftime('%Y-%m-%d %H:%M')}] {user}: {text}"
                formatted_messages.append(formatted_message)
        
        return "\n".join(formatted_messages)
    
    def _clean_slack_formatting(self, text: str) -> str:
        """
        Clean Slack-specific formatting from message text
        
        Args:
            text: Raw message text from Slack
            
        Returns:
            Cleaned text
        """
        # Remove user mentions (<@U123456>)
        text = re.sub(r'<@[UW][A-Z0-9]+>', '@user', text)
        
        # Remove channel mentions (<#C123456|channel-name>)
        text = re.sub(r'<#[A-Z0-9]+\|([^>]+)>', r'#\1', text)
        
        # Remove links (<http://example.com|Example>)
        text = re.sub(r'<(https?://[^|>]+)\|([^>]+)>', r'\2 (\1)', text)
        
        # Remove plain links (<http://example.com>)
        text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
        
        # Remove special formatting
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'&amp;', '&', text)
        
        return text.strip()
    
    def generate_summary(self, messages: List[Dict], channel_name: str = None, timeframe_hours: int = 24) -> str:
        """
        Generate a summary of channel messages using the specified prompt format
        
        Args:
            messages: List of message dictionaries from Slack API
            channel_name: Name of the channel being summarized
            timeframe_hours: Number of hours covered in the summary
            
        Returns:
            Formatted summary string
        """
        if not messages:
            return self._generate_empty_summary(channel_name, timeframe_hours)
        
        formatted_messages = self.format_messages_for_analysis(messages)
        timeframe_text = self._hours_to_timeframe_text(timeframe_hours)
        
        # Use the exact prompt format specified by the user
        prompt = f"""
            Please analyze these Slack messages and provide a summary in EXACTLY this format, with NO DEVIATION:

            Summary Report – #{channel_name or 'channel'}

            Key Topics

            • [First key topic with period.]

            • [Second key topic with period.]

            • [Third key topic with period.]

            Decisions & Actions

            • [First decision/action with period.]

            • [Second decision/action with period.]

            Status & Questions

            • Current Status: [One line status with period.]

            • Open Questions: [Key questions with question marks?]

            Contributors

            • [One line about participant count with period.]

            Needs Immediate Attention 🚨

            • [First urgent item with period.]

            • [Second urgent item with period.]

            Summary Details
            Messages analyzed: {len(messages)}
            Timeframe: {timeframe_text}
            Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}

            CRITICAL FORMATTING RULES:
            1. Use ONLY the bullet character "•" (not emojis, dashes, or asterisks)
            2. Add exactly one line break after each bullet point
            3. End each bullet point with proper punctuation (period or question mark)
            4. Keep section titles EXACTLY as shown (no emojis except 🚨 in "Needs Immediate Attention")
            5. Use exactly two line breaks between sections
            6. Keep all formatting and spacing exactly as shown
            7. Do not add any additional sections or formatting
            8. Do not use any emojis except 🚨 in the "Needs Immediate Attention" section title

            MESSAGES TO ANALYZE:
            {formatted_messages}
            """
        
        try:
            response = self.model.generate_content(
                prompt,
                generation_config=self.generation_config
            )
            
            if response.text:
                return response.text.strip()
            else:
                logger.error("No response text from Gemini API")
                return self._generate_error_summary(channel_name, "No response from AI service", timeframe_hours)
                
        except Exception as e:
            logger.error(f"Error generating summary with Gemini: {str(e)}")
            return self._generate_error_summary(channel_name, str(e), timeframe_hours)
    
    def generate_followup_response(self, question: str, summary_context: str, channel_name: str = None) -> str:
        """
        Generate a response to follow-up questions based on the summary context
        
        Args:
            question: The follow-up question from the user
            summary_context: The previously generated summary
            channel_name: Name of the channel
            
        Returns:
            AI-generated response to the follow-up question
        """
        prompt = f"""
        Based on the following channel summary, please answer this follow-up question clearly and concisely.
        
        CHANNEL SUMMARY:
        {summary_context}
        
        FOLLOW-UP QUESTION:
        {question}
        
        Please provide a helpful response based on the information available in the summary. If the summary doesn't contain enough information to answer the question, please say so politely and suggest what additional information might be needed.
        
        Keep your response conversational and helpful, as if you're a team member who has reviewed the channel messages.
        """
        
        try:
            response = self.model.generate_content(
                prompt,
                generation_config=self.generation_config
            )
            
            if response.text:
                return response.text.strip()
            else:
                return "I'm sorry, I couldn't generate a response to your question. Please try asking again."
                
        except Exception as e:
            logger.error(f"Error generating follow-up response: {str(e)}")
            return "I encountered an error while processing your question. Please try again later."
    
    def generate_unread_summary(self, messages: List[Dict], channel_name: str = None, unread_count: int = 0) -> str:
        """
        Generate a summary of unread channel messages using the specified prompt format
        
        Args:
            messages: List of unread message dictionaries from Slack API
            channel_name: Name of the channel being summarized
            unread_count: Total number of unread messages found
            
        Returns:
            Formatted summary string for unread messages
        """
        if not messages:
            return self._generate_empty_unread_summary(channel_name, unread_count)
        
        formatted_messages = self.format_messages_for_analysis(messages)
        
        # Use the exact prompt format specified by the user
        prompt = f"""
            Please analyze these UNREAD Slack messages and provide a summary in EXACTLY this format, with NO DEVIATION:

            Summary Report – #{channel_name or 'channel'} (Unread Messages)

            Key Topics

            • [First key topic with period.]

            • [Second key topic with period.]

            • [Third key topic with period.]

            Decisions & Actions

            • [First decision/action with period.]

            • [Second decision/action with period.]

            Status & Questions

            • Current Status: [One line status with period.]

            • Open Questions: [Key questions with question marks?]

            Contributors

            • [One line about participant count with period.]

            Needs Immediate Attention 🚨

            • [First urgent item with period.]

            • [Second urgent item with period.]

            Summary Details
            Unread messages analyzed: {len(messages)}
            Total unread count: {unread_count}
            Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}

            CRITICAL FORMATTING RULES:
            1. Use ONLY the bullet character "•" (not emojis, dashes, or asterisks)
            2. Add exactly one line break after each bullet point
            3. End each bullet point with proper punctuation (period or question mark)
            4. Keep section titles EXACTLY as shown (no emojis except 🚨 in "Needs Immediate Attention")
            5. Use exactly two line breaks between sections
            6. Keep all formatting and spacing exactly as shown
            7. Do not add any additional sections or formatting
            8. Do not use any emojis except 🚨 in the "Needs Immediate Attention" section title

            UNREAD MESSAGES TO ANALYZE:
            {formatted_messages}
            """
        
        try:
            response = self.model.generate_content(
                prompt,
                generation_config=self.generation_config
            )
            
            if response.text:
                return response.text.strip()
            else:
                logger.error("No response text from Gemini API for unread summary")
                return self._generate_error_unread_summary(channel_name, "No response from AI service", unread_count)
                
        except Exception as e:
            logger.error(f"Error generating unread summary with Gemini: {str(e)}")
            return self._generate_error_unread_summary(channel_name, str(e), unread_count)

    def _hours_to_timeframe_text(self, hours: int) -> str:
        """Convert hours to human-readable timeframe text"""
        if hours <= 24:
            return f"Last {hours} hours" if hours != 24 else "Last 24 hours"
        elif hours <= 168:  # 7 days
            days = hours // 24
            return f"Last {days} days" if days != 1 else "Last day"
        else:
            weeks = hours // 168
            return f"Last {weeks} weeks" if weeks != 1 else "Last week"
    
    def _generate_empty_summary(self, channel_name: str, timeframe_hours: int = 24) -> str:
        """Generate a summary for when no messages are found"""
        timeframe_text = self._hours_to_timeframe_text(timeframe_hours)
        
        return f"""
Summary Report – #{channel_name or 'channel'}

Key Topics

• No messages found in the specified timeframe.

Decisions & Actions

• No decisions or actions recorded.

Status & Questions

• Current Status: Channel appears to be inactive.

• Open Questions: No questions identified.

Contributors

• No active contributors in the analyzed timeframe.

Needs Immediate Attention 🚨

• No urgent items identified.

Summary Details
Messages analyzed: 0
Timeframe: {timeframe_text}
Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}
        """.strip()
    
    def _generate_empty_unread_summary(self, channel_name: str, unread_count: int = 0) -> str:
        """Generate a summary for when no unread messages are found"""
        return f"""
Summary Report – #{channel_name or 'channel'} (Unread Messages)

Key Topics

• No unread messages found in this channel.

Decisions & Actions

• No decisions or actions in unread messages.

Status & Questions

• Current Status: You're all caught up! No unread messages.

• Open Questions: No questions identified in unread messages.

Contributors

• No contributors in unread messages.

Needs Immediate Attention 🚨

• No urgent items in unread messages.

Summary Details
Unread messages analyzed: 0
Total unread count: {unread_count}
Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}
        """.strip()
    
    def _generate_error_summary(self, channel_name: str, error: str, timeframe_hours: int = 24) -> str:
        """Generate an error summary when AI processing fails"""
        timeframe_text = self._hours_to_timeframe_text(timeframe_hours)
        
        return f"""
Summary Report – #{channel_name or 'channel'}

⚠️ Summary Generation Error

An error occurred while generating the summary: {error}

Please try again later or contact your administrator if the problem persists.

Summary Details
Messages analyzed: Error occurred during processing
Timeframe: {timeframe_text}
Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}
        """.strip()
    
    def _generate_error_unread_summary(self, channel_name: str, error: str, unread_count: int = 0) -> str:
        """Generate an error summary when AI processing fails for unread messages"""
        return f"""
Summary Report – #{channel_name or 'channel'} (Unread Messages)

⚠️ Summary Generation Error

An error occurred while generating the unread summary: {error}

Please try again later or contact your administrator if the problem persists.

Summary Details
Unread messages analyzed: Error occurred during processing
Total unread count: {unread_count}
Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}
        """.strip()


# Utility functions for message filtering and processing
def filter_messages_by_timeframe(messages: List[Dict], hours: int = 24) -> List[Dict]:
    """
    Filter messages to only include those from the specified timeframe
    
    Args:
        messages: List of message dictionaries
        hours: Number of hours to look back (default: 24)
        
    Returns:
        Filtered list of messages
    """
    cutoff_time = datetime.now() - timedelta(hours=hours)
    cutoff_timestamp = cutoff_time.timestamp()
    
    filtered_messages = []
    for message in messages:
        try:
            message_time = float(message.get('ts', 0))
            if message_time >= cutoff_timestamp:
                filtered_messages.append(message)
        except (ValueError, TypeError):
            continue  # Skip messages with invalid timestamps
    
    return filtered_messages


def extract_channel_name_from_command(command_text: str) -> Optional[str]:
    """
    Extract channel name from command text like "/summary channel-name"
    
    Args:
        command_text: The full command text
        
    Returns:
        Channel name if found, None otherwise
    """
    # Remove the command part and get the channel name
    parts = command_text.strip().split()
    if len(parts) >= 2:
        channel_name = parts[1].strip()
        # Remove # if present
        if channel_name.startswith('#'):
            channel_name = channel_name[1:]
        return channel_name
    return None


def extract_unread_command_details(command_text: str) -> Tuple[Optional[str], bool]:
    """
    Extract channel name and unread flag from command text like "/summary unread channel-name" or "/summary unread"
    
    Args:
        command_text: The full command text
        
    Returns:
        Tuple of (channel_name, is_unread_command)
    """
    parts = command_text.strip().split()
    
    # Check if it's an unread command
    if len(parts) >= 2 and parts[1].lower() == 'unread':
        is_unread = True
        
        # Check if channel name is provided
        if len(parts) >= 3:
            channel_name = parts[2].strip()
            # Remove # if present
            if channel_name.startswith('#'):
                channel_name = channel_name[1:]
            return channel_name, is_unread
        else:
            # No channel specified, use current channel
            return None, is_unread
    
    return None, False