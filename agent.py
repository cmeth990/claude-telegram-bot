#!/usr/bin/env python3
import socket
import json
import subprocess
import os
import base64
from datetime import datetime

PORT = 9999
SECRET = os.environ.get('MAC_AGENT_SECRET', '0eea2cc233ae59295e0ac411d45b1eb5a886d71c0376d2abfe481f0ade12f334')
SCREENSHOT_DIR = os.path.expanduser('~/Desktop')

def log(msg):
    """Simple logging with timestamp"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def execute_command(command):
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        return {'success': True, 'stdout': result.stdout[:5000], 'stderr': result.stderr[:1000], 'returncode': result.returncode}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def execute_applescript(script):
    try:
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=30)
        return {'success': result.returncode == 0, 'stdout': result.stdout.strip()[:5000], 'stderr': result.stderr[:1000]}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def read_file(filepath):
    try:
        filepath = os.path.expanduser(filepath)
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            return {'success': True, 'content': f.read(50000)}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def read_image(filepath):
    try:
        filepath = os.path.expanduser(filepath)
        if os.path.getsize(filepath) > 5*1024*1024:
            return {'success': False, 'error': 'File too large'}
        with open(filepath, 'rb') as f:
            return {'success': True, 'image_data': base64.b64encode(f.read()).decode('utf-8')}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def get_window_bounds(app_name):
    script = f'''
tell application "System Events"
    tell process "{app_name}"
        tell window 1
            set windowPosition to position
            set windowSize to size
            return (item 1 of windowPosition) & "," & (item 2 of windowPosition) & "," & (item 1 of windowSize) & "," & (item 2 of windowSize)
        end tell
    end tell
end tell
'''
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=10)
    if result.returncode == 0:
        try:
            x, y, w, h = result.stdout.strip().split(',')
            return {'success': True, 'x': int(x), 'y': int(y), 'width': int(w), 'height': int(h)}
        except:
            return {'success': False, 'error': 'Could not parse window bounds'}
    return {'success': False, 'error': result.stderr}

def take_screenshot(mode='full', app_name=None, region=None):
    """
    Take a screenshot.

    For region mode, coordinates MUST be SCREEN coordinates (not viewport).
    The bot should pass coordinates that already include window.screenX/Y offsets.
    macOS screencapture -R uses POINTS, not pixels.
    """
    try:
        filename = 'screenshot_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.png'
        filepath = os.path.join(SCREENSHOT_DIR, filename)

        if mode == 'window' and app_name:
            bounds = get_window_bounds(app_name)
            if not bounds.get('success'):
                return bounds
            log(f"Window screenshot for '{app_name}': x={bounds['x']}, y={bounds['y']}, w={bounds['width']}, h={bounds['height']}")
            cmd = ['screencapture', '-x', '-R', f"{bounds['x']},{bounds['y']},{bounds['width']},{bounds['height']}", filepath]
            subprocess.run(cmd, timeout=10)
        elif mode == 'region' and region:
            x = int(region.get('x', 0))
            y = int(region.get('y', 0))
            width = int(region.get('width', 800))
            height = int(region.get('height', 600))

            # Log the coordinates for debugging
            log(f"Region screenshot: x={x}, y={y}, w={width}, h={height}")

            # IMPORTANT: Coordinates should already be screen coords (with screenX/Y added)
            # macOS screencapture uses POINTS not pixels, so no DPR scaling needed
            cmd = ['screencapture', '-x', '-R', f"{x},{y},{width},{height}", filepath]
            subprocess.run(cmd, timeout=10)
        else:
            log("Full screen screenshot")
            subprocess.run(['screencapture', '-x', filepath], timeout=10)

        if os.path.exists(filepath):
            log(f"Screenshot saved: {filepath}")
            return {'success': True, 'filepath': filepath, 'mode': mode}
        return {'success': False, 'error': 'Screenshot failed'}
    except Exception as e:
        log(f"Screenshot error: {e}")
        return {'success': False, 'error': str(e)}

def list_windows():
    script = '''
tell application "System Events"
    set windowList to {}
    repeat with theProcess in (every process whose visible is true)
        set processName to name of theProcess
        try
            repeat with theWindow in (every window of theProcess)
                set windowName to name of theWindow
                set end of windowList to processName & " - " & windowName
            end repeat
        end try
    end repeat
    return windowList
end tell
'''
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=10)
    if result.returncode == 0:
        windows = [w.strip() for w in result.stdout.strip().split(',') if w.strip()]
        return {'success': True, 'windows': windows}
    return {'success': False, 'error': 'Could not list windows'}

def scroll_page(app_name='Google Chrome', direction='down', amount=3):
    """Scroll page using arrow keys"""
    script = f'''
tell application "{app_name}"
    activate
end tell
tell application "System Events"
    '''

    if direction == 'down':
        script += f'repeat {amount} times\n        key code 125\n        delay 0.1\n    end repeat'
    else:
        script += f'repeat {amount} times\n        key code 126\n        delay 0.1\n    end repeat'

    script += '\nend tell'

    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=15)
    if result.returncode == 0:
        return {'success': True, 'message': f'Scrolled {direction} {amount} times'}
    return {'success': False, 'error': result.stderr}

def execute_js_in_chrome(js_code):
    """Execute JavaScript in active Chrome tab"""
    # Escape quotes properly
    escaped_js = js_code.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')

    script = f'''
tell application "Google Chrome"
    tell active tab of front window
        execute javascript "{escaped_js}"
    end tell
end tell
'''
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=10)
    if result.returncode == 0:
        return {'success': True, 'result': result.stdout.strip()}
    return {'success': False, 'error': result.stderr}

def capture_webpage_images(count=5, min_width=150, min_height=150):
    """
    General-purpose webpage image capture.
    Finds visible images on the current page, screenshots them, and returns with links.
    Works on any website - Pinterest, fashion sites, apartment listings, etc.
    """
    import time

    # JavaScript to find all substantial images and their associated links
    js_code = f'''
    (function() {{
        const results = [];
        const images = document.querySelectorAll('img');

        for (const img of images) {{
            if (results.length >= {count}) break;

            const rect = img.getBoundingClientRect();

            // Skip small images (icons, thumbnails, avatars)
            if (rect.width < {min_width} || rect.height < {min_height}) continue;

            // Skip if not visible in viewport
            if (rect.bottom < 0 || rect.top > window.innerHeight) continue;
            if (rect.right < 0 || rect.left > window.innerWidth) continue;

            // Skip if mostly off-screen
            if (rect.top < -50 || rect.left < -50) continue;

            // Find the closest link
            let link = img.closest('a');
            if (!link) {{
                let parent = img.parentElement;
                for (let i = 0; i < 5 && parent; i++) {{
                    if (parent.tagName === 'A') {{
                        link = parent;
                        break;
                    }}
                    if (parent.onclick || parent.getAttribute('data-href')) {{
                        link = parent;
                        break;
                    }}
                    parent = parent.parentElement;
                }}
            }}

            const url = link ? (link.href || link.getAttribute('data-href') || '') : '';

            results.push({{
                x: Math.round(rect.left + window.screenX),
                y: Math.round(rect.top + window.screenY),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                url: url,
                alt: img.alt || '',
                src: img.src || ''
            }});
        }}

        return JSON.stringify(results);
    }})();
    '''

    escaped_js = js_code.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
    script = f'''
tell application "Google Chrome"
    set currentURL to URL of active tab of front window
    set pageTitle to title of active tab of front window
    tell active tab of front window
        set jsResult to execute javascript "{escaped_js}"
    end tell
    return pageTitle & "|||" & currentURL & "|||" & jsResult
end tell
'''

    log("Finding images on page...")
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=15)

    if result.returncode != 0:
        return {'success': False, 'error': f'JS failed: {result.stderr}'}

    try:
        parts = result.stdout.strip().split('|||')
        page_title = parts[0] if len(parts) > 0 else ''
        page_url = parts[1] if len(parts) > 1 else ''
        images_json = parts[2] if len(parts) > 2 else '[]'
        images_data = json.loads(images_json)
    except Exception as e:
        return {'success': False, 'error': f'Could not parse: {e} - {result.stdout}'}

    if not images_data:
        return {'success': False, 'error': 'No images found matching criteria', 'page_url': page_url}

    log(f"Found {len(images_data)} images, capturing screenshots...")

    screenshots = []
    for i, img in enumerate(images_data):
        filename = f"capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{i}.png"
        filepath = os.path.join(SCREENSHOT_DIR, filename)

        x, y, w, h = img['x'], img['y'], img['width'], img['height']
        log(f"  Image {i+1}: {w}x{h} at ({x},{y})")

        cmd = ['screencapture', '-x', '-R', f"{x},{y},{w},{h}", filepath]
        subprocess.run(cmd, timeout=10)

        if os.path.exists(filepath):
            # Read the image as base64
            with open(filepath, 'rb') as f:
                image_b64 = base64.b64encode(f.read()).decode('utf-8')

            screenshots.append({
                'filepath': filepath,
                'image_data': image_b64,
                'url': img['url'],
                'alt': img['alt'],
                'src': img['src'],
                'width': w,
                'height': h
            })

        time.sleep(0.1)

    log(f"Captured {len(screenshots)} screenshots")
    return {
        'success': True,
        'screenshots': screenshots,
        'count': len(screenshots),
        'page_title': page_title,
        'page_url': page_url
    }

def handle_request(data):
    action = data.get('action')
    if action == 'ping':
        return {'success': True, 'message': 'pong'}
    elif action == 'execute':
        return execute_command(data.get('command', ''))
    elif action == 'applescript':
        return execute_applescript(data.get('script', ''))
    elif action == 'read_file':
        return read_file(data.get('filepath', ''))
    elif action == 'read_image':
        return read_image(data.get('filepath', ''))
    elif action == 'screenshot':
        mode = data.get('mode', 'full')
        app_name = data.get('app_name')
        region = data.get('region')
        return take_screenshot(mode=mode, app_name=app_name, region=region)
    elif action == 'list_windows':
        return list_windows()
    elif action == 'get_window_bounds':
        return get_window_bounds(data.get('app_name', ''))
    elif action == 'scroll':
        return scroll_page(data.get('app_name', 'Google Chrome'), data.get('direction', 'down'), data.get('amount', 3))
    elif action == 'execute_js':
        return execute_js_in_chrome(data.get('js_code', ''))
    elif action == 'capture_images':
        return capture_webpage_images(
            count=data.get('count', 5),
            min_width=data.get('min_width', 150),
            min_height=data.get('min_height', 150)
        )
    return {'success': False, 'error': 'Unknown action'}

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(('0.0.0.0', PORT))
server.listen(5)
print('=' * 50)
print('Mac Agent v2 - With coordinate logging')
print('=' * 50)
print(f'Port: {PORT}')
print(f'Screenshots: {SCREENSHOT_DIR}')
print('')

try:
    while True:
        try:
            client, addr = server.accept()
            log(f'Connection from {addr}')
            client.settimeout(30)
            chunks = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                try:
                    json.loads(b''.join(chunks).decode('utf-8', errors='ignore'))
                    break
                except:
                    continue
            if chunks:
                req = json.loads(b''.join(chunks).decode('utf-8', errors='ignore'))
                if req.get('secret') == SECRET:
                    log(f"Action: {req.get('action')}")
                    resp = handle_request(req)
                else:
                    resp = {'success': False, 'error': 'Invalid secret'}
                client.sendall(json.dumps(resp).encode('utf-8'))
            client.close()
            log('Done\n')
        except Exception as e:
            log(f'Error: {e}')
            try:
                client.close()
            except:
                pass
except KeyboardInterrupt:
    print('\nShutting down...')
finally:
    server.close()
