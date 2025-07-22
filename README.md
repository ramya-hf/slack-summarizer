# Slack Bot - Channel Management & Task Tracking

A Django-based Slack bot that helps manage channels, track tasks, and summarize conversations using Google Gemini AI.

## Core Features

### 1. `/summary` - Channel Summarization
- Summarize last 24 hours of messages in any channel
- Get insights into key topics and discussions
- AI-powered analysis and formatting

```bash
/summary                    # Summarize current channel
/summary general           # Summarize specific channel
/summary category dev     # Summarize all channels in a category
```

### 2. `/category` - Channel Organization
- Group related channels together (2-5 channels per category)
- Get cross-channel insights and summaries
- Manage channel categories easily

```bash
/category create          # Create a new category
/category list           # View all categories
/category help           # Show category help
```

### 3. `/task` - Channel Task Management
- Create and manage tasks in channels
- Track to-do items and their status
- AI-powered task detection from messages

```bash
/task                    # Create/update task list
/task list              # View all tasks
/task help              # Show task management help
```

## Setup Requirements

1. Python 3.11+
2. Django 5.2+
3. Slack App with required scopes
4. Google Gemini API key

## Environment Variables

```bash
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_SIGNING_SECRET=your-signing-secret
GEMINI_API_KEY=your-api-key
```

## Required Slack Scopes

- `channels:history`
- `channels:read`
- `chat:write`
- `commands`
- `users:read`
- `groups:history` (for private channels)
- `groups:read` (for private channels)

## Quick Start

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run migrations:
```bash
python manage.py migrate
```

3. Start the server:
```bash
python manage.py runserver
```

4. Configure your Slack app to point to:
```
https://your-domain.com/slack/events/
```
