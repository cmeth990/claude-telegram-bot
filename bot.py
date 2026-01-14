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

MAC_TOOLS = [
    {"name": "execute_mac_command", "description": "Execute a shell command on the Mac", "input_schema": {"type": "object", "properties": {"command": {"type": "string", "description": "Shell command"}}, "required": ["command"]}},
    {"name": "execute_applescript", "description": "Execute AppleScript to control Mac applications", "input_schema": {"type": "object", "properties": {"script": {"type": "string", "description": "AppleScript code"}}, "required": ["script"]}},
    {"name": "read_mac_file", "description": "Read file contents", "input_schema": {"type": "object", "properties": {"filepath": {"type": "string"}}, "required": ["filepath"]}},
    {"name": "take_screenshot", "description": "Take a screenshot. Modes: 'full' (entire screen), 'window' (specific app window, requires app_name like 'Google Chrome' or 'Safari'), 'region' (specific coordinates, requires region with x, y, width, height)", "input_schema": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["full", "window", "region"], "description": "Screenshot mode"}, "app_name": {"type": "string", "description": "Application name for window mode (e.g. 'Google Chrome', 'Safari')"}, "region": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "width": {"type": "integer"}, "height": {"type": "integer"}}, "description": "Region coordinates for region mode"}}, "required": []}},
    {"name": "list_windows", "description": "List all open windows with their application names. Useful for finding window names before taking window-specific screenshots.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_window_bounds", "description": "Get the position and size of a specific application window", "input_schema": {"type": "object", "properties": {"app_name": {"type": "string", "description": "Application name"}}, "required": ["app_name"]}},
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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_conversations[update.effective_user.id] = []

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_conversations:
        user_conversations[user_id] = []
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
                    elif tool_name == "take_screenshot":
                        mode = tool_input.get("mode", "full")
                        app_name = tool_input.get("app_name")
                        region = tool_input.get("region")
                        result = call_mac("screenshot", mode=mode, app_name=app_name, region=region)
                        if result.get("success") and result.get("filepath"):
                            image_result = call_mac("read_image", filepath=result["filepath"])
                            if image_result.get("success") and image_result.get("image_data"):
                                screenshots_to_send.append({"data": image_result["image_data"], "mode": mode, "app": app_name})
                                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_result["image_data"]}}, {"type": "text", "text": f"Screenshot captured ({mode} mode). Please analyze."}]})
                            else:
                                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"success": False, "error": "Failed to read"})})
                        else:
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "check_mac_status":
                        result = call_mac("ping")
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
            
            for idx, screenshot in enumerate(screenshots_to_send):
                try:
                    screenshot_bytes = base64.b64decode(screenshot["data"])
                    caption = f"Screenshot {idx+1}"
                    if screenshot.get("mode"):
                        caption += f" ({screenshot['mode']} mode)"
                    if screenshot.get("app"):
                        caption += f" - {screenshot['app']}"
                    await update.message.reply_photo(photo=io.BytesIO(screenshot_bytes), caption=caption)
                    logger.info(f"Screenshot {idx+1} sent")
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Starting bot...")
    logger.info(f"Mac: {MAC_IP}:{MAC_PORT}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
