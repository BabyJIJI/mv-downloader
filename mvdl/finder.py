"""Find the videos on a page.

- yt-dlp (dedicated extractors for ~1800 sites, plus its generic one)
- our own scan of the HTML: <video>/<source>, og:video, media URLs inside scripts (also packed
  `eval(function(p,a,c,k,e,d)...)` ones and player configs like `file: "https://..."`), iframes that
  yt-dlp has an extractor for (YouTube / Vimeo embeds, ...), and the other iframes (player pages of
  hosts yt-dlp does not know), whose HTML is scanned the same way
- server / mirror buttons (`<a data-link="...">EVS</a>`): their player URL when it is written plainly,
  in base64 or in hex; when it is encrypted (only the site's script can turn it into the player), the
  built-in browser clicks each button and takes the player it opens (app.py, SERVER_SCRIPT)
- (capture.py) the media requests the built-in browser made while the user watched the page
"""
import base64
import binascii
import html.parser
import re
import urllib.parse
from dataclasses import dataclass, field

import yt_dlp
from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.networking import Request

from .tools import ydl_base_opts

MEDIA_EXT = ('mp4', 'm4v', 'mov', 'webm', 'mkv', 'm3u8', 'mpd', 'flv')
VIDEO_EXT = ('mp4', 'm4v', 'mov', 'webm', 'mkv', 'flv', 'ts', '3gp', 'avi', 'wmv')
MEDIA_URL_RE = re.compile(
    r'''(?:https?:)?//[^\s"'<>()\\]+?\.(?:%s)(?:\?[^\s"'<>()\\]*)?(?=["'\s<>()\\]|$)''' % '|'.join(MEDIA_EXT),
    re.I)
# player configs (JW Player, Video.js, Plyr, Clappr, ...) often give the stream without an extension:
# `file: "https://host/stream/abc"`, `"hls":"..."`. Only these keys, so ordinary links are not probed.
PLAYER_KEYS = ('file', 'src', 'source', 'hls', 'hlsurl', 'hls_url', 'm3u8', 'video', 'videourl', 'video_url',
               'stream', 'streamurl', 'stream_url', 'playlist', 'mp4', 'contenturl')
PLAYER_URL_RE = re.compile(
    r'''["']?\b(?:%s)["']?\s*[:=]\s*["']((?:https?:)?//[^\s"'<>\\]+)["']''' % '|'.join(PLAYER_KEYS), re.I)
# never a video: not worth a check
NOT_MEDIA_EXT = ('js', 'css', 'json', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'ico', 'woff', 'woff2', 'ttf',
                 'vtt', 'srt', 'ass', 'xml', 'txt', 'html', 'htm', 'mp3', 'aac', 'm4a', 'ogg', 'opus', 'wav')
PACKED_RE = re.compile(r'''eval\(function\(p,a,c,k,e,[rd]\).*?\.split\(['"]\|['"]\)[^)]*\)\)''', re.S)
# attributes of server / mirror switchers that hold the player (a URL, an <iframe>, or that encoded)
SWITCH_ATTRS = ('data-link', 'data-embed', 'data-iframe', 'data-player', 'data-server', 'data-video',
                'data-stream', 'data-mirror', 'data-hash')
SWITCH_LABEL_MAX = 40
# Streamtape and the hosts that copy it: the link is written by a script, split up to keep bots out:
# `getElementById('norobotlink').innerHTML = '//host/get_video?id=..&token=' + ('xyztok').substring(1).substring(2)`
SPLIT_LINK_RE = re.compile(
    r'''innerHTML\s*=\s*(['"])([^'"]*)\1\s*\+\s*\(?\s*(['"])([^'"]*)\3\s*\)?((?:\.substr(?:ing)?\(\d+\))*)''')
# a page that only sends the browser on (a site's "jump" page in front of the player host)
JS_REDIRECT_RE = re.compile(
    r'''(?:\blocation(?:\.href)?\s*=\s*|\blocation\.(?:replace|assign)\(\s*)["']((?:https?:)?//[^"'\s]+|/[^"'\s/][^"'\s]*)["']''')
MAX_FRAMES = 12       # player pages followed per search
MAX_FRAME_DEPTH = 2   # a page -> its player iframe -> the iframe inside that


@dataclass
class Candidate:
    url: str            # what is handed to yt-dlp for the download
    title: str
    kind: str           # site name (yt-dlp extractor), "mp4", "HLS", "DASH", ...
    source: str         # 'yt-dlp' / 'ページ内' / 'ブラウザ'
    headers: dict = field(default_factory=dict)  # Referer / Cookie for direct URLs
    resolution: str = ''        # "1920×1080", or "720p" when only the height is known
    duration: float | None = None  # seconds
    size: int | None = None     # bytes (best quality)
    size_approx: bool = False   # estimated (bitrate x duration, or the site's estimate)


class Logger:
    """yt-dlp logger -> callback(str)."""

    def __init__(self, log):
        self.log = log

    def debug(self, msg):
        if not msg.startswith('[debug]'):
            self.log(msg)

    def info(self, msg):
        self.log(msg)

    def warning(self, msg):
        self.log('警告: ' + msg)

    def error(self, msg):
        self.log(msg)


def kind_of(url):
    path = urllib.parse.urlparse(url).path.lower()
    ext = path.rsplit('.', 1)[-1] if '.' in path.rsplit('/', 1)[-1] else ''
    return {'m3u8': 'HLS', 'mpd': 'DASH'}.get(ext, ext or '動画')


def fmt_size(n, approx=False):
    if n is None:
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            text = f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
            return ('約 ' if approx else '') + text
        n /= 1024


def ytdlp_size(info):
    """(bytes, approx) of the formats yt-dlp picked, or (None, False) when any part is unknown."""
    total, approx = 0, False
    for f in info.get('requested_formats') or [info]:
        if f.get('filesize'):
            total += f['filesize']
        elif f.get('filesize_approx'):
            total, approx = total + f['filesize_approx'], True
        else:
            return None, False
    return total or None, approx


def fmt_resolution(info):
    w, h = info.get('width'), info.get('height')
    if w and h:
        return f'{w}×{h}'
    if h:
        return f'{h}p'
    res = info.get('resolution') or ''
    return res.replace('x', '×') if res and res != 'audio only' else ''


def fmt_duration(sec):
    if not sec:
        return '—'
    d = int(round(sec))
    return f'{d // 3600}:{d // 60 % 60:02}:{d % 60:02}' if d >= 3600 else f'{d // 60}:{d % 60:02}'


def is_cloudflare_block(e):
    """403 / 503 from Cloudflare's bot check (yt-dlp says so in the message; else look at the response)."""
    if 'cloudflare' in str(e).lower():
        return True
    resp = getattr(e, 'response', None) or getattr(getattr(e, 'exc_info', [None, None])[1], 'response', None)
    headers = getattr(resp, 'headers', None) or {}
    return getattr(resp, 'status', None) in (403, 503) and (
        headers.get('cf-mitigated') == 'challenge' or 'cloudflare' in (headers.get('server') or '').lower())


class Blocked(Exception):
    """The site's bot check (Cloudflare) refused us: only a visible browser, with the user, can get in."""


class NeedsBrowser(Exception):
    """Nothing found, and the page's server buttons are encrypted: only the site's script, in the
    browser, turns them into the player."""

    def __init__(self, url, servers):
        super().__init__(url)
        self.url, self.servers = url, servers


def find_with_ytdlp(url, log, headers=None):
    opts = ydl_base_opts(Logger(log)) | {'skip_download': True, 'extract_flat': 'in_playlist',
                                         'playlistend': 500, 'ignoreerrors': 'only_download'}
    if headers:
        opts['http_headers'] = headers
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        log(f'yt-dlp: 見つかりませんでした（{_short(e)}）')
        if is_cloudflare_block(e):
            raise Blocked() from e
        return []
    if not info:
        return []
    if not has_site_extractor(url) and not has_site_extractor(info.get('webpage_url') or url):
        # found by yt-dlp's generic machinery (Generic, HTML5MediaEmbed, ...) on an ordinary page,
        # not by a site's own extractor: the result points back at the page itself
        return _from_generic(info, url, log, headers)
    site = info.get('extractor_key') or 'yt-dlp'
    entries = info.get('entries')
    if entries is None:
        if not _has_video(info):
            log(f'動画ではないので除きました: {info.get("title") or url}')
            return []
        size, approx = ytdlp_size(info)
        return [Candidate(info.get('webpage_url') or url, info.get('title') or url, site, 'yt-dlp', headers or {},
                          fmt_resolution(info), info.get('duration'), size, approx)]
    out = []  # a playlist / channel of a supported site: its entries are that site's videos
    for e in entries:
        if not e:
            continue
        u = e.get('webpage_url') or e.get('url')
        if not u:
            continue
        out.append(Candidate(u, e.get('title') or u, e.get('ie_key') or site, 'yt-dlp', headers or {},
                             fmt_resolution(e), e.get('duration')))
    return out


def _has_video(info):
    formats = info.get('formats') or [info]
    return any(f.get('vcodec') != 'none' and (f.get('vcodec') or f.get('height') or f.get('width')
                                               or f.get('ext') in VIDEO_EXT or 'm3u8' in (f.get('protocol') or ''))
               for f in formats)


def _from_generic(info, url, log, headers):
    """yt-dlp's generic extractors found media on an arbitrary page (<video>, <audio>, JSON-LD, ...). Its
    entries all point back at the page, so take each one's media URL and check it like the ones from
    our own HTML scan: audio and anything that is not a video is dropped."""
    entries = list(info['entries'] or [])[:50] if 'entries' in info else [info]
    referer = {'Referer': info.get('webpage_url') or url}
    out, direct = [], []
    for e in entries:
        if not e:
            continue
        if e.get('_type') in ('url', 'url_transparent'):  # an embed (YouTube, Vimeo, ...)
            if has_site_extractor(e.get('url') or ''):
                out += find_with_ytdlp(e['url'], log, referer)
            continue
        formats = e.get('formats') or [e]
        f = next((f for f in reversed(formats) if f.get('vcodec') != 'none'), formats[-1])
        media = f.get('manifest_url') or f.get('url')
        if not media or not media.startswith('http'):
            continue
        h = referer | {k: v for k, v in (f.get('http_headers') or {}).items() if k in ('User-Agent', 'Cookie')}
        kind = 'HLS' if 'm3u8' in (f.get('protocol') or '') else kind_of(media)
        title = e.get('title') or info.get('title') or url
        if len(entries) == 1:  # yt-dlp numbers the page's media entries: "Title (1)"
            title = re.sub(r' \(1\)$', '', title)
        direct.append(Candidate(media, title, kind, 'ページ内', h))
    from .probe import keep_videos
    seen = set()
    direct = [c for c in direct if not (c.url in seen or seen.add(c.url))]
    return out + keep_videos(direct, log)


def _short(e):
    s = str(e).replace('ERROR: ', '')
    return s if len(s) < 160 else s[:160] + '…'


class _PageParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.media, self.iframes, self.title, self._in_title = [], [], '', False
        self._in_video = 0  # <source> also appears in <audio> and <picture>
        self.switches = []  # [label, value] of server buttons (SWITCH_ATTRS)
        self.redirects = []  # where the page sends the browser on (meta refresh)
        self._switch = None  # (tag, entry) whose label text is being read

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in ('iframe', 'embed', 'video', 'source', 'img', 'script', 'link', 'meta'):
            value = next((a[k] for k in SWITCH_ATTRS if a.get(k)), None)
            if value:
                entry = [a.get('title') or a.get('aria-label') or '', value.strip()]
                self.switches.append(entry)
                if not entry[0]:
                    self._switch = (tag, entry)
        if tag == 'video':
            self._in_video += 1
        if tag == 'video' or (tag == 'source' and self._in_video):
            for k in ('src', 'data-src'):
                if a.get(k):
                    self.media.append(a[k])
        elif tag in ('iframe', 'embed'):
            for k in ('src', 'data-src'):
                if a.get(k):
                    self.iframes.append(a[k])
        elif tag == 'meta':
            key = (a.get('property') or a.get('name') or '').lower()
            if key in ('og:video', 'og:video:url', 'og:video:secure_url', 'twitter:player:stream') and a.get('content'):
                self.media.append(a['content'])
                self.iframes.append(a['content'])  # may be a player page (YouTube embed, ...)
            elif key == 'og:title' and a.get('content') and not self.title:
                self.title = a['content']
            elif (a.get('http-equiv') or '').lower() == 'refresh':  # <meta http-equiv="refresh" content="0;url=...">
                m = re.search(r'url\s*=\s*["\']?([^"\'\s;]+)', a.get('content') or '', re.I)
                if m:
                    self.redirects.append(m.group(1))
        elif tag == 'title':
            self._in_title = True

    def handle_endtag(self, tag):
        if self._switch and self._switch[0] == tag:
            self._switch = None
        if tag == 'video' and self._in_video:
            self._in_video -= 1
        if tag == 'title':
            self._in_title = False

    def handle_data(self, data):
        if self._switch:
            entry = self._switch[1]
            entry[0] = (entry[0] + ' ' + data.strip()).strip()[:SWITCH_LABEL_MAX]
        if self._in_title and not self.title:
            self.title = data.strip()


def _url_in(text):
    """The player URL a decoded switcher value gives: the value itself, or the src of the <iframe> it is."""
    text = text.strip()
    m = re.search(r'''<(?:iframe|embed|video|source)[^>]+?src\s*=\s*["']?([^"'\s>]+)''', text, re.I)
    if m:
        text = m.group(1)
    text = html.unescape(text)
    if re.fullmatch(r'''(?:https?:)?//[^\s"'<>]+''', text):
        return text
    return None


def decode_link(value):
    """The URL in a server button's value: plain, %-escaped, base64 or hex (also of an <iframe> tag).
    None when it is encrypted (only the site's own script can read it)."""
    v = (value or '').strip()
    if not v:
        return None
    tries = [v, urllib.parse.unquote(v)]
    if re.fullmatch(r'[0-9a-fA-F]+', v) and len(v) % 2 == 0:
        try:
            tries.append(binascii.unhexlify(v).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            pass
    if re.fullmatch(r'[A-Za-z0-9+/_-]+=*', v) and len(v) >= 12:
        for dec in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                tries.append(dec(v + '=' * (-len(v) % 4)).decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                pass
    for t in tries:
        u = _url_in(t)
        if u:
            return u
    return None


def split_links(text):
    """Links a player page puts together in its script (SPLIT_LINK_RE)."""
    out = []
    for m in SPLIT_LINK_RE.finditer(text):
        tail = m.group(4)
        for n in re.findall(r'\((\d+)\)', m.group(5)):
            tail = tail[int(n):]
        u = m.group(2) + tail
        if re.match(r'(?:https?:)?//[^\s/]+/', u):
            out.append(u)
    return out


def fetch_page(url, log, headers=None):
    """GET the page with yt-dlp's networking (same UA / proxy handling as the download)."""
    with yt_dlp.YoutubeDL(ydl_base_opts(Logger(log))) as ydl:
        with ydl.urlopen(Request(url, headers=dict(headers or {}))) as r:
            body = r.read(20 * 1024 * 1024)
            charset = r.headers.get_content_charset() if hasattr(r.headers, 'get_content_charset') else None
            final = r.url
    m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', body[:4096], re.I)
    for enc in (charset, m and m.group(1).decode('ascii', 'ignore'), 'utf-8', 'cp932'):
        if not enc:
            continue
        try:
            return body.decode(enc), final
        except (LookupError, UnicodeDecodeError):
            pass
    return body.decode('utf-8', 'replace'), final


_extractors = None


def has_site_extractor(url):
    global _extractors
    if _extractors is None:
        _extractors = [ie for ie in gen_extractor_classes() if ie.ie_key() != 'Generic']
    return any(ie.suitable(url) for ie in _extractors)


def find_in_html(url, log):
    """Fetch the page ourselves and look through its HTML."""
    try:
        text, final = fetch_page(url, log)
    except Exception as e:  # noqa: BLE001 - any network error means "nothing found here"
        log(f'ページを読み込めませんでした: {_short(e)}')
        if is_cloudflare_block(e):
            raise Blocked() from e
        return []
    state = new_state()
    found = _checked(scan_html(text, final, log, {'Referer': final}, _state=state), log)
    if not found and state['locked']:
        raise NeedsBrowser(final, state['locked'])
    return found


def unpack_scripts(text):
    """The page text plus what its obfuscation hides: JSON-escaped slashes, \\u002F / \\x2F escapes, and
    Dean Edwards packed scripts (`eval(function(p,a,c,k,e,d){...})`, common on player hosts)."""
    from yt_dlp.utils import decode_packed_codes
    parts = [text]
    for m in PACKED_RE.finditer(text):
        try:
            parts.append(decode_packed_codes(m.group(0)).replace("\\'", "'"))
        except Exception:  # noqa: BLE001 - not the usual packer format
            pass
    out = '\n'.join(parts)
    out = re.sub(r'\\(?:u002[fF]|x2[fF])', '/', out)
    return out.replace('\\/', '/')


def _ext(url):
    last = urllib.parse.urlparse(url).path.rsplit('/', 1)[-1]
    return last.rsplit('.', 1)[-1].lower() if '.' in last else ''


def _same_site(a, b):
    ha, hb = urllib.parse.urlparse(a).hostname or '', urllib.parse.urlparse(b).hostname or ''
    return ha == hb or ha.endswith('.' + hb) or hb.endswith('.' + ha)


def _frame_headers(headers, page_url, frame_url):
    """Headers to load a player iframe the way the browser does: the page as Referer, its cookies only
    when the iframe is on the same site."""
    h = {k: v for k, v in headers.items() if k.lower() not in ('cookie', 'referer')}
    if headers.get('Cookie') and _same_site(page_url, frame_url):
        h['Cookie'] = headers['Cookie']
    return h | {'Referer': page_url}


def new_state():
    """What one search has done so far: URLs seen, player pages opened, server buttons it could not read."""
    return {'seen': set(), 'frames': 0, 'depth': 0, 'locked': []}


def scan_html(text, page_url, log, headers, title=None, _state=None, servers=()):
    """Media URLs in an HTML document (<video>, og:video, URLs inside scripts, player configs) and the
    embedded players: those yt-dlp knows (YouTube / Vimeo iframes, ...) go to yt-dlp, the other iframes
    are loaded and scanned the same way (up to MAX_FRAMES, MAX_FRAME_DEPTH deep). Not checked yet: see
    _checked().

    servers: [(label, player URL)] the browser got by clicking the page's server buttons; they and the
    server buttons we can read go first (ad iframes must not use up MAX_FRAMES)."""
    state = _state if _state is not None else new_state()
    seen = state['seen']
    p = _PageParser()
    try:
        p.feed(text)
    except Exception:  # noqa: BLE001 - broken HTML; the regex scan below still runs
        pass
    title = title or p.title or page_url
    script = unpack_scripts(text)
    found = [*p.media, *MEDIA_URL_RE.findall(script)]
    found += [u for u in PLAYER_URL_RE.findall(script) if _ext(u) not in NOT_MEDIA_EXT]
    joined = split_links(script)
    found = [u for u in found if not any(j != u and j.startswith(u) for j in joined)] + joined  # not half a link
    frames = [(label, u) for label, u in servers]
    locked = []
    for label, value in p.switches:
        u = decode_link(value)
        if u:
            frames.append((label, u))
        elif len(value) >= 16 and re.fullmatch(r'[A-Za-z0-9+/=_%.:-]+', value) and not value.isdigit():
            locked.append(label or value[:12] + '…')  # encrypted (not an ID or a name)
    if locked:
        state['locked'] += locked
        log(f'サーバーの切り替えボタン（{", ".join(locked)}）の URL は暗号化されていて、ページの HTML からは読めません')
    frames += [('', u) for u in p.iframes]
    # a jump page (meta refresh, location.href = ...): what it leads to is opened like an iframe
    frames += [('', u) for u in [*p.redirects, *JS_REDIRECT_RE.findall(script)]
               if _ext(u) in ('html', 'htm') or _ext(u) not in NOT_MEDIA_EXT]
    out = []
    for raw in found:
        u = urllib.parse.urljoin(page_url, raw.strip())
        if not u.startswith('http') or u in seen or has_site_extractor(u):
            continue
        seen.add(u)
        out.append(Candidate(u, title, kind_of(u), 'ページ内', dict(headers)))
    for label, raw in frames:
        u = urllib.parse.urljoin(page_url, raw.strip())
        if u in seen or not u.startswith('http'):
            continue
        seen.add(u)
        ftitle = f'{title}（{label}）' if label else title
        if label:
            log(f'サーバー「{label}」のプレーヤー: {u}')
        if has_site_extractor(u):
            log(f'埋め込みプレーヤーを調べています: {u}')
            found_here = find_with_ytdlp(u, log, {'Referer': page_url})
            for c in found_here:
                c.title = ftitle if label else c.title
            out += found_here
            continue
        if _ext(u) in MEDIA_EXT:  # the button gives the video itself
            out.append(Candidate(u, ftitle, kind_of(u), 'ページ内', dict(headers)))
            continue
        if _ext(u) in MEDIA_EXT or _ext(u) in NOT_MEDIA_EXT and _ext(u) not in ('html', 'htm'):
            continue  # a media file is in the list above already; images / scripts are no player
        if state['depth'] >= MAX_FRAME_DEPTH or state['frames'] >= MAX_FRAMES:
            log(f'埋め込みページが多い（深い）ので、これ以上は開きません: {u}')
            continue
        state['frames'] += 1
        log(f'埋め込みページを調べています: {u}')
        h = _frame_headers(headers, page_url, u)
        try:
            frame_text, final = fetch_page(u, log, h)
        except Exception as e:  # noqa: BLE001 - this player only; the rest of the page still counts
            log(f'埋め込みページを読み込めませんでした: {u}（{_short(e)}）')
            continue
        state['depth'] += 1
        try:
            # what the player page loads is requested with it as Referer (player hosts check that)
            in_frame = scan_html(frame_text, final, log, h | {'Referer': final}, ftitle, state)
        finally:
            state['depth'] -= 1
        if not in_frame:
            log(f'埋め込みページに動画の URL は見つかりませんでした: {u}'
                + ('（ブラウザでこのサーバーを選んで再生すると「ブラウザで読み込んだ動画」に出ます）' if label else ''))
        out += in_frame
    return out


def _checked(cands, log):
    """Keep the real videos (yt-dlp results are already checked), without duplicates, best first."""
    from .probe import keep_videos
    seen = set()
    cands = [c for c in cands if not (c.url in seen or seen.add(c.url))]
    out = [c for c in cands if c.source == 'yt-dlp'] + keep_videos([c for c in cands if c.source != 'yt-dlp'], log)
    # site results first, then manifests (whole stream) before single files, master playlists first
    out.sort(key=lambda c: (c.source != 'yt-dlp', c.kind not in ('HLS', 'DASH'), 'master' not in c.url.lower()))
    return out


def find_in_page(page_url, title, html, captured, headers, log, servers=()):
    """「このページの動画を探す」 in the built-in browser.

    captured: Candidates for the media the browser loaded (capture.Capture.candidates()).
    html:     the page's DOM as it is now (after JavaScript).
    headers:  Cookie / User-Agent of the browser, for yt-dlp and the checks.
    servers:  [(label, player URL)] opened by clicking the page's server buttons (app.py SERVER_SCRIPT).
    """
    out = []
    if has_site_extractor(page_url):
        log('yt-dlp で調べています…')
        try:
            out += find_with_ytdlp(page_url, log, headers)
        except Blocked:
            log('yt-dlp はサイトの確認画面で止められました（ブラウザで読み込んだ動画から探します）')
    log(f'ページ内と、ブラウザが読み込んだ動画（{len(captured)} 件）を調べています…')
    out += captured
    if html or servers:
        out += scan_html(html or '', page_url, log, headers | {'Referer': page_url}, servers=servers)
    for c in out:
        if c.title == c.url or not c.title:
            c.title = title or page_url
    return _checked(out, log)


def find_videos(url, log):
    """Without the browser: yt-dlp, then our own look at the HTML. Raises Blocked on a bot check, and
    NeedsBrowser when the player is behind encrypted server buttons."""
    log('yt-dlp で調べています…')
    found = find_with_ytdlp(url, log)
    if found:
        return found
    log('ページの HTML から探しています…')
    return find_in_html(url, log)
