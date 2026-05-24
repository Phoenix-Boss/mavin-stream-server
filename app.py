import os
import yt_dlp
import tempfile
import base64
from flask import Flask, request, jsonify

app = Flask(__name__)

def get_cookie_file():
    b64 = os.environ.get('YT_COOKIES_B64')
    if not b64:
        print('No YT_COOKIES_B64 found', flush=True)
        return None
    try:
        data = base64.b64decode(b64)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.txt', mode='wb')
        tmp.write(data)
        tmp.flush()
        print(f'Cookies written to {tmp.name}', flush=True)
        return tmp.name
    except Exception as e:
        print(f'Failed to decode cookies: {e}', flush=True)
        return None

COOKIE_FILE = get_cookie_file()

@app.route('/health', methods=['GET', 'HEAD'])
def health():
    return 'OK', 200

@app.route('/resolve-stream', methods=['POST'])
def resolve_stream():
    body = request.get_json()
    url = (body or {}).get('url')
    if not url:
        return jsonify({'success': False, 'error': 'Missing url', 'audioUrl': None,
                        'videoUrl': None, 'muxedVideoUrl': None, 'duration': 0,
                        'title': '', 'uploaderUrl': None, 'likeCount': -1, 'viewCount': -1})

    print(f'Resolving: {url}', flush=True)

    clients = ['tv_embedded', 'web_embedded', 'android_embedded', 'ios']
    last_error = None

    for client in clients:
        try:
            print(f'Trying client: {client}', flush=True)
            opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'extractor_args': {
                    'youtube': {
                        'player_client': [client],
                        }
                },
            }
            if COOKIE_FILE:
                opts['cookiefile'] = COOKIE_FILE

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            formats = info.get('formats', [])
            audio_formats = [
                f for f in formats
                if f.get('vcodec') == 'none' and f.get('acodec') != 'none' and f.get('url')
            ]
            best_audio = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or 0) if audio_formats else None
            audio_url = best_audio['url'] if best_audio else None

            if not audio_url:
                last_error = f'No direct audio URL from {client}'
                print(last_error, flush=True)
                continue

            video_formats = [
                f for f in formats
                if f.get('acodec') == 'none' and f.get('vcodec') != 'none' and f.get('url') and f.get('height')
            ]
            best_video = next((f for f in video_formats if f.get('height') == 720), None) or \
                         (max(video_formats, key=lambda f: f.get('height') or 0) if video_formats else None)
            video_url = best_video['url'] if best_video else None

            muxed_formats = [
                f for f in formats
                if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('url') and f.get('height')
            ]
            best_muxed = max(muxed_formats, key=lambda f: f.get('height') or 0) if muxed_formats else None
            muxed_url = best_muxed['url'] if best_muxed else None

            print(f"Resolved via {client}: {info.get('title')} ({info.get('duration')}s)", flush=True)

            return jsonify({
                'success': True,
                'audioUrl': audio_url,
                'videoUrl': video_url,
                'muxedVideoUrl': muxed_url,
                'duration': info.get('duration') or 0,
                'title': info.get('title') or '',
                'uploaderUrl': info.get('uploader_url') or None,
                'likeCount': info.get('like_count') or -1,
                'viewCount': info.get('view_count') or -1,
            })

        except Exception as e:
            last_error = str(e)
            print(f'Client {client} failed: {e}', flush=True)
            continue

    return jsonify({'success': False, 'error': last_error, 'audioUrl': None,
                    'videoUrl': None, 'muxedVideoUrl': None, 'duration': 0,
                    'title': '', 'uploaderUrl': None, 'likeCount': -1, 'viewCount': -1})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print(f'Starting on port {port}', flush=True)
    app.run(host='0.0.0.0', port=port)

