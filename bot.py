#!/usr/bin/env python3
import os, logging, json, socket, base64, io
from telegram import Update
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

MAC_TOOLS = [
    {"name": "execute_mac_command", "description": "Execute a shell command on the Mac", "input_schema": {"type": "object", "properties": {"command": {"type": "string", "description": "Shell command"}}, "required": ["command"]}},
    {"name": "execute_applescript", "description": "Execute AppleScript to control Mac applications", "input_schema": {"type": "object", "properties": {"script": {"type": "string", "description": "AppleScript code"}}, "required": ["script"]}},
    {"name": "read_mac_file", "description": "Read file contents", "input_schema": {"type": "object", "properties": {"filepath": {"type": "string"}}, "required": ["filepath"]}},
    {"name": "take_screenshot", "description": "Take a screenshot. Modes: 'full' (entire screen), 'window' (specific app window, requires app_name like 'Google Chrome'), 'region' (specific coordinates, requires region with x, y, width, height). Optional: include 'metadata' object with additional info like 'url', 'title', 'description' to attach to the screenshot.", "input_schema": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["full", "window", "region"], "description": "Screenshot mode"}, "app_name": {"type": "string", "description": "Application name for window mode"}, "region": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "width": {"type": "integer"}, "height": {"type": "integer"}}, "description": "Region coordinates"}, "metadata": {"type": "object", "properties": {"url": {"type": "string", "description": "URL associated with this screenshot (e.g., Pinterest pin link)"}, "title": {"type": "string", "description": "Title or description"}, "description": {"type": "string", "description": "Additional context"}}, "description": "Optional metadata to attach to screenshot"}}, "required": []}},
    {"name": "list_windows", "description": "List all open windows with their application names", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_window_bounds", "description": "Get the position and size of a specific application window", "input_schema": {"type": "object", "properties": {"app_name": {"type": "string", "description": "Application name"}}, "required": ["app_name"]}},
    {"name": "scroll_page", "description": "Scroll the page in the specified application. Direction: 'down' or 'up'. Amount: number of times to press arrow key (default 3)", "input_schema": {"type": "object", "properties": {"app_name": {"type": "string", "description": "Application name (default: Google Chrome)"}, "direction": {"type": "string", "enum": ["down", "up"], "description": "Scroll direction"}, "amount": {"type": "integer", "description": "Number of times to scroll (default 3)"}}, "required": []}},
    {"name": "execute_javascript_in_chrome", "description": "Execute JavaScript code in the active Chrome tab. Returns the result. Useful for extracting page data, URLs, element positions, etc. IMPORTANT for Pinterest: Use this selector to find actual pin IMAGE containers (not sidebar links): document.querySelectorAll('[data-test-id=\"pin\"], [data-test-id=\"pinWrapper\"], div[class*=\"pinWrapper\"]'). Get parent anchor tag for URL. Example: Array.from(document.querySelectorAll('[data-test-id=\"pin\"]')).slice(0,5).map(pin => {const a = pin.closest('a') || pin.querySelector('a[href*=\"/pin/\"]'); const rect = pin.getBoundingClientRect(); return {url: a?.href || 'no-url', x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}})", "input_schema": {"type": "object", "properties": {"js_code": {"type": "string", "description": "JavaScript code to execute"}}, "required": ["js_code"]}},
    {"name": "kill_mac_agent", "description": "Kill and restart the Mac agent process. Use this if the agent is stuck or not responding.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "check_mac_status", "description": "Check if Mac is online", "input_schema": {"type": "object", "properties": {}, "required": []}}
]

def call_mac(action, **kwargs):
    if not MAC_IP or not MAC_PORT or not MAC_SECRET:
        return {"success": False, "error": "Mac agent not configured"}
    try:
        request = {"secret": MAC_SECRET, "action": action, **kwargs}
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30.0)
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
    await update.message.reply_text(f"Welcome!\n\nMac: {mac_status}\n\nCommands: /start /clear /help /restart")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_conversations[user_id] = []
    if user_id in screenshot_metadata:
        screenshot_metadata[user_id] = []
    await update.message.reply_text("Cleared!")

async def restart_mac_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kill and restart the Mac agent and clear conversation"""
    user_id = update.effective_user.id
    await update.message.reply_text(
        "🔄 Restarting system...\n\n"
        "⚠️ This will:\n"
        "• Stop all running processes\n"
        "• Clear your conversation history\n"
        "• Require manual restart of Mac agent"
    )
    status_msg = "📊 Checking active processes...\n"
    try:
        check_result = call_mac("execute", command="ps aux | grep -E 'agent.py|screencapture|osascript' | grep -v grep | wc -l")
        if check_result.get("success"):
            process_count = check_result.get("stdout", "0").strip()
            status_msg += f"• Found {process_count} Mac agent process(es)\n"
    except:
        status_msg += "• Unable to check Mac processes\n"
    user_conversations[user_id] = []
    if user_id in screenshot_metadata:
        screenshot_metadata[user_id] = []
    status_msg += "• ✅ Conversation history cleared\n"
    status_msg += "• ✅ Screenshot queue cleared\n"
    await update.message.reply_text(status_msg)
    await update.message.reply_text("🛑 Stopping Mac agent...")
    try:
        kill_result = call_mac("execute", command="pkill -9 -f agent.py; pkill -9 screencapture; pkill -9 osascript")
        import asyncio
        await asyncio.sleep(1)
        verify_result = call_mac("execute", command="ps aux | grep -E 'agent.py|screencapture|osascript' | grep -v grep | wc -l")
        if verify_result.get("success"):
            remaining = verify_result.get("stdout", "0").strip()
            if remaining == "0":
                await update.message.reply_text(
                    "✅ All processes stopped!\n\n"
                    "📋 Verification:\n"
                    "• Mac agent: Stopped\n"
                    "• Screenshot processes: Stopped\n"
                    "• AppleScript processes: Stopped\n\n"
                    "⚠️ To restart Mac agent:\n"
                    "cd ~/claude_mac_agent && python3 agent.py"
                )
            else:
                await update.message.reply_text(
                    f"⚠️ Some processes still running ({remaining})\n\n"
                    "Run manually:\n"
                    "pkill -9 -f agent.py\n"
                    "pkill -9 screencapture\n"
                    "pkill -9 osascript\n\n"
                    "Then restart:\n"
                    "cd ~/claude_mac_agent && python3 agent.py"
                )
        else:
            await update.message.reply_text(
                "✅ Kill command sent!\n\n"
                "⚠️ To restart Mac agent:\n"
                "cd ~/claude_mac_agent && python3 agent.py"
            )
    except Exception as e:
        logger.error(f"Restart error: {e}")
        await update.message.reply_text(
            "⚠️ Could not verify process status\n\n"
            "Manually restart:\n"
            "pkill -9 -f agent.py && cd ~/claude_mac_agent && python3 agent.py"
        )

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_conversations:
        user_conversations[user_id] = []
    if user_id not in screenshot_metadata:
        screenshot_metadata[user_id] = []
    user_conversations[user_id].append({"role": "user", "content": update.message.text})
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        tools = MAC_TOOLS if (MAC_IP and MAC_PORT and MAC_SECRET) else None
        response = claude_client.messages.create(model="claude-sonnet-4-20250514", max_tokens=4096, messages=user_conversations[user_id], tools=tools if tools else anthropic.NOT_GIVEN)
        
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
                        app_name = tool_input.get("app_name", "Google Chrome")
                        direction = tool_input.get("direction", "down")
                        amount = tool_input.get("amount", 3)
                        result = call_mac("scroll", app_name=app_name, direction=direction, amount=amount)
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "execute_javascript_in_chrome":
                        result = call_mac("execute_js", js_code=tool_input["js_code"])
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "take_screenshot":
                        mode = tool_input.get("mode", "full")
                        app_name = tool_input.get("app_name")
                        region = tool_input.get("region")
                        metadata = tool_input.get("metadata", {})
                        result = call_mac("screenshot", mode=mode, app_name=app_name, region=region)
                        if result.get("success") and result.get("filepath"):
                            image_result = call_mac("read_image", filepath=result["filepath"])
                            if image_result.get("success") and image_result.get("image_data"):
                                screenshot_data = {
                                    "data": image_result["image_data"],
                                    "mode": mode,
                                    "app": app_name,
                                    "url": metadata.get("url"),
                                    "title": metadata.get("title"),
                                    "description": metadata.get("description")
                                }
                                screenshots_to_send.append(screenshot_data)
                                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_result["image_data"]}}, {"type": "text", "text": f"Screenshot captured ({mode} mode). Metadata: {json.dumps(metadata)}"}]})
                            else:
                                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"success": False, "error": "Failed to read"})})
                        else:
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "kill_mac_agent":
                        result = call_mac("kill_agent")
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "check_mac_status":
                        result = call_mac("ping")
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
            for idx, screenshot in enumerate(screenshots_to_send):
                try:
                    screenshot_bytes = base64.b64decode(screenshot["data"])
                    caption_parts = [f"Screenshot {idx+1}"]
                    if screenshot.get("title"):
                        caption_parts.append(f"📌 {screenshot['title']}")
                    if screenshot.get("mode"):
                        caption_parts.append(f"({screenshot['mode']} mode)")
                    if screenshot.get("app"):
                        caption_parts.append(f"- {screenshot['app']}")
                    if screenshot.get("description"):
                        caption_parts.append(f"\n{screenshot['description']}")
                    if screenshot.get("url"):
                        caption_parts.append(f"\n🔗 {screenshot['url']}")
                    caption = " ".join(caption_parts)
                    await update.message.reply_photo(photo=io.BytesIO(screenshot_bytes), caption=caption)
                    logger.info(f"Screenshot {idx+1} sent with metadata: {screenshot.get('url', 'no url')}")
                except Exception as e:
                    logger.error(f"Failed to send screenshot {idx+1}: {e}")
            user_conversations[user_id].append({"role": "user", "content": tool_results})
            response = claude_client.messages.create(model="claude-sonnet-4-20250514", max_tokens=4096, messages=user_conversations[user_id], tools=tools if tools else anthropic.NOT_GIVEN)
        
        assistant_message = ""
        for block in response.content:
            if hasattr(block, "text"):
                assistant_message += block.text
        user_conversations[user_id].append({"role": "assistant", "content": assistant_message})
        if len(user_conversations[user_id]) > 40:
            user_conversations[user_id] = user_conversations[user_id][-40:]
        if assistant_message:
            await update.message.reply_text(assistant_message)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await update.message.reply_text(f"Error: {str(e)}")

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Voice needs OPENAI_API_KEY")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Document processing available")

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
    application.add_handler(CommandHandler("restart", restart_mac_agent))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Starting bot...")
    logger.info(f"Mac: {MAC_IP}:{MAC_PORT}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
