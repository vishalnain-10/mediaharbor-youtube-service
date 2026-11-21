"""Private yt-dlp/FFmpeg job service. Only the Sites server holds its API key."""
import copy
import concurrent.futures
import contextlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(os.environ.get('DATA_DIR', tempfile.gettempdir() + '/mediaharbor-youtube')).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
LOCK = threading.Lock()
JOBS = {}
CACHE = {}
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)
SLOTS = threading.BoundedSemaphore(4)
INFO_SLOTS = threading.BoundedSemaphore(2)
TTL = 3600
MAX_BYTES = int(os.environ.get('MAX_FILE_BYTES', str(6 * 1024**3)))
CONNECTION = {}
TOKEN = os.environ.get('MEDIAHARBOR_BACKEND_TOKEN', '')


def video_id(value):
    u = urlparse(value)
    if u.scheme not in ('http', 'https') or u.username or u.password or u.port:
        raise ValueError('Use a public YouTube watch, Shorts, or youtu.be URL.')
    host = (u.hostname or '').lower()
    if host == 'youtu.be':
        vid = u.path.strip('/').split('/')[0]
    elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
        vid = parse_qs(u.query).get('v', [''])[0] if u.path == '/watch' else (u.path.split('/')[2] if re.match(r'^/(shorts|embed|live)/', u.path) else '')
    else:
        raise ValueError('This service accepts YouTube URLs only.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
        raise ValueError('The YouTube video ID is invalid.')
    return vid


def base_args():
    args = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--no-cache-dir', '--no-colors', '--socket-timeout', '20', '--retries', '2', '--extractor-retries', '1', '--js-runtimes', 'node:' + os.environ.get('NODE_BINARY', 'node')]
    if os.environ.get('FFMPEG_LOCATION'):
        args += ['--ffmpeg-location', os.environ['FFMPEG_LOCATION']]
    return args


def friendly_error(text):
    # Never return signed CDN links, filesystem paths, or raw extractor output.
    if 'confirm you' in text.lower() or 'bot' in text.lower():
        return 'YouTube blocked extraction from this server. Please try again later.'
    if 'private' in text.lower() or 'sign in' in text.lower() or 'age' in text.lower():
        return 'This video requires login or is restricted. Only public, unrestricted videos are supported.'
    if 'max-filesize' in text or 'larger than' in text:
        return 'The selected file exceeds this service’s size limit. Choose a lower quality.'
    return 'YouTube could not prepare this video. It may be unavailable, restricted, or temporarily blocking this server.'


def stream_bytes(stream, duration):
    size = stream.get('filesize') or stream.get('filesize_approx')
    if size and size > 0:
        return int(size)
    rate = stream.get('tbr')
    return int(rate * 1000 * duration / 8) if rate and duration else None


def quality_sizes(data, heights):
    from yt_dlp import YoutubeDL
    sizes = {}
    for height in heights:
        options = {'quiet': True, 'no_warnings': True, 'skip_download': True,
                   'format': f'bestvideo[height<={height}]+bestaudio/best[height<={height}]',
                   'format_sort': ['res', 'ext:mp4:m4a']}
        try:
            with YoutubeDL(options) as ydl:
                selected = ydl.process_ie_result(copy.deepcopy(data), download=False)
            chosen = selected
            streams = chosen.get('requested_formats') or [chosen]
            values = [stream_bytes(stream, data.get('duration')) for stream in streams]
            sizes[str(height)] = sum(values) if values and all(value is not None for value in values) else None
        except Exception:
            sizes[str(height)] = None
    return sizes


def info(value):
    vid = video_id(value)
    with LOCK:
        cached = CACHE.get(vid)
        if cached and cached[0] > time.time():
            return cached[1]
    if not INFO_SLOTS.acquire(blocking=False):
        raise ValueError('The service is busy. Try again shortly.')
    try:
        result = subprocess.run(base_args() + ['--skip-download', '--dump-single-json', 'https://www.youtube.com/watch?v=' + vid], capture_output=True, text=True, encoding='utf-8', timeout=100)
        if result.returncode:
            raise ValueError(friendly_error(result.stderr))
        data = json.loads(result.stdout)
        if data.get('is_live') or data.get('live_status') in ('is_upcoming', 'post_live'):
            raise ValueError('Live and upcoming streams are not supported. Try a completed public video.')
        if (data.get('duration') or 0) > 4 * 3600:
            raise ValueError('This service supports videos up to four hours long.')
        heights = sorted({int(f['height']) for f in data.get('formats', []) if f.get('height') and f.get('vcodec') != 'none' and f.get('height') <= 2160}, reverse=True)
        if not heights:
            raise ValueError('No downloadable video formats are available for this video.')
        output = {'videoId': vid, 'title': data.get('title', vid), 'thumbnail': 'https://i.ytimg.com/vi/' + vid + '/hqdefault.jpg', 'duration': data.get('duration'), 'qualities': heights, 'qualitySizes': quality_sizes(data, heights), 'sourceUrl': 'https://www.youtube.com/watch?v=' + vid}
        with LOCK:
            if len(CACHE) > 100:
                CACHE.clear()
            CACHE[vid] = (time.time() + 120, output)
        return output
    finally:
        INFO_SLOTS.release()


def set_job(job_id, **changes):
    with LOCK:
        JOBS[job_id].update(changes, updated=time.time())


def prepare(job_id, vid, height):
    folder = ROOT / job_id
    proc = None
    try:
        folder.mkdir()
        set_job(job_id, status='downloading', progress=0)
        selection = f'bestvideo[height<={height}]+bestaudio/best[height<={height}]'
        args = base_args() + ['--newline', '--no-simulate', '--max-filesize', str(MAX_BYTES), '-f', selection, '-S', 'res,ext:mp4:m4a', '--merge-output-format', 'mp4', '--remux-video', 'mp4', '--progress-template', 'download:MH:%(progress._percent_str)s', '-o', str(folder / 'video.%(ext)s'), 'https://www.youtube.com/watch?v=' + vid]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
        timed_out = threading.Event()
        def expire():
            timed_out.set()
            proc.kill()
        timer = threading.Timer(7200, expire)
        timer.start()
        recent = ''
        try:
            for line in proc.stdout:
                recent = (recent + line)[-6000:]
                match = re.search(r'MH:\s*([\d.]+)%', line)
                if match:
                    # yt-dlp reports each track separately; avoid a misleading combined percentage.
                    set_job(job_id, progress=min(100, float(match[1])), stage='Downloading media track')
                elif '[Merger]' in line or '[VideoRemuxer]' in line:
                    set_job(job_id, status='merging', progress=None, stage='Combining video and audio')
                if sum(p.stat().st_size for p in folder.glob('*') if p.is_file()) > MAX_BYTES * 2:
                    proc.kill()
                    raise ValueError('The download exceeds this service’s temporary storage limit. Choose a lower quality.')
            code = proc.wait()
        finally:
            timer.cancel()
        if timed_out.is_set():
            raise ValueError('The download timed out. Try a lower quality.')
        if code:
            raise ValueError(friendly_error(recent))
        file = folder / 'video.mp4'
        if not file.is_file() or file.stat().st_size < 1024:
            raise ValueError('No completed MP4 file was produced.')
        if file.stat().st_size > MAX_BYTES:
            raise ValueError('The completed file is too large. Choose a lower quality.')
        set_job(job_id, status='ready', progress=100, size=file.stat().st_size, stage='Ready to download')
    except Exception as error:
        if proc and proc.poll() is None:
            proc.kill()
        set_job(job_id, status='failed', error=str(error) if isinstance(error, ValueError) else 'The download failed. Try again.')
    finally:
        SLOTS.release()


def create_job(value, quality):
    data = info(value)
    cap = max(data['qualities']) if quality == 'original' else int(quality)
    if cap not in data['qualities'] or cap > 2160:
        raise ValueError('Choose one of the video’s available resolutions, up to 2160p.')
    if not SLOTS.acquire(blocking=False):
        raise ValueError('The download queue is full. Please try again shortly.')
    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {'id': job_id, 'status': 'queued', 'progress': None, 'height': cap, 'title': data['title'], 'videoId': data['videoId'], 'created': time.time(), 'updated': time.time()}
    POOL.submit(prepare, job_id, data['videoId'], cap)
    return job(job_id)


def job(job_id):
    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise ValueError('Invalid download ID.')
    with LOCK:
        data = JOBS.get(job_id)
        if not data:
            raise ValueError('This download expired or the server restarted. Prepare the video again.')
        return dict(data)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_):
        pass

    def send_json(self, data, status=200):
        encoded = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(encoded)

    def authorized(self):
        return bool(TOKEN) and hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + TOKEN)

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == '/health':
            return self.send_json({'ok': True})
        if not self.authorized():
            return self.send_json({'error': 'Unauthorized'}, 401)
        try:
            if path.path == '/connection':
                with LOCK:
                    connection = dict(CONNECTION)
                if not connection or time.time() - connection.get('updated', 0) > 180:
                    return self.send_json({'error': 'The computer helper is offline. Start the MediaHarbor YouTube helper on your computer.'}, 503)
                return self.send_json({'origin': connection['origin']})
            if path.path == '/info':
                return self.send_json(info(parse_qs(path.query).get('url', [''])[0]))
            match = re.fullmatch(r'/jobs/([0-9a-f]{32})(/file)?', path.path)
            if match:
                data = job(match[1])
                if not match[2]:
                    return self.send_json(data)
                if data['status'] != 'ready':
                    return self.send_json({'error': 'This file is not ready.'}, 409)
                return self.send_file(match[1], data)
            self.send_json({'error': 'Not found'}, 404)
        except (ValueError, subprocess.TimeoutExpired) as error:
            self.send_json({'error': str(error) if isinstance(error, ValueError) else 'YouTube took too long to respond. Try again.'}, 400)
        except Exception:
            self.send_json({'error': 'The video service encountered an error.'}, 500)

    def do_POST(self):
        if not self.authorized():
            return self.send_json({'error': 'Unauthorized'}, 401)
        if self.path == '/register':
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 4096:
                    raise ValueError('Invalid request size.')
                data = json.loads(self.rfile.read(length))
                u = urlparse(data.get('origin', ''))
                if u.scheme != 'https' or not re.fullmatch(r'[a-z0-9-]+\.trycloudflare\.com', u.hostname or '') or u.port or u.username or u.password or u.path not in ('', '/') or u.query or u.fragment:
                    raise ValueError('Invalid helper origin.')
                with LOCK:
                    CONNECTION.update(origin='https://' + u.hostname, updated=time.time())
                return self.send_json({'ok': True})
            except Exception:
                return self.send_json({'error': 'Invalid helper registration.'}, 400)
        if self.path != '/jobs':
            return self.send_json({'error': 'Not found'}, 404)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 4096:
                raise ValueError('Invalid request size.')
            data = json.loads(self.rfile.read(length))
            self.send_json(create_job(data.get('url', ''), str(data.get('quality', 'original'))), 202)
        except (ValueError, TypeError, subprocess.TimeoutExpired) as error:
            self.send_json({'error': str(error) if isinstance(error, ValueError) else 'Could not prepare this video.'}, 400)
        except Exception:
            self.send_json({'error': 'Could not start the download.'}, 500)

    def send_file(self, job_id, data):
        file = ROOT / job_id / 'video.mp4'
        size = file.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get('Range')
        if range_header:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header)
            if not match or not any(match.groups()):
                return self.send_json({'error': 'Invalid byte range.'}, 416)
            if not match[1]:
                start = max(0, size - int(match[2]))
            else:
                start = int(match[1]);end = min(int(match[2]) if match[2] else end, end)
            if start > end or start >= size:
                return self.send_json({'error': 'Range is outside this file.'}, 416)
        self.send_response(206 if range_header else 200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Content-Disposition', 'attachment; filename="youtube-' + data['videoId'] + '-' + str(data['height']) + 'p.mp4"')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-store')
        if range_header:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        set_job(job_id)
        with file.open('rb') as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk);remaining -= len(chunk)


def cleanup():
    while True:
        time.sleep(60)
        with LOCK:
            expired = [key for key, value in JOBS.items() if value['status'] in ('ready', 'failed') and time.time() - value['updated'] > TTL]
            for key in expired:
                del JOBS[key]
        for key in expired:
            folder = ROOT / key
            if folder.parent == ROOT and re.fullmatch(r'[0-9a-f]{32}', key):
                shutil.rmtree(folder, ignore_errors=True)


if __name__ == '__main__':
    if len(TOKEN) < 32:
        raise SystemExit('MEDIAHARBOR_BACKEND_TOKEN must be set to at least 32 characters.')
    threading.Thread(target=cleanup, daemon=True).start()
    ThreadingHTTPServer((os.environ.get('HOST', '0.0.0.0'), int(os.environ.get('PORT', '8080'))), Handler).serve_forever()
