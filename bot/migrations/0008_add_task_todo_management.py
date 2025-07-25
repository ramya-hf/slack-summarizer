# Generated by Django 5.2.3 on 2025-07-18 04:22

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0007_categorychannel_bot_categor_categor_fdb6ff_idx_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='ChannelCanvas',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('canvas_id', models.CharField(max_length=100, unique=True)),
                ('canvas_url', models.URLField()),
                ('canvas_title', models.CharField(default='Todo List', max_length=200)),
                ('last_updated', models.DateTimeField(auto_now=True)),
                ('last_sync_at', models.DateTimeField(blank=True, null=True)),
                ('sync_errors', models.TextField(blank=True)),
                ('total_todos', models.IntegerField(default=0)),
                ('pending_todos', models.IntegerField(default=0)),
                ('channel', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to='bot.slackchannel')),
            ],
            options={
                'verbose_name_plural': 'Channel Canvas Documents',
                'ordering': ['-last_updated'],
            },
        ),
        migrations.CreateModel(
            name='ChannelTodo',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=500)),
                ('description', models.TextField(blank=True)),
                ('task_type', models.CharField(choices=[('bug', 'Bug Fix'), ('feature', 'Feature Development'), ('meeting', 'Meeting/Event'), ('review', 'Code Review'), ('deadline', 'Deadline/Due Date'), ('general', 'General Task'), ('urgent', 'Urgent Item')], default='general', max_length=20)),
                ('priority', models.CharField(choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('critical', 'Critical')], default='medium', max_length=20)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('in_progress', 'In Progress'), ('completed', 'Completed'), ('cancelled', 'Cancelled')], default='pending', max_length=20)),
                ('assigned_to', models.CharField(blank=True, help_text='Slack user ID', max_length=100)),
                ('assigned_to_username', models.CharField(blank=True, help_text='Slack username for display', max_length=100)),
                ('due_date', models.DateTimeField(blank=True, null=True)),
                ('created_from_message', models.CharField(blank=True, help_text='Original message timestamp', max_length=100)),
                ('created_from_message_link', models.URLField(blank=True)),
                ('created_by', models.CharField(max_length=100)),
                ('created_by_username', models.CharField(blank=True, max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('completed_by', models.CharField(blank=True, max_length=100)),
                ('canvas_block_id', models.CharField(blank=True, help_text='Canvas block reference', max_length=100)),
                ('channel', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='bot.slackchannel')),
            ],
            options={
                'verbose_name_plural': 'Channel Todos',
                'ordering': ['-priority', '-created_at'],
            },
        ),
        migrations.CreateModel(
            name='TaskReminder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reminder_type', models.CharField(choices=[('due_soon', 'Due Soon'), ('overdue', 'Overdue'), ('priority_escalation', 'Priority Escalation'), ('assignment', 'New Assignment')], max_length=50)),
                ('reminder_time', models.DateTimeField()),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('message_sent', models.TextField(blank=True)),
                ('is_sent', models.BooleanField(default=False)),
                ('todo', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='bot.channeltodo')),
            ],
            options={
                'ordering': ['reminder_time'],
            },
        ),
        migrations.CreateModel(
            name='TaskSummary',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('summary_text', models.TextField()),
                ('total_tasks', models.IntegerField()),
                ('pending_tasks', models.IntegerField()),
                ('completed_tasks', models.IntegerField()),
                ('high_priority_tasks', models.IntegerField()),
                ('overdue_tasks', models.IntegerField()),
                ('timeframe', models.CharField(default='Last 24 hours', max_length=100)),
                ('timeframe_hours', models.IntegerField(default=24)),
                ('requested_by_user', models.CharField(max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('channel', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='bot.slackchannel')),
            ],
            options={
                'verbose_name_plural': 'Task Summaries',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='channeltodo',
            index=models.Index(fields=['channel', 'status'], name='bot_channel_channel_e9fcbd_idx'),
        ),
        migrations.AddIndex(
            model_name='channeltodo',
            index=models.Index(fields=['channel', 'priority'], name='bot_channel_channel_22228f_idx'),
        ),
        migrations.AddIndex(
            model_name='channeltodo',
            index=models.Index(fields=['assigned_to', 'status'], name='bot_channel_assigne_3e9145_idx'),
        ),
        migrations.AddIndex(
            model_name='channeltodo',
            index=models.Index(fields=['due_date'], name='bot_channel_due_dat_d24a63_idx'),
        ),
        migrations.AddIndex(
            model_name='channeltodo',
            index=models.Index(fields=['task_type', 'status'], name='bot_channel_task_ty_2d68d0_idx'),
        ),
        migrations.AddIndex(
            model_name='taskreminder',
            index=models.Index(fields=['reminder_time', 'is_sent'], name='bot_taskrem_reminde_c8f53f_idx'),
        ),
        migrations.AddIndex(
            model_name='taskreminder',
            index=models.Index(fields=['todo', 'reminder_type'], name='bot_taskrem_todo_id_8f0961_idx'),
        ),
        migrations.AddIndex(
            model_name='tasksummary',
            index=models.Index(fields=['channel', 'created_at'], name='bot_tasksum_channel_d9938d_idx'),
        ),
        migrations.AddIndex(
            model_name='tasksummary',
            index=models.Index(fields=['requested_by_user'], name='bot_tasksum_request_4194db_idx'),
        ),
    ]
