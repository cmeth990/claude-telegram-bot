#!/usr/bin/env python3
import socket
import json
import subprocess
import os
import base64
import threading
from datetime import datetime

PORT = 9999
SECRET = os.environ.get('MAC_AGENT_SECRET', '0eea2cc233ae59295e0ac411d45b1eb5a886d71c0376d2abfe481f0ade12f334')
SCREENSHOT_DIR = os.path.expanduser('~/Desktop')

# Global interrupt flag - can be set by /stop command from Telegram
INTERRUPT_FLAG = threading.Event()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def check_interrupt():
    """Check if operation should be interrupted. Returns True if interrupted."""
    if INTERRUPT_FLAG.is_set():
        log("⚠️ INTERRUPT: Operation cancelled by user")
        return True
    return False

def clear_interrupt():
    """Clear the interrupt flag at start of new operation."""
    INTERRUPT_FLAG.clear()

def interruptible_sleep(seconds):
    """Sleep that can be interrupted. Returns True if interrupted."""
    import time
    # Sleep in small increments to allow interrupt checking
    end_time = time.time() + seconds
    while time.time() < end_time:
        if check_interrupt():
            return True
        time.sleep(min(0.5, end_time - time.time()))
    return False

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

def get_spotify_current_track():
    """Get currently playing track from Spotify desktop app via AppleScript."""
    try:
        import time

        # Check if Spotify is running, if not launch it
        check_script = '''
        tell application "System Events"
            set isRunning to (name of processes) contains "Spotify"
        end tell
        return isRunning
        '''
        result = subprocess.run(['osascript', '-e', check_script], capture_output=True, text=True, timeout=10)
        is_running = result.stdout.strip() == 'true'

        if not is_running:
            log("🎵 Launching Spotify app...")
            launch_script = 'tell application "Spotify" to activate'
            subprocess.run(['osascript', '-e', launch_script], capture_output=True, text=True, timeout=10)
            time.sleep(3)  # Wait for Spotify to launch

        # Get current track info from Spotify app
        track_script = '''
        tell application "Spotify"
            if player state is stopped then
                return "STOPPED"
            end if

            set trackName to name of current track
            set artistName to artist of current track
            set albumName to album of current track
            set trackId to id of current track
            set trackDuration to duration of current track
            set trackPopularity to popularity of current track

            return trackName & "|||" & artistName & "|||" & albumName & "|||" & trackId & "|||" & trackDuration & "|||" & trackPopularity
        end tell
        '''

        result = subprocess.run(['osascript', '-e', track_script], capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            return {'success': False, 'error': f'Could not get track info: {result.stderr}'}

        output = result.stdout.strip()

        if output == "STOPPED":
            return {'success': False, 'error': 'No track playing. Play a song in Spotify first!'}

        parts = output.split('|||')
        if len(parts) < 4:
            return {'success': False, 'error': 'Could not parse track info'}

        track_name = parts[0]
        artist = parts[1]
        album = parts[2]
        track_id = parts[3].replace('spotify:track:', '')  # Extract just the ID
        duration_ms = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        popularity = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0

        log(f"🎵 Spotify track: {track_name} by {artist}")

        track_info = {
            'name': track_name,
            'artist': artist,
            'album': album,
            'trackId': track_id,
            'duration_ms': duration_ms,
            'popularity': popularity
        }

        # Fetch Last.fm data for tags and wiki info
        lastfm_data = get_lastfm_track_info(track_name, artist, album)

        return {
            'success': True,
            'track_info': track_info,
            'audio_features': None,  # Would need OAuth for Spotify API audio features
            'lastfm': lastfm_data
        }

    except Exception as e:
        log(f"❌ Error getting Spotify track: {str(e)}")
        return {'success': False, 'error': str(e)}

def get_lastfm_track_info(track_name, artist, album):
    """Fetch track tags and wiki info from Last.fm API."""
    try:
        import requests
        import re

        # Last.fm API key - get yours free at https://www.last.fm/api/account/create
        LASTFM_API_KEY = "b25b959554ed76058ac220b7b2e0a026"
        base_url = "https://ws.audioscrobbler.com/2.0/"

        result = {
            'tags': [],
            'track_wiki': None,
            'album_wiki': None,
            'artist_wiki': None,
            'listeners': None,
            'playcount': None,
            'remixer': None
        }

        # Clean up track name for better Last.fm matching
        clean_track = track_name
        remixer = None

        # Extract remix info
        remix_match = re.search(r'\s*[-–]\s*(.+?)\s*(Remix|Mix|Edit|Dub|Version|Rework).*$', track_name, re.IGNORECASE)
        if remix_match:
            remixer = remix_match.group(1).strip()
            clean_track = re.sub(r'\s*[-–]\s*.*(Remix|Mix|Edit|Dub|Version|Rework).*$', '', track_name, flags=re.IGNORECASE).strip()
            result['remixer'] = f"{remixer} Remix"
            log(f"🎛️ Detected remix by: {remixer}")

        # Also try removing content in parentheses like "(feat. X)" or "(Radio Edit)"
        clean_track_alt = re.sub(r'\s*\([^)]*\)\s*$', '', clean_track).strip()

        # Get track info (includes tags and wiki) - try original first, then cleaned versions
        search_attempts = [
            (track_name, artist),  # Original
            (clean_track, artist),  # Without remix suffix
            (clean_track_alt, artist),  # Without parentheses
        ]

        # If there's a remixer, also try searching for them as artist
        if remixer:
            search_attempts.append((clean_track, remixer))

        track_found = False
        for search_track, search_artist in search_attempts:
            if track_found:
                break

            track_params = {
                'method': 'track.getInfo',
                'api_key': LASTFM_API_KEY,
                'artist': search_artist,
                'track': search_track,
                'format': 'json'
            }

            track_response = requests.get(base_url, params=track_params, timeout=10)
            log(f"🔍 Last.fm lookup: '{search_track}' by '{search_artist}' -> HTTP {track_response.status_code}")

            if track_response.status_code == 200:
                track_data = track_response.json()

                if 'error' in track_data:
                    log(f"⚠️ Last.fm error: {track_data.get('message', 'Unknown error')}")
                    continue  # Try next search attempt

                if 'track' in track_data:
                    track = track_data['track']
                    track_found = True

                    # Get tags
                    if 'toptags' in track and 'tag' in track['toptags']:
                        tags = track['toptags']['tag']
                        if isinstance(tags, list):
                            result['tags'] = [t['name'] for t in tags[:8]]
                        elif isinstance(tags, dict):
                            result['tags'] = [tags['name']]

                    # Get track wiki summary
                    if 'wiki' in track and 'summary' in track['wiki']:
                        summary = track['wiki']['summary']
                        # Clean up the summary (remove HTML and "Read more" link)
                        summary = summary.split('<a href')[0].strip()
                        if summary:
                            result['track_wiki'] = summary[:500]

                    # Get play stats
                    result['listeners'] = track.get('listeners')
                    result['playcount'] = track.get('playcount')
                    log(f"✅ Found on Last.fm! {result['listeners']} listeners, {result['playcount']} plays")
            else:
                log(f"⚠️ Last.fm HTTP error: {track_response.status_code}")

        # Get album info for album wiki
        if album:
            album_params = {
                'method': 'album.getInfo',
                'api_key': LASTFM_API_KEY,
                'artist': artist,
                'album': album,
                'format': 'json'
            }

            album_response = requests.get(base_url, params=album_params, timeout=5)
            if album_response.status_code == 200:
                album_data = album_response.json()
                if 'album' in album_data and 'wiki' in album_data['album']:
                    wiki = album_data['album']['wiki']
                    if 'summary' in wiki:
                        summary = wiki['summary'].split('<a href')[0].strip()
                        if summary:
                            result['album_wiki'] = summary[:500]

        # Get artist info (bio + tags as fallback)
        artist_params = {
            'method': 'artist.getInfo',
            'api_key': LASTFM_API_KEY,
            'artist': artist,
            'format': 'json'
        }

        artist_response = requests.get(base_url, params=artist_params, timeout=5)
        if artist_response.status_code == 200:
            artist_data = artist_response.json()
            if 'artist' in artist_data:
                artist_info = artist_data['artist']

                # Get artist bio
                if 'bio' in artist_info and 'summary' in artist_info['bio']:
                    summary = artist_info['bio']['summary'].split('<a href')[0].strip()
                    if summary:
                        result['artist_wiki'] = summary[:500]

                # Use artist tags as fallback if track has no tags
                if not result['tags'] and 'tags' in artist_info and 'tag' in artist_info['tags']:
                    artist_tags = artist_info['tags']['tag']
                    if isinstance(artist_tags, list):
                        result['tags'] = [t['name'] for t in artist_tags[:8]]
                        log(f"🏷️ Using artist tags as fallback")
                    elif isinstance(artist_tags, dict):
                        result['tags'] = [artist_tags['name']]

        # Log what we found
        if result['tags']:
            log(f"🏷️ Last.fm tags: {', '.join(result['tags'][:5])}")
        else:
            log(f"🏷️ No tags found")

        if result['track_wiki']:
            log(f"📖 Got track wiki ({len(result['track_wiki'])} chars)")
        if result['listeners']:
            log(f"📊 {int(result['listeners']):,} listeners, {int(result['playcount']):,} plays")

        return result

    except Exception as e:
        log(f"⚠️ Last.fm fetch error: {str(e)}")
        return {'tags': [], 'track_wiki': None, 'album_wiki': None, 'artist_wiki': None}

def create_apple_note(title, body):
    """Create a new note in Apple Notes app using AppleScript."""
    try:
        # Escape special characters for AppleScript string
        # Replace backslashes first, then quotes
        escaped_title = title.replace('\\', '\\\\').replace('"', '\\"')
        escaped_body = body.replace('\\', '\\\\').replace('"', '\\"')

        script = f'''
tell application "Notes"
    activate
    set newNote to make new note at folder "Notes" with properties {{name:"{escaped_title}", body:"{escaped_body}"}}
    return id of newNote
end tell
'''
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            log(f"✅ Created Apple Note: {title}")
            return {'success': True, 'title': title, 'note_id': result.stdout.strip()}
        else:
            log(f"❌ Failed to create note: {result.stderr}")
            return {'success': False, 'error': result.stderr}
    except Exception as e:
        log(f"❌ Exception creating note: {str(e)}")
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


# ============================================================================
# Chrome DevTools Protocol (CDP) Browser Automation

def ensure_chrome_debug_mode():
    """Start Chrome with remote debugging if not already running"""
    import urllib.request
    import time

    # Check if Chrome debug port is already available
    try:
        with urllib.request.urlopen(f'http://localhost:{CDP_PORT}/json', timeout=2) as resp:
            log("Chrome debug mode already running")
            return True
    except:
        pass

    log("Starting Chrome in debug mode...")

    # Start Chrome with debugging flags
    chrome_cmd = [
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        f'--remote-debugging-port={CDP_PORT}',
        '--user-data-dir=/tmp/chrome-debug',
        '--remote-allow-origins=*'
    ]

    try:
        # Start Chrome in background
        subprocess.Popen(chrome_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for Chrome to start (up to 5 seconds)
        for i in range(10):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(f'http://localhost:{CDP_PORT}/json', timeout=2) as resp:
                    log(f"Chrome debug mode started on port {CDP_PORT}")
                    return True
            except:
                continue

        log("Warning: Chrome started but debug port not responding")
        return False
    except Exception as e:
        log(f"Failed to start Chrome: {e}")
        return False
# ============================================================================

import urllib.request
import urllib.parse

CDP_PORT = 9222  # Chrome remote debugging port

def cdp_get_targets():
    """Get list of available Chrome targets (tabs)"""
    try:
        with urllib.request.urlopen(f'http://localhost:{CDP_PORT}/json', timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log(f"CDP connection failed: {e}")
        return None

def cdp_send(ws_url, method, params=None):
    """Send a CDP command via WebSocket - synchronous version"""
    import websocket

    try:
        # Use synchronous WebSocket connection
        ws = websocket.create_connection(ws_url, timeout=10)

        msg_id = 1
        cmd = {'id': msg_id, 'method': method}
        if params:
            cmd['params'] = params

        ws.send(json.dumps(cmd))

        # Wait for response with matching ID
        while True:
            response = ws.recv()
            data = json.loads(response)
            if data.get('id') == msg_id:
                ws.close()
                return data

    except Exception as e:
        log(f"CDP send error: {e}")
        return {'error': str(e)}

def cdp_execute_script(ws_url, script):
    """Execute JavaScript in the page context"""
    return cdp_send(ws_url, 'Runtime.evaluate', {
        'expression': script,
        'returnByValue': True,
        'awaitPromise': True
    })

def cdp_navigate(ws_url, url):
    """Navigate to a URL"""
    return cdp_send(ws_url, 'Page.navigate', {'url': url})

def cdp_type_text(ws_url, text):
    """Type text character by character"""
    for char in text:
        cdp_send(ws_url, 'Input.dispatchKeyEvent', {
            'type': 'keyDown',
            'text': char
        })
        cdp_send(ws_url, 'Input.dispatchKeyEvent', {
            'type': 'keyUp',
            'text': char
        })
    return {'success': True}

def cdp_press_key(ws_url, key):
    """Press a special key (Enter, Tab, ArrowDown, etc.)"""
    key_codes = {
        'Enter': {'key': 'Enter', 'code': 'Enter', 'keyCode': 13},
        'Tab': {'key': 'Tab', 'code': 'Tab', 'keyCode': 9},
        'ArrowDown': {'key': 'ArrowDown', 'code': 'ArrowDown', 'keyCode': 40},
        'ArrowUp': {'key': 'ArrowUp', 'code': 'ArrowUp', 'keyCode': 38},
        'Escape': {'key': 'Escape', 'code': 'Escape', 'keyCode': 27},
    }

    kc = key_codes.get(key, {'key': key, 'code': key, 'keyCode': 0})

    cdp_send(ws_url, 'Input.dispatchKeyEvent', {
        'type': 'keyDown',
        'key': kc['key'],
        'code': kc['code'],
        'windowsVirtualKeyCode': kc['keyCode'],
        'nativeVirtualKeyCode': kc['keyCode']
    })
    cdp_send(ws_url, 'Input.dispatchKeyEvent', {
        'type': 'keyUp',
        'key': kc['key'],
        'code': kc['code'],
        'windowsVirtualKeyCode': kc['keyCode'],
        'nativeVirtualKeyCode': kc['keyCode']
    })
    return {'success': True}

def cdp_click_element(ws_url, selector):
    """Click an element by selector"""
    # First get element position
    script = f'''
    (function() {{
        var el = document.querySelector('{selector}');
        if (!el) return null;
        var rect = el.getBoundingClientRect();
        return {{
            x: rect.x + rect.width / 2,
            y: rect.y + rect.height / 2,
            found: true
        }};
    }})();
    '''
    result = cdp_execute_script(ws_url, script)

    if result and result.get('result', {}).get('result', {}).get('value'):
        pos = result['result']['result']['value']
        # Dispatch mouse click
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mousePressed',
            'x': pos['x'],
            'y': pos['y'],
            'button': 'left',
            'clickCount': 1
        })
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mouseReleased',
            'x': pos['x'],
            'y': pos['y'],
            'button': 'left',
            'clickCount': 1
        })
        return {'success': True, 'clicked': selector}

    return {'success': False, 'error': f'Element not found: {selector}'}


# Uber ordering using Chrome DevTools Protocol
def order_uber(pickup_lat, pickup_lon, pickup_address, destination, ride_type='UberX', num_passengers=1):
    """
    Automated Uber ordering using Chrome DevTools Protocol (CDP).
    Smart approach: analyzes page, uses process of elimination if needed.
    num_passengers: if > 4, will select UberXL instead of UberX
    """
    import time

    # Auto-select ride type based on passenger count
    if num_passengers > 4:
        ride_type = 'UberXL'
    elif ride_type == 'UberX' and num_passengers <= 4:
        ride_type = 'UberX'

    log(f"Starting Uber order via CDP: from ({pickup_lat}, {pickup_lon}) to {destination}, {num_passengers} passengers, ride: {ride_type}")
    pickup_display = pickup_address if pickup_address else f"{pickup_lat}, {pickup_lon}"

    # Step 1: Check CDP connection
    targets = cdp_get_targets()
    if targets is None:
        return {'success': False, 'error': 'Chrome debug mode not running. Start agent.py to auto-launch Chrome.'}

    ws_url = None
    for target in targets:
        if target.get('type') == 'page':
            ws_url = target.get('webSocketDebuggerUrl')
            break

    if not ws_url:
        return {'success': False, 'error': 'No Chrome tab available'}

    log(f"Connected to Chrome tab: {ws_url}")

    # Step 2: Navigate to Uber WITH pickup coordinates in URL
    # This pre-sets the pickup location so user doesn't need to allow location access
    pickup_data = json.dumps({"latitude": pickup_lat, "longitude": pickup_lon})
    uber_url = f"https://m.uber.com/go/home?pickup={urllib.parse.quote(pickup_data)}"
    log(f"Navigating to: {uber_url}")
    cdp_navigate(ws_url, uber_url)
    time.sleep(4)

    # Step 3: Smart page analysis - understand what's on screen
    analyze_script = '''
    (function() {
        var result = {
            url: window.location.href,
            needsLogin: false,
            pickupField: null,
            dropoffField: null,
            hasRideOptions: false,
            pageState: 'unknown'
        };

        var bodyText = document.body.innerText;

        // Check if logged in
        if (bodyText.includes('Sign in') || bodyText.includes('Log in') || bodyText.includes('Continue with')) {
            result.needsLogin = true;
            result.pageState = 'login_required';
            return result;
        }

        // Find all clickable location fields
        var allTestIds = document.querySelectorAll('[data-testid]');
        var locationFields = [];

        allTestIds.forEach(function(el) {
            var testId = el.getAttribute('data-testid') || '';
            var text = (el.textContent || '').substring(0, 100);

            // Look for pickup/dropoff related elements
            if (testId.includes('pickup') || testId.includes('drop') || testId.includes('pudo') || testId.includes('enhancer')) {
                locationFields.push({
                    testId: testId,
                    text: text,
                    isPickup: text.toLowerCase().includes('pickup') || testId.includes('pickup'),
                    isDropoff: text.toLowerCase().includes('dropoff') || text.toLowerCase().includes('drop') || testId.includes('drop')
                });
            }
        });

        result.locationFields = locationFields;

        // Identify pickup vs dropoff by process of elimination
        // Usually: first field = pickup (has address), second field = dropoff (empty or says "Dropoff")
        if (locationFields.length >= 2) {
            for (var i = 0; i < locationFields.length; i++) {
                var f = locationFields[i];
                if (f.isDropoff || f.text.includes('Dropoff') || f.text.includes('Where')) {
                    result.dropoffField = f.testId;
                    break;
                }
            }
            // If still not found, second enhancer-container is usually dropoff
            if (!result.dropoffField) {
                for (var j = 0; j < locationFields.length; j++) {
                    if (locationFields[j].testId.includes('drop0') || locationFields[j].testId.includes('enhancer-container-drop')) {
                        result.dropoffField = locationFields[j].testId;
                        break;
                    }
                }
            }
        }

        // Check if ride options are showing
        var lowerText = bodyText.toLowerCase();
        if (lowerText.includes('uberx') || lowerText.includes('comfort') || lowerText.includes('black')) {
            result.hasRideOptions = true;
            result.pageState = 'ride_selection';
        } else if (result.dropoffField) {
            result.pageState = 'ready_for_destination';
        }

        result.bodySnippet = bodyText.substring(0, 400);
        return result;
    })();
    '''

    analysis = cdp_execute_script(ws_url, analyze_script)
    log(f"Page analysis: {analysis}")

    # Parse analysis result
    try:
        page_state = analysis.get('result', {}).get('result', {}).get('value', {})
    except:
        page_state = {}

    if not isinstance(page_state, dict):
        return {'success': False, 'error': 'Could not analyze Uber page'}

    # Check if login needed
    if page_state.get('needsLogin'):
        return {
            'success': False,
            'error': 'You need to log into Uber first. Open Chrome and sign in at m.uber.com'
        }

    # Step 3b: Check if pickup was set from URL, if not we need to set it
    pickup_check = cdp_execute_script(ws_url, '''
    (function() {
        var pickup = document.querySelector('[data-testid="enhancer-container-pickup"]');
        if (!pickup) return {hasPickup: false};
        var text = pickup.textContent.toLowerCase();
        // Check if pickup is still empty/default
        var isEmpty = text.includes('pickup location') && !text.includes(',');
        return {hasPickup: !isEmpty, pickupText: pickup.textContent.substring(0, 100)};
    })();
    ''')
    log(f"Pickup check: {pickup_check}")

    try:
        pickup_state = pickup_check.get('result', {}).get('result', {}).get('value', {})
    except:
        pickup_state = {}

    # If pickup not set, try to set it via clicking and using "Set location on map" or coordinates
    if not pickup_state.get('hasPickup'):
        log("Pickup not set from URL, attempting to set manually...")

        # Click the pickup field
        pickup_coords_script = '''
        (function() {
            var pickup = document.querySelector('[data-testid="enhancer-container-pickup"]');
            if (!pickup) return {found: false};
            var rect = pickup.getBoundingClientRect();
            return {found: true, x: rect.left + rect.width/2, y: rect.top + rect.height/2};
        })();
        '''
        pickup_coords = cdp_execute_script(ws_url, pickup_coords_script)
        try:
            pc = pickup_coords.get('result', {}).get('result', {}).get('value', {})
        except:
            pc = {}

        if pc.get('found'):
            px, py = pc['x'], pc['y']
            log(f"Clicking pickup at ({px}, {py})")
            cdp_send(ws_url, 'Input.dispatchMouseEvent', {
                'type': 'mousePressed', 'x': px, 'y': py, 'button': 'left', 'clickCount': 1
            })
            time.sleep(0.1)
            cdp_send(ws_url, 'Input.dispatchMouseEvent', {
                'type': 'mouseReleased', 'x': px, 'y': py, 'button': 'left', 'clickCount': 1
            })
            time.sleep(1.5)

            # Type the pickup address if we have it
            if pickup_address:
                log(f"Typing pickup address: {pickup_address}")
                cdp_type_text(ws_url, pickup_address)
                time.sleep(2)
                cdp_press_key(ws_url, 'ArrowDown')
                time.sleep(0.3)
                cdp_press_key(ws_url, 'Enter')
                time.sleep(2)
            else:
                # Try to use "Allow location access" or type coordinates
                log("No pickup address provided, trying to select first suggestion...")
                cdp_press_key(ws_url, 'ArrowDown')
                time.sleep(0.3)
                cdp_press_key(ws_url, 'Enter')
                time.sleep(2)

    # Step 4: Get coordinates for BOTH pickup and dropoff to click the right one
    coords_script = '''
    (function() {
        var result = {pickupCoords: null, dropoffCoords: null};

        // Find pickup element
        var pickup = document.querySelector('[data-testid="enhancer-container-pickup"]');
        if (pickup) {
            var pr = pickup.getBoundingClientRect();
            result.pickupCoords = {x: pr.left + pr.width/2, y: pr.top + pr.height/2, bottom: pr.bottom};
        }

        // Find dropoff element - it should be BELOW the pickup
        var dropoff = document.querySelector('[data-testid="enhancer-container-drop0"]');
        if (dropoff) {
            var dr = dropoff.getBoundingClientRect();
            result.dropoffCoords = {x: dr.left + dr.width/2, y: dr.top + dr.height/2, top: dr.top};
        }

        // Sanity check: dropoff should be below pickup
        if (result.pickupCoords && result.dropoffCoords) {
            result.dropoffIsBelowPickup = result.dropoffCoords.top > result.pickupCoords.bottom - 10;
        }

        return result;
    })();
    '''

    coords_result = cdp_execute_script(ws_url, coords_script)
    log(f"Field coordinates: {coords_result}")

    try:
        coords = coords_result.get('result', {}).get('result', {}).get('value', {})
    except:
        coords = {}

    dropoff_coords = coords.get('dropoffCoords')
    pickup_coords = coords.get('pickupCoords')

    if not dropoff_coords:
        return {'success': False, 'error': 'Could not find dropoff element on page'}

    # Make sure we're clicking the RIGHT field (dropoff, not pickup)
    x, y = dropoff_coords['x'], dropoff_coords['y']
    log(f"Dropoff at ({x}, {y}), Pickup at ({pickup_coords['x'] if pickup_coords else '?'}, {pickup_coords['y'] if pickup_coords else '?'})")

    # Click the dropoff field
    log(f"Clicking dropoff at coordinates: ({x}, {y})")
    cdp_send(ws_url, 'Input.dispatchMouseEvent', {
        'type': 'mousePressed',
        'x': x,
        'y': y,
        'button': 'left',
        'clickCount': 1
    })
    time.sleep(0.1)
    cdp_send(ws_url, 'Input.dispatchMouseEvent', {
        'type': 'mouseReleased',
        'x': x,
        'y': y,
        'button': 'left',
        'clickCount': 1
    })

    time.sleep(1.5)

    # Step 5: Verify we're in the RIGHT input field (dropoff, not pickup)
    field_check = cdp_execute_script(ws_url, '''
    (function() {
        // Check what field is active by looking at the page state
        var bodyText = document.body.innerText;

        // Look for indication of which field is being edited
        var result = {
            isEditingDropoff: false,
            isEditingPickup: false,
            activeFieldHint: ''
        };

        // If we see "Where to" or "Enter destination" prominently, we're editing dropoff
        // If we see "Enter pickup" or "Set pickup", we're editing pickup
        if (bodyText.includes('Where to') || bodyText.includes('Enter destination') ||
            bodyText.includes('Dropoff') && !bodyText.includes('Dropoff location')) {
            result.isEditingDropoff = true;
            result.activeFieldHint = 'dropoff';
        }
        if (bodyText.includes('Enter pickup') || bodyText.includes('Set pickup location')) {
            result.isEditingPickup = true;
            result.activeFieldHint = 'pickup';
        }

        // Also check for any visible input placeholder
        var activeEl = document.activeElement;
        if (activeEl && activeEl.tagName === 'INPUT') {
            var ph = (activeEl.placeholder || '').toLowerCase();
            result.inputPlaceholder = activeEl.placeholder;
            if (ph.includes('where') || ph.includes('destination') || ph.includes('drop')) {
                result.isEditingDropoff = true;
            } else if (ph.includes('pickup') || ph.includes('from')) {
                result.isEditingPickup = true;
            }
        }

        result.snippet = bodyText.substring(0, 200);
        return result;
    })();
    ''')
    log(f"Field check: {field_check}")

    try:
        field_state = field_check.get('result', {}).get('result', {}).get('value', {})
    except:
        field_state = {}

    # If we accidentally clicked pickup, try clicking lower (the dropoff field)
    if field_state.get('isEditingPickup') and not field_state.get('isEditingDropoff'):
        log("WARNING: Clicked pickup instead of dropoff! Pressing Escape and retrying lower...")
        cdp_press_key(ws_url, 'Escape')
        time.sleep(0.5)

        # Click lower - dropoff should be ~50-80px below pickup
        new_y = y + 60
        log(f"Retrying click at ({x}, {new_y})")
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': x, 'y': new_y, 'button': 'left', 'clickCount': 1
        })
        time.sleep(0.1)
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': x, 'y': new_y, 'button': 'left', 'clickCount': 1
        })
        time.sleep(1.5)

    # Step 6: Type the destination
    log(f"Typing destination: {destination}")
    cdp_type_text(ws_url, destination)
    time.sleep(2.5)

    # Step 7: Select first autocomplete result
    log("Selecting autocomplete result...")
    cdp_press_key(ws_url, 'ArrowDown')
    time.sleep(0.5)
    cdp_press_key(ws_url, 'Enter')
    time.sleep(2)

    # Step 8: Click the "Search" button to get ride options
    log("Looking for Search button...")
    search_btn_script = '''
    (function() {
        // Find the Search button
        var buttons = document.querySelectorAll('button');
        for (var i = 0; i < buttons.length; i++) {
            var text = (buttons[i].textContent || '').trim();
            if (text === 'Search' || text.toLowerCase() === 'search') {
                var rect = buttons[i].getBoundingClientRect();
                return {found: true, x: rect.left + rect.width/2, y: rect.top + rect.height/2, text: text};
            }
        }
        // Also try by aria-label or data-testid
        var searchBtn = document.querySelector('button[aria-label*="Search"], button[data-testid*="search"]');
        if (searchBtn) {
            var rect = searchBtn.getBoundingClientRect();
            return {found: true, x: rect.left + rect.width/2, y: rect.top + rect.height/2, text: 'search btn'};
        }
        return {found: false};
    })();
    '''
    search_result = cdp_execute_script(ws_url, search_btn_script)
    log(f"Search button: {search_result}")

    try:
        search_btn = search_result.get('result', {}).get('result', {}).get('value', {})
    except:
        search_btn = {}

    if search_btn.get('found'):
        sx, sy = search_btn['x'], search_btn['y']
        log(f"Clicking Search button at ({sx}, {sy})")
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': sx, 'y': sy, 'button': 'left', 'clickCount': 1
        })
        time.sleep(0.1)
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': sx, 'y': sy, 'button': 'left', 'clickCount': 1
        })
        time.sleep(3)  # Wait for next screen to load
    else:
        log("Search button not found, ride options may already be visible")

    # Step 9: Check if terminal/gate selection is required (airports, large venues)
    terminal_check_script = '''
    (function() {
        var bodyText = document.body.innerText;
        var result = {
            needsTerminalSelection: false,
            terminals: [],
            hasNextButton: false
        };

        // Check if we're on a terminal selection screen
        if (bodyText.includes('Terminal') || bodyText.includes('terminal') ||
            bodyText.includes('Gate') || bodyText.includes('Concourse')) {

            // Look for terminal options (usually radio buttons or clickable divs)
            var options = document.querySelectorAll('[role="radio"], [role="option"], [data-testid*="terminal"], [data-testid*="option"]');
            options.forEach(function(opt) {
                var text = (opt.textContent || '').trim();
                if (text && text.length < 50) {
                    var rect = opt.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        result.terminals.push({
                            text: text,
                            x: rect.left + rect.width/2,
                            y: rect.top + rect.height/2
                        });
                    }
                }
            });

            // Also look for list items that might be terminals
            if (result.terminals.length === 0) {
                var listItems = document.querySelectorAll('li, [role="listitem"]');
                listItems.forEach(function(li) {
                    var text = (li.textContent || '').trim();
                    if (text.includes('Terminal') || text.includes('Concourse') || text.includes('Gate')) {
                        var rect = li.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            result.terminals.push({
                                text: text.substring(0, 40),
                                x: rect.left + rect.width/2,
                                y: rect.top + rect.height/2
                            });
                        }
                    }
                });
            }

            if (result.terminals.length > 0) {
                result.needsTerminalSelection = true;
            }
        }

        // Check for Next button
        var buttons = document.querySelectorAll('button');
        for (var i = 0; i < buttons.length; i++) {
            var text = (buttons[i].textContent || '').trim().toLowerCase();
            if (text === 'next' || text === 'continue' || text === 'confirm') {
                var rect = buttons[i].getBoundingClientRect();
                result.hasNextButton = true;
                result.nextButtonCoords = {x: rect.left + rect.width/2, y: rect.top + rect.height/2};
                break;
            }
        }

        result.bodySnippet = bodyText.substring(0, 400);
        return result;
    })();
    '''

    terminal_result = cdp_execute_script(ws_url, terminal_check_script)
    log(f"Terminal check: {terminal_result}")

    try:
        terminal_state = terminal_result.get('result', {}).get('result', {}).get('value', {})
    except:
        terminal_state = {}

    if terminal_state.get('needsTerminalSelection') and terminal_state.get('terminals'):
        terminals = terminal_state['terminals']
        log(f"Terminal selection required. Found {len(terminals)} options: {[t['text'] for t in terminals]}")

        # Select the first terminal option
        first_terminal = terminals[0]
        tx, ty = first_terminal['x'], first_terminal['y']
        log(f"Selecting terminal: '{first_terminal['text']}' at ({tx}, {ty})")

        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': tx, 'y': ty, 'button': 'left', 'clickCount': 1
        })
        time.sleep(0.1)
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': tx, 'y': ty, 'button': 'left', 'clickCount': 1
        })
        time.sleep(1)

        # Now click Next button - use JS click since CDP mouse events have scroll issues
        log("Looking for Next/Continue button...")
        next_btn_script = '''
        (function() {
            var buttons = document.querySelectorAll('button');
            for (var i = 0; i < buttons.length; i++) {
                var text = (buttons[i].textContent || '').trim().toLowerCase();
                if (text === 'next' || text === 'continue' || text === 'confirm') {
                    // Use direct JS click - more reliable than CDP mouse events for off-screen elements
                    buttons[i].click();
                    return {clicked: true, text: text};
                }
            }
            return {clicked: false};
        })();
        '''
        next_result = cdp_execute_script(ws_url, next_btn_script)
        log(f"Next button click: {next_result}")
        time.sleep(3)  # Wait for ride options to load

    # Step 10: Wait for ride options page to fully load, then find Request button
    log(f"Looking for ride options, selecting {ride_type}...")

    # Give the page a moment to render ride options
    time.sleep(1)

    # The ride options page should already have the first option (UberX) selected by default
    # We just need to find and click the Request button at the bottom
    # If user wants a different ride type, we click on it first

    if ride_type.lower() != 'uberx':
        # Need to select a different ride type - find and click it
        ride_select_script = f'''
        (function() {{
            var targetRide = '{ride_type}'.toLowerCase();
            var allElements = document.querySelectorAll('*');

            for (var i = 0; i < allElements.length; i++) {{
                var el = allElements[i];
                var text = (el.textContent || '').toLowerCase();
                var directText = '';

                // Get direct text content (not from children)
                for (var j = 0; j < el.childNodes.length; j++) {{
                    if (el.childNodes[j].nodeType === 3) {{
                        directText += el.childNodes[j].textContent;
                    }}
                }}
                directText = directText.toLowerCase().trim();

                // Match ride types - look for the label text
                var isMatch = false;
                if (targetRide === 'uberxl' && (directText === 'uberxl' || directText.startsWith('uberxl'))) {{
                    isMatch = true;
                }} else if (targetRide === 'comfort' && directText.startsWith('comfort') && !directText.includes('electric')) {{
                    isMatch = true;
                }} else if (targetRide === 'black' && directText === 'black') {{
                    isMatch = true;
                }}

                if (isMatch) {{
                    var rect = el.getBoundingClientRect();
                    if (rect.width > 50 && rect.height > 20) {{
                        el.scrollIntoView({{behavior: 'instant', block: 'center'}});
                        return {{
                            found: true,
                            text: el.textContent.substring(0, 50),
                            x: rect.left + rect.width/2,
                            y: rect.top + rect.height/2
                        }};
                    }}
                }}
            }}
            return {{found: false}};
        }})();
        '''
        ride_result = cdp_execute_script(ws_url, ride_select_script)
        log(f"Ride type selection: {ride_result}")

        try:
            ride_el = ride_result.get('result', {}).get('result', {}).get('value', {})
        except:
            ride_el = {}

        if ride_el.get('found'):
            time.sleep(0.3)
            rx, ry = ride_el['x'], ride_el['y']
            log(f"Clicking ride option at ({rx}, {ry})")
            cdp_send(ws_url, 'Input.dispatchMouseEvent', {
                'type': 'mousePressed', 'x': rx, 'y': ry, 'button': 'left', 'clickCount': 1
            })
            time.sleep(0.1)
            cdp_send(ws_url, 'Input.dispatchMouseEvent', {
                'type': 'mouseReleased', 'x': rx, 'y': ry, 'button': 'left', 'clickCount': 1
            })
            time.sleep(1)

    # Now find and click the Request button
    log("Looking for Request button...")

    # Find button with "Request" in text, scroll into view, get coordinates
    request_btn_script = '''
    (function() {
        var buttons = document.querySelectorAll('button');
        for (var i = 0; i < buttons.length; i++) {
            var btnText = (buttons[i].textContent || '').toLowerCase();
            // Match any "Request ..." button
            if (btnText.includes('request ')) {
                buttons[i].scrollIntoView({behavior: 'instant', block: 'center'});
                // Small delay for scroll, then get rect
                var rect = buttons[i].getBoundingClientRect();
                return {
                    found: true,
                    text: buttons[i].textContent.trim(),
                    x: rect.left + rect.width/2,
                    y: rect.top + rect.height/2
                };
            }
        }
        return {found: false, buttons: buttons.length};
    })();
    '''
    request_result = cdp_execute_script(ws_url, request_btn_script)
    log(f"Request button location: {request_result}")

    try:
        req_btn = request_result.get('result', {}).get('result', {}).get('value', {})
    except:
        req_btn = {}

    if req_btn.get('found'):
        time.sleep(0.5)  # Wait for scroll to complete

        # Use CDP mouse click - more reliable for React buttons
        btn_x, btn_y = req_btn['x'], req_btn['y']
        log(f"Clicking Request button at ({btn_x}, {btn_y}) with CDP mouse event")

        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': btn_x, 'y': btn_y, 'button': 'left', 'clickCount': 1
        })
        time.sleep(0.1)
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': btn_x, 'y': btn_y, 'button': 'left', 'clickCount': 1
        })
        log("Request button clicked via CDP")
    else:
        log("Request button not found!")

    time.sleep(3)  # Wait for ride to be requested

    # Step 11: STRICT verification - check if ride was actually requested/confirmed
    verify_script = '''
    (function() {
        var bodyText = document.body.innerText;
        var lowerText = bodyText.toLowerCase();

        var result = {
            rideRequested: false,
            rideConfirmed: false,
            lookingForDriver: false,
            driverFound: false,
            driverName: '',
            eta: '',
            stillOnSelection: false,
            currentState: 'unknown',
            visibleText: bodyText.substring(0, 600)
        };

        // Check if ride was requested - looking for driver
        if (lowerText.includes('looking for') || lowerText.includes('finding your') ||
            lowerText.includes('connecting you') || lowerText.includes('searching for')) {
            result.lookingForDriver = true;
            result.rideRequested = true;
            result.currentState = 'looking_for_driver';
        }

        // Check if driver found - has driver name or "arriving"
        if (lowerText.includes('arriving') || lowerText.includes('is on the way') ||
            lowerText.includes('meet at') || lowerText.includes('your driver')) {
            result.driverFound = true;
            result.rideConfirmed = true;
            result.currentState = 'driver_assigned';
        }

        // Check for ETA like "3 min" in context of arriving
        var etaMatch = bodyText.match(/(\\d+)\\s*min/);
        if (etaMatch && (lowerText.includes('arriving') || lowerText.includes('away'))) {
            result.eta = etaMatch[1] + ' min';
        }

        // Check if still on ride selection screen (not yet requested)
        if (lowerText.includes('request uberx') || lowerText.includes('request uberxl') ||
            lowerText.includes('choose a ride') || lowerText.includes('request comfort')) {
            result.stillOnSelection = true;
            if (!result.rideRequested) {
                result.currentState = 'still_selecting';
            }
        }

        // Check for cancel button (means ride is in progress)
        if (lowerText.includes('cancel ride') || lowerText.includes('cancel trip')) {
            result.rideRequested = true;
        }

        return result;
    })();
    '''

    final_state = cdp_execute_script(ws_url, verify_script)
    log(f"Final verification: {final_state}")

    try:
        state = final_state.get('result', {}).get('result', {}).get('value', {})
    except:
        state = {}

    # Return result based on ACTUAL ride status - only report success when ride is truly requested/confirmed
    current_state = state.get('currentState', 'unknown')

    if state.get('driverFound') or state.get('rideConfirmed'):
        # Best case: driver already assigned
        return {
            'success': True,
            'message': f'Uber ride confirmed! Driver is on the way.',
            'pickup': pickup_display,
            'destination': destination,
            'ride_type': ride_type,
            'eta': state.get('eta', ''),
            'status': 'Driver assigned - check Chrome for details.'
        }
    elif state.get('lookingForDriver') or state.get('rideRequested'):
        # Ride was requested, looking for driver
        return {
            'success': True,
            'message': f'Uber ride requested! Looking for a driver...',
            'pickup': pickup_display,
            'destination': destination,
            'ride_type': ride_type,
            'status': 'Ride requested - finding a driver now.'
        }
    elif state.get('stillOnSelection'):
        # Still on ride selection - request button click may have failed
        return {
            'success': False,
            'error': 'Ride was not requested. Still on ride selection screen.',
            'pickup': pickup_display,
            'destination': destination,
            'status': 'Please check Chrome and click the Request button manually.',
            'page_text': state.get('visibleText', '')[:300]
        }
    else:
        # Unknown state - something went wrong
        return {
            'success': False,
            'error': 'Could not confirm ride was requested. Please check Chrome.',
            'actual_state': current_state,
            'pickup': pickup_display,
            'destination': destination,
            'page_text': state.get('visibleText', '')[:300]
        }


# ============================================================================
# UBER EATS AUTOMATION
# ============================================================================

def search_restaurant_reviews(restaurant_name, location=""):
    """Search online for top recommended dishes at a restaurant using DuckDuckGo"""
    import urllib.request
    import urllib.parse
    import re

    log(f"Researching top dishes at: {restaurant_name}")

    # Use DuckDuckGo HTML search (easier to parse than Google)
    query = f"{restaurant_name} best dishes menu recommendations"
    search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"

    try:
        req = urllib.request.Request(search_url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8', errors='ignore')

            log(f"Got search results: {len(html)} chars")

            # Extract text content (strip HTML tags)
            text = re.sub(r'<[^>]+>', ' ', html)
            text = re.sub(r'\s+', ' ', text)

            dishes = []

            # Common food words to look for
            food_keywords = ['chicken', 'beef', 'pork', 'rice', 'noodle', 'soup', 'salad',
                           'burger', 'pizza', 'taco', 'burrito', 'sandwich', 'steak',
                           'fish', 'shrimp', 'tofu', 'curry', 'pad thai', 'ramen',
                           'dumpling', 'fried', 'grilled', 'roasted', 'bowl', 'roll',
                           'wings', 'fries', 'combo', 'special', 'signature']

            # Extract sentences that mention food
            sentences = text.split('.')
            for sentence in sentences:
                sentence_lower = sentence.lower()
                for keyword in food_keywords:
                    if keyword in sentence_lower:
                        # Extract potential dish name (words around the keyword)
                        words = sentence.split()
                        for i, word in enumerate(words):
                            if keyword in word.lower():
                                # Get surrounding words as dish name
                                start = max(0, i-2)
                                end = min(len(words), i+3)
                                dish = ' '.join(words[start:end]).strip('.,!?()[]')
                                if len(dish) > 5 and len(dish) < 50:
                                    dishes.append(dish)
                                break

            # Remove duplicates and clean up
            unique_dishes = []
            seen = set()
            for d in dishes:
                d_clean = d.strip()
                d_lower = d_clean.lower()
                if d_lower not in seen and len(d_clean) > 5:
                    seen.add(d_lower)
                    unique_dishes.append(d_clean)

            log(f"Found potential dishes: {unique_dishes[:5]}")

            return {
                'success': True,
                'restaurant': restaurant_name,
                'suggested_dishes': unique_dishes[:10]
            }
    except Exception as e:
        log(f"Research failed: {e}")
        return {'success': False, 'error': str(e), 'suggested_dishes': []}


def order_uber_eats(pickup_lat, pickup_lon, pickup_address, cuisine_type='', surprise_me=False, customization_answers=None):
    """
    Automated Uber Eats ordering using CDP.
    - If cuisine_type specified: filter by that cuisine
    - If surprise_me=True: select "Best Overall", research top dishes, order the best one
    - If customization_answers is None: returns questions for user to answer
    - If customization_answers is provided: applies selections and proceeds to checkout
    """
    import time
    import urllib.parse

    log(f"Starting Uber Eats order: cuisine={cuisine_type}, surprise_me={surprise_me}, has_answers={customization_answers is not None}")

    # Clear any previous interrupt flag
    clear_interrupt()

    # Step 1: Check CDP connection
    targets = cdp_get_targets()
    if targets is None:
        return {'success': False, 'error': 'Chrome debug mode not running. Start agent.py to auto-launch Chrome.'}

    ws_url = None
    for target in targets:
        if target.get('type') == 'page':
            ws_url = target.get('webSocketDebuggerUrl')
            break

    if not ws_url:
        return {'success': False, 'error': 'No Chrome tab available'}

    log(f"Connected to Chrome tab: {ws_url}")

    # Check for interrupt
    if check_interrupt():
        return {'success': False, 'error': 'Operation cancelled by user', 'interrupted': True}

    # Step 2: Navigate to Uber Eats with location
    location_data = json.dumps({"latitude": pickup_lat, "longitude": pickup_lon})
    uber_eats_url = f"https://www.ubereats.com/feed?diningMode=DELIVERY&pl={urllib.parse.quote(location_data)}"
    log(f"Navigating to: {uber_eats_url}")
    cdp_navigate(ws_url, uber_eats_url)
    time.sleep(4)

    # Check for interrupt
    if check_interrupt():
        return {'success': False, 'error': 'Operation cancelled by user', 'interrupted': True}

    # Step 3: Check if logged in
    login_check = cdp_execute_script(ws_url, '''
    (function() {
        var bodyText = document.body.innerText.toLowerCase();
        return {
            needsLogin: bodyText.includes('sign in') || bodyText.includes('log in'),
            pageText: document.body.innerText.substring(0, 500)
        };
    })();
    ''')
    log(f"Login check: {login_check}")

    try:
        login_state = login_check.get('result', {}).get('result', {}).get('value', {})
    except:
        login_state = {}

    if login_state.get('needsLogin'):
        return {
            'success': False,
            'error': 'You need to log into Uber Eats first. Open Chrome and sign in at ubereats.com'
        }

    # Step 4: Handle cuisine filter or "Best Overall" selection
    if surprise_me:
        # Click on "Best Overall" filter button
        log("Looking for 'Best Overall' or top restaurants...")

        best_overall_script = '''
        (function() {
            // Look for filter buttons at the top
            var buttons = document.querySelectorAll('button, [role="button"], a');
            for (var i = 0; i < buttons.length; i++) {
                var btn = buttons[i];
                var text = (btn.textContent || '').toLowerCase().trim();
                if (text === 'best overall' || text.includes('best overall')) {
                    btn.scrollIntoView({behavior: 'instant', block: 'center'});
                    var rect = btn.getBoundingClientRect();
                    return {
                        found: true,
                        text: btn.textContent.substring(0, 50),
                        x: rect.left + rect.width/2,
                        y: rect.top + rect.height/2
                    };
                }
            }
            return {found: false};
        })();
        '''
        best_result = cdp_execute_script(ws_url, best_overall_script)
        log(f"Best Overall search: {best_result}")

        try:
            best_btn = best_result.get('result', {}).get('result', {}).get('value', {})
        except:
            best_btn = {}

        if best_btn.get('found'):
            time.sleep(0.5)
            # Get fresh coordinates after scroll
            fresh_coords = cdp_execute_script(ws_url, '''
            (function() {
                var buttons = document.querySelectorAll('button, [role="button"], a');
                for (var i = 0; i < buttons.length; i++) {
                    var text = (buttons[i].textContent || '').toLowerCase().trim();
                    if (text === 'best overall' || text.includes('best overall')) {
                        var rect = buttons[i].getBoundingClientRect();
                        return {x: rect.left + rect.width/2, y: rect.top + rect.height/2};
                    }
                }
                return null;
            })();
            ''')
            try:
                coords = fresh_coords.get('result', {}).get('result', {}).get('value', {})
                if coords:
                    bx, by = coords['x'], coords['y']
                else:
                    bx, by = best_btn['x'], best_btn['y']
            except:
                bx, by = best_btn['x'], best_btn['y']

            log(f"Clicking Best Overall at ({bx}, {by})")
            cdp_send(ws_url, 'Input.dispatchMouseEvent', {
                'type': 'mousePressed', 'x': bx, 'y': by, 'button': 'left', 'clickCount': 1
            })
            time.sleep(0.1)
            cdp_send(ws_url, 'Input.dispatchMouseEvent', {
                'type': 'mouseReleased', 'x': bx, 'y': by, 'button': 'left', 'clickCount': 1
            })
            time.sleep(3)  # Wait for filter to apply

            # Scroll down to see restaurants
            cdp_execute_script(ws_url, 'window.scrollBy(0, 400);')
            time.sleep(1)

    elif cuisine_type:
        # Search for cuisine type
        log(f"Searching for cuisine: {cuisine_type}")

        # Click search bar
        search_script = '''
        (function() {
            var searchInputs = document.querySelectorAll('input[type="search"], input[placeholder*="search"], input[placeholder*="Search"], input[aria-label*="search"]');
            for (var i = 0; i < searchInputs.length; i++) {
                var rect = searchInputs[i].getBoundingClientRect();
                if (rect.width > 50) {
                    searchInputs[i].focus();
                    return {found: true, x: rect.left + rect.width/2, y: rect.top + rect.height/2};
                }
            }
            // Try clicking a search icon/button
            var searchBtns = document.querySelectorAll('[aria-label*="search"], [data-testid*="search"]');
            for (var j = 0; j < searchBtns.length; j++) {
                var rect2 = searchBtns[j].getBoundingClientRect();
                if (rect2.width > 20) {
                    searchBtns[j].click();
                    return {found: true, clicked: 'search button'};
                }
            }
            return {found: false};
        })();
        '''
        search_result = cdp_execute_script(ws_url, search_script)
        log(f"Search field: {search_result}")
        time.sleep(1)

        # Type cuisine
        cdp_type_text(ws_url, cuisine_type)
        time.sleep(1)
        cdp_press_key(ws_url, 'Enter')
        time.sleep(3)

    # Step 5: Get list of restaurants
    log("Getting restaurant list...")
    time.sleep(2)

    # Scroll down a bit to load restaurants
    cdp_execute_script(ws_url, 'window.scrollBy(0, 300);')
    time.sleep(1)

    restaurants_script = '''
    (function() {
        var restaurants = [];
        var seen = {};

        // Get page text and parse restaurant names
        var pageText = document.body.innerText;

        // Pattern: Restaurant names appear before "$0 Delivery Fee" or rating patterns
        // Split by newlines and look for patterns
        var lines = pageText.split('\\n');

        for (var i = 0; i < lines.length && restaurants.length < 10; i++) {
            var line = lines[i].trim();

            // Skip empty or too short
            if (line.length < 5 || line.length > 50) continue;

            // Skip obvious non-restaurant lines
            if (line.includes('$')) continue;
            if (line.includes('Skip to')) continue;
            if (line.includes('Search')) continue;
            if (line.includes('Delivery Fee')) continue;
            if (line.includes('results')) continue;
            if (line.includes('Sponsored')) continue;
            if (line.match(/^\\d/)) continue;
            if (line.includes('min')) continue;
            if (line.includes('Offer')) continue;
            if (line.includes('Reset')) continue;
            if (line.includes('Buy 1')) continue;
            if (line.includes('Get 1')) continue;
            if (line.includes('Free')) continue;
            if (line.includes('Save ')) continue;
            if (line.includes('Spend ')) continue;
            if (line.includes('Top Offer')) continue;
            if (line.includes('Farther Away')) continue;

            // Check if next lines have delivery info (confirms restaurant card)
            var hasDeliveryInfo = false;
            for (var j = i + 1; j < i + 8 && j < lines.length; j++) {
                if (lines[j].includes('Delivery Fee') || lines[j].includes(' min')) {
                    hasDeliveryInfo = true;
                    break;
                }
            }
            if (!hasDeliveryInfo) continue;

            if (seen[line]) continue;
            seen[line] = true;

            // Now find this text on the page and get its position
            var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            var node;
            while (node = walker.nextNode()) {
                if (node.textContent.trim() === line) {
                    var parent = node.parentElement;
                    var clickable = parent.closest('a') || parent;
                    var rect = clickable.getBoundingClientRect();

                    if (rect.top > 200 && rect.top < window.innerHeight && rect.width > 50) {
                        restaurants.push({
                            name: line,
                            x: rect.left + rect.width/2,
                            y: rect.top + rect.height/2
                        });
                        break;
                    }
                }
            }
        }

        return {
            count: restaurants.length,
            restaurants: restaurants,
            pageText: pageText.substring(0, 1500)
        };
    })();
    '''
    restaurants_result = cdp_execute_script(ws_url, restaurants_script)
    log(f"Restaurants found: {restaurants_result}")

    try:
        rest_data = restaurants_result.get('result', {}).get('result', {}).get('value', {})
    except:
        rest_data = {}

    restaurants = rest_data.get('restaurants', [])

    if not restaurants:
        return {
            'success': False,
            'error': 'No restaurants found. Try a different search or check if Uber Eats loaded correctly.',
            'page_text': rest_data.get('pageText', '')[:300]
        }

    # Step 6: Select first/best restaurant
    selected_restaurant = restaurants[0]
    log(f"Selecting restaurant: {selected_restaurant['name']}")

    # Find the restaurant link and navigate to it directly
    escaped_name = selected_restaurant['name'].replace('"', '\\"').replace("'", "\\'")
    find_link_script = '''
    (function() {
        var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
        var node;
        while (node = walker.nextNode()) {
            if (node.textContent.trim() === "''' + escaped_name + '''") {
                var el = node.parentElement;
                var link = el.closest('a');
                if (link && link.href && link.href.includes('/store/')) {
                    return {found: true, href: link.href};
                }
            }
        }
        return {found: false};
    })();
    '''
    link_result = cdp_execute_script(ws_url, find_link_script)
    log(f"Restaurant link: {link_result}")

    try:
        link_data = link_result.get('result', {}).get('result', {}).get('value', {})
    except:
        link_data = {}

    if link_data.get('found') and link_data.get('href'):
        # Navigate directly to the restaurant page
        store_url = link_data['href']
        log(f"Navigating to restaurant: {store_url}")
        cdp_navigate(ws_url, store_url)
        time.sleep(5)  # Wait for restaurant page to load
    else:
        log("Could not find restaurant link, trying click...")
        rx, ry = selected_restaurant['x'], selected_restaurant['y']
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': rx, 'y': ry, 'button': 'left', 'clickCount': 1
        })
        time.sleep(0.1)
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': rx, 'y': ry, 'button': 'left', 'clickCount': 1
        })
        time.sleep(5)

    # Verify we navigated to restaurant page
    page_check = cdp_execute_script(ws_url, 'window.location.href')
    log(f"Current URL: {page_check}")

    # Check if we're actually on a store page
    try:
        current_url = page_check.get('result', {}).get('result', {}).get('value', '')
    except:
        current_url = ''

    if '/store/' not in current_url:
        log("Not on a store page, trying to find and click first restaurant link...")
        # Try to find any restaurant link on the page and navigate
        find_any_store = cdp_execute_script(ws_url, '''
        (function() {
            var links = document.querySelectorAll('a[href*="/store/"]');
            for (var i = 0; i < links.length; i++) {
                var link = links[i];
                var rect = link.getBoundingClientRect();
                if (rect.top > 100 && rect.width > 50) {
                    return {found: true, href: link.href, text: link.textContent.substring(0, 50)};
                }
            }
            return {found: false};
        })();
        ''')
        log(f"Found store link: {find_any_store}")

        try:
            store_link = find_any_store.get('result', {}).get('result', {}).get('value', {})
        except:
            store_link = {}

        if store_link.get('found') and store_link.get('href'):
            log(f"Navigating to store: {store_link['href']}")
            cdp_navigate(ws_url, store_link['href'])
            time.sleep(5)

    # Step 7: If surprise_me, research top dishes for this restaurant
    recommended_dish = None
    if surprise_me:
        log(f"Researching top dishes at {selected_restaurant['name']}...")
        research = search_restaurant_reviews(selected_restaurant['name'], pickup_address)
        if research.get('suggested_dishes'):
            recommended_dish = research['suggested_dishes'][0]
            log(f"Top recommended dish: {recommended_dish}")

    # Step 8: Get menu items from restaurant page
    log("Getting menu items...")
    time.sleep(2)

    # Wait for menu to load - check if we're on a restaurant page
    page_check = cdp_execute_script(ws_url, '''
    (function() {
        return {
            url: window.location.href,
            hasMenu: document.body.innerText.includes('$'),
            pageText: document.body.innerText.substring(0, 500)
        };
    })();
    ''')
    log(f"Restaurant page check: {page_check}")

    # Scroll down to load more menu items
    cdp_execute_script(ws_url, 'window.scrollBy(0, 300);')
    time.sleep(1)

    menu_script = '''
    (function() {
        var items = [];
        var seen = {};

        // Look for clickable elements that have a price
        var allElements = document.querySelectorAll('button, [role="button"], li, article, div');

        allElements.forEach(function(el) {
            if (items.length >= 15) return;

            var text = (el.textContent || '').trim();

            // Must have a price
            if (!text.includes('$')) return;

            // Extract price
            var priceMatch = text.match(/\\$\\d+\\.?\\d*/);
            if (!priceMatch) return;

            // Skip if too long (probably a container) or too short
            if (text.length > 300 || text.length < 10) return;

            // Get just the item name (text before the price usually)
            var parts = text.split('$')[0].trim();
            var name = parts.split('\\n')[0].trim();
            if (name.length < 3 || name.length > 80) return;

            // Skip duplicates
            if (seen[name]) return;
            seen[name] = true;

            var rect = el.getBoundingClientRect();
            // Only visible elements of reasonable size
            if (rect.width > 150 && rect.height > 50 && rect.top > 0 && rect.top < window.innerHeight + 200) {
                items.push({
                    name: name,
                    price: priceMatch[0],
                    x: rect.left + rect.width/2,
                    y: rect.top + rect.height/2
                });
            }
        });

        return {
            count: items.length,
            items: items,
            pageText: document.body.innerText.substring(0, 1500)
        };
    })();
    '''
    menu_result = cdp_execute_script(ws_url, menu_script)
    log(f"Menu items: {menu_result}")

    try:
        menu_data = menu_result.get('result', {}).get('result', {}).get('value', {})
    except:
        menu_data = {}

    menu_items = menu_data.get('items', [])

    if not menu_items:
        return {
            'success': False,
            'error': 'No menu items found on restaurant page.',
            'restaurant': selected_restaurant['name'],
            'page_text': menu_data.get('pageText', '')[:300]
        }

    # Step 9: Select an item - skip garbage entries, find real menu items
    # Filter out garbage entries - be strict about what's a real menu item
    real_items = []
    garbage_patterns = [
        'Delivery', 'Group', 'Menu ', 'Heart outline', 'Featured items',
        'AM –', 'PM', 'Pickup', 'Schedule', 'Sign in', 'Search', 'Cart',
        'Order type', 'Store info', 'More info', 'See details', 'Ratings',
        'Popular', 'Promoted', '全日', '菜單', 'items', 'Free with'
    ]

    for item in menu_items:
        name = item['name']
        price = item.get('price', '')

        # Skip items matching garbage patterns
        skip = False
        for pattern in garbage_patterns:
            if pattern in name:
                skip = True
                break
        if skip:
            continue

        # Skip prices that indicate non-food items
        if price in ['$0', '$30', '$0.00']:
            continue

        # Name must be reasonable length for a food item
        if len(name) < 4 or len(name) > 60:
            continue

        # Skip if name is mostly numbers or punctuation
        alpha_count = sum(1 for c in name if c.isalpha())
        if alpha_count < len(name) * 0.5:
            continue

        real_items.append(item)

    log(f"Filtered menu: {len(real_items)} real items from {len(menu_items)} total")

    if not real_items:
        real_items = menu_items  # Fallback to all if filtering removes everything

    # Priority 1: Look for recommended dish from research
    selected_item = None
    if recommended_dish:
        for item in real_items:
            if recommended_dish.lower() in item['name'].lower():
                selected_item = item
                log(f"Found recommended dish in menu: {item['name']}")
                break

    # Priority 2: Look for "#1 most liked" items
    if not selected_item:
        for item in real_items:
            if '#1 most liked' in item['name'] or 'most liked' in item['name'].lower():
                selected_item = item
                log(f"Found most liked item: {item['name']}")
                break

    # Priority 3: Look for items with food keywords
    if not selected_item:
        food_keywords = ['chicken', 'beef', 'pork', 'rice', 'noodle', 'soup', 'burger',
                        'taco', 'burrito', 'sandwich', 'steak', 'fish', 'shrimp',
                        'curry', 'ramen', 'dumpling', 'wings', 'combo', 'bowl', 'roll']
        for item in real_items:
            item_lower = item['name'].lower()
            for keyword in food_keywords:
                if keyword in item_lower:
                    selected_item = item
                    log(f"Found item with food keyword '{keyword}': {item['name']}")
                    break
            if selected_item:
                break

    # Fallback: just take first item
    if not selected_item:
        selected_item = real_items[0]

    log(f"Selecting item: {selected_item['name'][:60]} - {selected_item['price']}")

    # First scroll down to see menu items
    cdp_execute_script(ws_url, 'window.scrollBy(0, 350);')
    time.sleep(1)

    # Clean up item name for searching (remove prefixes like "#1 most likedPlus small")
    clean_name = selected_item['name']
    for prefix in ['#1 most likedPlus small', '#2 most likedPlus small', '#3 most likedPlus small',
                   '#1 most liked', '#2 most liked', '#3 most liked', 'Plus small']:
        clean_name = clean_name.replace(prefix, '')
    clean_name = clean_name.strip()
    item_price = selected_item['price']

    log(f"Looking for: '{clean_name}' at {item_price}")

    # Click menu item - search for element containing the clean item name and price
    clean_name_escaped = clean_name.replace('\\', '\\\\').replace('"', '\\"').replace("'", "\\'")
    click_item_script = '''
    (function() {
        var targetName = "''' + clean_name_escaped + '''";
        var targetPrice = "''' + item_price + '''";

        // First, look for links with the item name (most reliable)
        var links = document.querySelectorAll('a');
        for (var i = 0; i < links.length; i++) {
            var link = links[i];
            var text = link.textContent || '';
            if (text.includes(targetName) && text.includes(targetPrice)) {
                var rect = link.getBoundingClientRect();
                if (rect.width > 80 && rect.height > 50 && rect.top > 0) {
                    link.scrollIntoView({behavior: 'instant', block: 'center'});
                    link.click();
                    return {clicked: true, method: 'link', text: text.substring(0, 80)};
                }
            }
        }

        // Try buttons
        var buttons = document.querySelectorAll('button, [role="button"]');
        for (var j = 0; j < buttons.length; j++) {
            var btn = buttons[j];
            var text2 = btn.textContent || '';
            if (text2.includes(targetName)) {
                var rect2 = btn.getBoundingClientRect();
                if (rect2.width > 80 && rect2.height > 50) {
                    btn.scrollIntoView({behavior: 'instant', block: 'center'});
                    btn.click();
                    return {clicked: true, method: 'button', text: text2.substring(0, 80)};
                }
            }
        }

        // Try div/article elements containing the item
        var containers = document.querySelectorAll('li, article, div');
        for (var k = 0; k < containers.length; k++) {
            var el = containers[k];
            var text3 = el.textContent || '';

            // Must contain our item name
            if (!text3.includes(targetName)) continue;

            var rect3 = el.getBoundingClientRect();
            // Must be a reasonable menu item card size
            if (rect3.width < 100 || rect3.width > 400) continue;
            if (rect3.height < 80 || rect3.height > 350) continue;
            if (rect3.top < 0 || rect3.top > 900) continue;

            // Found a good container - click it
            el.scrollIntoView({behavior: 'instant', block: 'center'});
            el.click();
            return {clicked: true, method: 'container', tag: el.tagName, text: text3.substring(0, 80)};
        }

        return {clicked: false, targetName: targetName};
    })();
    '''
    click_result = cdp_execute_script(ws_url, click_item_script)
    log(f"Click item result: {click_result}")

    try:
        click_data = click_result.get('result', {}).get('result', {}).get('value', {})
    except:
        click_data = {}

    time.sleep(3)  # Wait for modal to open

    # Verify modal opened - check for dialog element
    modal_verify = cdp_execute_script(ws_url, '''
    (function() {
        var dialog = document.querySelector('[role="dialog"], [data-testid*="modal"], [class*="modal"]');
        var bodyText = document.body.innerText.toLowerCase();
        var hasAddBtn = bodyText.includes('add 1') || bodyText.includes('add to cart') ||
                        bodyText.includes('add to order') || bodyText.includes('add for');

        return {
            hasDialog: dialog !== null,
            hasAddButton: hasAddBtn,
            dialogText: dialog ? dialog.innerText.substring(0, 300) : '',
            url: window.location.href
        };
    })();
    ''')
    log(f"Modal verify: {modal_verify}")

    try:
        modal_data = modal_verify.get('result', {}).get('result', {}).get('value', {})
    except:
        modal_data = {}

    # If modal didn't open, try direct coordinate click
    if not modal_data.get('hasDialog') and not modal_data.get('hasAddButton'):
        log("Modal not detected, trying coordinate click...")
        ix, iy = selected_item['x'], selected_item['y']

        # If x is too far right, it's in a horizontal scroll
        if ix > 1000:
            log(f"Item at x={ix} is likely off-screen, scrolling carousel...")
            cdp_execute_script(ws_url, '''
            (function() {
                var scrollContainers = document.querySelectorAll('[class*="scroll"], [class*="carousel"], [style*="overflow"]');
                for (var i = 0; i < scrollContainers.length; i++) {
                    var el = scrollContainers[i];
                    if (el.scrollWidth > el.clientWidth) {
                        el.scrollLeft += 400;
                        return {scrolled: true};
                    }
                }
                return {scrolled: false};
            })();
            ''')
            time.sleep(1)
            ix = min(ix, 600)  # Adjust to visible area

        log(f"Clicking at coordinates ({ix}, {iy})")
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': ix, 'y': iy, 'button': 'left', 'clickCount': 1
        })
        time.sleep(0.1)
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': ix, 'y': iy, 'button': 'left', 'clickCount': 1
        })
        time.sleep(3)

    # Step 10: Click "Add to Cart" or similar button
    log("Looking for Add to Cart button...")
    time.sleep(1)  # Extra wait for modal to fully render

    # Check what's on the page after clicking item
    modal_check = cdp_execute_script(ws_url, '''
    (function() {
        var bodyText = document.body.innerText.toLowerCase();
        return {
            hasAddButton: bodyText.includes('add to') || bodyText.includes('add 1'),
            hasCustomize: bodyText.includes('required') || bodyText.includes('choose') || bodyText.includes('select'),
            pageText: document.body.innerText.substring(0, 800)
        };
    })();
    ''')
    log(f"Modal check: {modal_check}")

    # Handle required customizations - ONLY select required fields, skip optional add-ons
    # First scroll modal to TOP to find required sections
    cdp_execute_script(ws_url, '''
    (function() {
        var modal = document.querySelector('[role="dialog"]');
        if (modal) {
            modal.scrollTop = 0;
            // Also try scrollable containers inside modal
            var scrollables = modal.querySelectorAll('[style*="overflow"], [data-testid*="scroll"]');
            for (var s of scrollables) { s.scrollTop = 0; }
        }
    })();
    ''')
    time.sleep(0.5)

    # Debug: Log what's inside the modal
    modal_debug = cdp_execute_script(ws_url, '''
    (function() {
        var modal = document.querySelector('[role="dialog"]');
        if (!modal) return {error: 'No modal found'};

        // Get all clickable-looking elements
        var allDivs = modal.querySelectorAll('div[data-testid], li, label, [role="radio"], [role="checkbox"], [role="option"], [role="button"], [role="menuitem"], [role="listitem"]');
        var clickables = [];
        for (var i = 0; i < Math.min(allDivs.length, 30); i++) {
            var el = allDivs[i];
            var text = (el.textContent || '').trim().substring(0, 60);
            var role = el.getAttribute('role') || '';
            var testid = el.getAttribute('data-testid') || '';
            if (text && text.length > 2) {
                clickables.push({text: text, role: role, testid: testid, tag: el.tagName});
            }
        }

        // Get sections with Required
        var allText = modal.innerText;
        var hasRequired = allText.includes('Required');
        var hasChoose = allText.includes('Choose');

        return {
            hasModal: true,
            modalChildCount: modal.children.length,
            hasRequired: hasRequired,
            hasChoose: hasChoose,
            clickables: clickables,
            textPreview: allText.substring(0, 500)
        };
    })();
    ''')
    log(f"Modal debug: {modal_debug}")

    # EXTRACT CUSTOMIZATION QUESTIONS from the modal using DOM structure
    # This finds actual customization sections and their options
    extract_questions = cdp_execute_script(ws_url, '''
    (function() {
        var modal = document.querySelector('[role="dialog"]');
        if (!modal) return {error: 'No modal found'};

        var questions = [];
        var seenHeaders = {};

        // Scroll modal to top first
        modal.scrollTop = 0;

        // Find customization sections by data-testid attributes (most reliable)
        var sections = modal.querySelectorAll('[data-testid="customization-pick-one"], [data-testid="customization-pick-many"], [data-testid*="customization-pick"]');

        for (var s = 0; s < sections.length; s++) {
            var section = sections[s];
            var sectionText = section.innerText || '';

            // Skip if no text
            if (!sectionText.trim()) continue;

            // Parse the section - first line is usually the header
            var lines = sectionText.split('\\n').map(function(l) { return l.trim(); }).filter(function(l) { return l.length > 0; });
            if (lines.length < 2) continue;

            // Extract header (first meaningful line)
            var header = lines[0];

            // Clean up header - remove "Choose X" patterns
            var cleanHeader = header
                .replace(/Choose between [0-9]+ and [0-9]+/gi, '')
                .replace(/Choose up to [0-9]+/gi, '')
                .replace(/Choose [0-9]+/gi, '')
                .replace(/Required/gi, '')
                .replace(/Select [0-9]+/gi, '')
                .trim();

            if (!cleanHeader || cleanHeader.length < 2) {
                // Try second line as header
                if (lines.length > 1) cleanHeader = lines[1];
            }

            // Skip if we've seen this header
            if (seenHeaders[cleanHeader]) continue;
            seenHeaders[cleanHeader] = true;

            // Is it required?
            var isRequired = sectionText.includes('Required');

            // Find options - look for labels within this section
            var optionLabels = section.querySelectorAll('label, [data-testid="customization-option-label"]');
            var options = [];

            for (var o = 0; o < optionLabels.length && options.length < 10; o++) {
                var optText = (optionLabels[o].textContent || '').trim();
                // Skip prices, empty, duplicates
                if (!optText || optText.startsWith('+$') || optText.startsWith('$') || options.indexOf(optText) !== -1) continue;
                // Skip if it's just a number or too short
                if (optText.length < 2 || /^[0-9]+$/.test(optText)) continue;
                options.push(optText);
            }

            // Only add if we have actual options
            if (options.length > 0 && cleanHeader) {
                questions.push({
                    question: cleanHeader,
                    required: isRequired,
                    options: options
                });
            }
        }

        // If no structured sections found, just note that customization is needed
        if (questions.length === 0) {
            var allText = modal.innerText;
            if (allText.includes('Required') || allText.includes('Choose')) {
                questions.push({
                    question: 'Customization Required',
                    required: true,
                    options: ['Please check the screen for options']
                });
            }
        }

        // Get item name and price from modal
        var itemName = '';
        var itemPrice = '';
        var h1 = modal.querySelector('h1, h2, [data-testid*="title"]');
        if (h1) itemName = h1.textContent.trim();

        var priceMatch = allText.match(/\\$\\d+\\.\\d{2}/);
        if (priceMatch) itemPrice = priceMatch[0];

        return {
            itemName: itemName,
            itemPrice: itemPrice,
            questions: questions.filter(function(q) { return q.options.length > 0; }),
            hasRequired: allText.includes('Required'),
            modalText: allText.substring(0, 800)
        };
    })();
    ''')
    log(f"Extracted questions: {extract_questions}")

    try:
        questions_data = extract_questions.get('result', {}).get('result', {}).get('value', {})
    except:
        questions_data = {}

    questions_list = questions_data.get('questions', [])

    # If we have questions and NO answers provided, return questions for user
    if questions_list and customization_answers is None:
        # Clean up item name for display
        display_name = selected_item['name']
        for prefix in ['#1 most likedPlus small', '#2 most likedPlus small', '#3 most likedPlus small',
                       '#1 most liked', '#2 most liked', '#3 most liked', 'Plus small', '1']:
            display_name = display_name.replace(prefix, '')
        display_name = display_name.strip()

        return {
            'success': True,
            'needs_customization': True,
            'restaurant': selected_restaurant['name'],
            'item': display_name,
            'price': selected_item.get('price', ''),
            'questions': questions_list,
            'message': f"I found {display_name} at {selected_restaurant['name']}! Please answer these customization questions:",
            'recommended_dish': recommended_dish if surprise_me else None
        }

    # If answers provided, apply them
    if customization_answers:
        log(f"Applying customization answers: {customization_answers}")
        for answer in customization_answers:
            answer_text = answer.get('answer', '') if isinstance(answer, dict) else str(answer)
            if not answer_text:
                continue

            # Escape the answer text for JavaScript
            escaped_answer = answer_text.replace('"', "'").replace('\n', ' ')

            # Click the option that matches the answer
            js_code = '''
            (function() {
                var modal = document.querySelector('[role="dialog"]');
                if (!modal) return {error: 'No modal'};

                var answerText = "ANSWER_PLACEHOLDER".toLowerCase();

                // Find all clickable options
                var options = modal.querySelectorAll('label, [role="radio"], [role="checkbox"], li, div[data-testid*="option"]');

                for (var i = 0; i < options.length; i++) {
                    var opt = options[i];
                    var optText = (opt.textContent || '').trim().toLowerCase();

                    if (optText.includes(answerText) || answerText.includes(optText.substring(0, 20))) {
                        opt.scrollIntoView({behavior: 'instant', block: 'center'});
                        opt.click();

                        // Also try clicking inner input
                        var input = opt.querySelector('input');
                        if (input) input.click();

                        return {clicked: true, text: opt.textContent.trim().substring(0, 50)};
                    }
                }

                return {clicked: false, answer: answerText};
            })();
            '''.replace('ANSWER_PLACEHOLDER', escaped_answer)

            apply_answer = cdp_execute_script(ws_url, js_code)
            log(f"Apply answer result: {apply_answer}")
            time.sleep(0.3)

    # Check for interrupt before customization loop
    if check_interrupt():
        return {'success': False, 'error': 'Operation cancelled by user', 'interrupted': True}

    # If no questions or already answered, auto-select first options for required fields
    for custom_round in range(10):
        # Check for interrupt in loop
        if check_interrupt():
            return {'success': False, 'error': 'Operation cancelled by user', 'interrupted': True}
        select_required = cdp_execute_script(ws_url, '''
        (function() {
            var modal = document.querySelector('[role="dialog"]');
            if (!modal) return {error: 'No modal'};

            var results = {clicked: [], debug: []};

            // Find required sections that don't have a selection yet
            var sections = modal.querySelectorAll('[data-testid="customization-pick-one"], [data-testid*="customization"]');

            for (var s = 0; s < sections.length; s++) {
                var section = sections[s];
                var sectionText = section.innerText || '';

                // Only process required sections
                if (!sectionText.includes('Required')) continue;

                // Check if already has selection
                var hasSelection = section.querySelector('[aria-checked="true"], input:checked, .selected');
                if (hasSelection) continue;

                // Find first unselected option and click it
                var options = section.querySelectorAll('label, [role="radio"], li');
                for (var o = 0; o < options.length; o++) {
                    var opt = options[o];
                    var isSelected = opt.querySelector('[aria-checked="true"], input:checked');
                    if (isSelected) continue;

                    var optText = (opt.textContent || '').trim();
                    if (optText.length < 3 || optText.includes('Required')) continue;

                    opt.scrollIntoView({behavior: 'instant', block: 'center'});
                    opt.click();
                    results.clicked.push(optText.substring(0, 40));
                    break;
                }
            }

            return results;
        })();
        ''')
        log(f"Auto-select round {custom_round+1}: {select_required}")

        time.sleep(0.5)

        # Check if Add button is now enabled
        check_add = cdp_execute_script(ws_url, '''
        (function() {
            var modal = document.querySelector('[role="dialog"]');
            if (!modal) return {error: 'No modal'};

            var addBtn = null;
            var buttons = modal.querySelectorAll('button');
            for (var i = 0; i < buttons.length; i++) {
                var text = (buttons[i].textContent || '').toLowerCase();
                if (text.includes('add 1') || text.includes('add to order') || text.includes('add for $')) {
                    addBtn = buttons[i];
                    break;
                }
            }

            return {
                addBtnFound: addBtn !== null,
                addBtnEnabled: addBtn ? !addBtn.disabled : false,
                addBtnText: addBtn ? addBtn.textContent.trim().substring(0, 40) : null
            };
        })();
        ''')
        log(f"Add button check: {check_add}")

        try:
            add_data = check_add.get('result', {}).get('result', {}).get('value', {})
        except:
            add_data = {}

        if add_data.get('addBtnEnabled'):
            log("Add button is now enabled!")
            break

        # Scroll modal if needed
        cdp_execute_script(ws_url, '''
        (function() {
            var modal = document.querySelector('[role="dialog"]');
            if (modal) modal.scrollTop += 200;
        })();
        ''')
        time.sleep(0.3)

    # Final: scroll to bottom to see Add button
    cdp_execute_script(ws_url, '''
    (function() {
        var modal = document.querySelector('[role="dialog"]');
        if (modal) modal.scrollTop = modal.scrollHeight;
    })();
    ''')
    time.sleep(0.5)

    # Check for interrupt before Add to Cart
    if check_interrupt():
        return {'success': False, 'error': 'Operation cancelled by user', 'interrupted': True}

    # Now find and click Add to Cart - try multiple times
    add_success = False
    for attempt in range(3):
        if check_interrupt():
            return {'success': False, 'error': 'Operation cancelled by user', 'interrupted': True}
        add_cart_script = '''
        (function() {
            var buttons = document.querySelectorAll('button');
            var targets = ['add to cart', 'add to order', 'add item', 'add 1 to order', 'add 1 for'];

            for (var i = 0; i < buttons.length; i++) {
                var btn = buttons[i];
                var text = (btn.textContent || '').toLowerCase();

                // Skip disabled buttons
                if (btn.disabled) continue;

                for (var j = 0; j < targets.length; j++) {
                    if (text.includes(targets[j])) {
                        btn.scrollIntoView({behavior: 'instant', block: 'center'});

                        // Try multiple click methods
                        btn.focus();
                        btn.click();

                        // Also dispatch a real click event
                        var evt = new MouseEvent('click', {
                            bubbles: true,
                            cancelable: true,
                            view: window
                        });
                        btn.dispatchEvent(evt);

                        return {
                            found: true,
                            clicked: true,
                            text: btn.textContent.trim(),
                            disabled: btn.disabled
                        };
                    }
                }
            }
            return {found: false, availableButtons: Array.from(buttons).slice(0,10).map(b => b.textContent.trim().substring(0,30))};
        })();
        '''
        add_cart_result = cdp_execute_script(ws_url, add_cart_script)
        log(f"Add to cart attempt {attempt+1}: {add_cart_result}")

        try:
            add_btn = add_cart_result.get('result', {}).get('result', {}).get('value', {})
        except:
            add_btn = {}

        if add_btn.get('clicked'):
            add_success = True
            log(f"Clicked: {add_btn.get('text', 'unknown')}")
            time.sleep(2)
            break
        else:
            log(f"Add button not found, available: {add_btn.get('availableButtons', [])}")
            time.sleep(1)

    if not add_success:
        log("Failed to click Add to Cart after 3 attempts")

    # Step 11: Go to cart and checkout
    log("Looking for cart/checkout...")

    cart_script = '''
    (function() {
        // Look for cart button or checkout
        var targets = ['view cart', 'go to cart', 'checkout', 'view order'];
        var buttons = document.querySelectorAll('button, a');

        for (var i = 0; i < buttons.length; i++) {
            var text = (buttons[i].textContent || '').toLowerCase();
            for (var j = 0; j < targets.length; j++) {
                if (text.includes(targets[j])) {
                    buttons[i].scrollIntoView({behavior: 'instant', block: 'center'});
                    var rect = buttons[i].getBoundingClientRect();
                    return {
                        found: true,
                        text: buttons[i].textContent.trim(),
                        x: rect.left + rect.width/2,
                        y: rect.top + rect.height/2
                    };
                }
            }
        }
        return {found: false};
    })();
    '''
    cart_result = cdp_execute_script(ws_url, cart_script)
    log(f"Cart/checkout: {cart_result}")

    try:
        cart_btn = cart_result.get('result', {}).get('result', {}).get('value', {})
    except:
        cart_btn = {}

    if cart_btn.get('found'):
        time.sleep(0.3)
        cx, cy = cart_btn['x'], cart_btn['y']
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': cx, 'y': cy, 'button': 'left', 'clickCount': 1
        })
        time.sleep(0.1)
        cdp_send(ws_url, 'Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': cx, 'y': cy, 'button': 'left', 'clickCount': 1
        })
        time.sleep(3)

    # Step 12: Final verification - check if item is actually in cart
    verify_script = '''
    (function() {
        var bodyText = document.body.innerText;
        var lowerText = bodyText.toLowerCase();

        // Look for cart indicators
        var hasCartBadge = false;
        var cartBadges = document.querySelectorAll('[data-testid*="cart"], [aria-label*="cart"]');
        for (var i = 0; i < cartBadges.length; i++) {
            var text = cartBadges[i].textContent || '';
            if (text.match(/[1-9]/)) {
                hasCartBadge = true;
                break;
            }
        }

        // Also check for "View cart" or similar buttons with item count
        var viewCartBtn = null;
        var buttons = document.querySelectorAll('button, a');
        for (var j = 0; j < buttons.length; j++) {
            var btnText = (buttons[j].textContent || '').toLowerCase();
            if (btnText.includes('view cart') || btnText.includes('view order') || btnText.includes('checkout')) {
                viewCartBtn = buttons[j].textContent.trim();
                break;
            }
        }

        return {
            inCart: lowerText.includes('your order') || lowerText.includes('cart') || lowerText.includes('checkout'),
            hasItems: lowerText.includes('$') && (lowerText.includes('subtotal') || lowerText.includes('total')),
            hasCartBadge: hasCartBadge,
            viewCartBtn: viewCartBtn,
            pageText: bodyText.substring(0, 800)
        };
    })();
    '''
    final_state = cdp_execute_script(ws_url, verify_script)
    log(f"Final state: {final_state}")

    try:
        state = final_state.get('result', {}).get('result', {}).get('value', {})
    except:
        state = {}

    # Determine actual success
    cart_verified = state.get('hasCartBadge', False) or state.get('hasItems', False) or add_success
    actual_success = add_success and (state.get('inCart', False) or state.get('hasCartBadge', False) or True)

    if not add_success:
        return {
            'success': False,
            'error': 'Could not add item to cart. The Add to Cart button was not found or could not be clicked.',
            'restaurant': selected_restaurant['name'],
            'item': selected_item['name'][:50],
            'price': selected_item.get('price', ''),
            'recommended_dish': recommended_dish if surprise_me else None,
            'page_state': state.get('pageText', '')[:300]
        }

    # Clean the item name for display
    display_name = selected_item['name']
    for prefix in ['#1 most likedPlus small', '#2 most likedPlus small', '#3 most likedPlus small',
                   '#1 most liked', '#2 most liked', '#3 most liked', 'Plus small']:
        display_name = display_name.replace(prefix, '')
    display_name = display_name.strip()

    # Step 13: Automatically proceed to checkout with quantity 1
    log("Proceeding to checkout...")
    time.sleep(1)

    # Click View Cart / Go to Checkout button
    checkout_click = cdp_execute_script(ws_url, '''
    (function() {
        var buttons = document.querySelectorAll('button, a');
        var targets = ['view cart', 'go to checkout', 'checkout', 'view order'];

        for (var i = 0; i < buttons.length; i++) {
            var btn = buttons[i];
            var text = (btn.textContent || '').toLowerCase();

            if (btn.disabled) continue;

            for (var j = 0; j < targets.length; j++) {
                if (text.includes(targets[j])) {
                    btn.scrollIntoView({behavior: 'instant', block: 'center'});
                    btn.click();
                    return {clicked: true, text: btn.textContent.trim()};
                }
            }
        }

        // Also try cart icon
        var cartIcon = document.querySelector('[data-testid*="cart"], [aria-label*="cart"]');
        if (cartIcon) {
            cartIcon.click();
            return {clicked: true, method: 'cart-icon'};
        }

        return {clicked: false};
    })();
    ''')
    log(f"Checkout click: {checkout_click}")
    time.sleep(3)

    # Click "Go to Checkout" if we're in cart view
    go_checkout = cdp_execute_script(ws_url, '''
    (function() {
        var buttons = document.querySelectorAll('button, a');
        for (var i = 0; i < buttons.length; i++) {
            var text = (buttons[i].textContent || '').toLowerCase();
            if (text.includes('go to checkout') || text.includes('proceed to checkout')) {
                buttons[i].click();
                return {clicked: true, text: buttons[i].textContent.trim()};
            }
        }
        return {clicked: false};
    })();
    ''')
    log(f"Go to checkout: {go_checkout}")
    time.sleep(3)

    # Check for interrupt before popup handling
    if check_interrupt():
        return {'success': False, 'error': 'Operation cancelled by user', 'interrupted': True}

    # Handle any popups/modals that appear (upsell, promo, etc.) - click the bottom button in the modal
    for popup_attempt in range(3):
        if check_interrupt():
            return {'success': False, 'error': 'Operation cancelled by user', 'interrupted': True}
        dismiss_popup = cdp_execute_script(ws_url, '''
        (function() {
            // First check if we're already on the final checkout page (no popup needed)
            var pageText = document.body.innerText.toLowerCase();
            if (pageText.includes('place order') && !document.querySelector('[role="dialog"]')) {
                return {dismissed: false, reason: 'already on checkout', onCheckout: true};
            }

            // Look for modal/dialog overlay - try multiple selectors
            var modal = document.querySelector('[role="dialog"]');
            if (!modal) modal = document.querySelector('[data-testid*="modal"]');
            if (!modal) modal = document.querySelector('[aria-modal="true"]');
            if (!modal) modal = document.querySelector('[class*="modal"]');
            if (!modal) modal = document.querySelector('[class*="Modal"]');
            if (!modal) modal = document.querySelector('[class*="overlay"]');
            if (!modal) modal = document.querySelector('[class*="Overlay"]');

            // Try finding by z-index (popups usually have high z-index)
            if (!modal) {
                var allDivs = document.querySelectorAll('div');
                for (var d = 0; d < allDivs.length; d++) {
                    var div = allDivs[d];
                    var style = window.getComputedStyle(div);
                    var zIndex = parseInt(style.zIndex) || 0;
                    var position = style.position;

                    // High z-index, fixed/absolute position, covering screen
                    if (zIndex > 100 && (position === 'fixed' || position === 'absolute')) {
                        var rect = div.getBoundingClientRect();
                        if (rect.width > 300 && rect.height > 200) {
                            modal = div;
                            break;
                        }
                    }
                }
            }

            if (!modal) return {dismissed: false, reason: 'no modal found'};

            // Find all buttons - search INSIDE the modal first, then globally if needed
            var buttons = modal.querySelectorAll('button');

            // Find the bottom-most visible button (horizontal bar at bottom of popup)
            var bottomBtn = null;
            var maxY = -1;

            for (var i = 0; i < buttons.length; i++) {
                var btn = buttons[i];

                // Skip disabled
                if (btn.disabled) continue;

                // Skip navigation/accessibility links
                var text = (btn.textContent || '').toLowerCase().trim();
                if (text === 'skip to content' || text === 'back' || text === 'close') continue;

                var rect = btn.getBoundingClientRect();

                // Must be visible (has size and on screen)
                if (rect.width < 80 || rect.height < 30) continue;
                if (rect.bottom < 0 || rect.top > window.innerHeight) continue;

                // Track the bottom-most button
                if (rect.bottom > maxY) {
                    maxY = rect.bottom;
                    bottomBtn = btn;
                }
            }

            if (bottomBtn) {
                bottomBtn.scrollIntoView({behavior: 'instant', block: 'center'});
                bottomBtn.click();
                return {
                    dismissed: true,
                    text: bottomBtn.textContent.trim().substring(0, 50),
                    method: 'bottom-button',
                    y: maxY
                };
            }

            // Try finding buttons with common dismiss text anywhere on page
            var dismissTexts = ['next', 'skip', 'continue', 'no thanks', 'not now', 'done', 'got it'];
            var allButtons = document.querySelectorAll('button');
            for (var j = 0; j < allButtons.length; j++) {
                var b = allButtons[j];
                var bText = (b.textContent || '').toLowerCase().trim();
                for (var k = 0; k < dismissTexts.length; k++) {
                    if (bText === dismissTexts[k] || bText.startsWith(dismissTexts[k] + ' ')) {
                        var bRect = b.getBoundingClientRect();
                        if (bRect.width > 50 && bRect.height > 20 && !b.disabled) {
                            b.click();
                            return {dismissed: true, text: b.textContent.trim(), method: 'text-match'};
                        }
                    }
                }
            }

            // Fallback: try clicking X/close buttons
            var closeBtn = modal.querySelector('[aria-label="Close"], [aria-label="close"], button[class*="close"]');
            if (closeBtn) {
                closeBtn.click();
                return {dismissed: true, method: 'close-btn'};
            }

            return {dismissed: false, hasModal: true, buttonCount: buttons.length};
        })();
        ''')
        log(f"Popup dismiss attempt {popup_attempt+1}: {dismiss_popup}")

        try:
            popup_data = dismiss_popup.get('result', {}).get('result', {}).get('value', {})
        except:
            popup_data = {}

        if popup_data.get('dismissed'):
            time.sleep(1.5)
            # Keep trying in case there are multiple popups
        else:
            # No more popups to dismiss
            break

    time.sleep(1)

    # Check final state
    final_check = cdp_execute_script(ws_url, '''
    (function() {
        var url = window.location.href;
        var bodyText = document.body.innerText;
        var isCheckout = url.includes('checkout') || bodyText.toLowerCase().includes('place order');

        return {
            url: url,
            isCheckout: isCheckout,
            pagePreview: bodyText.substring(0, 600)
        };
    })();
    ''')
    log(f"Final check: {final_check}")

    try:
        final_data = final_check.get('result', {}).get('result', {}).get('value', {})
    except:
        final_data = {}

    on_checkout = final_data.get('isCheckout', False)

    return {
        'success': True,
        'message': f"Added {display_name} ({selected_item.get('price', '')}) to cart!",
        'restaurant': selected_restaurant['name'],
        'item': display_name,
        'item_full': selected_item['name'][:50],
        'price': selected_item.get('price', ''),
        'recommended_dish': recommended_dish if surprise_me else None,
        'status': 'On checkout page - please review and place order in Chrome!' if on_checkout else 'Item in cart - open Chrome to complete checkout',
        'on_checkout': on_checkout,
        'in_cart': True,
        'cart_button': state.get('viewCartBtn', None)
    }


def set_quantity_and_checkout(quantity=1):
    """
    Set the quantity for the item in cart and proceed to checkout.
    """
    import time

    log(f"Setting quantity to {quantity} and going to checkout...")

    # Get CDP connection
    targets = cdp_get_targets()
    if targets is None:
        return {'success': False, 'error': 'Chrome not connected'}

    ws_url = None
    for target in targets:
        if target.get('type') == 'page':
            ws_url = target.get('webSocketDebuggerUrl')
            break

    if not ws_url:
        return {'success': False, 'error': 'No Chrome tab available'}

    # First, find and click the cart icon to open cart
    open_cart = cdp_execute_script(ws_url, '''
    (function() {
        // Look for cart button/icon
        var cartBtn = document.querySelector('[data-testid*="cart"], [aria-label*="cart"], [href*="cart"]');
        if (!cartBtn) {
            // Try finding by text
            var buttons = document.querySelectorAll('button, a');
            for (var i = 0; i < buttons.length; i++) {
                var text = (buttons[i].textContent || '').toLowerCase();
                if (text.includes('cart') || text.includes('view order')) {
                    cartBtn = buttons[i];
                    break;
                }
            }
        }

        if (cartBtn) {
            cartBtn.click();
            return {clicked: true};
        }
        return {clicked: false};
    })();
    ''')
    log(f"Open cart: {open_cart}")
    time.sleep(2)

    # If quantity > 1, we need to increase it
    if quantity > 1:
        for q in range(quantity - 1):
            increase_qty = cdp_execute_script(ws_url, '''
            (function() {
                // Look for + button or increase quantity button
                var buttons = document.querySelectorAll('button, [role="button"]');
                for (var i = 0; i < buttons.length; i++) {
                    var btn = buttons[i];
                    var text = btn.textContent || '';
                    var label = btn.getAttribute('aria-label') || '';

                    // Look for + or "increase" or "add"
                    if (text === '+' || text === 'Add' || label.includes('increase') || label.includes('Increase') || label.includes('add 1')) {
                        btn.click();
                        return {clicked: true, btn: text || label};
                    }
                }

                // Also try finding by Plus icon
                var plusBtns = document.querySelectorAll('[data-testid*="increase"], [data-testid*="plus"], [aria-label*="Add"]');
                if (plusBtns.length > 0) {
                    plusBtns[0].click();
                    return {clicked: true, method: 'plus-btn'};
                }

                return {clicked: false};
            })();
            ''')
            log(f"Increase quantity: {increase_qty}")
            time.sleep(0.5)

    time.sleep(1)

    # Now click "Go to Checkout" button
    checkout_result = cdp_execute_script(ws_url, '''
    (function() {
        var buttons = document.querySelectorAll('button, a');
        var targets = ['go to checkout', 'checkout', 'proceed to checkout', 'place order'];

        for (var i = 0; i < buttons.length; i++) {
            var btn = buttons[i];
            var text = (btn.textContent || '').toLowerCase();

            if (btn.disabled) continue;

            for (var j = 0; j < targets.length; j++) {
                if (text.includes(targets[j])) {
                    btn.scrollIntoView({behavior: 'instant', block: 'center'});
                    btn.click();
                    return {
                        clicked: true,
                        text: btn.textContent.trim()
                    };
                }
            }
        }
        return {clicked: false, availableButtons: Array.from(buttons).slice(0,15).map(b => b.textContent.trim().substring(0,30))};
    })();
    ''')
    log(f"Checkout click: {checkout_result}")

    try:
        checkout_data = checkout_result.get('result', {}).get('result', {}).get('value', {})
    except:
        checkout_data = {}

    time.sleep(3)

    # Verify we're on checkout page
    verify_checkout = cdp_execute_script(ws_url, '''
    (function() {
        var bodyText = document.body.innerText;
        var url = window.location.href;

        return {
            onCheckout: url.includes('checkout') || bodyText.toLowerCase().includes('place order') || bodyText.toLowerCase().includes('payment'),
            url: url,
            pagePreview: bodyText.substring(0, 500)
        };
    })();
    ''')
    log(f"Checkout verify: {verify_checkout}")

    try:
        verify_data = verify_checkout.get('result', {}).get('result', {}).get('value', {})
    except:
        verify_data = {}

    return {
        'success': checkout_data.get('clicked', False),
        'quantity': quantity,
        'on_checkout': verify_data.get('onCheckout', False),
        'message': f'Quantity set to {quantity}. {"On checkout page - please review and place order!" if verify_data.get("onCheckout") else "Please complete checkout in Chrome."}',
        'url': verify_data.get('url', '')
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
            ride_type=data.get('ride_type', 'UberX'),
            num_passengers=data.get('num_passengers', 1)
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
    elif action == 'order_uber_eats':
        return order_uber_eats(
            pickup_lat=data.get('pickup_lat'),
            pickup_lon=data.get('pickup_lon'),
            pickup_address=data.get('pickup_address', ''),
            cuisine_type=data.get('cuisine_type', ''),
            surprise_me=data.get('surprise_me', False),
            customization_answers=data.get('customization_answers', None)
        )
    elif action == 'uber_eats_checkout':
        return set_quantity_and_checkout(
            quantity=data.get('quantity', 1)
        )
    elif action == 'interrupt':
        # Set interrupt flag to stop current operation
        INTERRUPT_FLAG.set()
        log("🛑 INTERRUPT flag set - current operation will stop")
        return {'success': True, 'message': 'Interrupt signal received'}
    elif action == 'create_note':
        # Create an Apple Note using AppleScript
        title = data.get('title', 'Untitled')
        body = data.get('body', '')
        return create_apple_note(title, body)
    elif action == 'get_spotify_track':
        # Get currently playing track from Spotify web player
        return get_spotify_current_track()
    elif action == 'clear_interrupt':
        # Clear interrupt flag
        INTERRUPT_FLAG.clear()
        return {'success': True, 'message': 'Interrupt flag cleared'}
    return {'success': False, 'error': 'Unknown action'}

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(('0.0.0.0', PORT))
server.listen(5)
print('=' * 50)
print('Mac Agent v7 - Auto Chrome Debug Mode')
print('=' * 50)
print(f'Port: {PORT}')
print(f'Screenshots: {SCREENSHOT_DIR}')
print('')

# Auto-start Chrome in debug mode
ensure_chrome_debug_mode()
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
