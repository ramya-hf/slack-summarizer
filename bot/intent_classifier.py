"""
Natural Language Understanding and Intent Classification for Slack Bot
"""
import re
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import google.generativeai as genai
from django.conf import settings

logger = logging.getLogger(__name__)


class IntentClassifier:
    """
    Classifies user intents and extracts parameters from natural language messages
    """
    
    def __init__(self):
        """Initialize the intent classifier"""
        if settings.GEMINI_API_KEY:
            genai.configure(api_key=settings.GEMINI_API_KEY)
            self.model = genai.GenerativeModel('gemini-1.5-flash')
        else:
            self.model = None
            logger.warning("GEMINI_API_KEY not configured, using rule-based classification only")
    
    def classify_intent(self, message: str, user_id: str = None) -> Dict:
        """
        Classify the intent of a user message
        
        Args:
            message: User's message text
            user_id: User ID for context
            
        Returns:
            Dictionary with intent, confidence, and extracted parameters
        """
        message_lower = message.lower().strip()
        
        # First try rule-based classification for common patterns
        rule_result = self._rule_based_classification(message_lower)
        
        if rule_result['confidence'] > 0.8:
            return rule_result
        
        # If rule-based classification is uncertain, use AI
        if self.model:
            ai_result = self._ai_classification(message)
            # Combine rule-based and AI results, preferring higher confidence
            if ai_result['confidence'] > rule_result['confidence']:
                return ai_result
        
        return rule_result
    
    def _rule_based_classification(self, message: str) -> Dict:
        """
        Rule-based intent classification using patterns
        
        Args:
            message: Lowercase message text
            
        Returns:
            Classification result dictionary
        """
        # Summary request patterns
        summary_patterns = [
            r'what.*happening.*in\s+(\w+)',
            r'summary.*of\s+(\w+)',
            r'summarize\s+(\w+)',
            r'what.*going.*on.*in\s+(\w+)',
            r'catch.*up.*on\s+(\w+)',
            r'update.*from\s+(\w+)',
            r'what.*discussed.*in\s+(\w+)',
            r'recap.*of\s+(\w+)',
            r'brief.*about\s+(\w+)',
            r'overview.*of\s+(\w+)',
        ]
        
        # Check for summary requests
        for pattern in summary_patterns:
            match = re.search(pattern, message)
            if match:
                channel_name = match.group(1)
                timeframe = self._extract_timeframe(message)
                return {
                    'intent': 'summary_request',
                    'confidence': 0.9,
                    'parameters': {
                        'channel_name': channel_name,
                        'timeframe_hours': timeframe,
                        'timeframe_text': self._hours_to_text(timeframe)
                    }
                }
        
        # General summary patterns (current channel)
        general_summary_patterns = [
            r'what.*happening',
            r'catch.*up',
            r'summary',
            r'what.*missed',
            r'update.*me',
            r'brief.*me',
            r'recap',
            r'overview',
        ]
        
        for pattern in general_summary_patterns:
            if re.search(pattern, message):
                timeframe = self._extract_timeframe(message)
                return {
                    'intent': 'summary_request',
                    'confidence': 0.7,
                    'parameters': {
                        'channel_name': None,  # Current channel
                        'timeframe_hours': timeframe,
                        'timeframe_text': self._hours_to_text(timeframe)
                    }
                }
        
        # Help request patterns
        help_patterns = [
            r'help',
            r'how.*use',
            r'what.*can.*do',
            r'commands',
            r'features',
            r'instructions',
        ]
        
        for pattern in help_patterns:
            if re.search(pattern, message):
                return {
                    'intent': 'help_request',
                    'confidence': 0.9,
                    'parameters': {}
                }
        
        # Greeting patterns
        greeting_patterns = [
            r'^(hi|hello|hey|good morning|good afternoon|good evening)',
            r'thanks?$',
            r'thank you',
        ]
        
        for pattern in greeting_patterns:
            if re.search(pattern, message):
                return {
                    'intent': 'greeting',
                    'confidence': 0.8,
                    'parameters': {}
                }
        
        # Status check patterns
        status_patterns = [
            r'status',
            r'health',
            r'working',
            r'alive',
            r'online',
        ]
        
        for pattern in status_patterns:
            if re.search(pattern, message):
                return {
                    'intent': 'status_check',
                    'confidence': 0.8,
                    'parameters': {}
                }
        
        # Default to general chat
        return {
            'intent': 'general_chat',
            'confidence': 0.5,
            'parameters': {}
        }
    
    def _ai_classification(self, message: str) -> Dict:
        """
        AI-powered intent classification using Gemini
        
        Args:
            message: Original message text
            
        Returns:
            Classification result dictionary
        """
        try:
            prompt = f"""
            Analyze this Slack message and classify its intent. The user might be asking about:
            1. Channel summaries (what's happening in a channel, catch up, etc.)
            2. Help/instructions about the bot
            3. General conversation/greeting
            4. Status check of the bot
            
            Message: "{message}"
            
            Respond with a JSON object containing:
            {{
                "intent": "summary_request|help_request|greeting|status_check|general_chat",
                "confidence": 0.0-1.0,
                "channel_name": "extracted channel name or null",
                "timeframe_hours": 24,
                "reasoning": "brief explanation"
            }}
            
            For summary requests, extract:
            - Channel name if mentioned (without #)
            - Timeframe (convert to hours: "2 days" = 48, "1 week" = 168, default = 24)
            
            Examples:
            - "What's happening in social?" → {{"intent": "summary_request", "confidence": 0.9, "channel_name": "social", "timeframe_hours": 24}}
            - "Catch up on general for 2 days" → {{"intent": "summary_request", "confidence": 0.9, "channel_name": "general", "timeframe_hours": 48}}
            - "Help me" → {{"intent": "help_request", "confidence": 0.9, "channel_name": null, "timeframe_hours": 24}}
            """
            
            response = self.model.generate_content(prompt)
            
            if response.text:
                # Extract JSON from response
                import json
                try:
                    # Try to find JSON in the response
                    json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
                    if json_match:
                        result = json.loads(json_match.group())
                        
                        return {
                            'intent': result.get('intent', 'general_chat'),
                            'confidence': float(result.get('confidence', 0.5)),
                            'parameters': {
                                'channel_name': result.get('channel_name'),
                                'timeframe_hours': int(result.get('timeframe_hours', 24)),
                                'timeframe_text': self._hours_to_text(int(result.get('timeframe_hours', 24))),
                                'ai_reasoning': result.get('reasoning', '')
                            }
                        }
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.error(f"Error parsing AI classification result: {e}")
            
        except Exception as e:
            logger.error(f"Error in AI classification: {e}")
        
        # Fallback to rule-based
        return self._rule_based_classification(message.lower())
    
    def _extract_timeframe(self, message: str) -> int:
        """
        Extract timeframe from message and convert to hours
        
        Args:
            message: Message text
            
        Returns:
            Number of hours
        """
        # Patterns for different timeframes
        timeframe_patterns = [
            (r'(\d+)\s*days?', lambda x: int(x) * 24),
            (r'(\d+)\s*weeks?', lambda x: int(x) * 24 * 7),
            (r'(\d+)\s*hours?', lambda x: int(x)),
            (r'yesterday', lambda x: 24),
            (r'today', lambda x: 24),
            (r'last\s*week', lambda x: 168),
            (r'this\s*week', lambda x: 168),
        ]
        
        for pattern, converter in timeframe_patterns:
            match = re.search(pattern, message)
            if match:
                try:
                    if pattern in ['yesterday', 'today', 'last\\s*week', 'this\\s*week']:
                        return converter(None)
                    else:
                        return converter(match.group(1))
                except (ValueError, IndexError):
                    continue
        
        return 24  # Default to 24 hours
    
    def _hours_to_text(self, hours: int) -> str:
        """Convert hours to human-readable text"""
        if hours <= 24:
            return f"Last {hours} hours" if hours != 24 else "Last 24 hours"
        elif hours <= 168:
            days = hours // 24
            return f"Last {days} days" if days != 1 else "Last day"
        else:
            weeks = hours // 168
            return f"Last {weeks} weeks" if weeks != 1 else "Last week"
    
    def extract_channel_mentions(self, message: str) -> List[str]:
        """
        Extract channel mentions from message
        
        Args:
            message: Message text
            
        Returns:
            List of channel names
        """
        # Pattern for #channel mentions
        channel_pattern = r'#(\w+)'
        channels = re.findall(channel_pattern, message)
        
        # Pattern for "in channel" mentions
        in_pattern = r'in\s+(\w+)'
        in_channels = re.findall(in_pattern, message.lower())
        
        return list(set(channels + in_channels))


class ChatbotResponder:
    """
    Generate contextual responses for different types of interactions
    """
    
    def generate_help_response(self) -> str:
        """Generate help response"""
        return """Here's what I can help you with:

🔹 **Channel Summaries**: I can summarize channel conversations
   • `/summary` - Summarize current channel (last 24 hours)
   • `/summary [channel-name]` - Summarize specific channel
   • `/summary unread` - Summarize unread messages in current channel
   • `/summary unread [channel-name]` - Summarize unread messages in specific channel

🔹 **Natural Language**: Just mention me or DM me!
   • "Can you summarize #general from the last 2 hours?"
   • "What happened in #engineering today?"
   • "Show me unread messages summary"

🔹 **Follow-up Questions**: Ask me anything about the summaries I provide

Need more help? Just ask me anything! 😊"""
    
    def generate_greeting_response(self, message: str) -> str:
        """Generate greeting response"""
        greetings = [
            "Hello! 👋 I'm your Slack summary bot. I can help you catch up on channel conversations!",
            "Hi there! 😊 Ready to help you summarize channel activity. What would you like to know?",
            "Hey! 🙂 I'm here to help you stay on top of your Slack conversations. Try asking for a summary!"
        ]
        
        import random
        return random.choice(greetings)
    
    def generate_status_response(self) -> str:
        """Generate status response"""
        return "🟢 I'm online and ready to help! I can summarize channels, answer questions about summaries, and help you catch up on conversations. What can I do for you?"
    
    def generate_general_chat_response(self, message: str) -> str:
        """Generate general chat response"""
        responses = [
            "I'm a summary bot, so I'm best at helping with channel summaries and conversation analysis. Try asking me to summarize a channel!",
            "That's interesting! I specialize in helping you catch up on Slack conversations. Would you like me to summarize any channels for you?",
            "I'm here to help with channel summaries and keeping you updated on conversations. What would you like to know about?",
            "Thanks for chatting! I'm most useful for summarizing channel activity. Try '/summary' or just ask me to summarize a channel!"
        ]
        
        import random
        return random.choice(responses)
    
    def generate_followup_response(self, question: str, summary_text: str, channel_name: str) -> str:
        """Generate response to follow-up questions about summaries"""
        question_lower = question.lower()
        
        if any(word in question_lower for word in ['who', 'participants', 'people', 'users']):
            return f"Based on the summary of #{channel_name}, I can see various participants were involved in the conversations. For specific user details, you might want to check the channel directly or ask for a more detailed breakdown."
        
        elif any(word in question_lower for word in ['when', 'time', 'timestamp']):
            return f"The summary covers recent activity in #{channel_name}. For specific timestamps, you'd need to check the channel directly as summaries focus on content rather than exact timing."
        
        elif any(word in question_lower for word in ['what', 'details', 'more', 'elaborate', 'explain']):
            return f"The summary I provided covers the main topics and activities in #{channel_name}. For more specific details about any particular topic mentioned, I'd recommend checking the channel directly or asking about a specific aspect you're interested in."
        
        elif any(word in question_lower for word in ['how', 'why']):
            return f"Based on the summary of #{channel_name}, I've captured the key discussions and activities. For deeper context about how or why specific things happened, you might want to review the actual channel messages."
        
        else:
            return f"I'd be happy to help clarify anything about the #{channel_name} summary! Could you be more specific about what aspect you'd like to know more about?"
    
    def generate_chat_followup_response(self, question: str, last_message: str, last_response: str) -> str:
        """Generate response to follow-up questions in general chat"""
        return "I remember our conversation! However, I'm most helpful with channel summaries and conversation analysis. Is there a specific channel you'd like me to summarize or analyze?"
