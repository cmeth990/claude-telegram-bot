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
    {"name": "take_screenshot", "description": """Take a screenshot. Modes: 'full', 'window' (requires app_name), 'region' (requires region with x,y,width,height as SCREEN coordinates).""",
     "input_schema": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["full", "window", "region"]}, "app_name": {"type": "string"}, "region": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "width": {"type": "integer"}, "height": {"type": "integer"}}}}, "required": []}},
    {"name": "list_windows", "description": "List all open windows", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_window_bounds", "description": "Get window position and size", "input_schema": {"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]}},
    {"name": "scroll_page", "description": "Scroll page up or down", "input_schema": {"type": "object", "properties": {"app_name": {"type": "string"}, "direction": {"type": "string", "enum": ["down", "up"]}, "amount": {"type": "integer"}}, "required": []}},
    {"name": "execute_javascript_in_chrome", "description": "Execute JavaScript in Chrome's active tab", "input_schema": {"type": "object", "properties": {"js_code": {"type": "string"}}, "required": ["js_code"]}},
    {"name": "wait", "description": "Wait for seconds (1-30)", "input_schema": {"type": "object", "properties": {"seconds": {"type": "integer"}}, "required": ["seconds"]}},
    {"name": "check_mac_status", "description": "Check if Mac is online", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "capture_images", "description": """Capture multiple images from current webpage (auto-selects visible images).
Returns: {screenshots: [{image_data, url, alt, width, height}...], page_title, page_url, count}""",
     "input_schema": {"type": "object", "properties": {"count": {"type": "integer", "description": "Max images (default 5)"}, "min_width": {"type": "integer", "description": "Min width px (default 150)"}, "min_height": {"type": "integer", "description": "Min height px (default 150)"}}, "required": []}},
    {"name": "list_page_images", "description": """STEP 1 for visual curation: Get metadata about all images on current page.
Use this AFTER taking a window screenshot so you can see the page.
Returns index, position, size, alt text for each image. Then use download_selected_images with the indices you want.""",
     "input_schema": {"type": "object", "properties": {"min_width": {"type": "integer"}, "min_height": {"type": "integer"}}, "required": []}},
    {"name": "download_selected_images", "description": """STEP 2 for visual curation: Download specific images by index.
After viewing the page screenshot and calling list_page_images, use this to download only the images that match what the user wants.
Pass the indices of images you want to download.""",
     "input_schema": {"type": "object", "properties": {"indices": {"type": "array", "items": {"type": "integer"}, "description": "List of image indices to download (from list_page_images)"}}, "required": ["indices"]}}
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
    await update.message.reply_text(f"Welcome!\n\nMac: {mac_status}\n\nCommands: /start /clear /help")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_conversations[user_id] = []
    await update.message.reply_text("Cleared!")

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
                                screenshots_to_send.append({"data": image_result["image_data"], "mode": mode})
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
                            for i, img in enumerate(result["screenshots"]):
                                screenshots_to_send.append({"data": img["image_data"], "url": img.get("url", ""), "title": img.get("alt", f"Image {i+1}"), "mode": "capture", "description": f"{img.get('width', '?')}x{img.get('height', '?')}px"})
                            summary = {"success": True, "count": result["count"], "page_title": result.get("page_title", ""), "page_url": result.get("page_url", ""), "images": [{"url": s["url"], "alt": s.get("alt", ""), "width": s["width"], "height": s["height"]} for s in result["screenshots"]]}
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(summary)})
                        else:
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "list_page_images":
                        result = call_mac("list_page_images", min_width=tool_input.get("min_width", 150), min_height=tool_input.get("min_height", 150))
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "download_selected_images":
                        result = call_mac("download_selected_images", indices=tool_input.get("indices", []))
                        if result.get("success") and result.get("screenshots"):
                            for i, img in enumerate(result["screenshots"]):
                                screenshots_to_send.append({"data": img["image_data"], "url": img.get("url", ""), "title": img.get("alt", f"Image {img.get('index', i)}"), "mode": "selected", "description": f"{img.get('width', '?')}x{img.get('height', '?')}px"})
                            summary = {"success": True, "count": result["count"], "downloaded_indices": [s["index"] for s in result["screenshots"]]}
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(summary)})
                        else:
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
            for idx, screenshot in enumerate(screenshots_to_send):
                try:
                    screenshot_bytes = base64.b64decode(screenshot["data"])
                    caption_parts = [f"Screenshot {idx+1}"]
                    if screenshot.get("title"):
                        caption_parts.append(f"{screenshot['title']}")
                    if screenshot.get("description"):
                        caption_parts.append(f"\n{screenshot['description']}")
                    if screenshot.get("url"):
                        caption_parts.append(f"\n{screenshot['url']}")
                    caption = " ".join(caption_parts)
                    await update.message.reply_photo(photo=io.BytesIO(screenshot_bytes), caption=caption)
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
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Starting bot...")
    logger.info(f"Mac: {MAC_IP}:{MAC_PORT}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
