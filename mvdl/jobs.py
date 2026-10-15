"""The download queue's records: one Job per download, the queue saved between runs, and the partial
files of a cancelled download.

Unfinished downloads come back as 一時停止 on the next start: 再開 continues them from their partial files
(yt-dlp picks up its .part / fragment state).
"""
import glob
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field

from .finder import Candidate

WAITING, RUNNING, PAUSED, DONE, FAILED, CANCELLED = '待機中', 'ダウンロード中', '一時停止', '完了', '失敗', '中止'
UNFINISHED = (WAITING, RUNNING, PAUSED)
RESTORED = '一時停止（前回の続き。再開で続きから）'


@dataclass
class Job:
    id: str
    cand: Candidate
    outdir: str
    quality: str
    state: str = WAITING
    text: str = ''
    progress: float | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    pausing: bool = False         # the stop is a pause: keep the partial files and resume later
    files: list = field(default_factory=list)     # saved files
    partials: list = field(default_factory=list)  # files yt-dlp worked on (their .part etc. are the partials)

    def to_dict(self):
        return {'id': self.id, 'cand': asdict(self.cand), 'outdir': self.outdir, 'quality': self.quality,
                'state': self.state, 'text': self.text, 'progress': self.progress, 'files': self.files,
                'partials': self.partials}

    @classmethod
    def from_dict(cls, d):
        job = cls(d['id'], Candidate(**d['cand']), d['outdir'], d['quality'], d.get('state', WAITING),
                  d.get('text', ''), d.get('progress'), files=list(d.get('files') or []),
                  partials=list(d.get('partials') or []))
        if job.state in (WAITING, RUNNING):  # cut off by the last exit: wait for 再開 (its cookies may be old)
            job.state, job.text = PAUSED, RESTORED
        return job


def save_queue(path, jobs):
    """Write the queue (atomically: a crash never leaves half a file)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump([j.to_dict() for j in jobs], f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def load_queue(path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    jobs = []
    for d in data if isinstance(data, list) else []:
        try:
            jobs.append(Job.from_dict(d))
        except (KeyError, TypeError):  # a record from an older version: skip it
            pass
    return jobs


# a single format of a download that is merged afterwards: "Title.f137.mp4" (deleted with the partials)
FORMAT_PART = re.compile(r'\.f[\w-]+\.\w+$')


def partial_files(names):
    """The partial files of the downloads named `names` (the file names yt-dlp reported): .part,
    .part-FragN, .ytdl and, for merged downloads, the single-format files. Never the finished file."""
    out = set()
    for name in names:
        base = glob.escape(name)
        for pattern in (base + '.part', base + '.part-Frag*', base + '.ytdl', base + '.part.ytdl'):
            out.update(p for p in glob.glob(pattern) if os.path.isfile(p))
        if FORMAT_PART.search(name) and os.path.isfile(name):
            out.add(name)
    return sorted(out)


def remove_partials(names):
    """Delete them; returns the files deleted."""
    removed = []
    for p in partial_files(names):
        try:
            os.remove(p)
            removed.append(p)
        except OSError:
            pass
    return removed
