import http.server
import os
import shutil
import subprocess
import tempfile
import threading

import pytest

from mvdl.capture import Capture, is_media
from mvdl.downloader import download
from mvdl.finder import Blocked, Candidate, find_in_html, find_in_page, find_videos, find_with_ytdlp, kind_of


def _make_media():
    """A real 1 s video and an audio-only file when ffmpeg is installed (the ffprobe check needs real
    files); otherwise placeholder bytes, which the Content-Type check accepts."""
    if not shutil.which('ffmpeg'):
        return b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 4096, b'\x00\x00\x00\x18ftypM4A ' + b'\x00' * 4096
    d = tempfile.mkdtemp()
    run = lambda *a: subprocess.run(['ffmpeg', '-v', 'error', '-y', *a], check=True)  # noqa: E731
    run('-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=10', '-t', '1', '-pix_fmt', 'yuv420p', f'{d}/v.mp4')
    run('-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '1', '-c:a', 'aac', f'{d}/a.mp4')
    data = open(f'{d}/v.mp4', 'rb').read(), open(f'{d}/a.mp4', 'rb').read()
    shutil.rmtree(d)
    return data


VIDEO, AUDIO_MP4 = _make_media()

MASTER = b'''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="ja",URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",AUDIO="aud"
v720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2",AUDIO="aud"
v360.m3u8
'''
AUDIO_MASTER = b'''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=128000,CODECS="mp4a.40.2"
radio.m3u8
'''
SUBS = b'#EXTM3U\n#EXTINF:10.0,\nsub0.vtt\n#EXT-X-ENDLIST\n'
HLS = 'application/vnd.apple.mpegurl'
FILES = {  # path -> (content-type, body)
    '/media/clip.mp4': ('video/mp4', VIDEO),
    '/media/master.m3u8': (HLS, MASTER),
    '/media/v720.m3u8': (HLS, b'#EXTM3U\n#EXTINF:4.0,\ns0.ts\n#EXT-X-ENDLIST\n'),
    '/media/audio-only.m3u8': (HLS, AUDIO_MASTER),
    '/media/subs.m3u8': (HLS, SUBS),
    '/media/fake.mp4': ('text/html', b'<html>404 not found</html>'),
    '/media/voice.mp4': ('audio/mp4', AUDIO_MP4),
}

PAGES = {
    '/video-tag.html': b'<html><head><title>Video tag</title></head><body>'
                       b'<video controls><source src="/media/clip.mp4" type="video/mp4"></video></body></html>',
    # a player config inside a script, JSON-escaped slashes
    '/script.html': b'<html><head><meta property="og:title" content="Script player"></head><body><script>'
                    b'var cfg = {"hls": "http:\\/\\/HOST\\/media\\/master.m3u8", "poster": "/p.jpg"};'
                    b'</script></body></html>',
    # a real video among things that only look like media
    '/mixed.html': b'<html><head><title>Mixed</title></head><body>'
                   b'<video src="/media/clip.mp4"></video>'
                   b'<audio><source src="/media/song.mp3"></audio>'
                   b'<picture><source src="/media/pic.webp"><img src="/media/pic.jpg"></picture>'
                   b'<script>var a = ["/media/fake.mp4", "http://HOST/media/master.m3u8", "http://HOST/media/v720.m3u8",'
                   b' "http://HOST/media/audio-only.m3u8", "http://HOST/media/subs.m3u8", "http://HOST/media/voice.mp4"];'
                   b'</script></body></html>',
    # yt-dlp's generic extractor turns each <video> / <audio> into an entry pointing back at the page
    '/av.html': b'<html><head><title>AV</title></head><body>'
                b'<video src="/media/clip.mp4"></video><audio src="/media/voice.mp4"></audio></body></html>',
    '/empty.html': b'<html><head><title>Nothing</title></head><body><p>no video</p></body></html>',
}

SEEN_HEADERS = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET(body=False)

    def do_GET(self, body=True):
        path = self.path.split('?')[0]
        if path == '/cf.html':  # what Cloudflare's bot check answers
            data = b'<html><head><title>Just a moment...</title></head><body></body></html>'
            self.send_response(403)
            self.send_header('Server', 'cloudflare')
            self.send_header('cf-mitigated', 'challenge')
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            if body:
                self.wfile.write(data)
            return
        if path.startswith('/media/'):
            SEEN_HEADERS.append(dict(self.headers))
        if path == '/media/browser-only.mp4':  # refuses anyone but the browser
            self.send_error(403)
            return
        if path in PAGES:
            data, ctype = PAGES[path].replace(b'HOST', self.headers['Host'].encode()), 'text/html; charset=utf-8'
        elif path in FILES:
            ctype, data = FILES[path]
        else:
            self.send_error(404)
            return
        start, end = 0, len(data)
        rng = self.headers.get('Range', '')
        if rng.startswith('bytes='):
            a, _, b = rng[6:].partition('-')
            start, end = int(a or 0), min(int(b) + 1 if b else len(data), len(data))
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end - 1}/{len(data)}')
        else:
            self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(end - start))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        if body:
            self.wfile.write(data[start:end])


@pytest.fixture(scope='module')
def server():
    s = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{s.server_port}'
    s.shutdown()


@pytest.fixture(params=['ffprobe', 'content-type'])
def check_mode(request, monkeypatch):
    """Run the video check both ways: with ffprobe (when installed) and with the Content-Type fallback."""
    import mvdl.probe
    if request.param == 'ffprobe':
        if not shutil.which('ffprobe'):
            pytest.skip('ffprobe not installed')
    else:
        monkeypatch.setattr(mvdl.probe, 'find_tool', lambda name: None)
    return request.param


def test_kind_of():
    assert kind_of('https://x/a/master.m3u8?t=1') == 'HLS'
    assert kind_of('https://x/a.mpd') == 'DASH'
    assert kind_of('https://x/a.MP4') == 'mp4'


def test_is_media():
    assert is_media('https://cdn/x/master.m3u8', 'application/vnd.apple.mpegurl')
    assert is_media('https://cdn/stream', 'video/mp4')
    assert not is_media('https://cdn/seg-001.ts', 'video/mp2t')
    assert not is_media('https://cdn/seg-001.m4s', 'video/iso.segment')
    assert not is_media('https://cdn/app.js', 'application/javascript')
    assert not is_media('blob:https://site/1234', 'video/mp4')


def test_html_video_tag(server, check_mode):
    found = find_in_html(server + '/video-tag.html', print)
    assert [(c.url, c.title, c.kind) for c in found] == [(server + '/media/clip.mp4', 'Video tag', 'mp4')]
    assert found[0].headers['Referer'] == server + '/video-tag.html'
    assert found[0].size == len(VIDEO) and not found[0].size_approx
    if check_mode == 'ffprobe':
        assert (found[0].resolution, round(found[0].duration)) == ('320×240', 1)


def test_html_script_url(server, check_mode):
    found = find_in_html(server + '/script.html', print)
    assert [(c.url, c.title, c.kind) for c in found] == [(server + '/media/master.m3u8', 'Script player', 'HLS')]


def test_only_videos_listed(server, check_mode):
    found = find_in_html(server + '/mixed.html', print)
    assert sorted(c.url for c in found) == [server + '/media/clip.mp4', server + '/media/master.m3u8']
    master = next(c for c in found if c.kind == 'HLS')
    # 2 Mbps (best variant) x 4 s
    assert (master.resolution, master.duration, master.size, master.size_approx) == ('1280×720', 4.0, 1_000_000, True)


def test_fmt_resolution_duration():
    from mvdl.finder import fmt_duration, fmt_resolution
    assert fmt_resolution({'width': 1920, 'height': 1080}) == '1920×1080'
    assert fmt_resolution({'height': 720}) == '720p'
    assert fmt_resolution({'resolution': 'audio only'}) == ''
    assert fmt_duration(None) == '—'
    assert fmt_duration(83.6) == '1:24'
    assert fmt_duration(3725) == '1:02:05'


def test_fmt_size():
    from mvdl.finder import fmt_size
    assert fmt_size(None) == '—'
    assert fmt_size(1_000_000, approx=True) == '約 976.6 KB'
    assert fmt_size(5 * 1024 ** 3) == '5.0 GB'


def test_nothing_found(server):
    assert find_videos(server + '/empty.html', print) == []


def test_ytdlp_generic_finds_video_tag(server, check_mode):
    found = find_with_ytdlp(server + '/video-tag.html', print)
    assert [(c.url, c.kind) for c in found] == [(server + '/media/clip.mp4', 'mp4')]


def test_ytdlp_generic_lists_media_not_page(server, check_mode):
    """Each entry becomes its media URL (not the page), and the audio one is dropped."""
    found = find_with_ytdlp(server + '/av.html', print)
    assert [c.url for c in found] == [server + '/media/clip.mp4']
    assert found[0].size == len(VIDEO)


def test_download_direct(server, tmp_path):
    c = Candidate(server + '/media/clip.mp4', 'ページ: タイトル/あり?', 'mp4', 'ページ内',
                  {'Referer': server + '/video-tag.html'})
    progress = []
    files = download(c, str(tmp_path), '最高画質', print, lambda f, t: progress.append(f))
    assert len(files) == 1
    assert os.path.basename(files[0]).endswith('.mp4')
    assert open(files[0], 'rb').read() == VIDEO
    assert progress and progress[-1] == 1.0


def test_cloudflare_block(server):
    with pytest.raises(Blocked):
        find_videos(server + '/cf.html', print)


def test_download_sends_browser_headers(server, tmp_path):
    SEEN_HEADERS.clear()
    c = Candidate(server + '/media/clip.mp4', 'cookie test', 'mp4', 'ブラウザ',
                  {'Referer': server + '/page', 'User-Agent': 'Mozilla/5.0 test-ua', 'Cookie': 'cf_clearance=abc'})
    download(c, str(tmp_path), '最高画質', print)
    got = SEEN_HEADERS[-1]
    assert got.get('User-Agent') == 'Mozilla/5.0 test-ua'
    assert got.get('Referer') == server + '/page'
    assert 'cf_clearance=abc' in got.get('Cookie', '')


def test_capture_keeps_media_with_sent_headers():
    cap = Capture()
    assert cap.add('https://cdn/x/master.m3u8', 'application/vnd.apple.mpegurl',
                   {'Cookie': 'cf_clearance=1', 'User-Agent': 'UA', 'Referer': 'https://site/watch', 'Accept': '*/*'})
    assert not cap.add('https://cdn/x/seg1.ts', 'video/mp2t', {})          # segment
    assert not cap.add('https://site/app.js', 'application/javascript', {})
    assert not cap.add('https://cdn/x/master.m3u8', 'application/vnd.apple.mpegurl', {})  # seen already
    assert cap.add('https://cdn/v.mp4', 'video/mp4', {})
    [hls, mp4] = cap.candidates('https://site/watch', 'Title', 'BrowserUA', {'https://cdn/v.mp4': 'a=b'}.get)
    assert (hls.kind, hls.source, hls.title) == ('HLS', 'ブラウザ', 'Title')
    assert hls.headers == {'Referer': 'https://site/watch', 'User-Agent': 'UA', 'Cookie': 'cf_clearance=1'}
    assert mp4.headers == {'Referer': 'https://site/watch', 'User-Agent': 'BrowserUA', 'Cookie': 'a=b'}
    cap.reset()
    assert len(cap) == 0


def test_find_in_page_merges_capture_and_dom(server, check_mode):
    """The browser's DOM (after JavaScript) and its captured requests, checked and without duplicates."""
    dom = f'<html><title>DOM</title><video src="{server}/media/clip.mp4"></video><audio src="/media/voice.mp4"></audio>'
    cap = Capture()
    cap.add(server + '/media/master.m3u8', 'application/vnd.apple.mpegurl', {'Cookie': 'k=v'})
    cap.add(server + '/media/clip.mp4', 'video/mp4', {})
    cap.add(server + '/media/fake.mp4', 'video/mp4', {})  # really HTML: dropped by the check
    found = find_in_page(server + '/page', 'Page title', dom, cap.candidates(server + '/page', 'Page title', 'UA'),
                         {'User-Agent': 'UA'}, print)
    assert sorted(c.url for c in found) == [server + '/media/clip.mp4', server + '/media/master.m3u8']
    assert found[0].kind == 'HLS' and found[0].headers['Cookie'] == 'k=v'


def test_browser_video_kept_when_our_check_is_refused(server, check_mode):
    """Captured by the browser but refused to us (403): kept as unchecked. From the HTML: dropped."""
    cap = Capture()
    cap.add(server + '/media/browser-only.mp4', 'video/mp4', {})
    found = find_in_page(server + '/page', 'T', '', cap.candidates(server + '/page', 'T', 'UA'), {}, print)
    assert [(c.url, c.source) for c in found] == [(server + '/media/browser-only.mp4', 'ブラウザ（未確認）')]
    dom = f'<video src="{server}/media/browser-only.mp4"></video>'
    assert find_in_page(server + '/page', 'T', dom, [], {}, print) == []
