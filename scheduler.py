#!/usr/bin/env python3
"""
Scheduled Tasks Module for Claude Telegram Bot

This module enables:
1. Creating scheduled tasks that run at specific times (daily, weekly, once)
2. Tasks send prompts to Claude automatically
3. Claude processes them (with optional Mac agent tools)
4. Results are sent back through Telegram to the user

Usage:
    from scheduler import TaskScheduler
    scheduler = TaskScheduler(claude_client, call_mac_func, send_telegram_func)
    scheduler.add_task(user_id, "daily", "09:00", "Give me a morning briefing")
    scheduler.start()
"""

import json
import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, asdict
from enum import Enum
import threading
import anthropic

logger = logging.getLogger(__name__)

class TaskFrequency(Enum):
    ONCE = "once"           # Run once at specified datetime
    DAILY = "daily"         # Run daily at specified time
    WEEKLY = "weekly"       # Run weekly on specified day/time
    HOURLY = "hourly"       # Run every hour at specified minute
    CUSTOM = "custom"       # Run at custom interval (minutes)


@dataclass
class ScheduledTask:
    """Represents a scheduled task"""
    task_id: str
    user_id: int
    chat_id: int
    prompt: str                      # The prompt to send to Claude
    frequency: str                   # TaskFrequency value
    time_spec: str                   # Time specification (HH:MM for daily, "monday 09:00" for weekly, etc.)
    use_tools: bool = True           # Whether Claude can use Mac agent tools
    enabled: bool = True
    created_at: str = ""
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    run_count: int = 0
    description: str = ""            # User-friendly description

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.next_run:
            self.next_run = self._calculate_next_run()

    def _calculate_next_run(self) -> str:
        """Calculate the next run time based on frequency and time_spec"""
        now = datetime.now()

        if self.frequency == TaskFrequency.ONCE.value:
            # time_spec is ISO datetime string
            try:
                return self.time_spec
            except:
                return (now + timedelta(hours=1)).isoformat()

        elif self.frequency == TaskFrequency.DAILY.value:
            # time_spec is "HH:MM"
            try:
                hour, minute = map(int, self.time_spec.split(":"))
                next_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_time <= now:
                    next_time += timedelta(days=1)
                return next_time.isoformat()
            except:
                return (now + timedelta(days=1)).isoformat()

        elif self.frequency == TaskFrequency.WEEKLY.value:
            # time_spec is "monday 09:00" or "0 09:00" (0=Monday)
            try:
                parts = self.time_spec.lower().split()
                day_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                          "friday": 4, "saturday": 5, "sunday": 6}

                if parts[0] in day_map:
                    target_day = day_map[parts[0]]
                else:
                    target_day = int(parts[0])

                hour, minute = map(int, parts[1].split(":"))

                days_ahead = target_day - now.weekday()
                if days_ahead < 0 or (days_ahead == 0 and now.hour * 60 + now.minute >= hour * 60 + minute):
                    days_ahead += 7

                next_time = now + timedelta(days=days_ahead)
                next_time = next_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
                return next_time.isoformat()
            except Exception as e:
                logger.error(f"Error calculating weekly next_run: {e}")
                return (now + timedelta(weeks=1)).isoformat()

        elif self.frequency == TaskFrequency.HOURLY.value:
            # time_spec is minute of the hour "30" means :30 every hour
            try:
                minute = int(self.time_spec)
                next_time = now.replace(minute=minute, second=0, microsecond=0)
                if next_time <= now:
                    next_time += timedelta(hours=1)
                return next_time.isoformat()
            except:
                return (now + timedelta(hours=1)).isoformat()

        elif self.frequency == TaskFrequency.CUSTOM.value:
            # time_spec is interval in minutes
            try:
                interval = int(self.time_spec)
                return (now + timedelta(minutes=interval)).isoformat()
            except:
                return (now + timedelta(hours=1)).isoformat()

        return (now + timedelta(hours=1)).isoformat()

    def update_next_run(self):
        """Update next_run after task execution"""
        self.last_run = datetime.now().isoformat()
        self.run_count += 1

        if self.frequency == TaskFrequency.ONCE.value:
            self.enabled = False  # Disable after single run
        else:
            self.next_run = self._calculate_next_run()


class TaskScheduler:
    """
    Manages scheduled tasks for the Claude Telegram Bot.

    Tasks are stored in a JSON file and run automatically based on their schedule.
    When a task runs, it sends the prompt to Claude and delivers the response via Telegram.
    """

    def __init__(
        self,
        claude_client: anthropic.Anthropic,
        call_mac_func: Callable,
        send_telegram_func: Callable,
        mac_tools: List[Dict],
        tasks_file: str = "scheduled_tasks.json"
    ):
        self.claude_client = claude_client
        self.call_mac = call_mac_func
        self.send_telegram = send_telegram_func  # async function(chat_id, message)
        self.mac_tools = mac_tools
        self.tasks_file = os.path.join(os.path.dirname(__file__), tasks_file)
        self.tasks: Dict[str, ScheduledTask] = {}
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None

        self._load_tasks()

    def _load_tasks(self):
        """Load tasks from JSON file"""
        if os.path.exists(self.tasks_file):
            try:
                with open(self.tasks_file, 'r') as f:
                    data = json.load(f)
                    for task_id, task_data in data.items():
                        self.tasks[task_id] = ScheduledTask(**task_data)
                logger.info(f"Loaded {len(self.tasks)} scheduled tasks")
            except Exception as e:
                logger.error(f"Error loading tasks: {e}")
                self.tasks = {}
        else:
            self.tasks = {}

    def _save_tasks(self):
        """Save tasks to JSON file"""
        try:
            data = {task_id: asdict(task) for task_id, task in self.tasks.items()}
            with open(self.tasks_file, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info(f"Saved {len(self.tasks)} scheduled tasks")
        except Exception as e:
            logger.error(f"Error saving tasks: {e}")

    def add_task(
        self,
        user_id: int,
        chat_id: int,
        prompt: str,
        frequency: str,
        time_spec: str,
        use_tools: bool = True,
        description: str = ""
    ) -> ScheduledTask:
        """
        Add a new scheduled task.

        Args:
            user_id: Telegram user ID
            chat_id: Telegram chat ID for sending responses
            prompt: The prompt to send to Claude
            frequency: "once", "daily", "weekly", "hourly", or "custom"
            time_spec: Time specification based on frequency:
                - once: ISO datetime "2025-01-15T09:00:00"
                - daily: "09:00" (24-hour format)
                - weekly: "monday 09:00"
                - hourly: "30" (minute of hour)
                - custom: "60" (interval in minutes)
            use_tools: Whether Claude can use Mac agent tools
            description: User-friendly description of the task

        Returns:
            The created ScheduledTask
        """
        task_id = f"task_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        task = ScheduledTask(
            task_id=task_id,
            user_id=user_id,
            chat_id=chat_id,
            prompt=prompt,
            frequency=frequency,
            time_spec=time_spec,
            use_tools=use_tools,
            description=description or prompt[:50]
        )

        self.tasks[task_id] = task
        self._save_tasks()

        logger.info(f"Added task {task_id}: '{description}' for user {user_id}")
        return task

    def remove_task(self, task_id: str) -> bool:
        """Remove a task by ID"""
        if task_id in self.tasks:
            del self.tasks[task_id]
            self._save_tasks()
            logger.info(f"Removed task {task_id}")
            return True
        return False

    def get_user_tasks(self, user_id: int) -> List[ScheduledTask]:
        """Get all tasks for a specific user"""
        return [task for task in self.tasks.values() if task.user_id == user_id]

    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        """Get a specific task by ID"""
        return self.tasks.get(task_id)

    def toggle_task(self, task_id: str) -> Optional[bool]:
        """Toggle a task's enabled status. Returns new status or None if not found."""
        if task_id in self.tasks:
            self.tasks[task_id].enabled = not self.tasks[task_id].enabled
            self._save_tasks()
            return self.tasks[task_id].enabled
        return None

    async def execute_task(self, task: ScheduledTask):
        """Execute a scheduled task - send prompt to Claude and deliver response"""
        logger.info(f"Executing task {task.task_id}: {task.description}")

        try:
            # Build system prompt for scheduled task
            system_prompt = f"""You are executing a scheduled task for the user.
The user has set up this automated task to run at scheduled times.

Task Description: {task.description}
Task Created: {task.created_at}
Run Count: {task.run_count + 1}

Respond to the prompt naturally. If you need to use tools to gather information
(like checking the current time, weather, taking screenshots, etc.), you may do so.
Keep your response concise and focused on the task."""

            # Prepare tools if enabled
            tools = self.mac_tools if task.use_tools else None

            # Send to Claude
            messages = [{"role": "user", "content": task.prompt}]

            response = self.claude_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2048,
                system=system_prompt,
                messages=messages,
                tools=tools if tools else anthropic.NOT_GIVEN
            )

            # Handle tool use loop (simplified version for scheduled tasks)
            max_iterations = 5
            iteration = 0

            while response.stop_reason == "tool_use" and iteration < max_iterations:
                iteration += 1
                assistant_content = response.content
                messages.append({"role": "assistant", "content": assistant_content})

                tool_results = []
                for block in assistant_content:
                    if block.type == "tool_use":
                        tool_name = block.name
                        tool_input = block.input
                        logger.info(f"Scheduled task {task.task_id} using tool: {tool_name}")

                        # Execute tool (simplified - main tool handlers)
                        result = await self._execute_tool(tool_name, tool_input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result)
                        })

                messages.append({"role": "user", "content": tool_results})
                response = self.claude_client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=2048,
                    system=system_prompt,
                    messages=messages,
                    tools=tools if tools else anthropic.NOT_GIVEN
                )

            # Extract final text response
            final_message = ""
            for block in response.content:
                if hasattr(block, "text"):
                    final_message += block.text

            # Send response via Telegram
            if final_message:
                header = f"[Scheduled Task: {task.description[:30]}]\n\n"
                await self.send_telegram(task.chat_id, header + final_message)

            # Update task state
            task.update_next_run()
            self._save_tasks()

            logger.info(f"Task {task.task_id} completed successfully")

        except Exception as e:
            logger.error(f"Error executing task {task.task_id}: {e}")
            try:
                await self.send_telegram(
                    task.chat_id,
                    f"[Scheduled Task Error]\nTask: {task.description}\nError: {str(e)}"
                )
            except:
                pass

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        """Execute a tool call for scheduled tasks"""
        try:
            if tool_name == "execute_mac_command":
                return self.call_mac("execute", command=tool_input.get("command", ""))
            elif tool_name == "execute_applescript":
                return self.call_mac("applescript", script=tool_input.get("script", ""))
            elif tool_name == "read_mac_file":
                return self.call_mac("read_file", filepath=tool_input.get("filepath", ""))
            elif tool_name == "take_screenshot":
                return self.call_mac("screenshot",
                    mode=tool_input.get("mode", "full"),
                    app_name=tool_input.get("app_name"))
            elif tool_name == "execute_javascript_in_chrome":
                return self.call_mac("execute_js", js_code=tool_input.get("js_code", ""))
            elif tool_name == "check_mac_status":
                return self.call_mac("ping")
            else:
                return {"success": False, "error": f"Tool {tool_name} not available for scheduled tasks"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _scheduler_loop(self):
        """Main scheduler loop - checks and executes due tasks"""
        logger.info("Scheduler loop started")

        while self._running:
            try:
                now = datetime.now()

                for task_id, task in list(self.tasks.items()):
                    if not task.enabled:
                        continue

                    try:
                        next_run = datetime.fromisoformat(task.next_run)
                        if now >= next_run:
                            logger.info(f"Task {task_id} is due, executing...")
                            await self.execute_task(task)
                    except Exception as e:
                        logger.error(f"Error checking task {task_id}: {e}")

                # Sleep for 30 seconds before next check
                await asyncio.sleep(30)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")
                await asyncio.sleep(60)

        logger.info("Scheduler loop stopped")

    def start(self, loop: asyncio.AbstractEventLoop):
        """Start the scheduler"""
        if self._running:
            return

        self._running = True
        self._loop = loop
        self._task = loop.create_task(self._scheduler_loop())
        logger.info("Task scheduler started")

    def stop(self):
        """Stop the scheduler"""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("Task scheduler stopped")

    def get_status(self) -> dict:
        """Get scheduler status"""
        return {
            "running": self._running,
            "total_tasks": len(self.tasks),
            "enabled_tasks": sum(1 for t in self.tasks.values() if t.enabled),
            "tasks_file": self.tasks_file
        }


# Helper function to parse natural language time specifications
def parse_schedule_input(text: str) -> tuple[str, str]:
    """
    Parse user input into frequency and time_spec.

    Examples:
        "daily at 9am" -> ("daily", "09:00")
        "every monday at 10:30" -> ("weekly", "monday 10:30")
        "in 2 hours" -> ("once", ISO datetime)
        "every 30 minutes" -> ("custom", "30")
        "hourly at :15" -> ("hourly", "15")

    Returns:
        Tuple of (frequency, time_spec)
    """
    import re
    text = text.lower().strip()

    # Daily pattern: "daily at 9am", "every day at 14:00"
    daily_match = re.search(r'daily\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', text)
    if daily_match or "every day" in text:
        if daily_match:
            hour = int(daily_match.group(1))
            minute = int(daily_match.group(2) or 0)
            ampm = daily_match.group(3)
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            return ("daily", f"{hour:02d}:{minute:02d}")
        return ("daily", "09:00")

    # Weekly pattern: "every monday at 9am", "weekly on tuesday at 10:30"
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for day in days:
        if day in text:
            time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', text)
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2) or 0)
                ampm = time_match.group(3)
                if ampm == "pm" and hour < 12:
                    hour += 12
                elif ampm == "am" and hour == 12:
                    hour = 0
                return ("weekly", f"{day} {hour:02d}:{minute:02d}")
            return ("weekly", f"{day} 09:00")

    # Hourly pattern: "hourly at :30", "every hour at 15"
    hourly_match = re.search(r'(?:hourly|every\s+hour)\s+(?:at\s+)?:?(\d{1,2})', text)
    if hourly_match:
        minute = int(hourly_match.group(1))
        return ("hourly", str(minute))

    # Custom interval: "every 30 minutes", "every 2 hours"
    interval_match = re.search(r'every\s+(\d+)\s*(minute|hour)', text)
    if interval_match:
        value = int(interval_match.group(1))
        unit = interval_match.group(2)
        if unit == "hour":
            value *= 60
        return ("custom", str(value))

    # One-time: "in 2 hours", "at 3pm today", "tomorrow at 9am"
    if "in " in text:
        time_match = re.search(r'in\s+(\d+)\s*(minute|hour|day)', text)
        if time_match:
            value = int(time_match.group(1))
            unit = time_match.group(2)
            if unit == "minute":
                delta = timedelta(minutes=value)
            elif unit == "hour":
                delta = timedelta(hours=value)
            else:
                delta = timedelta(days=value)
            run_time = datetime.now() + delta
            return ("once", run_time.isoformat())

    if "tomorrow" in text:
        time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', text)
        tomorrow = datetime.now() + timedelta(days=1)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2) or 0)
            ampm = time_match.group(3)
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            run_time = tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)
        else:
            run_time = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
        return ("once", run_time.isoformat())

    # Default: daily at 9am
    return ("daily", "09:00")
