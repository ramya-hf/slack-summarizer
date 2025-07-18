# 🎨 Canvas Integration Setup Guide

## 🔍 **Issue Diagnosis**

Your Canvas creation is failing because:
1. **Using legacy Canvas API**: Current code uses deprecated `files.upload` method
2. **Missing required scopes**: Need `canvases:write` and `canvases:read` instead of `files:write`
3. **Outdated implementation**: Modern Canvas API has dedicated endpoints

## ✅ **Solution Implemented**

I've updated your Canvas implementation to use the **modern Canvas API**:

### Updated Methods:
- ✅ **Canvas Creation**: `conversations.canvases.create` for channel canvases
- ✅ **Canvas Updates**: `canvases.edit` with operation-based changes  
- ✅ **Canvas Deletion**: `canvases.delete` with direct canvas ID
- ✅ **Modern Markdown**: Optimized content generation for Canvas API

## 🔧 **Required Slack App Configuration**

### 1. **Update OAuth Scopes**

You need to add these scopes to your Slack app:

**Go to:** [https://api.slack.com/apps](https://api.slack.com/apps) → Your App → OAuth & Permissions

**Add these Bot Token Scopes:**
```
canvases:read
canvases:write
```

**Keep existing scopes:**
```
channels:history
channels:read
commands
chat:write
groups:read
groups:history
users:read
im:history
im:read
im:write
app_mentions:read
mpim:history
reactions:read
conversations.connect:manage
conversations.connect:read
mpim:read
chat:write.public
users:read.email
```

### 2. **Reinstall App to Workspace**

After adding scopes:
1. Click **"Install to Workspace"** button
2. Authorize the new permissions
3. Update your `.env` file with the new token if it changed

### 3. **Verify Canvas Feature is Enabled**

Ensure Canvas is enabled for your workspace:
- Canvas might be disabled on free Slack plans for certain features
- Check with workspace admins if Canvas is available

## 🚀 **How Modern Canvas Integration Works**

### **Channel Canvas Creation:**
```python
# Creates canvas automatically tied to channel
response = client.api_call(
    "conversations.canvases.create",
    json={
        "channel_id": channel_id,
        "document_content": {
            "type": "markdown", 
            "markdown": content
        }
    }
)
```

### **Canvas Updates:**
```python
# Updates canvas with replace operation
response = client.api_call(
    "canvases.edit",
    json={
        "canvas_id": canvas_id,
        "changes": [{
            "operation": "replace",
            "document_content": {
                "type": "markdown",
                "markdown": updated_content
            }
        }]
    }
)
```

## 📋 **Testing Your Canvas Integration**

### 1. **Test Canvas Creation:**
```
/canvas create "My Test Canvas"
```

### 2. **Test Todo → Canvas Sync:**
```
/task
```
This should:
- ✅ Analyze all channel messages
- ✅ Create todos from actionable content  
- ✅ Create Canvas if it doesn't exist
- ✅ Sync all todos to Canvas
- ✅ Show success message with stats

### 3. **Test Canvas Commands:**
```
/canvas show        # View canvas info and link
/canvas update      # Force sync todos to canvas
/canvas delete      # Remove canvas
```

## 🎯 **Expected Canvas Features**

### **Visual Todo Board:**
- ✅ **Priority-based organization** (Critical → High → Medium → Low)
- ✅ **Status indicators** (☐ Pending, ⏳ In Progress, ✅ Completed)
- ✅ **Rich metadata** (Due dates, assignees, task types)
- ✅ **Recent completion history**
- ✅ **Progress statistics**

### **Auto-sync:**
- ✅ **Real-time updates** when todos change
- ✅ **Background sync** on todo creation/completion
- ✅ **Smart sync detection** (only when needed)

### **Canvas Content Example:**
```markdown
# 📋 Todo List - #social

> *Last updated: 2024-01-15 14:30*

## 📌 Pending Tasks

### 🔴 CRITICAL PRIORITY
- ☐ **Fix production bug** | @john | 🚨 **OVERDUE** 01/14 15:00 | 🐛 bug

### 🟠 HIGH PRIORITY  
- ⏳ **Review quarterly report** | @sarah | 📅 Due: 01/16 17:00 | 👀 review
- ☐ **Deploy new feature** | @mike | ✨ feature

## ✅ Recently Completed
- ✅ ~~Setup testing environment~~ | Completed by @alex | 01/14

## 📊 Statistics
- **Total todos:** 12
- **Pending:** 8  
- **Completed:** 4
- **Completion rate:** 33.3%
```

## 🐛 **Troubleshooting**

### **Canvas Creation Still Fails:**
1. **Check scopes**: Ensure `canvases:write` is added and app is reinstalled
2. **Verify Canvas availability**: Some Slack plans have Canvas restrictions
3. **Check logs**: Look for specific API error messages

### **Canvas Not Updating:**
1. **Force sync**: Use `/canvas update` to trigger manual sync
2. **Check Canvas exists**: Use `/canvas show` to verify Canvas is created
3. **Verify permissions**: Ensure bot has access to channel

### **Canvas Shows "Permission Denied":**
1. **Bot channel membership**: Add bot to channel if private
2. **Workspace Canvas settings**: Check admin Canvas permissions
3. **Token validity**: Verify bot token is current

## 📞 **Support Commands**

### **Canvas Management:**
```bash
/canvas create [title]     # Create new Canvas
/canvas show              # View Canvas info and link
/canvas update            # Force sync latest todos
/canvas delete            # Remove Canvas document
/canvas help              # Show Canvas commands
```

### **Todo Commands:**
```bash
/task                     # Process ALL channel messages → todos + Canvas  
/todo list                # Show all todos
/todo add "task name"     # Add single todo
/todo complete [id]       # Mark todo as completed
```

## 🎉 **Benefits of Modern Canvas API**

✅ **Better Performance**: Direct API calls, no file upload overhead
✅ **Rich Formatting**: Full markdown support with Slack-specific elements  
✅ **Real-time Updates**: Instant canvas synchronization
✅ **Proper Permissions**: Uses Canvas-specific scopes, not file permissions
✅ **Channel Integration**: Native channel canvas support
✅ **Visual Appeal**: Beautiful, interactive todo boards
✅ **Team Collaboration**: Shared visual project tracking

---

**After updating your Slack app scopes and reinstalling, your Canvas integration should work perfectly! 🚀** 