#!/usr/bin/env python3
import os, logging, json, socket, base64, io
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import anthropic
from pathlib import Path

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
    Returns progress updates and final order status.""",
     "input_schema": {"type": "object", "properties": {
         "cuisine_type": {"type": "string", "description": "Type of cuisine to search for (pizza, sushi, mexican, etc.)"},
         "surprise_me": {"type": "boolean", "description": "If true, auto-select best restaurant and top-rated dish", "default": False}
     }, "required": []}}
]

def call_mac(action, timeout=30.0, **kwargs):
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

    await update.message.reply_text(
        f"Welcome!\n\nMac: {mac_status}\nLocation: {location_status}\n\nCommands: /start /clear /help /location"
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
    if user_id not in user_conversations:
        user_conversations[user_id] = []
    user_conversations[user_id].append({"role": "user", "content": update.message.text})
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        tools = MAC_TOOLS if (MAC_IP and MAC_PORT and MAC_SECRET) else None
        # Build dynamic system prompt with user location if available
        location_context = ""
        if user_id in user_locations:
            loc = user_locations[user_id]
            loc_coords = f"{loc['lat']}, {loc['lon']}"
            loc_display = loc.get('address', loc_coords)
            location_context = f"\n\nUSER LOCATION: The user has shared their location: {loc_display} (lat: {loc['lat']}, lon: {loc['lon']})"

        system_prompt = f"""You control a Mac with Chrome browser.

AFTER USER SHARES LOCATION:
When user shares location, they see two options:
1. Enter a destination → Uber ride
2. Type 'food' → Uber Eats

UBER RIDES:
- If user types a destination (address, place name, airport, etc.): Order an Uber ride
- MANDATORY: Ask "More than 4 passengers?" BEFORE calling order_uber
  - If YES: use num_passengers=5 (selects UberXL)
  - If NO: use num_passengers=1 (selects UberX)
- Only after user answers, call order_uber with destination AND num_passengers
- DO NOT try to manually control the browser for Uber - just use order_uber

UBER EATS:
- If user types 'food' or asks about Uber Eats, ask them:
  "What type of food? You can:
   • Type a cuisine (pizza, sushi, mexican, thai, etc.)
   • Type 'surprise me' for a top-rated recommendation"

- If user picks a cuisine: call order_uber_eats with cuisine_type set to their choice
- If user says 'surprise me': call order_uber_eats with surprise_me=true
  This will find the best-rated restaurant, research top dishes online, and order the #1 recommended item

LOCATION CHECK:
- If user tries to order without sharing location, tell them to use /location command first
{location_context}

IMAGE CAPTURE:
USE capture_images - it's the simplest and most reliable option. It downloads actual image files (not screenshots) with their source links in one step.
Example: capture_images with count=5 will get 5 images from the current page.
AVOID take_screenshot for webpage images - it creates overlapping crops. Only use take_screenshot for actual screen captures."""
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
                            if surprise_me:
                                await update.message.reply_text("🍔 Finding the best restaurant and top-rated dish for you...")
                            else:
                                await update.message.reply_text(f"🍔 Searching for {cuisine_type} restaurants near you...")
                            # Call the Uber Eats automation - longer timeout for research
                            result = call_mac(
                                "order_uber_eats",
                                timeout=180.0,
                                pickup_lat=loc["lat"],
                                pickup_lon=loc["lon"],
                                pickup_address=loc.get("address", ""),
                                cuisine_type=cuisine_type,
                                surprise_me=surprise_me
                            )
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

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("location", request_location))
    application.add_handler(MessageHandler(filters.LOCATION, handle_location))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Starting bot...")
    logger.info(f"Mac: {MAC_IP}:{MAC_PORT}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
