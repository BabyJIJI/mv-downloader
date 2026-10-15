"""Find the videos on a page.

1. yt-dlp (dedicated extractors for ~1800 sites, plus its generic one)
2. our own scan of the HTML: <video>/<source>, og:video, media URLs inside scripts, and iframes
   that yt-dlp has an extractor for (YouTube / Vimeo embeds, ...)
3. (browser.py) open the page in headless Edge, play it, and watch the network
"""
import html.parser
import re
import urllib.parse
from dataclasses import dataclass, field

import yt_dlp
from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.networking import Request

from .tools import ydl_base_opts

MEDIA_EXT = ('mp4', 'm4v', 'mov', 'webm', 'mkv', 'm3u8', 'mpd', 'flv')
MEDIA_URL_RE = re.compile(
    r'''(?:https?:)?//[^\s"'<>()\\]+?\.(?:%s)(?:\?[^\s"'<>()\\]*)?(?=["'\s<>()\\]|$)''' % '|'.join(MEDIA_EXT),
    re.I)


@dataclass
class Candidate:
    url: str            # what is handed to yt-dlp for the download
    title: str
    kind: str           # site name (yt-dlp extractor), "mp4", "HLS", "DASH", ...
    source: str         # 'yt-dlp' / 'ページ内' / 'ブラウザ'
    detail: str = ''    # resolution, duration
    headers: dict = field(default_factory=dict)  # Referer / Cookie for direct URLs


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


def _detail(info):
    parts = []
    if info.get('height'):
        parts.append(f"{info['height']}p")
    elif info.get('resolution') and info['resolution'] != 'audio only':
        parts.append(info['resolution'])
    if info.get('duration'):
        d = int(info['duration'])
        parts.append(f'{d // 3600}:{d // 60 % 60:02}:{d % 60:02}' if d >= 3600 else f'{d // 60}:{d % 60:02}')
    return ' '.join(parts)


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


def find_with_ytdlp(url, log, headers=None):
    opts = ydl_base_opts(Logger(log)) | {'skip_download': True, 'extract_flat': 'in_playlist',
                                         'playlistend': 500}
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
    site = info.get('extractor_key') or 'yt-dlp'
    if site == 'Generic':
        site = kind_of(info.get('url') or url) if info.get('url') else 'ページ'
    entries = info.get('entries')
    if entries is None:
        return [Candidate(info.get('webpage_url') or url, info.get('title') or url, site, 'yt-dlp',
                          _detail(info), headers or {})]
    out = []
    for e in entries:
        if not e:
            continue
        u = e.get('webpage_url') or e.get('url')
        if not u:
            continue
        out.append(Candidate(u, e.get('title') or u, e.get('ie_key') or site, 'yt-dlp', _detail(e), headers or {}))
    return out


def _short(e):
    s = str(e).replace('ERROR: ', '')
    return s if len(s) < 160 else s[:160] + '…'


class _PageParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.media, self.iframes, self.title, self._in_title = [], [], '', False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('video', 'source', 'audio'):
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
            elif key == 'og:title' and a.get('content') and not self.title:
                self.title = a['content']
        elif tag == 'title':
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()


def fetch_page(url, log):
    """GET the page with yt-dlp's networking (same UA / proxy handling as the download)."""
    with yt_dlp.YoutubeDL(ydl_base_opts(Logger(log))) as ydl:
        with ydl.urlopen(Request(url)) as r:
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
    try:
        text, final = fetch_page(url, log)
    except Exception as e:  # noqa: BLE001 - any network error means "nothing found here"
        log(f'ページを読み込めませんでした: {_short(e)}')
        if is_cloudflare_block(e):
            raise Blocked() from e
        return []
    p = _PageParser()
    try:
        p.feed(text)
    except Exception:  # noqa: BLE001 - broken HTML; the regex scan below still runs
        pass
    title = p.title or final
    found = [*p.media, *MEDIA_URL_RE.findall(text.replace('\\/', '/'))]

    seen, out = set(), []
    referer = {'Referer': final}
    for raw in found:
        u = urllib.parse.urljoin(final, raw.strip())
        if not u.startswith('http') or u in seen:
            continue
        seen.add(u)
        out.append(Candidate(u, title, kind_of(u), 'ページ内', '', referer))
    for raw in p.iframes:
        u = urllib.parse.urljoin(final, raw.strip())
        if u in seen or not u.startswith('http') or not has_site_extractor(u):
            continue
        seen.add(u)
        log(f'埋め込みプレーヤーを調べています: {u}')
        out += find_with_ytdlp(u, log, referer)
    # manifests (whole stream) before single files; master playlists before variants
    out.sort(key=lambda c: (c.kind not in ('HLS', 'DASH'), 'master' not in c.url.lower()))
    return out


def find_videos(url, log, use_browser=True, stop=None):
    """Try each method in turn and return the first non-empty result.

    Raises Blocked when Cloudflare's bot check stops us (a headless browser would be stopped too).
    """
    log('yt-dlp で調べています…')
    found = find_with_ytdlp(url, log)
    if found:
        return found
    log('ページの HTML から探しています…')
    found = find_in_html(url, log)
    if found or not use_browser:
        return found
    from .browser import sniff
    log('ブラウザで再生して、通信から探しています…')
    return sniff(url, log, stop=stop)
