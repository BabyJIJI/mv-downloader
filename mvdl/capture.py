"""What the built-in browser loaded: the media requests of the current page, with the headers they were
really sent with (Cookie, User-Agent, Referer), so the download looks like the browser's own request.

The browser only reports its traffic here; nothing is injected into the page and nothing is clicked, so
it stays an ordinary browser while the user browses (bot checks such as Cloudflare see a person).
"""
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

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


@dataclass
class Media:
    url: str
    mime: str
    headers: dict = field(default_factory=dict)  # as sent, lower-case names (KEEP_HEADERS only)
    at: float = field(default_factory=time.time)


class Capture:
    """Media requests seen since the page was opened. Fed from the browser's UI thread, read from workers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.media = {}  # url -> Media, in the order first seen

    def reset(self):
        with self.lock:
            self.media.clear()

    def add(self, url, mime, headers):
        """One response the browser received. Returns the Media when it is a new media URL, else None."""
        if not is_media(url, mime):
            return None
        h = {k.lower(): v for k, v in headers.items() if k.lower() in KEEP_HEADERS}
        with self.lock:
            m = self.media.get(url)
            if m is None:
                m = self.media[url] = Media(url, mime or '', h)
                return m
            if h.get('cookie'):  # keep the latest cookies (a passed check renews them)
                m.headers = h
        return None

    def entries(self):
        with self.lock:
            return list(self.media.values())

    def __len__(self):
        with self.lock:
            return len(self.media)

    def candidates(self, page_url, title, user_agent, cookie_for=None, urls=None):
        """Candidates for the download (all, or those in `urls`). `cookie_for(url)` fills in cookies the
        request did not show."""
        out = []
        for m in self.entries():
            if urls is not None and m.url not in urls:
                continue
            url, h = m.url, m.headers
            headers = {'Referer': h.get('referer') or page_url, 'User-Agent': h.get('user-agent') or user_agent}
            cookie = h.get('cookie') or (cookie_for(url) if cookie_for else None)
            if cookie:
                headers['Cookie'] = cookie
            if h.get('authorization'):
                headers['Authorization'] = h['authorization']
            kind = kind_of(url)
            out.append(Candidate(url, title or page_url, 'mp4' if kind == '動画' else kind, 'ブラウザ', headers))
        return out
