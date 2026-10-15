"""What the built-in browser loaded: the media requests of the current page, with the headers they were
really sent with (Cookie, User-Agent, Referer), so the download looks like the browser's own request.

The browser only reports its traffic here; nothing is injected into the page and nothing is clicked, so
it stays an ordinary browser while the user browses (bot checks such as Cloudflare see a person).
"""
import threading
import urllib.parse

from .finder import MEDIA_EXT, Candidate, kind_of

MEDIA_MIME = ('video/', 'application/vnd.apple.mpegurl', 'application/x-mpegurl', 'audio/mpegurl',
              'application/dash+xml')
SEGMENT_EXT = ('ts', 'm4s', 'aac', 'm4a', 'cmfv', 'cmfa', 'key')
KEEP_HEADERS = ('cookie', 'user-agent', 'referer', 'origin', 'authorization')


def is_media(url, mime):
    """A whole video (file or HLS / DASH manifest), not a segment of one."""
    if not url.startswith('http'):
        return False
    path = urllib.parse.urlparse(url).path.lower()
    ext = path.rsplit('.', 1)[-1] if '.' in path.rsplit('/', 1)[-1] else ''
    if ext in SEGMENT_EXT:
        return False
    mime = (mime or '').lower()
    if mime.startswith('video/mp2t'):  # HLS segment
        return False
    return ext in MEDIA_EXT or mime.startswith(MEDIA_MIME)


class Capture:
    """Media requests seen since the page was opened. Fed from the browser's UI thread, read from workers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.media = {}  # url -> request headers (lower-case names)

    def reset(self):
        with self.lock:
            self.media.clear()

    def add(self, url, mime, headers):
        """One response the browser received. Returns True when it is a new media URL."""
        if not is_media(url, mime):
            return False
        h = {k.lower(): v for k, v in headers.items() if k.lower() in KEEP_HEADERS}
        with self.lock:
            new = url not in self.media
            if new or h.get('cookie'):  # keep the latest cookies (a passed check renews them)
                self.media[url] = h
        return new

    def __len__(self):
        with self.lock:
            return len(self.media)

    def candidates(self, page_url, title, user_agent, cookie_for=None):
        """Candidates for the download. `cookie_for(url)` fills in cookies the request did not show."""
        with self.lock:
            items = list(self.media.items())
        out = []
        for url, h in items:
            headers = {'Referer': h.get('referer') or page_url, 'User-Agent': h.get('user-agent') or user_agent}
            cookie = h.get('cookie') or (cookie_for(url) if cookie_for else None)
            if cookie:
                headers['Cookie'] = cookie
            if h.get('authorization'):
                headers['Authorization'] = h['authorization']
            kind = kind_of(url)
            out.append(Candidate(url, title or page_url, 'mp4' if kind == '動画' else kind, 'ブラウザ', headers))
        return out
