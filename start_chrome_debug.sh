#!/bin/bash
# Start Chrome with remote debugging enabled for CDP browser automation
# This is required for the Uber ordering feature

# Kill any existing Chrome instances first (optional - uncomment if needed)
# pkill -f "Google Chrome"
# sleep 1

echo "Starting Chrome with remote debugging on port 9222..."
echo ""

# Start Chrome with debugging flags
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=9222 \
    --user-data-dir=/tmp/chrome-debug \
    --remote-allow-origins=* \
    &

sleep 2

# Test if debugging port is accessible
if curl -s http://localhost:9222/json > /dev/null 2>&1; then
    echo "✅ Chrome debug mode is running!"
    echo "   Port 9222 is accessible"
    echo ""
    echo "IMPORTANT: Log into Uber in this Chrome window before using the bot."
    echo ""
    curl -s http://localhost:9222/json | python3 -c "import json,sys; tabs=json.load(sys.stdin); print(f'   {len(tabs)} tab(s) available')"
else
    echo "❌ Failed to connect to Chrome debug port"
    echo "   Please check if Chrome started correctly"
fi
