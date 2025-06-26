from django.db import models
from django.utils import timezone
import json


class SlackWorkspace(models.Model):
    """Model to store Slack workspace information"""
    workspace_id = models.CharField(max_length=100, unique=True)
    workspace_name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.workspace_name} ({self.workspace_id})"


class SlackChannel(models.Model):
    """Model to store Slack channel information"""
    workspace = models.ForeignKey(SlackWorkspace, on_delete=models.CASCADE)
    channel_id = models.CharField(max_length=100)
    channel_name = models.CharField(max_length=200)
    is_private = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ('workspace', 'channel_id')
    
    def __str__(self):
        return f"#{self.channel_name} ({self.channel_id})"


class ChannelSummary(models.Model):
    """Model to store channel summaries"""
    channel = models.ForeignKey(SlackChannel, on_delete=models.CASCADE)
    summary_text = models.TextField()
    messages_count = models.IntegerField()
    timeframe = models.CharField(max_length=100, default="Last 24 hours")
    timeframe_hours = models.IntegerField(default=24)  # Store actual hours for queries
    summary_type = models.CharField(max_length=50, default="regular", choices=[
        ('regular', 'Regular Time-based Summary'),
        ('unread', 'Unread Messages Summary'),
    ])  # Track type of summary
    requested_by_user = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Summary for #{self.channel.channel_name} at {self.created_at}"


class ConversationContext(models.Model):
    """Model to store conversation context for follow-up questions"""
    user_id = models.CharField(max_length=100)
    channel_id = models.CharField(max_length=100)
    thread_ts = models.CharField(max_length=50, null=True, blank=True)
    context_type = models.CharField(max_length=50)  # 'summary', 'followup', 'chat', etc.
    context_data = models.TextField()  # JSON data
    last_summary = models.ForeignKey(ChannelSummary, on_delete=models.CASCADE, null=True, blank=True)
    last_interaction_type = models.CharField(max_length=50, default='summary')  # Track interaction type
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-updated_at']
    
    def set_context_data(self, data):
        self.context_data = json.dumps(data)
    
    def get_context_data(self):
        return json.loads(self.context_data) if self.context_data else {}
    
    def __str__(self):
        return f"Context for {self.user_id} in {self.channel_id}"


class BotCommand(models.Model):
    """Model to track bot commands and usage"""
    command = models.CharField(max_length=100)
    user_id = models.CharField(max_length=100)
    channel_id = models.CharField(max_length=100)
    parameters = models.TextField(blank=True)
    status = models.CharField(max_length=50, choices=[
        ('initiated', 'Initiated'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ], default='initiated')
    error_message = models.TextField(blank=True)
    execution_time = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.command} by {self.user_id} - {self.status}"


class ChatbotInteraction(models.Model):
    """Model to track all chatbot interactions including natural language"""
    user_id = models.CharField(max_length=100)
    channel_id = models.CharField(max_length=100)
    message_type = models.CharField(max_length=50, choices=[
        ('natural_language', 'Natural Language'),
        ('slash_command', 'Slash Command'),
        ('followup', 'Follow-up Question'),
        ('general_chat', 'General Chat'),
        ('intent_summary', 'Intent: Summary Request'),
        ('intent_help', 'Intent: Help Request'),
        ('intent_status', 'Intent: Status Check'),
    ])
    user_message = models.TextField()
    bot_response = models.TextField()
    intent_classified = models.CharField(max_length=100, blank=True)
    confidence_score = models.FloatField(null=True, blank=True)
    processing_time = models.FloatField(null=True, blank=True)
    extracted_parameters = models.TextField(blank=True)  # JSON for extracted params
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def set_extracted_parameters(self, params):
        self.extracted_parameters = json.dumps(params)
    
    def get_extracted_parameters(self):
        return json.loads(self.extracted_parameters) if self.extracted_parameters else {}
    
    def __str__(self):
        return f"{self.message_type} - {self.user_id} at {self.created_at}"


class UserReadStatus(models.Model):
    """Model to track user read status for channels"""
    user_id = models.CharField(max_length=100)
    channel_id = models.CharField(max_length=100)
    last_read_ts = models.CharField(max_length=50)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ('user_id', 'channel_id')
        ordering = ['-updated_at']
    
    def __str__(self):
        return f"{self.user_id} in {self.channel_id}"
