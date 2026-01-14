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

    # Simplified and faster JS
    js_code = f'''
    (function() {{
        var r = [];
        var imgs = document.querySelectorAll('img');
        for (var i = 0; i < imgs.length && r.length < {count}; i++) {{
            var img = imgs[i];
            if (img.naturalWidth < {min_width} || img.naturalHeight < {min_height}) continue;
            var rect = img.getBoundingClientRect();
            if (rect.width < 80 || rect.height < 80) continue;
            var src = img.src;
            if (img.srcset) {{
                var parts = img.srcset.split(',');
                var last = parts[parts.length - 1].trim().split(' ')[0];
                if (last) src = last;
            }}
            if (!src || src.indexOf('data:') === 0) continue;
            var link = img.closest('a');
            r.push({{s: src, u: link ? link.href : '', a: img.alt || '', w: img.naturalWidth, h: img.naturalHeight}});
        }}
        return JSON.stringify(r);
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
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        return {'success': False, 'error': f'JS failed: {result.stderr}'}

    try:
        parts = result.stdout.strip().split('|||')
        page_title = parts[0] if len(parts) > 0 else ''
        page_url = parts[1] if len(parts) > 1 else ''
        images_json = parts[2] if len(parts) > 2 else '[]'
        raw_data = json.loads(images_json)
        # Convert short keys back to full names
        images_data = [
            {
                'src': img.get('s', ''),
                'url': img.get('u', ''),
                'alt': img.get('a', ''),
                'width': img.get('w', 0),
                'height': img.get('h', 0)
            }
            for img in raw_data
        ]
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

    # Simplified JS - faster execution
    js_code = f'''
    (function() {{
        var r = [];
        var imgs = document.querySelectorAll('img');
        var idx = 0;
        for (var i = 0; i < imgs.length && idx < 30; i++) {{
            var img = imgs[i];
            if (img.naturalWidth < {min_width} || img.naturalHeight < {min_height}) continue;
            var rect = img.getBoundingClientRect();
            if (rect.width < 80 || rect.height < 80) continue;
            var src = img.src;
            if (img.srcset) {{
                var parts = img.srcset.split(',');
                var last = parts[parts.length - 1].trim().split(' ')[0];
                if (last) src = last;
            }}
            if (!src || src.indexOf('data:') === 0) continue;
            var link = img.closest('a');
            r.push({{i: idx++, s: src, u: link ? link.href : '', a: img.alt || '', w: img.naturalWidth, h: img.naturalHeight}});
        }}
        return JSON.stringify(r);
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
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        return {'success': False, 'error': f'JS failed: {result.stderr}'}

    try:
        parts = result.stdout.strip().split('|||')
        page_title = parts[0] if len(parts) > 0 else ''
        page_url = parts[1] if len(parts) > 1 else ''
        images_json = parts[2] if len(parts) > 2 else '[]'
        images_data = json.loads(images_json)
        # Convert short keys back to full names
        images_data = [
            {
                'index': img.get('i', 0),
                'src': img.get('s', ''),
                'url': img.get('u', ''),
                'alt': img.get('a', ''),
                'width': img.get('w', 0),
                'height': img.get('h', 0)
            }
            for img in images_data
        ]
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
                'alt': img['alt'][:100] if img['alt'] else f"Image {img['index']}",
                'width': img['width'],
                'height': img['height'],
                'has_link': bool(img['url'])
            }
            for img in images_data
        ]
    }


# ============================================================================
# UBER AUTOMATION - Hybrid Visual + DOM approach
# ============================================================================

def uber_open_app(pickup_lat=None, pickup_lon=None):
    """
    Step 1: Open Uber mobile web app in Chrome.
    Optionally pre-set pickup coordinates via URL parameter.
    Returns success and current page state for visual analysis.
    """
    import time
    import urllib.parse

    log(f"Opening Uber app...")

    # Build URL - with or without pickup coordinates
    if pickup_lat and pickup_lon:
        uber_url = "https://m.uber.com/go/product-selection"
    else:
        uber_url = "https://m.uber.com"

    # Open Chrome and navigate
    open_script = '''
tell application "Google Chrome"
    activate
    if (count of windows) = 0 then
        make new window
    end if
    set URL of active tab of front window to "''' + uber_url + '''"
end tell
'''
    result = subprocess.run(['osascript', '-e', open_script], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return {'success': False, 'error': 'Failed to open Chrome: ' + result.stderr}

    log("Opened Uber, waiting for page load...")
    time.sleep(3)

    return {
        'success': True,
        'message': 'Uber app opened in Chrome',
        'url': uber_url,
        'next_step': 'Take a screenshot to see current state (login required? pickup set?)'
    }


def uber_get_page_state():
    """
    Analyze current Uber page state by extracting visible text and elements.
    Returns structured info about what's on screen.
    """
    import time

    js_code = '''
    (function() {
        var state = {
            url: window.location.href,
            title: document.title,
            isLoggedIn: false,
            hasPickup: false,
            hasDestination: false,
            rideOptions: [],
            visibleButtons: [],
            visibleInputs: [],
            pageText: ''
        };

        // Check for login indicators
        var loginBtn = document.querySelector('[data-testid="login-button"]') ||
                      document.querySelector('a[href*="login"]') ||
                      Array.from(document.querySelectorAll('button')).find(b =>
                          b.textContent.toLowerCase().includes('sign in') ||
                          b.textContent.toLowerCase().includes('log in'));
        state.isLoggedIn = !loginBtn;

        // Check for pickup/destination
        var inputs = document.querySelectorAll('input');
        inputs.forEach(function(inp) {
            var placeholder = (inp.placeholder || '').toLowerCase();
            var value = inp.value || '';
            state.visibleInputs.push({
                placeholder: inp.placeholder,
                value: value,
                id: inp.id,
                name: inp.name
            });
            if (placeholder.includes('pickup') || placeholder.includes('from')) {
                state.hasPickup = value.length > 0;
            }
            if (placeholder.includes('destination') || placeholder.includes('where') || placeholder.includes('to')) {
                state.hasDestination = value.length > 0;
            }
        });

        // Find clickable buttons
        var buttons = document.querySelectorAll('button, [role="button"], a[href]');
        buttons.forEach(function(btn) {
            var text = (btn.textContent || '').trim().substring(0, 50);
            if (text && !text.includes('\\n')) {
                state.visibleButtons.push(text);
            }
        });

        // Extract ride options if visible
        var rideCards = document.querySelectorAll('[data-testid*="product"], [class*="product"]');
        rideCards.forEach(function(card) {
            var name = card.querySelector('[class*="name"], [class*="title"]');
            var price = card.querySelector('[class*="price"], [class*="fare"]');
            if (name) {
                state.rideOptions.push({
                    name: (name.textContent || '').trim(),
                    price: price ? (price.textContent || '').trim() : ''
                });
            }
        });

        // Get visible page text for context
        state.pageText = document.body.innerText.substring(0, 2000);

        return JSON.stringify(state);
    })();
    '''

    escaped_js = js_code.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
    script = f'''
tell application "Google Chrome"
    tell active tab of front window
        execute javascript "{escaped_js}"
    end tell
end tell
'''

    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=15)

    if result.returncode != 0:
        return {'success': False, 'error': f'Failed to analyze page: {result.stderr}'}

    try:
        state = json.loads(result.stdout.strip())
        return {'success': True, 'state': state}
    except:
        return {'success': True, 'state': {'raw': result.stdout.strip()}}


def uber_click_element(selector=None, text_contains=None, element_type='button'):
    """
    Click an element on the Uber page.
    Can target by CSS selector or by text content.
    """
    import time

    if selector:
        js_code = f'''
        (function() {{
            var el = document.querySelector('{selector}');
            if (el) {{
                el.click();
                return JSON.stringify({{success: true, clicked: '{selector}'}});
            }}
            return JSON.stringify({{success: false, error: 'Element not found: {selector}'}});
        }})();
        '''
    elif text_contains:
        safe_text = text_contains.replace("'", "\\'")
        js_code = f'''
        (function() {{
            var elements = document.querySelectorAll('{element_type}, [role="button"], a');
            for (var i = 0; i < elements.length; i++) {{
                var el = elements[i];
                if (el.textContent.toLowerCase().includes('{safe_text.lower()}')) {{
                    el.click();
                    return JSON.stringify({{success: true, clicked: el.textContent.trim().substring(0, 50)}});
                }}
            }}
            return JSON.stringify({{success: false, error: 'No element containing "{safe_text}" found'}});
        }})();
        '''
    else:
        return {'success': False, 'error': 'Must provide selector or text_contains'}

    escaped_js = js_code.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
    script = f'''
tell application "Google Chrome"
    tell active tab of front window
        execute javascript "{escaped_js}"
    end tell
end tell
'''

    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=10)

    if result.returncode != 0:
        return {'success': False, 'error': result.stderr}

    try:
        return json.loads(result.stdout.strip())
    except:
        return {'success': True, 'result': result.stdout.strip()}


def uber_type_text(text, selector=None, clear_first=True):
    """
    Type text into an input field.
    Can target by selector or will find the focused/active input.
    """
    import time

    safe_text = text.replace('\\', '\\\\').replace("'", "\\'").replace('"', '\\"')

    if selector:
        js_code = f'''
        (function() {{
            var el = document.querySelector('{selector}');
            if (!el) return JSON.stringify({{success: false, error: 'Input not found'}});
            el.focus();
            {'el.value = "";' if clear_first else ''}
            el.value = '{safe_text}';
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return JSON.stringify({{success: true, typed: '{safe_text}'}});
        }})();
        '''
    else:
        js_code = f'''
        (function() {{
            var el = document.activeElement;
            if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA')) {{
                // Try to find a visible input
                var inputs = document.querySelectorAll('input:not([type="hidden"])');
                for (var i = 0; i < inputs.length; i++) {{
                    var rect = inputs[i].getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {{
                        el = inputs[i];
                        break;
                    }}
                }}
            }}
            if (!el) return JSON.stringify({{success: false, error: 'No input found'}});
            el.focus();
            {'el.value = "";' if clear_first else ''}
            el.value = '{safe_text}';
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return JSON.stringify({{success: true, typed: '{safe_text}', element: el.tagName}});
        }})();
        '''

    escaped_js = js_code.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
    script = f'''
tell application "Google Chrome"
    tell active tab of front window
        execute javascript "{escaped_js}"
    end tell
end tell
'''

    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=10)

    if result.returncode != 0:
        return {'success': False, 'error': result.stderr}

    try:
        return json.loads(result.stdout.strip())
    except:
        return {'success': True, 'result': result.stdout.strip()}


def uber_set_location(location_type, lat, lon, address=''):
    """
    Set pickup or destination location using coordinates.
    location_type: 'pickup' or 'destination'
    """
    import time
    import urllib.parse

    log(f"Setting {location_type}: {lat}, {lon} ({address})")

    # For pickup, we can use URL parameters
    if location_type == 'pickup':
        # Navigate to URL with pickup coordinates
        pickup_json = json.dumps({"latitude": lat, "longitude": lon})
        uber_url = "https://m.uber.com/go/product-selection?pickup=" + urllib.parse.quote(pickup_json)

        script = '''
tell application "Google Chrome"
    set URL of active tab of front window to "''' + uber_url + '''"
end tell
'''
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=10)
        time.sleep(2)

        return {
            'success': result.returncode == 0,
            'message': f'Navigated to Uber with pickup at {address or f"{lat}, {lon}"}',
            'url': uber_url
        }

    # For destination, we need to interact with the page
    else:
        # First try to click the destination field
        click_result = uber_click_element(text_contains='where to')
        if not click_result.get('success'):
            click_result = uber_click_element(text_contains='destination')

        time.sleep(1)

        # Type the address
        if address:
            type_result = uber_type_text(address)
            time.sleep(2)  # Wait for autocomplete

            return {
                'success': True,
                'message': f'Entered destination: {address}',
                'click_result': click_result,
                'type_result': type_result,
                'next_step': 'Take a screenshot to see autocomplete results, then click the correct one'
            }
        else:
            return {
                'success': False,
                'error': 'Destination address required'
            }


def uber_select_autocomplete(index=0):
    """
    Select an autocomplete result by index (0 = first result).
    """
    import time

    js_code = f'''
    (function() {{
        // Look for autocomplete dropdown items
        var items = document.querySelectorAll('[data-testid*="autocomplete"] li, [class*="autocomplete"] li, [role="listbox"] [role="option"], [class*="suggestion"], [class*="result"]');
        if (items.length === 0) {{
            // Try more generic selectors
            items = document.querySelectorAll('ul li, [role="option"]');
        }}

        var validItems = [];
        items.forEach(function(item) {{
            var rect = item.getBoundingClientRect();
            if (rect.width > 50 && rect.height > 20) {{
                validItems.push(item);
            }}
        }});

        if (validItems.length > {index}) {{
            validItems[{index}].click();
            return JSON.stringify({{success: true, selected: validItems[{index}].textContent.trim().substring(0, 100)}});
        }}

        return JSON.stringify({{success: false, error: 'No autocomplete items found', found: validItems.length}});
    }})();
    '''

    escaped_js = js_code.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
    script = f'''
tell application "Google Chrome"
    tell active tab of front window
        execute javascript "{escaped_js}"
    end tell
end tell
'''

    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=10)

    if result.returncode != 0:
        return {'success': False, 'error': result.stderr}

    try:
        return json.loads(result.stdout.strip())
    except:
        return {'success': True, 'result': result.stdout.strip()}


def uber_select_ride_type(ride_type='UberX'):
    """
    Select a ride type from available options.
    """
    import time

    log(f"Selecting ride type: {ride_type}")

    # Click on the ride type option
    result = uber_click_element(text_contains=ride_type.lower())

    if not result.get('success'):
        # Try alternate names
        alternates = {
            'uberx': ['uber x', 'economy'],
            'comfort': ['uber comfort'],
            'uberxl': ['uber xl', 'xl'],
            'black': ['uber black', 'premium']
        }
        for alt in alternates.get(ride_type.lower(), []):
            result = uber_click_element(text_contains=alt)
            if result.get('success'):
                break

    return result


def uber_confirm_ride():
    """
    Click the confirm/request ride button.
    """
    log("Confirming ride request...")

    # Try various confirm button patterns
    patterns = ['confirm', 'request', 'book', 'continue']

    for pattern in patterns:
        result = uber_click_element(text_contains=pattern)
        if result.get('success'):
            return {
                'success': True,
                'message': f'Clicked "{pattern}" button',
                'result': result
            }

    return {
        'success': False,
        'error': 'Could not find confirm button',
        'next_step': 'Take a screenshot to see current state'
    }


def uber_keyboard_action(action):
    """
    Perform keyboard actions: 'enter', 'tab', 'escape', 'down', 'up'
    """
    key_codes = {
        'enter': 36,
        'return': 36,
        'tab': 48,
        'escape': 53,
        'down': 125,
        'up': 126,
        'left': 123,
        'right': 124
    }

    code = key_codes.get(action.lower())
    if not code:
        return {'success': False, 'error': f'Unknown key: {action}'}

    script = f'''
tell application "System Events"
    key code {code}
end tell
'''

    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=5)

    return {
        'success': result.returncode == 0,
        'action': action,
        'error': result.stderr if result.returncode != 0 else None
    }


# Fast, fully automated Uber ordering using Claude Code CLI with MCP browser tools
def order_uber(pickup_lat, pickup_lon, pickup_address, destination, ride_type='UberX'):
    """
    Fully automated Uber ordering using Claude Code CLI with MCP browser tools.
    This provides intelligent, visual-based browser automation.
    """
    log(f"Starting Uber order via Claude Code: from ({pickup_lat}, {pickup_lon}) to {destination}")

    pickup_display = pickup_address if pickup_address else f"{pickup_lat}, {pickup_lon}"

    prompt = f"""Order an Uber ride using the Chrome browser MCP tools.

PICKUP LOCATION: {pickup_display} (coordinates: {pickup_lat}, {pickup_lon})
DESTINATION: {destination}

Instructions:
1. First use tabs_context_mcp to get available tabs, then create a new tab with tabs_create_mcp
2. Navigate to https://m.uber.com using the navigate tool
3. Wait for the page to load, then take a screenshot to see the current state
4. Find and click on the "Where to?" or destination input field
5. Type "{destination}" into the destination field
6. Wait for autocomplete suggestions to appear, then select the first result
7. Take a final screenshot showing the ride options
8. STOP before confirming - do NOT request the ride

Be efficient and fast. Only take screenshots when needed to verify state."""

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json", "--allowedTools", "mcp__Claude_in_Chrome__*"],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode == 0:
            log("Claude Code completed Uber automation successfully")
            log(f"Claude stdout: {result.stdout[:1000] if result.stdout else 'empty'}")
            log(f"Claude stderr: {result.stderr[:500] if result.stderr else 'empty'}")
            return {
                'success': True,
                'message': f'Uber ride to {destination} is ready!',
                'pickup': pickup_display,
                'destination': destination,
                'status': 'Ride options should be visible in Chrome. Select your ride type and confirm.',
                'claude_output': result.stdout[:500] if result.stdout else ''
            }
        else:
            log(f"Claude Code error - stderr: {result.stderr}")
            log(f"Claude Code error - stdout: {result.stdout[:500] if result.stdout else 'empty'}")
            return {
                'success': False,
                'error': f'Claude Code failed: {result.stderr[:300]}',
                'stdout': result.stdout[:300] if result.stdout else ''
            }

    except subprocess.TimeoutExpired:
        log("Claude Code timed out")
        return {'success': False, 'error': 'Uber ordering timed out after 120 seconds'}
    except FileNotFoundError:
        log("Claude CLI not found, falling back to AppleScript method")
        # Fallback to basic AppleScript method
        return order_uber_fallback(pickup_lat, pickup_lon, pickup_address, destination, ride_type)
    except Exception as e:
        log(f"Error: {e}")
        return {'success': False, 'error': str(e)}


def order_uber_fallback(pickup_lat, pickup_lon, pickup_address, destination, ride_type='UberX'):
    """
    Fallback Uber ordering using AppleScript when Claude CLI is not available.
    """
    import time
    import urllib.parse

    log(f"Using fallback AppleScript method for Uber order")

    # Build URL with pickup coordinates
    pickup_data = json.dumps({"latitude": pickup_lat, "longitude": pickup_lon})
    uber_url = "https://m.uber.com/go/home?pickup=" + urllib.parse.quote(pickup_data)

    # Open Chrome and navigate
    script = '''
tell application "Google Chrome"
    activate
    if (count of windows) = 0 then
        make new window
    end if
    set URL of active tab of front window to "''' + uber_url + '''"
end tell
'''
    result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return {'success': False, 'error': 'Failed to open Chrome'}

    time.sleep(4)

    pickup_display = pickup_address if pickup_address else f"{pickup_lat}, {pickup_lon}"

    return {
        'success': True,
        'message': f'Uber opened with pickup at {pickup_display}. Please manually enter destination: {destination}',
        'pickup': pickup_display,
        'destination': destination,
        'status': 'Opened Uber page - manual destination entry required (Claude CLI not available)',
        'user_action_required': f'Enter "{destination}" in the destination field and select a ride.'
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
    elif action == 'order_uber':
        return order_uber(
            pickup_lat=data.get('pickup_lat'),
            pickup_lon=data.get('pickup_lon'),
            pickup_address=data.get('pickup_address', ''),
            destination=data.get('destination', ''),
            ride_type=data.get('ride_type', 'UberX')
        )
    # New granular Uber tools
    elif action == 'uber_open':
        return uber_open_app(
            pickup_lat=data.get('pickup_lat'),
            pickup_lon=data.get('pickup_lon')
        )
    elif action == 'uber_get_state':
        return uber_get_page_state()
    elif action == 'uber_click':
        return uber_click_element(
            selector=data.get('selector'),
            text_contains=data.get('text_contains'),
            element_type=data.get('element_type', 'button')
        )
    elif action == 'uber_type':
        return uber_type_text(
            text=data.get('text', ''),
            selector=data.get('selector'),
            clear_first=data.get('clear_first', True)
        )
    elif action == 'uber_set_location':
        return uber_set_location(
            location_type=data.get('location_type', 'destination'),
            lat=data.get('lat'),
            lon=data.get('lon'),
            address=data.get('address', '')
        )
    elif action == 'uber_select_autocomplete':
        return uber_select_autocomplete(index=data.get('index', 0))
    elif action == 'uber_select_ride':
        return uber_select_ride_type(ride_type=data.get('ride_type', 'UberX'))
    elif action == 'uber_confirm':
        return uber_confirm_ride()
    elif action == 'uber_keyboard':
        return uber_keyboard_action(action=data.get('key', 'enter'))
    return {'success': False, 'error': 'Unknown action'}

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(('0.0.0.0', PORT))
server.listen(5)
print('=' * 50)
print('Mac Agent v6 - Uber Automation + Visual Hybrid')
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
