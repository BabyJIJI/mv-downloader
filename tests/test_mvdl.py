import http.server
import os
import shutil
import subprocess
import tempfile
import threading

import pytest

from mvdl.capture import Capture, is_media
from mvdl.downloader import download
from mvdl.finder import (Blocked, Candidate, NeedsBrowser, decode_link, find_in_html, find_in_page, find_videos,
                         find_with_ytdlp, kind_of, split_links)


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
    # media playlists whose segments can't be judged alone: still streams
    '/media/disguised.m3u8': (HLS, b'#EXTM3U\n#EXTINF:6.0,\nseg0.jpg\n#EXTINF:6.0,\nseg1.jpg\n#EXT-X-ENDLIST\n'),
    '/media/fmp4.m3u8': (HLS, b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4.0,\nseg0.m4s\n#EXT-X-ENDLIST\n'),
    '/media/aes.m3u8': (HLS, b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n#EXTINF:4.0,\nseg0.ts\n#EXT-X-ENDLIST\n'),
    '/media/radio.m3u8': (HLS, b'#EXTM3U\n#EXTINF:10.0,\nseg0.aac\n#EXT-X-ENDLIST\n'),
    '/media/voice.mp4': ('audio/mp4', AUDIO_MP4),
    # no extension and no useful Content-Type: yt-dlp cannot tell the format
    '/media/stream': ('application/octet-stream', VIDEO),
    # an HLS master behind a URL without .m3u8, sent as text/plain
    '/media/hlsstream': ('text/plain', MASTER),
}
PACKED = (b"eval(function(p,a,c,k,e,d){while(c--)if(k[c])p=p.replace(new RegExp('\\\\b'+c.toString(a)+'\\\\b','g'),"
          b"k[c]);return p}('0={1:\\'2://3/4/5\\'};',10,6,'cfg|file|http|HOST|media|hlsstream'.split('|'),0,{}))")

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
    # the video page only embeds a player page (a host yt-dlp does not know)
    '/outer.html': b'<html><head><title>Outer</title></head><body>'
                   b'<iframe src="/player.html"></iframe><iframe src="/missing.html"></iframe></body></html>',
    # the player page: the stream in a packed script, a player config without extensions
    '/player.html': b'<html><head><title>Player</title></head><body><script>' + PACKED + b'</script>'
                    b'<script>jwplayer("p").setup({sources: [{file: "http://HOST/media/guarded"}],'
                    b' image: "http://HOST/media/poster.jpg"});</script></body></html>',
    # iframes inside iframes: followed MAX_FRAME_DEPTH deep only
    '/deep1.html': b'<html><title>Deep</title><iframe src="/deep2.html"></iframe></html>',
    '/deep2.html': b'<html><iframe src="/deep3.html"></iframe></html>',
    '/deep3.html': b'<html><video src="/media/clip.mp4"></video><iframe src="/deep4.html"></iframe></html>',
    '/deep4.html': b'<html><video src="/media/stream"></video></html>',
    # server buttons whose player URL is encrypted: the site's script asks its server on click (like the
    # page in the issue: btn-server data-link="hex", a play button that puts the player <iframe> in)
    '/servers.html': b'<html><head><title>Servers</title></head><body>'
                     b'<iframe src="/ad.html"></iframe>'
                     b'<div id="player-wrap"><div id="vserver" class="play-button">play</div></div>'
                     b'<div class="btns"><span class="btn-title">SERVER : </span>'
                     b'<a href="javascript:;" class="btn-server active" data-link="3bba8580ed78c8d9f27876a5435bb7ae89b0">EVS</a>'
                     b'<a href="javascript:;" class="btn-server" data-link="fb51c82a413ac5f4846ad00080ff826bae26">VOE</a>'
                     b'<a href="javascript:;" class="btn-server" data-link="773827ac6e2dbc0dcf6160b273bd56661810">DEAD</a>'
                     b'</div><script>'
                     b'let opened = false;'
                     b'const load = b => b.dataset.link.startsWith("7738") ? null : fetch("/api?l=" + b.dataset.link)'
                     b'  .then(r => r.text()).then(u => { document.getElementById("player-wrap").innerHTML ='
                     b'  `<iframe src="${u}"></iframe>`; window.open("/popup.html"); });'
                     b'document.querySelectorAll(".btn-server").forEach(b => b.onclick = () => {'
                     b'  document.querySelector(".btn-server.active").classList.remove("active");'
                     b'  b.classList.add("active"); if (opened) load(b); });'
                     b'document.getElementById("vserver").onclick = () => {'
                     b'  opened = true; load(document.querySelector(".btn-server.active")); };'
                     b'</script></body></html>',
    '/ad.html': b'<html><body>ad</body></html>',
    # the site's jump page in front of the player host (like rep.php?l=...): sends the browser on
    '/jump-outer.html': b'<html><head><title>Jump</title></head><body>'
                        b'<iframe src="/jump.php?l=3bba85&bg=x"></iframe><iframe src="/meta-jump.html"></iframe></body></html>',
    '/jump.php': b'<html><body><script>setTimeout(function () { window.location.replace("/player.html"); }, 0);'
                 b'</script></body></html>',
    '/meta-jump.html': b'<html><head><meta http-equiv="refresh" content="0; URL=\'/deep3.html\'"></head></html>',
    '/popup.html': b'<html><body>pop-up ad</body></html>',
    # server buttons that only hide the URL a little: base64, hex of an <iframe> tag, plain
    '/servers-plain.html': b'<html><head><title>Plain</title></head><body>'
                           b'<a data-link="B64">B64</a><button data-embed="HEX">HEX</button></body></html>',
    # a Streamtape-like player page: the link put together by its script
    '/split.html': b'<html><body><div id="norobotlink"></div><script>'
                   b"document.getElementById('norobotlink').innerHTML = '//HOST/media/clip.mp4?id=1&token=' + "
                   b"('xcdabc').substring(1).substring(2);</script></body></html>",
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
        if path == '/media/guarded':  # player hosts: only for requests from their own player page
            if not (self.headers.get('Referer') or '').endswith('/player.html'):
                self.send_error(403)
                return
            path = '/media/stream'
        host = self.headers['Host'].encode()
        if path == '/api':  # the site's script: an encrypted server -> its player page
            link = self.path.split('l=', 1)[1]
            data, ctype = b'http://' + host + (b'/player.html' if link.startswith('3bba') else b'/split.html'), 'text/plain'
        elif path in PAGES:
            data = PAGES[path].replace(b'HOST', host)
            if path == '/servers-plain.html':
                import base64
                data = data.replace(b'"B64"', b'"' + base64.b64encode(b'http://' + host + b'/player.html') + b'"')
                data = data.replace(b'"HEX"', b'"' + ('<iframe src="http://%s/split.html" frameborder=0></iframe>'
                                                      % host.decode()).encode().hex().encode() + b'"')
            ctype = 'text/html; charset=utf-8'
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


def test_unpack_scripts():
    from mvdl.finder import unpack_scripts
    text = unpack_scripts(PACKED.decode().replace('HOST', 'cdn.example') + ' "a":"https:\\u002F\\u002Fx\\/y.mp4"')
    assert "cfg={file:'http://cdn.example/media/hlsstream'};" in text
    assert '"https://x/y.mp4"' in text


def test_player_iframe_followed(server, check_mode):
    """A page whose video is in a player iframe: the player page is loaded and scanned (packed script,
    player config), and its media get it as Referer."""
    logs = []
    found = find_in_html(server + '/outer.html', logs.append)
    assert sorted((c.url, c.kind) for c in found) == [(server + '/media/guarded', '動画'),
                                                      (server + '/media/hlsstream', 'HLS')]
    assert all(c.title == 'Outer' and c.headers['Referer'] == server + '/player.html' for c in found)
    hls = next(c for c in found if c.kind == 'HLS')
    assert (hls.resolution, hls.size) == ('1280×720', 1_000_000)
    assert any('埋め込みページを読み込めませんでした' in m and '/missing.html' in m for m in logs)


def test_player_iframe_from_browser_dom(server, check_mode):
    dom = '<html><title>DOM</title><iframe src="/player.html"></iframe></html>'
    found = find_in_page(server + '/watch', 'Watch', dom, [], {'User-Agent': 'UA', 'Cookie': 'k=v'}, print)
    assert sorted(c.url for c in found) == [server + '/media/guarded', server + '/media/hlsstream']
    assert all(c.title == 'DOM' for c in found)  # the top page's title, not the player's


def test_iframes_followed_only_so_deep(server, check_mode):
    found = find_in_html(server + '/deep1.html', print)
    assert [c.url for c in found] == [server + '/media/clip.mp4']


def test_frame_headers_keep_cookies_on_the_site():
    from mvdl.finder import _frame_headers
    h = {'User-Agent': 'UA', 'Cookie': 'k=v', 'Referer': 'x'}
    assert _frame_headers(h, 'https://www.site.com/v/1', 'https://player.other.net/e/2') == {
        'User-Agent': 'UA', 'Referer': 'https://www.site.com/v/1'}
    assert _frame_headers(h, 'https://www.site.com/v/1', 'https://site.com/e/2')['Cookie'] == 'k=v'


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


def test_hls_media_playlists_judged_by_the_playlist(server, check_mode):
    """Disguised (.jpg), fMP4 and AES-128 segments can't be read alone: still videos. Audio-only: dropped."""
    cap = Capture()
    for name in ('disguised', 'fmp4', 'aes', 'radio'):
        cap.add(f'{server}/media/{name}.m3u8', 'application/vnd.apple.mpegurl', {})
    found = find_in_page(server + '/page', 'T', '', cap.candidates(server + '/page', 'T', 'UA'), {}, print)
    assert sorted(c.url for c in found) == [f'{server}/media/{n}.m3u8' for n in ('aes', 'disguised', 'fmp4')]
    assert {c.url.rsplit('/', 1)[1]: c.duration for c in found} == {'aes.m3u8': 4.0, 'disguised.m3u8': 12.0,
                                                                     'fmp4.m3u8': 4.0}
    assert all(c.source == 'ブラウザ' for c in found)


def test_download_without_known_format_gets_real_extension(server, tmp_path):
    """URL without an extension, served as octet-stream: saved as .mp4, not .unknown_video."""
    c = Candidate(server + '/media/stream?token=1', 'No ext', 'mp4', 'ブラウザ', {})
    files = download(c, str(tmp_path), '最高画質', print)
    assert [os.path.basename(f) for f in files] == ['No ext.mp4']
    assert os.listdir(tmp_path) == ['No ext.mp4']
    assert open(files[0], 'rb').read() == VIDEO


def test_container_of(tmp_path):
    from mvdl.downloader import container_of
    cases = {
        'mp4': b'\x00\x00\x00\x18ftypisom' + b'\x00' * 16,
        'mov': b'\x00\x00\x00\x14ftypqt  ' + b'\x00' * 16,
        'webm': b'\x1aE\xdf\xa3\x9fB\x86\x81\x01B\xf7\x81\x01B\xf2\x81\x04B\xf3\x81\x08B\x82\x84webm',
        'mkv': b'\x1aE\xdf\xa3\xa3B\x86\x81\x01B\x82\x88matroska',
        'flv': b'FLV\x01\x05' + b'\x00' * 16,
        'ts': (b'\x47' + b'\x00' * 187) * 3,
        None: b'#EXTM3U\n#EXTINF:4.0,\nseg.ts\n',
    }
    for want, head in cases.items():
        f = tmp_path / f'x{want}'
        f.write_bytes(head)
        assert container_of(str(f)) == want, want


class _Resume(http.server.BaseHTTPRequestHandler):
    """First 'drop' (promise the whole file, send half, hang up), then '416' (refuse any Range)."""
    mode = 'drop'
    data = b'\x00\x00\x00\x18ftypmp42' + bytes(range(256)) * 2000

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET(body=False)

    def do_GET(self, body=True):
        n = len(self.data)
        if self.mode == 'drop':
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(n))
            self.end_headers()
            if body:
                self.wfile.write(self.data[:n // 2])
            self.close_connection = True
            return
        if self.headers.get('Range'):
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{n}')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Content-Length', str(n))
        self.end_headers()
        if body:
            self.wfile.write(self.data)


def test_retry_starts_over_when_server_refuses_resume(tmp_path):
    """A failed download left a .part; the server answers the resume with 416: start that file over."""
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Resume)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Candidate(f'http://127.0.0.1:{srv.server_port}/v.mp4', 'Resume', 'mp4', 'ブラウザ', {})
    try:
        _Resume.mode = 'drop'
        with pytest.raises(Exception):
            download(c, str(tmp_path), '最高画質', print)
        assert os.listdir(tmp_path) == ['Resume.mp4.part']
        _Resume.mode = '416'
        files = download(c, str(tmp_path), '最高画質', print)
        assert os.listdir(tmp_path) == ['Resume.mp4']
        assert open(files[0], 'rb').read() == _Resume.data
    finally:
        srv.shutdown()


def test_no_file_is_not_done(tmp_path, monkeypatch):
    """yt-dlp returning without an error is not enough: the file has to be there."""
    import yt_dlp

    from mvdl.downloader import Incomplete
    (tmp_path / 'X.mp4.part').write_bytes(b'half')
    monkeypatch.setattr(yt_dlp.YoutubeDL, 'extract_info', lambda self, url, download: {
        'requested_downloads': [{'filepath': str(tmp_path / 'X.mp4')}]})
    c = Candidate('http://example.invalid/x.mp4', 'X', 'mp4', 'ブラウザ', {})
    with pytest.raises(Incomplete):
        download(c, str(tmp_path), '最高画質', print)


def test_partials_never_include_finished_files(tmp_path):
    from mvdl.jobs import partial_files
    names = ['Movie.final.mp4', 'Clip.fhd.mp4', 'Merged.f137.mp4', 'Merged.f299-1.mp4']
    for n in names:
        (tmp_path / n).write_bytes(b'x')
    (tmp_path / 'Movie.final.mp4.part').write_bytes(b'x')
    got = sorted(os.path.basename(p) for p in partial_files([str(tmp_path / n) for n in names]))
    assert got == ['Merged.f137.mp4', 'Merged.f299-1.mp4', 'Movie.final.mp4.part']


def test_decode_link():
    import base64
    url = 'https://player.example/e/abc123'
    assert decode_link(url) == url
    assert decode_link(base64.b64encode(url.encode()).decode()) == url
    assert decode_link(base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')) == url
    assert decode_link(f'<iframe src="{url}" allowfullscreen></iframe>'.encode().hex()) == url
    assert decode_link(base64.b64encode(f"<IFRAME SRC='{url}'>".encode()).decode()) == url
    assert decode_link('3bba8580ed78c8d9f27876a5435bb7ae89b090f4876114474b62023080dc4ed7') is None  # encrypted
    assert decode_link('12345') is None


def test_split_links():
    text = "document.getElementById('norobotlink').innerHTML = '//st.example/get_video?id=X&token=' + ('xyzTOK').substring(1).substring(2);"
    assert split_links(text) == ['//st.example/get_video?id=X&token=TOK']


def test_encrypted_server_buttons_need_the_browser(server):
    logs = []
    with pytest.raises(NeedsBrowser) as e:
        find_in_html(server + '/servers.html', logs.append)
    assert e.value.servers == ['EVS', 'VOE', 'DEAD'] and e.value.url == server + '/servers.html'
    assert any('暗号化' in m for m in logs)
    with pytest.raises(NeedsBrowser):
        find_videos(server + '/servers.html', print)


def test_readable_server_buttons_followed(server, check_mode):
    found = find_in_html(server + '/servers-plain.html', print)
    by_title = sorted((c.title, c.url) for c in found)
    assert by_title == [('Plain（B64）', server + '/media/guarded'), ('Plain（B64）', server + '/media/hlsstream'),
                        ('Plain（HEX）', server + '/media/clip.mp4?id=1&token=abc')]


def test_players_from_server_buttons(server, check_mode):
    """What the browser got by pressing the buttons (servers.SCRIPT) is followed first, named by server."""
    dom = '<html><title>DOM</title>' + ''.join(f'<iframe src="/ad.html?{i}"></iframe>' for i in range(15)) + '</html>'
    found = find_in_page(server + '/servers.html', 'Servers', dom, [], {'User-Agent': 'UA'}, print,
                         [('EVS', server + '/player.html'), ('VOE', server + '/split.html')])
    assert sorted((c.title, c.url) for c in found) == [
        ('DOM（EVS）', server + '/media/guarded'), ('DOM（EVS）', server + '/media/hlsstream'),
        ('DOM（VOE）', server + '/media/clip.mp4?id=1&token=abc')]


def test_server_script_presses_the_buttons(server):
    """servers.SCRIPT in a real browser on a page whose players only the site's script opens."""
    pw = pytest.importorskip('playwright.sync_api')
    import time

    from mvdl import servers
    try:
        p = pw.sync_playwright().start()
        exe = os.environ.get('MVDL_CHROMIUM')  # a Chromium of another Playwright version
        browser = p.chromium.launch(**({'executable_path': exe} if exe else {}))
    except Exception as e:  # noqa: BLE001
        pytest.skip(f'no browser: {e}')
    try:
        page = browser.new_page()
        page.goto(server + '/servers.html')
        assert page.evaluate(servers.SCRIPT) == 3
        deadline = time.time() + 60
        while not (result := page.evaluate('window.__mvdlServers')).get('done'):
            assert time.time() < deadline, result
            time.sleep(0.2)
        assert servers.players(result) == [('EVS', server + '/player.html'), ('VOE', server + '/split.html')]
        assert [i['label'] for i in result['items']] == ['EVS', 'VOE', 'DEAD']
        assert page.evaluate('document.querySelector(".btn-server.active").textContent') == 'EVS'  # put back
    finally:
        browser.close()
        p.stop()


def test_jump_pages_followed(server, check_mode):
    """A player iframe that is only a jump page (location.replace / meta refresh) is followed to the player."""
    found = find_in_html(server + '/jump-outer.html', print)
    assert sorted(c.url for c in found) == [server + '/media/clip.mp4', server + '/media/guarded',
                                            server + '/media/hlsstream']
    assert all(c.title == 'Jump' for c in found)
