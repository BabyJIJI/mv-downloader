import os

from mvdl.finder import Candidate
from mvdl.jobs import (CANCELLED, DONE, PAUSED, RESTORED, RUNNING, WAITING, Job, load_queue, partial_files,
                       remove_partials, save_queue)


def make(tmp_path, jid, state, partials=()):
    c = Candidate('https://cdn/v.m3u8', f'動画 {jid}', 'HLS', 'ブラウザ', {'Cookie': 'a=b', 'Referer': 'https://site/'},
                  '1920×1080', 61.0, 123456, True)
    return Job(jid, c, str(tmp_path), '最高画質', state, state, 0.4, partials=list(partials))


def test_queue_survives_a_restart(tmp_path):
    path = tmp_path / 'mvdl' / 'queue.json'
    jobs = [make(tmp_path, 'j0', DONE), make(tmp_path, 'j1', RUNNING, ['x.mp4']), make(tmp_path, 'j2', WAITING),
            make(tmp_path, 'j3', PAUSED), make(tmp_path, 'j4', CANCELLED)]
    save_queue(str(path), jobs)
    back = load_queue(str(path))
    assert [j.id for j in back] == ['j0', 'j1', 'j2', 'j3', 'j4']
    # cut off by the exit: paused, waiting for 再開; finished ones as they were
    assert [j.state for j in back] == [DONE, PAUSED, PAUSED, PAUSED, CANCELLED]
    assert back[1].text == RESTORED and back[3].text == PAUSED
    assert back[1].partials == ['x.mp4'] and back[1].progress == 0.4
    assert back[1].cand == jobs[1].cand  # headers (cookies) included, to resume with them
    assert not back[1].stop.is_set()


def test_broken_queue_file_is_ignored(tmp_path):
    p = tmp_path / 'queue.json'
    p.write_text('{not json')
    assert load_queue(str(p)) == []
    assert load_queue(str(tmp_path / 'missing.json')) == []
    p.write_text('[{"id": "j0"}]')  # an unknown record shape
    assert load_queue(str(p)) == []


def test_partials_are_found_and_only_partials_deleted(tmp_path):
    def touch(name):
        (tmp_path / name).write_bytes(b'x')
        return str(tmp_path / name)

    final = touch('動画 [1].mp4')                       # a finished file with the same name: kept
    touch('動画 [1].mp4.part')
    touch('動画 [1].mp4.part-Frag3')
    touch('動画 [1].mp4.ytdl')
    touch('動画 [1].f137.mp4')                          # one format of a merged download
    touch('動画 [1].f140.m4a.part')
    other = touch('別の動画.mp4.part')                   # another download: not touched
    names = [os.path.join(tmp_path, '動画 [1].mp4'), os.path.join(tmp_path, '動画 [1].f137.mp4'),
             os.path.join(tmp_path, '動画 [1].f140.m4a')]
    found = [os.path.basename(p) for p in partial_files(names)]
    assert found == sorted(['動画 [1].mp4.part', '動画 [1].mp4.part-Frag3', '動画 [1].mp4.ytdl', '動画 [1].f137.mp4',
                            '動画 [1].f140.m4a.part'])
    removed = remove_partials(names)
    assert len(removed) == 5
    assert os.path.exists(final) and os.path.exists(other)
    assert sorted(os.listdir(tmp_path)) == ['別の動画.mp4.part', '動画 [1].mp4']
