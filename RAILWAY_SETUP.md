# Railway Deployment - Hybrid Setup Guide

## Overview

This bot runs in **hybrid mode**:
- **Railway (Cloud)**: Telegram bot runs 24/7, handles chat, scheduled tasks, web search
- **Local Mac Agent**: Optional - enables browser control, screenshots, Uber ordering, etc.

## Cloud Mode Features (Always Available)
- Chat with Claude
- Web search (via DuckDuckGo)
- Scheduled tasks
- Get current time
- Location storage

## Full Mode Features (Requires Mac Agent)
- Browser control (Chrome)
- Screenshots
- Uber/Uber Eats ordering
- Apple Notes
- Spotify track analysis
- File operations
- AppleScript automation

---

## Railway Deployment Steps

### 1. Push to GitHub
```bash
cd ~/Desktop/Claude_Bot/claude_telegram_bot
git add .
git commit -m "Add Railway deployment files"
git push
```

### 2. Deploy on Railway

1. Go to [railway.app](https://railway.app)
2. Click "New Project" → "Deploy from GitHub repo"
3. Select your repository
4. Railway will auto-detect the Procfile

### 3. Set Environment Variables in Railway

Go to your service → Variables tab → Add these:

| Variable | Value |
|----------|-------|
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token |
| `CLAUDE_API_KEY` | Your Anthropic API key |
| `MAC_IP` | Your Mac's public IP (or leave empty for cloud-only) |
| `MAC_PORT` | 9999 (or your agent port) |
| `MAC_SECRET` | Your agent secret |

**For Cloud-Only Mode**: Leave `MAC_IP`, `MAC_PORT`, and `MAC_SECRET` empty.

### 4. Deploy
Railway will automatically deploy. Check the logs to confirm the bot started.

---

## Running Mac Agent Locally

When you want full features, run the agent on your Mac:

```bash
cd ~/Desktop/Claude_Bot/claude_mac_agent
python3 agent.py
```

### Making Mac Agent Accessible from Railway

Your Mac needs to be reachable from the internet:

**Option A: Port Forwarding (Router)**
1. Forward port 9999 to your Mac's local IP
2. Get your public IP from https://whatismyip.com
3. Set `MAC_IP` in Railway to your public IP

**Option B: ngrok (Easier)**
```bash
# Install ngrok
brew install ngrok

# Expose port 9999
ngrok tcp 9999
```
Use the ngrok forwarding address (e.g., `0.tcp.ngrok.io:12345`) in Railway.

**Option C: Tailscale (Best for Security)**
1. Install Tailscale on Mac and a small cloud VM
2. Use Tailscale IP addresses for secure private networking

---

## Switching Modes

The bot automatically detects if the Mac agent is online:

- **Mac Online** → Full Mode (all features)
- **Mac Offline** → Cloud Mode (chat, search, scheduled tasks)

Use `/start` to check current mode.

---

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | From @BotFather |
| `CLAUDE_API_KEY` | Yes | Anthropic API key |
| `MAC_IP` | No | Mac agent IP address |
| `MAC_PORT` | No | Mac agent port (default: 9999) |
| `MAC_SECRET` | No | Mac agent authentication secret |

---

## Troubleshooting

### Bot not responding
- Check Railway logs for errors
- Verify `TELEGRAM_BOT_TOKEN` is correct

### Mac features not working
- Run `/start` to check Mac status
- Ensure agent.py is running on Mac
- Check network connectivity (firewall, port forwarding)

### Scheduled tasks not running
- Tasks are stored in `scheduled_tasks.json`
- Use `/tasks` to see scheduled tasks
- Check Railway logs for execution errors
