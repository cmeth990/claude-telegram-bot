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
            log(f"Region screenshot: x={x}, y={y}, w={width}, h={height}")
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
    import urllib.request

    js_code = f'''
    (function() {{
        const results = [];
        const images = document.querySelectorAll('img');

        for (const img of images) {{
            if (results.length >= {count}) break;
            if (img.naturalWidth < {min_width} || img.naturalHeight < {min_height}) continue;

            const rect = img.getBoundingClientRect();
            if (rect.width < 100 || rect.height < 100) continue;
            if (rect.bottom < 0 || rect.top > window.innerHeight) continue;

            let imgSrc = img.src;
            if (img.srcset) {{
                const srcsetParts = img.srcset.split(',').map(s => s.trim());
                const lastSrc = srcsetParts[srcsetParts.length - 1];
                if (lastSrc) imgSrc = lastSrc.split(' ')[0];
            }}

            if (!imgSrc || imgSrc.startsWith('data:')) continue;

            let link = img.closest('a');
            if (!link) {{
                let parent = img.parentElement;
                for (let i = 0; i < 5 && parent; i++) {{
                    if (parent.tagName === 'A') {{ link = parent; break; }}
                    parent = parent.parentElement;
                }}
            }}

            results.push({{
                src: imgSrc,
                url: link ? link.href : '',
                alt: img.alt || '',
                width: img.naturalWidth,
                height: img.naturalHeight
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
        return {'success': False, 'error': f'Could not parse: {e}'}

    if not images_data:
        return {'success': False, 'error': 'No images found', 'page_url': page_url}

    log(f"Found {len(images_data)} images, downloading...")

    downloaded = []
    for i, img in enumerate(images_data):
        src = img.get('src', '')
        if not src:
            continue

        log(f"  Image {i+1}: {src[:50]}...")

        try:
            req = urllib.request.Request(src, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
            })
            with urllib.request.urlopen(req, timeout=10) as response:
                image_data = response.read()
                image_b64 = base64.b64encode(image_data).decode('utf-8')

                downloaded.append({
                    'image_data': image_b64,
                    'url': img.get('url', ''),
                    'src': src,
                    'alt': img.get('alt', ''),
                    'width': img.get('width', 0),
                    'height': img.get('height', 0)
                })
        except Exception as e:
            log(f"    Failed: {e}")
            continue

    log(f"Downloaded {len(downloaded)} images")
    return {
        'success': True,
        'screenshots': downloaded,
        'count': len(downloaded),
        'page_title': page_title,
        'page_url': page_url
    }

_cached_images = []

def list_page_images(min_width=150, min_height=150):
    global _cached_images

    js_code = f'''
    (function() {{
        const results = [];
        const images = document.querySelectorAll('img');
        let idx = 0;

        for (const img of images) {{
            if (img.naturalWidth < {min_width} || img.naturalHeight < {min_height}) continue;

            const rect = img.getBoundingClientRect();
            if (rect.width < 100 || rect.height < 100) continue;
            if (rect.bottom < 0 || rect.top > window.innerHeight) continue;

            let imgSrc = img.src;
            if (img.srcset) {{
                const srcsetParts = img.srcset.split(',').map(s => s.trim());
                const lastSrc = srcsetParts[srcsetParts.length - 1];
                if (lastSrc) imgSrc = lastSrc.split(' ')[0];
            }}

            if (!imgSrc || imgSrc.startsWith('data:')) continue;

            let link = img.closest('a');
            if (!link) {{
                let parent = img.parentElement;
                for (let i = 0; i < 5 && parent; i++) {{
                    if (parent.tagName === 'A') {{ link = parent; break; }}
                    parent = parent.parentElement;
                }}
            }}

            results.push({{
                index: idx++,
                src: imgSrc,
                url: link ? link.href : '',
                alt: img.alt || '',
                width: img.naturalWidth,
                height: img.naturalHeight,
                top: Math.round(rect.top),
                left: Math.round(rect.left)
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

    log("Listing images on page...")
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
        return {'success': False, 'error': f'Could not parse: {e}'}

    _cached_images = images_data

    log(f"Found {len(images_data)} images")

    return {
        'success': True,
        'count': len(images_data),
        'page_title': page_title,
        'page_url': page_url,
        'images': [
            {
                'index': img['index'],
                'alt': img['alt'][:100] if img['alt'] else f"Image at ({img['left']}, {img['top']})",
                'width': img['width'],
                'height': img['height'],
                'position': f"({img['left']}, {img['top']})",
                'has_link': bool(img['url'])
            }
            for img in images_data
        ]
    }


def download_selected_images(indices):
    global _cached_images
    import urllib.request

    if not _cached_images:
        return {'success': False, 'error': 'No cached images. Call list_page_images first.'}

    if not indices:
        return {'success': False, 'error': 'No indices provided'}

    log(f"Downloading images at indices: {indices}")

    downloaded = []
    for idx in indices:
        if idx < 0 or idx >= len(_cached_images):
            continue

        img = _cached_images[idx]
        src = img.get('src', '')
        if not src:
            continue

        log(f"  Downloading image {idx}: {src[:50]}...")

        try:
            req = urllib.request.Request(src, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
            })
            with urllib.request.urlopen(req, timeout=10) as response:
                image_data = response.read()
                image_b64 = base64.b64encode(image_data).decode('utf-8')

                downloaded.append({
                    'image_data': image_b64,
                    'url': img.get('url', ''),
                    'src': src,
                    'alt': img.get('alt', ''),
                    'width': img.get('width', 0),
                    'height': img.get('height', 0),
                    'index': idx
                })
        except Exception as e:
            log(f"    Failed: {e}")
            continue

    log(f"Downloaded {len(downloaded)} images")
    return {
        'success': True,
        'screenshots': downloaded,
        'count': len(downloaded)
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
        return take_screenshot(mode=data.get('mode', 'full'), app_name=data.get('app_name'), region=data.get('region'))
    elif action == 'list_windows':
        return list_windows()
    elif action == 'get_window_bounds':
        return get_window_bounds(data.get('app_name', ''))
    elif action == 'scroll':
        return scroll_page(data.get('app_name', 'Google Chrome'), data.get('direction', 'down'), data.get('amount', 3))
    elif action == 'execute_js':
        return execute_js_in_chrome(data.get('js_code', ''))
    elif action == 'capture_images':
        return capture_webpage_images(count=data.get('count', 5), min_width=data.get('min_width', 150), min_height=data.get('min_height', 150))
    elif action == 'list_page_images':
        return list_page_images(min_width=data.get('min_width', 150), min_height=data.get('min_height', 150))
    elif action == 'download_selected_images':
        return download_selected_images(indices=data.get('indices', []))
    return {'success': False, 'error': 'Unknown action'}

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(('0.0.0.0', PORT))
server.listen(5)
print('=' * 50)
print('Mac Agent v4 - Visual Curation')
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
