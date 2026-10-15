"""Find the videos on a page.

- yt-dlp (dedicated extractors for ~1800 sites, plus its generic one)
- our own scan of the HTML: <video>/<source>, og:video, media URLs inside scripts, and iframes
  that yt-dlp has an extractor for (YouTube / Vimeo embeds, ...)
- (capture.py) the media requests the built-in browser made while the user watched the page
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
VIDEO_EXT = ('mp4', 'm4v', 'mov', 'webm', 'mkv', 'flv', 'ts', '3gp', 'avi', 'wmv')
MEDIA_URL_RE = re.compile(
    r'''(?:https?:)?//[^\s"'<>()\\]+?\.(?:%s)(?:\?[^\s"'<>()\\]*)?(?=["'\s<>()\\]|$)''' % '|'.join(MEDIA_EXT),
    re.I)


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
    entries = info['entries'] if 'entries' in info else [info]
    referer = {'Referer': info.get('webpage_url') or url}
    out, direct = [], []
    for e in list(entries or [])[:50]:
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
        direct.append(Candidate(media, e.get('title') or info.get('title') or url, kind, 'ページ内', h))
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

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
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
        elif tag == 'title':
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == 'video' and self._in_video:
            self._in_video -= 1
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
    """Fetch the page ourselves and look through its HTML."""
    try:
        text, final = fetch_page(url, log)
    except Exception as e:  # noqa: BLE001 - any network error means "nothing found here"
        log(f'ページを読み込めませんでした: {_short(e)}')
        if is_cloudflare_block(e):
            raise Blocked() from e
        return []
    return _checked(scan_html(text, final, log, {'Referer': final}), log)


def scan_html(text, page_url, log, headers):
    """Media URLs in an HTML document (<video>, og:video, URLs inside scripts) and the embedded players
    yt-dlp knows (YouTube / Vimeo iframes, ...). Not checked yet: see _checked()."""
    p = _PageParser()
    try:
        p.feed(text)
    except Exception:  # noqa: BLE001 - broken HTML; the regex scan below still runs
        pass
    title = p.title or page_url
    found = [*p.media, *MEDIA_URL_RE.findall(text.replace('\\/', '/'))]
    seen, out = set(), []
    for raw in found:
        u = urllib.parse.urljoin(page_url, raw.strip())
        if not u.startswith('http') or u in seen or has_site_extractor(u):
            continue
        seen.add(u)
        out.append(Candidate(u, title, kind_of(u), 'ページ内', dict(headers)))
    for raw in p.iframes:
        u = urllib.parse.urljoin(page_url, raw.strip())
        if u in seen or not u.startswith('http') or not has_site_extractor(u):
            continue
        seen.add(u)
        log(f'埋め込みプレーヤーを調べています: {u}')
        out += find_with_ytdlp(u, log, {'Referer': page_url})
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


def find_in_page(page_url, title, html, captured, headers, log):
    """「このページの動画を探す」 in the built-in browser.

    captured: Candidates for the media the browser loaded (capture.Capture.candidates()).
    html:     the page's DOM as it is now (after JavaScript).
    headers:  Cookie / User-Agent of the browser, for yt-dlp and the checks.
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
    if html:
        out += scan_html(html, page_url, log, headers | {'Referer': page_url})
    for c in out:
        if c.title == c.url or not c.title:
            c.title = title or page_url
    return _checked(out, log)


def find_videos(url, log):
    """Without the browser: yt-dlp, then our own look at the HTML. Raises Blocked on a bot check."""
    log('yt-dlp で調べています…')
    found = find_with_ytdlp(url, log)
    if found:
        return found
    log('ページの HTML から探しています…')
    return find_in_html(url, log)
