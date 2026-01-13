#!/usr/bin/env python3
"""
Claude Telegram Bot with Mac Control
"""

import os
import logging
import json
import socket
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import anthropic
from pathlib import Path

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
MAC_IP = os.environ.get('MAC_IP', '')
MAC_PORT = int(os.environ.get('MAC_PORT', '0'))
MAC_SECRET = os.environ.get('MAC_SECRET', '')

if not TELEGRAM_TOKEN or not CLAUDE_API_KEY:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or CLAUDE_API_KEY")

claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
user_conversations = {}

MAC_TOOLS = [
    {"name": "execute_mac_command", "description": "Execute a shell command on the user's Mac", "input_schema": {"type": "object", "properties": {"command": {"type": "string", "description": "Shell command to execute"}}, "required": ["command"]}},
    {"name": "execute_applescript", "description": "Execute AppleScript to control Mac applications", "input_schema": {"type": "object", "properties": {"script": {"type": "string", "description": "AppleScript code"}}, "required": ["script"]}},
    {"name": "read_mac_file", "description": "Read file contents (50KB limit, home directory only)", "input_schema": {"type": "object", "properties": {"filepath": {"type": "string", "description": "File path (~ for home)"}}, "required": ["filepath"]}},
    {"name": "take_screenshot", "description": "Take screenshot and return for analysis", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "check_mac_status", "description": "Check Mac online status", "input_schema": {"type": "object", "properties": {}, "required": []}}
]

def call_mac(action, **kwargs):
    if not MAC_IP or not MAC_PORT or not MAC_SECRET:
        return {'success': False, 'error': 'Mac agent not configured'}
    try:
        request = {'secret': MAC_SECRET, 'action': action, **kwargs}
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30.0)
        sock.connect((MAC_IP, MAC_PORT))
        sock.sendall(json.dumps(request).encode('utf-8'))
        response_chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_chunks.append(chunk)
        sock.close()
        return json.loads(b''.join(response_chunks).decode('utf-8'))
    except socket.timeout:
        return {'success': False, 'error': 'Connection timed out'}
    except ConnectionRefusedError:
        return {'success': False, 'error': 'Connection refused'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_conversations[user_id] = []
    mac_status = "❌ Offline"
    if MAC_IP and MAC_PORT and MAC_SECRET:
        if call_mac('ping').get('success'):
            mac_status = "✅ Online"
    await update.message.reply_text(f"👋 Welcome to Claude AI Bot with Mac Control!\n\nMac Status: {mac_status}\n\nCommands:\n/start /clear /help")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_conversations[update.effective_user.id] = []
    await update.message.reply_text("✅ Conversation cleared!")

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
            screenshot_to_send = None
            
            for block in assistant_content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input
                    logger.info(f"Tool: {tool_name}")
                    
                    if tool_name == "execute_mac_command":
                        result = call_mac('execute', command=tool_input['command'])
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "execute_applescript":
                        result = call_mac('applescript', script=tool_input['script'])
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "read_mac_file":
                        result = call_mac('read_file', filepath=tool_input['filepath'])
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "take_screenshot":
                        result = call_mac('screenshot')
                        if result.get('success') and result.get('filepath'):
                            image_result = call_mac('read_image', filepath=result['filepath'])
                            if image_result.get('success') and image_result.get('image_data'):
                                screenshot_to_send = image_result['image_data']
                                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({'success': True, 'message': 'Screenshot taken'})})
                                user_conversations[user_id].append({"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_result['image_data']}}, {"type": "text", "text": "Here is the screenshot. Please analyze it."}]})
                            else:
                                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({'success': False, 'error': image_result.get('error', 'Failed to read screenshot')})})
                        else:
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                    elif tool_name == "check_mac_status":
                        result = call_mac('ping')
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
            
            if screenshot_to_send:
                import base64, io
                await update.message.reply_photo(photo=io.BytesIO(base64.b64decode(screenshot_to_send)), caption="📸 Screenshot from your Mac")
            
            user_conversations[user_id].append({"role": "user", "content": tool_results})
            response = claude_client.messages.create(model="claude-sonnet-4-20250514", max_tokens=4096, messages=user_conversations[user_id], tools=tools if tools else anthropic.NOT_GIVEN)
        
        assistant_message = ""
        for block in response.content:
            if hasattr(block, 'text'):
                assistant_message += block.text
        user_conversations[user_id].append({"role": "assistant", "content": assistant_message})
        if len(user_conversations[user_id]) > 40:
            user_conversations[user_id] = user_conversations[user_id][-40:]
        if assistant_message:
            await update.message.reply_text(assistant_message)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {str(e)}\n\nTry /clear")

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("🎤 Processing voice...")
    try:
        voice_file = await update.message.voice.get_file()
        voice_path = f"/tmp/voice_{user_id}.ogg"
        await voice_file.download_to_drive(voice_path)
        if OPENAI_API_KEY:
            from openai import OpenAI
            openai_client = OpenAI(api_key=OPENAI_API_KEY)
            with open(voice_path, 'rb') as audio_file:
                transcript = openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
                transcribed_text = transcript.text
            if user_id not in user_conversations:
                user_conversations[user_id] = []
            user_conversations[user_id].append({"role": "user", "content": f"[Voice]: {transcribed_text}"})
            response = claude_client.messages.create(model="claude-sonnet-4-20250514", max_tokens=2048, messages=user_conversations[user_id])
            assistant_message = response.content[0].text
            user_conversations[user_id].append({"role": "assistant", "content": assistant_message})
            if os.path.exists(voice_path):
                os.remove(voice_path)
            await update.message.reply_text(f"🎤 \"{transcribed_text}\"\n\n{assistant_message}")
        else:
            if os.path.exists(voice_path):
                os.remove(voice_path)
            await update.message.reply_text("Voice transcription requires OPENAI_API_KEY")
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    document = update.message.document
    await update.message.reply_text("📄 Processing document...")
    try:
        doc_file = await document.get_file()
        file_extension = Path(document.file_name).suffix.lower()
        doc_path = f"/tmp/doc_{user_id}{file_extension}"
        await doc_file.download_to_drive(doc_path)
        
        if file_extension in ['.txt', '.md', '.py', '.js', '.json', '.csv']:
            with open(doc_path, 'r', encoding='utf-8') as f:
                content = f.read()
            if len(content) > 10000:
                content = content[:10000] + "\n... (truncated)"
            message = f"File '{document.file_name}':\n\n```\n{content}\n```"
        elif file_extension in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
            import base64
            with open(doc_path, 'rb') as f:
                image_data = base64.standard_b64encode(f.read()).decode('utf-8')
            media_type = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp'}.get(file_extension, 'image/jpeg')
            if user_id not in user_conversations:
                user_conversations[user_id] = []
            user_conversations[user_id].append({"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}}, {"type": "text", "text": "Analyze this image"}]})
            response = claude_client.messages.create(model="claude-sonnet-4-20250514", max_tokens=2048, messages=user_conversations[user_id])
            assistant_message = response.content[0].text
            user_conversations[user_id].append({"role": "assistant", "content": assistant_message})
            if os.path.exists(doc_path):
                os.remove(doc_path)
            await update.message.reply_text(f"🖼️ {assistant_message}")
            return
        else:
            message = f"File '{document.file_name}' type not supported"
        
        if user_id not in user_conversations:
            user_conversations[user_id] = []
        user_conversations[user_id].append({"role": "user", "content": message})
        response = claude_client.messages.create(model="claude-sonnet-4-20250514", max_tokens=4096, messages=user_conversations[user_id])
        assistant_message = response.content[0].text
        user_conversations[user_id].append({"role": "assistant", "content": assistant_message})
        if os.path.exists(doc_path):
            os.remove(doc_path)
        await update.message.reply_text(assistant_message)
    except Exception as e:
        logger.error(f"Document error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("🖼️ Analyzing image...")
    try:
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        photo_path = f"/tmp/photo_{user_id}.jpg"
        await photo_file.download_to_drive(photo_path)
        import base64
        with open(photo_path, 'rb') as f:
            image_data = base64.standard_b64encode(f.read()).decode('utf-8')
        if user_id not in user_conversations:
            user_conversations[user_id] = []
        caption = update.message.caption or "Analyze this image"
        user_conversations[user_id].append({"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data}}, {"type": "text", "text": caption}]})
        response = claude_client.messages.create(model="claude-sonnet-4-20250514", max_tokens=2048, messages=user_conversations[user_id])
        assistant_message = response.content[0].text
        user_conversations[user_id].append({"role": "assistant", "content": assistant_message})
        if os.path.exists(photo_path):
            os.remove(photo_path)
        await update.message.reply_text(assistant_message)
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Starting Claude Telegram Bot with Mac Control...")
    logger.info(f"Mac: {MAC_IP}:{MAC_PORT} ({'configured' if MAC_IP and MAC_PORT else 'not configured'})")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
