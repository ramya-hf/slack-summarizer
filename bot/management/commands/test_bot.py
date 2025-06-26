"""
Management command to test the Slack bot functionality
"""
from django.core.management.base import BaseCommand
from django.conf import settings
from bot.slack import SlackBotHandler
from bot.summarizer import ChannelSummarizer
import json


class Command(BaseCommand):
    help = 'Test the Slack bot functionality'

    def add_arguments(self, parser):
        parser.add_argument(
            '--test-type',
            type=str,
            choices=['config', 'slack', 'ai', 'all'],
            default='all',
            help='Type of test to run'
        )

    def handle(self, *args, **options):
        test_type = options['test_type']
        
        self.stdout.write(self.style.SUCCESS('🤖 Starting Slack Bot Tests\n'))
        
        if test_type in ['config', 'all']:
            self.test_configuration()
        
        if test_type in ['slack', 'all']:
            self.test_slack_connection()
        
        if test_type in ['ai', 'all']:
            self.test_ai_functionality()
        
        self.stdout.write(self.style.SUCCESS('\n✅ All tests completed!'))

    def test_configuration(self):
        """Test configuration settings"""
        self.stdout.write(self.style.WARNING('Testing Configuration...'))
        
        config_checks = {
            'SLACK_BOT_TOKEN': bool(settings.SLACK_BOT_TOKEN),
            'SLACK_APP_TOKEN': bool(settings.SLACK_APP_TOKEN),
            'SLACK_SIGNING_SECRET': bool(settings.SLACK_SIGNING_SECRET),
            'GEMINI_API_KEY': bool(settings.GEMINI_API_KEY),
            'DEBUG': settings.DEBUG,
        }
        
        for key, value in config_checks.items():
            status = '✅' if value else '❌'
            self.stdout.write(f'  {status} {key}: {"Configured" if value else "Missing"}')
        
        self.stdout.write('')

    def test_slack_connection(self):
        """Test Slack API connection"""
        self.stdout.write(self.style.WARNING('Testing Slack Connection...'))
        
        try:
            bot_handler = SlackBotHandler()
            self.stdout.write(f'  ✅ Bot User ID: {bot_handler.bot_user_id}')
            
            # Test basic API call
            response = bot_handler.client.auth_test()
            self.stdout.write(f'  ✅ Team: {response.get("team")}')
            self.stdout.write(f'  ✅ User: {response.get("user")}')
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'  ❌ Slack connection failed: {str(e)}'))
        
        self.stdout.write('')

    def test_ai_functionality(self):
        """Test AI summarization functionality"""
        self.stdout.write(self.style.WARNING('Testing AI Functionality...'))
        
        try:
            summarizer = ChannelSummarizer()
            
            # Test with sample messages
            sample_messages = [
                {
                    'ts': '1640995200.000100',
                    'user': 'U12345',
                    'text': 'We need to discuss the project timeline for Q1.'
                },
                {
                    'ts': '1640995260.000200', 
                    'user': 'U67890',
                    'text': 'I think we should focus on the MVP features first.'
                },
                {
                    'ts': '1640995320.000300',
                    'user': 'U12345', 
                    'text': 'Good point. What about the deadline for the design phase?'
                }
            ]
            
            summary = summarizer.generate_summary(sample_messages, 'test-channel')
            
            if summary and 'Summary Report' in summary:
                self.stdout.write('  ✅ AI summarization working')
                self.stdout.write('  📝 Sample summary generated:')
                # Show first few lines of summary
                lines = summary.split('\n')[:5]
                for line in lines:
                    self.stdout.write(f'     {line}')
                self.stdout.write('     ...')
            else:
                self.stdout.write(self.style.ERROR('  ❌ AI summarization failed'))
                
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'  ❌ AI test failed: {str(e)}'))
        
        self.stdout.write('')
