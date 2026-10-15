import http.server
import os
import threading

import pytest

from mvdl.browser import is_media
from mvdl.downloader import download
from mvdl.finder import Candidate, find_in_html, find_videos, find_with_ytdlp, kind_of

VIDEO = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 4096  # content does not matter without ffmpeg

PAGES = {
    '/video-tag.html': b'<html><head><title>Video tag</title></head><body>'
                       b'<video controls><source src="/media/clip.mp4" type="video/mp4"></video></body></html>',
    # a player config inside a script, JSON-escaped slashes
    '/script.html': b'<html><head><meta property="og:title" content="Script player"></head><body><script>'
                    b'var cfg = {"hls": "http:\\/\\/HOST\\/media\\/master.m3u8", "poster": "/p.jpg"};'
                    b'</script></body></html>',
    '/empty.html': b'<html><head><title>Nothing</title></head><body><p>no video</p></body></html>',
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET(body=False)

    def do_GET(self, body=True):
        path = self.path.split('?')[0]
        if path in PAGES:
            data, ctype = PAGES[path].replace(b'HOST', self.headers['Host'].encode()), 'text/html; charset=utf-8'
        elif path == '/media/clip.mp4':
            data, ctype = VIDEO, 'video/mp4'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)


@pytest.fixture(scope='module')
def server():
    s = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{s.server_port}'
    s.shutdown()


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


def test_html_video_tag(server):
    found = find_in_html(server + '/video-tag.html', print)
    assert [(c.url, c.title, c.kind) for c in found] == [(server + '/media/clip.mp4', 'Video tag', 'mp4')]
    assert found[0].headers['Referer'] == server + '/video-tag.html'


def test_html_script_url(server):
    found = find_in_html(server + '/script.html', print)
    assert [(c.url, c.title, c.kind) for c in found] == [(server + '/media/master.m3u8', 'Script player', 'HLS')]


def test_nothing_found(server):
    assert find_videos(server + '/empty.html', print, use_browser=False) == []


def test_ytdlp_generic_finds_video_tag(server):
    found = find_with_ytdlp(server + '/video-tag.html', print)
    assert len(found) == 1 and found[0].source == 'yt-dlp'


def test_download_direct(server, tmp_path):
    c = Candidate(server + '/media/clip.mp4', 'ページ: タイトル/あり?', 'mp4', 'ページ内', '',
                  {'Referer': server + '/video-tag.html'})
    progress = []
    files = download(c, str(tmp_path), '最高画質', print, lambda f, t: progress.append(f))
    assert len(files) == 1
    assert os.path.basename(files[0]).endswith('.mp4')
    assert open(files[0], 'rb').read() == VIDEO
    assert progress and progress[-1] == 1.0
