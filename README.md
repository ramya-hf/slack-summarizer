# Slack Channel Summarizer Bot

A powerful Django-based Slack bot that uses Google Gemini AI to summarize channel conversations and answer follow-up questions.

## Features

- 🤖 **AI-Powered Summarization**: Uses Google Gemini 1.5 Flash for intelligent message analysis
- 📊 **Structured Summaries**: Provides consistent, well-formatted summaries with key topics, decisions, and action items
- 💬 **Follow-up Questions**: Answer questions about generated summaries using AI
- 🔄 **Multi-Channel Support**: Summarize any channel you have access to
- 📱 **Slash Commands**: Easy-to-use `/summary` command with subcommands
- 🗄️ **Database Storage**: Stores summaries and conversation context for reference
- 🛡️ **Security**: Proper Slack signature verification and environment variable management

## Commands

### `/summary`
Summarize the current channel (last 24 hours)
```
/summary
```

### `/summary [channel-name]`
Summarize a specific channel (last 24 hours)
```
/summary general
/summary dev-team
/summary marketing
```

## Installation & Setup

### 1. Prerequisites
- Python 3.8+
- Django 5.2+
- Slack workspace with admin access
- Google AI Studio account (for Gemini API)

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Environment Configuration
Create a `.env` file in the project root:
```env
DJANGO_SECRET_KEY=your-django-secret-key-here-generate-a-secure-random-string
DEBUG=True
SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
SLACK_APP_TOKEN=xapp-your-slack-app-token  
GEMINI_API_KEY=your-gemini-api-key
SLACK_SIGNING_SECRET=your-slack-signing-secret
```

### 4. Slack App Setup

#### Step 1: Create Slack App
1. Go to [Slack API](https://api.slack.com/apps)
2. Click "Create New App" → "From scratch"
3. Enter app name and select workspace

#### Step 2: Configure OAuth & Permissions
Add these OAuth scopes under "OAuth & Permissions":
- `channels:history` - Read messages from public channels
- `channels:read` - View basic information about public channels
- `chat:write` - Send messages as the bot
- `commands` - Add slash commands
- `groups:history` - Read messages from private channels (if needed)
- `groups:read` - View basic information about private channels (if needed)
- `users:read` - View people in the workspace

#### Step 3: Install App to Workspace
1. Click "Install to Workspace"
2. Copy the "Bot User OAuth Token" (starts with `xoxb-`)

#### Step 4: Configure Slash Commands
1. Go to "Slash Commands" → "Create New Command"
2. Command: `/summary`
3. Request URL: `https://your-domain.com/slack/events/`
4. Short Description: "Summarize channel messages"
5. Usage Hint: `[channel-name]`

#### Step 5: Configure Event Subscriptions
1. Go to "Event Subscriptions" → Enable Events
2. Request URL: `https://your-domain.com/slack/events/`
3. Subscribe to these bot events:
   - `message.channels` - Listen to messages in channels
   - `message.groups` - Listen to messages in private channels (if needed)

#### Step 6: Get Additional Tokens
- **Signing Secret**: Found in "Basic Information" → "App Credentials"
- **App Token**: Create in "Basic Information" → "App-Level Tokens" (needed for Socket Mode if used)

### 5. Google Gemini API Setup
1. Go to [Google AI Studio](https://makersuite.google.com/app/apikey)
2. Create a new API key
3. Add the key to your `.env` file as `GEMINI_API_KEY`

### 6. Database Setup
```bash
python manage.py makemigrations
python manage.py migrate
```

### 7. Test the Setup
```bash
python manage.py test_bot
```

### 8. Run the Server
```bash
python manage.py runserver 0.0.0.0:8000
```

### 9. Expose to Internet (for development)
Use ngrok to expose your local server:
```bash
ngrok http 8000
```
Then update your Slack app's Request URLs with the ngrok URL.

## Usage

### Basic Usage
1. In any Slack channel, type `/summary` to get a summary of the last 24 hours
2. Type `/summary channel-name` to summarize a specific channel
3. Ask follow-up questions about the summary in the same channel

### Example Workflow
```
User: /summary general
Bot: Your summary is getting generated ⏳

Bot: @user Here's your requested summary:

Summary Report – #general

Key Topics
• Project timeline discussions for Q1.
• MVP feature prioritization decisions.
• Design phase deadline clarification.

[... rest of summary ...]

💬 Ask me any follow-up questions about this summary!

User: What were the main concerns about the timeline?
Bot: @user Based on the summary, the main concerns about the timeline were...
```

## Project Structure

```
slackbot/
├── bot/                          # Main bot application
│   ├── models.py                 # Database models
│   ├── views.py                  # Django views for Slack events
│   ├── slack.py                  # Slack API integration
│   ├── summarizer.py             # AI summarization logic
│   ├── urls.py                   # URL routing
│   └── management/commands/      # Management commands
│       └── test_bot.py          # Bot testing command
├── slackbot/                     # Django project settings
│   ├── settings.py              # Main settings
│   ├── urls.py                  # Project URL routing
│   └── wsgi.py                  # WSGI configuration
├── requirements.txt              # Python dependencies
├── manage.py                    # Django management script
└── .env                         # Environment variables
```

## Database Models

### SlackWorkspace
Stores Slack workspace information

### SlackChannel  
Stores channel metadata and settings

### ChannelSummary
Stores generated summaries with metadata

### ConversationContext
Tracks conversation context for follow-up questions

### BotCommand
Logs all bot commands for analytics and debugging

## API Endpoints

### `/slack/events/` (POST)
Main endpoint for Slack events and commands

### `/slack/health/` (GET)
Health check endpoint for monitoring

### `/slack/info/` (GET)
Returns bot information and configuration status

## Security Features

- ✅ Slack signature verification
- ✅ Environment variable protection
- ✅ CSRF protection disabled only for Slack endpoints
- ✅ Input validation and sanitization
- ✅ Error handling and logging

## Customization

### Adding New Commands
1. Add command handling in `slack.py`:
```python
def _handle_your_command(self, payload: Dict, bot_command: BotCommand) -> Dict:
    # Your command logic here
    pass
```

2. Register in `process_slash_command()` method

### Modifying Summary Format
Edit the prompt in `summarizer.py` in the `generate_summary()` method.

### Adding New Event Types
Add event handling in `views.py` in the `handle_event_subscription()` function.

## Troubleshooting

### Common Issues

1. **Bot not responding to commands**
   - Check if the request URL in Slack app settings is correct
   - Verify that ngrok is running (for development)
   - Check Django logs for errors

2. **Permission errors**
   - Ensure all required OAuth scopes are added
   - Reinstall the app to workspace after adding scopes

3. **AI responses not working**
   - Verify GEMINI_API_KEY is correct
   - Check if you have quota remaining on your Gemini account
   - Run `python manage.py test_bot --test-type ai` to test

4. **Signature verification failures**
   - Ensure SLACK_SIGNING_SECRET matches the one in your Slack app
   - Check that timestamps are within acceptable range

### Testing Commands
```bash
# Test everything
python manage.py test_bot

# Test specific components
python manage.py test_bot --test-type config
python manage.py test_bot --test-type slack
python manage.py test_bot --test-type ai
```

### Logging
Check Django logs for detailed error information:
```bash
tail -f logs/django.log  # If you've configured file logging
```

## Production Deployment

### Environment Variables
Set these in your production environment:
- `DEBUG=False`
- `DJANGO_SECRET_KEY` (generate a secure random string)
- All Slack and Gemini API credentials

### Security Checklist
- [ ] Set `DEBUG=False`
- [ ] Configure `ALLOWED_HOSTS` properly
- [ ] Use HTTPS for all endpoints
- [ ] Set up proper logging
- [ ] Configure database backups
- [ ] Monitor API rate limits

### Scaling Considerations
- Use background task queue (Celery) for longer summarization tasks
- Implement caching for frequently requested summaries  
- Set up monitoring and alerting
- Consider using Slack's Socket Mode for real-time events

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

If you encounter any issues:
1. Check the troubleshooting section above
2. Run the test command: `python manage.py test_bot`
3. Check the GitHub issues page
4. Create a new issue with detailed information about your problem

---

**Happy Summarizing! 🤖📊**
