#!/usr/bin/env python3
import os, logging, json, socket, base64, io, asyncio
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import anthropic
from pathlib import Path
from scheduler import TaskScheduler, parse_schedule_input

# Load .env file if python-dotenv is installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, rely on environment variables

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
MAC_IP = os.environ.get("MAC_IP", "")
MAC_PORT = int(os.environ.get("MAC_PORT", "0"))
MAC_SECRET = os.environ.get("MAC_SECRET", "")

if not TELEGRAM_TOKEN or not CLAUDE_API_KEY:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or CLAUDE_API_KEY")

claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
user_conversations = {}
screenshot_metadata = {}
user_locations = {}  # Store user GPS locations {user_id: {"lat": x, "lon": y, "address": "..."}}

# Task Scheduler - initialized after MAC_TOOLS is defined
task_scheduler = None
pending_schedule_prompts = {}  # {user_id: {"step": "prompt"|"schedule", "prompt": str}}

# Interrupt handling - priority commands from Telegram
user_interrupt_flag = {}  # {user_id: True/False} - set True to interrupt current operation
active_operations = {}  # {user_id: "description"} - track what's running

MAC_TOOLS = [
    {"name": "capture_images", "description": """PREFERRED: Download images from the current webpage in Chrome. Returns clean image files (not screenshots) with their source links. Use count to specify how many images to capture (default 5).""",
     "input_schema": {"type": "object", "properties": {"count": {"type": "integer", "description": "Number of images to capture (default 5)"}, "min_width": {"type": "integer"}, "min_height": {"type": "integer"}}, "required": []}},
    {"name": "execute_mac_command", "description": "Execute a shell command on the Mac", "input_schema": {"type": "object", "properties": {"command": {"type": "string", "description": "Shell command"}}, "required": ["command"]}},
    {"name": "execute_applescript", "description": "Execute AppleScript to control Mac applications", "input_schema": {"type": "object", "properties": {"script": {"type": "string", "description": "AppleScript code"}}, "required": ["script"]}},
    {"name": "read_mac_file", "description": "Read file contents", "input_schema": {"type": "object", "properties": {"filepath": {"type": "string"}}, "required": ["filepath"]}},
    {"name": "take_screenshot", "description": """Take a screenshot of the screen (NOT for capturing webpage images - use capture_images instead). Modes: 'full', 'window' (requires app_name).""",
     "input_schema": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["full", "window"]}, "app_name": {"type": "string"}}, "required": []}},
    {"name": "list_windows", "description": "List all open windows", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_window_bounds", "description": "Get window position and size", "input_schema": {"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]}},
    {"name": "scroll_page", "description": "Scroll page up or down", "input_schema": {"type": "object", "properties": {"app_name": {"type": "string"}, "direction": {"type": "string", "enum": ["down", "up"]}, "amount": {"type": "integer"}}, "required": []}},
    {"name": "execute_javascript_in_chrome", "description": "Execute JavaScript in Chrome's active tab", "input_schema": {"type": "object", "properties": {"js_code": {"type": "string"}}, "required": ["js_code"]}},
    {"name": "wait", "description": "Wait for seconds (1-30)", "input_schema": {"type": "object", "properties": {"seconds": {"type": "integer"}}, "required": ["seconds"]}},
    {"name": "check_mac_status", "description": "Check if Mac is online", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "list_page_images", "description": """Get metadata about all images on current page. Returns index and info for each image. Use download_selected_images with the indices you want.""",
     "input_schema": {"type": "object", "properties": {"min_width": {"type": "integer"}, "min_height": {"type": "integer"}}, "required": []}},
    {"name": "download_selected_images", "description": """Download specific images by index. Pass the indices of images you want to download (from list_page_images). Returns clean image files with their source links.""",
     "input_schema": {"type": "object", "properties": {"indices": {"type": "array", "items": {"type": "integer"}, "description": "List of image indices to download"}}, "required": ["indices"]}},
    {"name": "order_uber", "description": """Order an Uber ride using the user's shared location. ALWAYS use this tool for Uber rides - it uses fast browser automation. User must have shared their location first via Telegram. Ask the user how many passengers before ordering - if >4, UberXL will be selected automatically.""",
     "input_schema": {"type": "object", "properties": {"destination": {"type": "string", "description": "Destination address or place name"}, "num_passengers": {"type": "integer", "description": "Number of passengers (if >4, UberXL is selected automatically)", "default": 1}, "ride_type": {"type": "string", "enum": ["UberX", "Comfort", "UberXL", "Black"], "description": "Type of Uber ride (default: UberX, auto-set to UberXL if num_passengers > 4)"}}, "required": ["destination", "num_passengers"]}},
    {"name": "get_user_location", "description": """Get the user's current stored location (latitude, longitude, and address if available). Returns error if user hasn't shared location.""",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "order_uber_eats", "description": """Order food via Uber Eats using browser automation. User must have shared location first.
    - cuisine_type: Filter by cuisine (pizza, sushi, mexican, thai, chinese, indian, etc.)
    - surprise_me: If true, selects 'Best Overall' restaurant, researches top dishes online, and orders the highest recommended item
    - customization_answers: Array of answers to customization questions (used in step 2)

    TWO-STEP FLOW:
    Step 1: Call without customization_answers -> Returns needs_customization=true with questions array
    Step 2: Present questions to user, collect answers, call again WITH customization_answers

    When needs_customization=true is returned:
    - Format the questions nicely for the user
    - Ask them to choose from the options
    - Once they respond, call order_uber_eats again with their answers as customization_answers

    Returns progress updates and final order status.""",
     "input_schema": {"type": "object", "properties": {
         "cuisine_type": {"type": "string", "description": "Type of cuisine to search for (pizza, sushi, mexican, etc.)"},
         "surprise_me": {"type": "boolean", "description": "If true, auto-select best restaurant and top-rated dish", "default": False},
         "customization_answers": {"type": "array", "items": {"type": "object", "properties": {"question": {"type": "string"}, "answer": {"type": "string"}}}, "description": "User's answers to customization questions from step 1"}
     }, "required": []}}
]

def call_mac_sync(action, timeout=30.0, **kwargs):
    """Synchronous call to Mac agent - blocks until complete"""
    if not MAC_IP or not MAC_PORT or not MAC_SECRET:
        return {"success": False, "error": "Mac agent not configured"}
    try:
        request = {"secret": MAC_SECRET, "action": action, **kwargs}
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((MAC_IP, MAC_PORT))
        sock.sendall(json.dumps(request).encode("utf-8"))
        response_chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_chunks.append(chunk)
        sock.close()
        return json.loads(b"".join(response_chunks).decode("utf-8"))
    except socket.timeout:
        return {"success": False, "error": "Connection timed out"}
    except ConnectionRefusedError:
        return {"success": False, "error": "Connection refused"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def call_mac(action, timeout=30.0, **kwargs):
    """Call Mac agent - wraps sync call"""
    return call_mac_sync(action, timeout, **kwargs)

def is_mac_configured():
    """Check if Mac agent connection is configured"""
    return bool(MAC_IP and MAC_PORT and MAC_SECRET)

def is_mac_online():
    """Check if Mac agent is currently reachable"""
    if not is_mac_configured():
        return False
    result = call_mac("ping", timeout=5.0)
    return result.get("success", False)

# Cloud-only tools that work without Mac agent
CLOUD_TOOLS = [
    {"name": "get_user_location", "description": """Get the user's current stored location (latitude, longitude, and address if available). Returns error if user hasn't shared location.""",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "web_search", "description": """Search the web for information. Use this for current events, facts, or any information lookup.""",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}}, "required": ["query"]}},
    {"name": "get_current_time", "description": """Get the current date and time.""",
     "input_schema": {"type": "object", "properties": {"timezone": {"type": "string", "description": "Timezone (e.g., 'America/New_York', 'UTC')", "default": "local"}}, "required": []}},
]

async def call_mac_async(action, timeout=30.0, **kwargs):
    """Async call to Mac agent - runs in thread pool so bot stays responsive"""
    import concurrent.futures
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        result = await loop.run_in_executor(pool, lambda: call_mac_sync(action, timeout, **kwargs))
    return result

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_conversations[user_id] = []
    mac_status = "Offline"
    if MAC_IP and MAC_PORT and MAC_SECRET:
        if call_mac("ping").get("success"):
            mac_status = "Online"

    # Check if user has shared location
    location_status = "Not shared"
    if user_id in user_locations:
        loc = user_locations[user_id]
        coords = f"{loc['lat']:.4f}, {loc['lon']:.4f}"
        location_status = f"📍 {loc.get('address', coords)}"

    # Count scheduled tasks
    tasks_count = 0
    if task_scheduler:
        tasks_count = len(task_scheduler.get_user_tasks(user_id))

    # Determine mode
    if mac_status == "Online":
        mode = "🟢 **FULL MODE** - All features available"
        mac_features = (
            "**Mac Features (Available):**\n"
            "/notes <text> - Create Apple Note\n"
            "/song - Analyze Spotify track\n"
            "🚗 Uber ordering\n"
            "🖼️ Browser & screenshots\n"
        )
    else:
        mode = "☁️ **CLOUD MODE** - Chat & scheduled tasks"
        mac_features = (
            "**Mac Features (Offline):**\n"
            "❌ Notes, Spotify, Uber, Browser\n"
            "_Start Mac agent for full access_\n"
        )

    await update.message.reply_text(
        f"👋 Welcome!\n\n"
        f"{mode}\n\n"
        f"🖥️ Mac Agent: {mac_status}\n"
        f"📍 Location: {location_status}\n"
        f"📅 Scheduled Tasks: {tasks_count}\n\n"
        f"**Always Available:**\n"
        f"💬 Chat with Claude\n"
        f"🔍 Web search\n"
        f"/schedule - Create scheduled task\n"
        f"/tasks - View your tasks\n\n"
        f"{mac_features}\n"
        f"/start /clear /help /location",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_conversations[user_id] = []
    await update.message.reply_text("Cleared!")

async def request_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to request user's location for Uber ordering"""
    location_button = KeyboardButton(text="📍 Share My Location", request_location=True)
    reply_markup = ReplyKeyboardMarkup([[location_button]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "To order an Uber, I need your current location.\n\nTap the button below to share:",
        reply_markup=reply_markup
    )

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming location from user"""
    user_id = update.effective_user.id
    location = update.message.location

    # Store the location
    user_locations[user_id] = {
        "lat": location.latitude,
        "lon": location.longitude,
        "timestamp": update.message.date.isoformat()
    }

    # Try to reverse geocode using a simple approach (optional - can be enhanced)
    try:
        import urllib.request
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={location.latitude}&lon={location.longitude}"
        req = urllib.request.Request(url, headers={'User-Agent': 'TelegramBot/1.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            address = data.get('display_name', '')[:100]
            user_locations[user_id]['address'] = address
    except Exception as e:
        logger.warning(f"Reverse geocoding failed: {e}")
        user_locations[user_id]['address'] = f"{location.latitude:.4f}, {location.longitude:.4f}"

    await update.message.reply_text(
        f"📍 Location saved!\n\n{user_locations[user_id].get('address', 'Unknown')}\n\n"
        "What would you like to do?\n\n"
        "🚗 Enter a destination for an Uber ride\n"
        "🍔 Type 'food' for Uber Eats",
        reply_markup=ReplyKeyboardRemove()
    )

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Check if user is in schedule creation flow
    if await handle_schedule_flow(update, context):
        return  # Message was handled by schedule flow

    if user_id not in user_conversations:
        user_conversations[user_id] = []
    user_conversations[user_id].append({"role": "user", "content": update.message.text})
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        # Determine available tools based on Mac agent status
        mac_available = is_mac_configured() and is_mac_online()
        if mac_available:
            tools = MAC_TOOLS + CLOUD_TOOLS
            mode_info = "FULL MODE: Mac agent is connected. All features available."
        else:
            tools = CLOUD_TOOLS
            mode_info = """CLOUD MODE: Mac agent is offline.
You can still:
- Have conversations and answer questions
- Use web_search for information lookup
- Get current time
- Access user's stored location
- Run scheduled tasks that don't need Mac

Features that require Mac (unavailable now):
- Browser control, screenshots, Uber ordering
- Apple Notes, Spotify analysis
- File operations on Mac

If user asks for Mac features, politely explain the Mac needs to be online."""

        # Build dynamic system prompt with user location if available
        location_context = ""
        if user_id in user_locations:
            loc = user_locations[user_id]
            loc_coords = f"{loc['lat']}, {loc['lon']}"
            loc_display = loc.get('address', loc_coords)
            location_context = f"\n\nUSER LOCATION: The user has shared their location: {loc_display} (lat: {loc['lat']}, lon: {loc['lon']})"

        system_prompt = f"""You are a helpful AI assistant accessible via Telegram.

STATUS: {mode_info}
{location_context}

WHEN MAC IS AVAILABLE:
- You can control Chrome browser, take screenshots, execute commands
- Order Uber rides and Uber Eats
- Create Apple Notes, analyze Spotify tracks
- Capture images from webpages

UBER RIDES (when Mac available):
- MANDATORY: Ask "More than 4 passengers?" BEFORE calling order_uber
- If user tries to order without sharing location, tell them to use /location command first

UBER EATS (when Mac available):
- Ask what type of food they want
- Handle the two-step customization flow

IMAGE CAPTURE (when Mac available):
USE capture_images - it downloads actual image files with their source links.

CLOUD TOOLS (always available):
- web_search: Search for any information
- get_current_time: Get current date/time
- get_user_location: Access stored location

Be helpful, conversational, and proactive. If you can help with something even without the Mac, do so!"""
        response = claude_client.messages.create(model="claude-sonnet-4-20250514", max_tokens=4096, system=system_prompt, messages=user_conversations[user_id], tools=tools if tools else anthropic.NOT_GIVEN)
        while response.stop_reason == "tool_use":
            assistant_content = response.content
            user_conversations[user_id].append({"role": "assistant", "content": assistant_content})
            tool_results = []
            screenshots_to_send = []
            for block in assistant_content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input
                    logger.info(f"Tool: {tool_name} - {tool_input}")
                    if tool_name == "execute_mac_command":
                        result = call_mac("execute", command=tool_input["command"])
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "execute_applescript":
                        result = call_mac("applescript", script=tool_input["script"])
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "read_mac_file":
                        result = call_mac("read_file", filepath=tool_input["filepath"])
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "list_windows":
                        result = call_mac("list_windows")
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "get_window_bounds":
                        result = call_mac("get_window_bounds", app_name=tool_input["app_name"])
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "scroll_page":
                        result = call_mac("scroll", app_name=tool_input.get("app_name", "Google Chrome"), direction=tool_input.get("direction", "down"), amount=tool_input.get("amount", 3))
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "execute_javascript_in_chrome":
                        result = call_mac("execute_js", js_code=tool_input["js_code"])
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "take_screenshot":
                        mode = tool_input.get("mode", "full")
                        result = call_mac("screenshot", mode=mode, app_name=tool_input.get("app_name"), region=tool_input.get("region"))
                        if result.get("success") and result.get("filepath"):
                            image_result = call_mac("read_image", filepath=result["filepath"])
                            if image_result.get("success") and image_result.get("image_data"):
                                screenshots_to_send.append({"data": image_result["image_data"], "mode": "screenshot"})
                                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_result["image_data"]}}, {"type": "text", "text": f"Screenshot captured ({mode})"}]})
                            else:
                                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"success": False, "error": "Failed to read"})})
                        else:
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "wait":
                        seconds = min(max(tool_input.get("seconds", 3), 1), 30)
                        import asyncio
                        await asyncio.sleep(seconds)
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"success": True, "message": f"Waited {seconds}s"})})
                    elif tool_name == "check_mac_status":
                        result = call_mac("ping")
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "capture_images":
                        result = call_mac("capture_images", count=tool_input.get("count", 5), min_width=tool_input.get("min_width", 150), min_height=tool_input.get("min_height", 150))
                        if result.get("success") and result.get("screenshots"):
                            for img in result["screenshots"]:
                                screenshots_to_send.append({
                                    "data": img["image_data"],
                                    "url": img.get("url", ""),
                                    "alt": img.get("alt", ""),
                                    "mode": "download"
                                })
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"success": True, "count": result["count"], "page_url": result.get("page_url", "")})})
                        else:
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "list_page_images":
                        result = call_mac("list_page_images", min_width=tool_input.get("min_width", 150), min_height=tool_input.get("min_height", 150))
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "download_selected_images":
                        result = call_mac("download_selected_images", indices=tool_input.get("indices", []))
                        if result.get("success") and result.get("screenshots"):
                            for img in result["screenshots"]:
                                screenshots_to_send.append({
                                    "data": img["image_data"],
                                    "url": img.get("url", ""),
                                    "alt": img.get("alt", ""),
                                    "mode": "download"
                                })
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"success": True, "count": result["count"]})})
                        else:
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "get_user_location":
                        if user_id in user_locations:
                            result = {"success": True, "location": user_locations[user_id]}
                        else:
                            result = {"success": False, "error": "User has not shared their location. Tell them to use /location command."}
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "web_search":
                        # Cloud tool - web search using DuckDuckGo
                        query = tool_input.get("query", "")
                        try:
                            import urllib.request
                            import urllib.parse
                            # Use DuckDuckGo instant answer API
                            encoded_query = urllib.parse.quote(query)
                            url = f"https://api.duckduckgo.com/?q={encoded_query}&format=json&no_html=1"
                            req = urllib.request.Request(url, headers={'User-Agent': 'TelegramBot/1.0'})
                            with urllib.request.urlopen(req, timeout=10) as response:
                                data = json.loads(response.read().decode())
                                abstract = data.get('Abstract', '')
                                answer = data.get('Answer', '')
                                related = [t.get('Text', '') for t in data.get('RelatedTopics', [])[:5] if t.get('Text')]
                                result = {
                                    "success": True,
                                    "query": query,
                                    "abstract": abstract,
                                    "answer": answer,
                                    "related_topics": related
                                }
                        except Exception as e:
                            result = {"success": False, "error": f"Search failed: {str(e)}"}
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "get_current_time":
                        # Cloud tool - get current time
                        from datetime import datetime
                        import time
                        tz = tool_input.get("timezone", "local")
                        try:
                            now = datetime.now()
                            result = {
                                "success": True,
                                "datetime": now.isoformat(),
                                "date": now.strftime("%Y-%m-%d"),
                                "time": now.strftime("%H:%M:%S"),
                                "day_of_week": now.strftime("%A"),
                                "timestamp": int(time.time())
                            }
                        except Exception as e:
                            result = {"success": False, "error": str(e)}
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "order_uber":
                        if user_id not in user_locations:
                            result = {"success": False, "error": "User has not shared their location. Tell them to use /location command first."}
                        else:
                            loc = user_locations[user_id]
                            destination = tool_input.get("destination", "")
                            num_passengers = tool_input.get("num_passengers", 1)
                            ride_type = tool_input.get("ride_type", "UberX")
                            await update.message.reply_text(f"🚗 Ordering Uber to {destination} for {num_passengers} passenger(s)...")
                            # Agent.py now handles Claude CLI integration for fast browser automation
                            # Use longer timeout (150s) since Claude Code may take up to 120s
                            result = call_mac(
                                "order_uber",
                                timeout=150.0,
                                pickup_lat=loc["lat"],
                                pickup_lon=loc["lon"],
                                pickup_address=loc.get("address", ""),
                                destination=destination,
                                ride_type=ride_type,
                                num_passengers=num_passengers
                            )
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "order_uber_eats":
                        if user_id not in user_locations:
                            result = {"success": False, "error": "User has not shared their location. Tell them to use /location command first."}
                        else:
                            loc = user_locations[user_id]
                            cuisine_type = tool_input.get("cuisine_type", "")
                            surprise_me = tool_input.get("surprise_me", False)
                            customization_answers = tool_input.get("customization_answers", None)

                            # Track operation and clear interrupt flag
                            user_interrupt_flag[user_id] = False
                            op_desc = f"Uber Eats: {cuisine_type or 'surprise me'}"
                            active_operations[user_id] = op_desc

                            if customization_answers:
                                await update.message.reply_text("🍔 Applying your choices and adding to cart...\n(Use /stop to cancel)")
                            elif surprise_me:
                                await update.message.reply_text("🍔 Finding the best restaurant and top-rated dish for you...\n(Use /stop to cancel)")
                            else:
                                await update.message.reply_text(f"🍔 Searching for {cuisine_type} restaurants near you...\n(Use /stop to cancel)")

                            # Check for interrupt before starting
                            if user_interrupt_flag.get(user_id):
                                result = {"success": False, "error": "Operation cancelled by user"}
                            else:
                                # Call the Uber Eats automation using async so bot stays responsive
                                result = await call_mac_async(
                                    "order_uber_eats",
                                    timeout=180.0,
                                    pickup_lat=loc["lat"],
                                    pickup_lon=loc["lon"],
                                    pickup_address=loc.get("address", ""),
                                    cuisine_type=cuisine_type,
                                    surprise_me=surprise_me,
                                    customization_answers=customization_answers
                                )

                            # Check if interrupted during execution
                            if user_interrupt_flag.get(user_id):
                                result = {"success": False, "error": "Operation cancelled by user", "interrupted": True}

                            # Clear active operation
                            active_operations.pop(user_id, None)
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
            for screenshot in screenshots_to_send:
                try:
                    screenshot_bytes = base64.b64decode(screenshot["data"])
                    if screenshot.get("mode") == "download":
                        caption = screenshot.get("url", "") if screenshot.get("url") else None
                    else:
                        caption = None
                    await update.message.reply_photo(photo=io.BytesIO(screenshot_bytes), caption=caption)
                except Exception as e:
                    logger.error(f"Failed to send image: {e}")
            user_conversations[user_id].append({"role": "user", "content": tool_results})
            response = claude_client.messages.create(model="claude-sonnet-4-20250514", max_tokens=4096, system=system_prompt, messages=user_conversations[user_id], tools=tools if tools else anthropic.NOT_GIVEN)
        assistant_message = ""
        for block in response.content:
            if hasattr(block, "text"):
                assistant_message += block.text
        user_conversations[user_id].append({"role": "assistant", "content": assistant_message})
        if len(user_conversations[user_id]) > 40:
            user_conversations[user_id] = user_conversations[user_id][-40:]
        if assistant_message:
            await update.message.reply_text(assistant_message)
    except anthropic.BadRequestError as e:
        # Handle conversation sync errors by clearing history
        if "tool_use_id" in str(e) or "tool_result" in str(e):
            logger.warning(f"Conversation sync error, clearing history: {e}")
            user_conversations[user_id] = []
            await update.message.reply_text("Conversation reset due to sync error. Please try again.")
        else:
            logger.error(f"API Error: {e}", exc_info=True)
            await update.message.reply_text(f"Error: {str(e)}")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await update.message.reply_text(f"Error: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("Analyzing...")
    try:
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        photo_path = f"/tmp/photo_{user_id}.jpg"
        await photo_file.download_to_drive(photo_path)
        with open(photo_path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("utf-8")
        if user_id not in user_conversations:
            user_conversations[user_id] = []
        caption = update.message.caption or "Analyze this"
        user_conversations[user_id].append({"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data}}, {"type": "text", "text": caption}]})
        response = claude_client.messages.create(model="claude-sonnet-4-20250514", max_tokens=2048, messages=user_conversations[user_id])
        assistant_message = response.content[0].text
        user_conversations[user_id].append({"role": "assistant", "content": assistant_message})
        if os.path.exists(photo_path):
            os.remove(photo_path)
        await update.message.reply_text(assistant_message)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"Error: {str(e)}")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop command - interrupt current operation immediately"""
    user_id = update.effective_user.id
    user_interrupt_flag[user_id] = True

    # Send interrupt signal to agent
    try:
        result = call_mac("interrupt", timeout=5.0)
        logger.info(f"Interrupt signal sent: {result}")
    except:
        pass

    operation = active_operations.get(user_id, "unknown operation")
    active_operations.pop(user_id, None)

    await update.message.reply_text(f"🛑 STOPPED! Interrupted: {operation}\n\nYou can now give me a new command.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command - show what's currently running"""
    user_id = update.effective_user.id
    operation = active_operations.get(user_id)

    if operation:
        await update.message.reply_text(f"🔄 Currently running: {operation}\n\nUse /stop to interrupt.")
    else:
        await update.message.reply_text("✅ No active operations. Ready for commands!")

async def notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /notes command - create an Apple Note with the provided text"""
    user_id = update.effective_user.id

    # Get the text after /notes
    if context.args:
        note_text = ' '.join(context.args)
    else:
        await update.message.reply_text("📝 Please provide text for your note.\n\nUsage: /notes Your note content here")
        return

    await update.message.reply_text("📝 Creating note...")

    # Generate a title using Claude
    try:
        title_response = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=50,
            messages=[{
                "role": "user",
                "content": f"Generate a short, relevant title (3-6 words max) for this note. Return ONLY the title, nothing else:\n\n{note_text}"
            }]
        )
        title = title_response.content[0].text.strip().strip('"').strip("'")
    except:
        # Fallback: use first few words
        words = note_text.split()[:5]
        title = ' '.join(words) + ('...' if len(note_text.split()) > 5 else '')

    # Create the note using AppleScript via agent
    result = call_mac("create_note", title=title, body=note_text)

    if result.get('success'):
        await update.message.reply_text(f"✅ Note created!\n\n📌 Title: {title}")
    else:
        await update.message.reply_text(f"❌ Failed to create note: {result.get('error', 'Unknown error')}")


# ============= SCHEDULED TASKS COMMANDS =============

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /schedule command - create a new scheduled task"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Check if user provided arguments directly
    if context.args:
        # Full inline format: /schedule daily 9am "Give me a morning briefing"
        full_text = ' '.join(context.args)

        # Try to parse quoted prompt
        import re
        quoted_match = re.search(r'"([^"]+)"', full_text)
        if quoted_match:
            prompt = quoted_match.group(1)
            schedule_text = full_text.replace(f'"{prompt}"', '').strip()
            frequency, time_spec = parse_schedule_input(schedule_text)

            task = task_scheduler.add_task(
                user_id=user_id,
                chat_id=chat_id,
                prompt=prompt,
                frequency=frequency,
                time_spec=time_spec,
                use_tools=True,
                description=prompt[:50]
            )

            await update.message.reply_text(
                f"✅ Scheduled task created!\n\n"
                f"🆔 ID: `{task.task_id}`\n"
                f"📝 Prompt: {prompt[:100]}\n"
                f"🕐 Schedule: {frequency} at {time_spec}\n"
                f"⏭️ Next run: {task.next_run}\n\n"
                f"Use /tasks to see all scheduled tasks.",
                parse_mode="Markdown"
            )
            return

    # Interactive mode - ask for prompt first
    pending_schedule_prompts[user_id] = {"step": "prompt", "chat_id": chat_id}
    await update.message.reply_text(
        "📅 Let's create a scheduled task!\n\n"
        "**Step 1:** What should Claude do? (Enter your prompt)\n\n"
        "Examples:\n"
        "• Give me a morning briefing with weather and news\n"
        "• Check my email and summarize important messages\n"
        "• Remind me to take a break\n"
        "• Search for today's tech news\n\n"
        "Or use inline format:\n"
        "`/schedule daily 9am \"Your prompt here\"`",
        parse_mode="Markdown"
    )


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tasks command - list all scheduled tasks for user"""
    user_id = update.effective_user.id
    tasks = task_scheduler.get_user_tasks(user_id)

    if not tasks:
        await update.message.reply_text(
            "📭 You have no scheduled tasks.\n\n"
            "Use /schedule to create one!"
        )
        return

    message = f"📋 **Your Scheduled Tasks** ({len(tasks)}):\n\n"

    for task in tasks:
        status = "⏸️ " if not task.enabled else "▶️ "
        next_run = task.next_run[:16].replace("T", " ") if task.next_run else "N/A"
        message += (
            f"{status}**{task.description[:40]}**\n"
            f"   🆔 `{task.task_id}`\n"
            f"   🕐 {task.frequency} at {task.time_spec}\n"
            f"   ⏭️ Next: {next_run}\n"
            f"   🔄 Runs: {task.run_count}\n\n"
        )

    message += (
        "**Commands:**\n"
        "/deletetask <id> - Delete a task\n"
        "/toggletask <id> - Pause/resume a task\n"
        "/runtask <id> - Run a task now"
    )

    await update.message.reply_text(message, parse_mode="Markdown")


async def delete_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /deletetask command - delete a scheduled task"""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("Usage: /deletetask <task_id>\n\nUse /tasks to see your task IDs.")
        return

    task_id = context.args[0]
    task = task_scheduler.get_task(task_id)

    if not task:
        await update.message.reply_text(f"❌ Task not found: {task_id}")
        return

    if task.user_id != user_id:
        await update.message.reply_text("⚠️ You can only delete your own tasks.")
        return

    if task_scheduler.remove_task(task_id):
        await update.message.reply_text(f"🗑️ Task deleted: {task.description}")
    else:
        await update.message.reply_text("❌ Failed to delete task.")


async def toggle_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /toggletask command - pause/resume a scheduled task"""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("Usage: /toggletask <task_id>\n\nUse /tasks to see your task IDs.")
        return

    task_id = context.args[0]
    task = task_scheduler.get_task(task_id)

    if not task:
        await update.message.reply_text(f"❌ Task not found: {task_id}")
        return

    if task.user_id != user_id:
        await update.message.reply_text("⚠️ You can only toggle your own tasks.")
        return

    new_status = task_scheduler.toggle_task(task_id)
    status_text = "▶️ ENABLED" if new_status else "⏸️ PAUSED"
    await update.message.reply_text(f"{status_text}: {task.description}")


async def run_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /runtask command - manually run a scheduled task now"""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("Usage: /runtask <task_id>\n\nUse /tasks to see your task IDs.")
        return

    task_id = context.args[0]
    task = task_scheduler.get_task(task_id)

    if not task:
        await update.message.reply_text(f"❌ Task not found: {task_id}")
        return

    if task.user_id != user_id:
        await update.message.reply_text("⚠️ You can only run your own tasks.")
        return

    await update.message.reply_text(f"🚀 Running task: {task.description}...")
    await task_scheduler.execute_task(task)


async def handle_schedule_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Handle the interactive schedule creation flow.
    Returns True if the message was handled as part of scheduling, False otherwise.
    """
    user_id = update.effective_user.id

    if user_id not in pending_schedule_prompts:
        return False

    state = pending_schedule_prompts[user_id]
    text = update.message.text

    if state["step"] == "prompt":
        # User entered their prompt, now ask for schedule
        state["prompt"] = text
        state["step"] = "schedule"

        await update.message.reply_text(
            f"✅ Got it! Your prompt:\n\"{text[:100]}{'...' if len(text) > 100 else ''}\"\n\n"
            "**Step 2:** When should this run?\n\n"
            "Examples:\n"
            "• daily at 9am\n"
            "• every monday at 10:30am\n"
            "• hourly at :30\n"
            "• every 2 hours\n"
            "• in 1 hour (one-time)\n"
            "• tomorrow at 8am (one-time)",
            parse_mode="Markdown"
        )
        return True

    elif state["step"] == "schedule":
        # User entered schedule, create the task
        prompt = state["prompt"]
        chat_id = state["chat_id"]
        frequency, time_spec = parse_schedule_input(text)

        task = task_scheduler.add_task(
            user_id=user_id,
            chat_id=chat_id,
            prompt=prompt,
            frequency=frequency,
            time_spec=time_spec,
            use_tools=True,
            description=prompt[:50]
        )

        del pending_schedule_prompts[user_id]

        await update.message.reply_text(
            f"✅ Scheduled task created!\n\n"
            f"🆔 ID: `{task.task_id}`\n"
            f"📝 Prompt: {prompt[:100]}\n"
            f"🕐 Schedule: {frequency} at {time_spec}\n"
            f"⏭️ Next run: {task.next_run}\n\n"
            f"Use /tasks to see all scheduled tasks.",
            parse_mode="Markdown"
        )
        return True

    return False

# ============= END SCHEDULED TASKS COMMANDS =============


async def song_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /song command - analyze currently playing Spotify track"""
    await update.message.reply_text("🎵 Checking Spotify...")

    # Get current track from Spotify via browser
    result = call_mac("get_spotify_track")

    if not result.get('success'):
        await update.message.reply_text(f"❌ Couldn't get track: {result.get('error', 'Unknown error')}\n\nMake sure Spotify is open in Chrome.")
        return

    track_info = result.get('track_info', {})
    audio_features = result.get('audio_features', {})
    lastfm = result.get('lastfm', {})
    track_name = track_info.get('name', 'Unknown')
    artist = track_info.get('artist', 'Unknown')
    album = track_info.get('album', '')

    await update.message.reply_text(f"🎧 Now playing: **{track_name}** by **{artist}**\n\n⏳ Analyzing...")

    # Build audio features section if available
    audio_section = ""
    if audio_features:
        audio_section = f"""

Spotify Audio Analysis Data:
- Danceability: {audio_features.get('danceability', 'N/A')} (0-1, how suitable for dancing)
- Energy: {audio_features.get('energy', 'N/A')} (0-1, intensity and activity)
- Valence: {audio_features.get('valence', 'N/A')} (0-1, musical positiveness/happiness)
- Tempo: {audio_features.get('tempo', 'N/A')} BPM
- Acousticness: {audio_features.get('acousticness', 'N/A')} (0-1, acoustic vs electronic)
- Instrumentalness: {audio_features.get('instrumentalness', 'N/A')} (0-1, vocal vs instrumental)
- Speechiness: {audio_features.get('speechiness', 'N/A')} (0-1, spoken words presence)
- Loudness: {audio_features.get('loudness', 'N/A')} dB
- Key: {audio_features.get('key', 'N/A')} (0=C, 1=C#, 2=D, etc.)
- Mode: {'Major' if audio_features.get('mode') == 1 else 'Minor' if audio_features.get('mode') == 0 else 'N/A'}
- Time Signature: {audio_features.get('time_signature', 'N/A')}/4"""

    # Build Last.fm section
    lastfm_section = ""
    logger.info(f"Last.fm data received: {lastfm}")
    if lastfm:
        tags = lastfm.get('tags', [])
        if tags:
            lastfm_section += f"\n\nLast.fm Tags: {', '.join(tags)}"
        if lastfm.get('listeners'):
            lastfm_section += f"\nListeners: {int(lastfm['listeners']):,} | Plays: {int(lastfm.get('playcount', 0)):,}"
        if lastfm.get('track_wiki'):
            lastfm_section += f"\n\nTrack Info: {lastfm['track_wiki']}"
        if lastfm.get('album_wiki'):
            lastfm_section += f"\n\nAlbum Info: {lastfm['album_wiki']}"
        if lastfm.get('artist_wiki'):
            lastfm_section += f"\n\nArtist Bio: {lastfm['artist_wiki']}"

    # Use Claude to analyze the track
    try:
        analysis_prompt = f"""Analyze this song in a fun, insightful way:

Song: "{track_name}"
Artist: {artist}
Album: {album if album else 'N/A'}{audio_section}{lastfm_section}

Provide a brief but engaging analysis covering:
1. 🎭 Mood/Vibe - What feeling does this song evoke?
2. 🎸 Musical Style - Genre, notable production elements
3. 🏷️ Tags & Context - Reference the Last.fm tags and wiki info to give cultural/historical context
4. 📝 Themes - What's the song about (use wiki info if available)?
5. 💡 Fun Fact - An interesting tidbit about the song or artist
6. 🎯 Perfect For - When/where is this song ideal to listen to?

Keep it concise but entertaining. Use emojis. Be conversational. Incorporate the Last.fm data naturally!"""

        response = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": analysis_prompt}]
        )
        analysis = response.content[0].text

        await update.message.reply_text(f"🎵 **{track_name}** - {artist}\n\n{analysis}")
    except Exception as e:
        await update.message.reply_text(f"🎵 **{track_name}** by {artist}\n\n❌ Couldn't generate analysis: {str(e)}")

async def send_telegram_message(chat_id: int, message: str):
    """Send a message to a Telegram chat - used by scheduler"""
    # This will be set when the application starts
    if hasattr(send_telegram_message, 'bot'):
        try:
            await send_telegram_message.bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:
            logger.error(f"Failed to send scheduled message: {e}")


def main():
    global task_scheduler

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Store bot reference for scheduler to use
    send_telegram_message.bot = application.bot

    # Initialize Task Scheduler
    task_scheduler = TaskScheduler(
        claude_client=claude_client,
        call_mac_func=call_mac,
        send_telegram_func=send_telegram_message,
        mac_tools=MAC_TOOLS
    )

    # Priority commands - registered first to ensure they're always responsive
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("cancel", stop_command))  # Alias for /stop
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("notes", notes_command))
    application.add_handler(CommandHandler("song", song_command))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("location", request_location))

    # Scheduled tasks commands
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("tasks", tasks_command))
    application.add_handler(CommandHandler("deletetask", delete_task_command))
    application.add_handler(CommandHandler("toggletask", toggle_task_command))
    application.add_handler(CommandHandler("runtask", run_task_command))

    application.add_handler(MessageHandler(filters.LOCATION, handle_location))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("=" * 50)
    print("Telegram Bot Started!")
    print("=" * 50)
    print(f"Mac Agent: {MAC_IP}:{MAC_PORT}")
    print(f"Scheduler: {task_scheduler.get_status()}")
    print("Ready for commands...")
    print("")

    # Start the scheduler when the event loop is running
    async def post_init(app):
        loop = asyncio.get_event_loop()
        task_scheduler.start(loop)
        logger.info("Task scheduler started")

    async def shutdown(app):
        task_scheduler.stop()
        logger.info("Task scheduler stopped")

    application.post_init = post_init
    application.post_shutdown = shutdown

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
