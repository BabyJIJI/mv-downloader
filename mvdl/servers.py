"""Server / mirror buttons whose player only the site's own script can open.

Pages like `<a class="btn-server" data-link="3bba85…">EVS</a><a … data-link="fb51c8…">VOE</a>` keep the
player URL encrypted; clicking a button makes the site's script ask its server (or decrypt it) and put the
player <iframe> into the page. On 「ブラウザのページから探す」 the browser runs SCRIPT: it presses each
button in turn (and the play button when the player only comes after it) and records the player URLs that
show up. The results come back through window.__mvdlServers (ExecuteScriptAsync does not wait for a
Promise): RESULT is polled until `done`.

While this runs, app.py refuses new windows (ad pop-ups on click) and navigations of the tab.
"""
from .finder import SWITCH_ATTRS

PLAY_SELECTOR = ('.play-button,.play-btn,.btn-play,.playbtn,.vjs-big-play-button,.plyr__control--overlaid,'
                 '.jw-display-icon-display,[class*="play-button"],[id*="play-button"]')
MAX_BUTTONS = 12
WAIT_CLICK_MS = 2500   # for the player after the server button
WAIT_PLAY_MS = 4000    # for the player after the play button
TIMEOUT_S = MAX_BUTTONS * (WAIT_CLICK_MS + WAIT_PLAY_MS) / 1000 + 5

SCRIPT = r'''(() => {
  const SEL = %(sel)s, PLAY = %(play)s;
  const SKIP = new Set(['IFRAME', 'EMBED', 'VIDEO', 'SOURCE', 'IMG', 'SCRIPT', 'LINK', 'META']);
  const r = window.__mvdlServers = {done: false, items: []};
  const visible = e => e.getClientRects().length > 0;
  const srcs = () => {
    const s = new Set();
    document.querySelectorAll('iframe[src],embed[src],video[src],video source[src]').forEach(e => {
      try { s.add(new URL(e.getAttribute('src'), location.href).href); } catch (_) {}
    });
    return s;
  };
  const value = b => SEL.split(',').map(a => b.getAttribute(a.slice(1, -1))).find(v => v) || '';
  const buttons = [...document.querySelectorAll(SEL)].filter(b =>
    !SKIP.has(b.tagName) && visible(b) && value(b).length >= 8
    && !(b.tagName === 'A' && /^https?:/i.test(b.getAttribute('href') || ''))).slice(0, %(max)d);
  const sleep = ms => new Promise(f => setTimeout(f, ms));
  const wait = async (known, ms) => {
    for (let t = 0; t < ms; t += 250) {
      await sleep(250);
      const n = [...srcs()].filter(u => /^https?:/i.test(u) && !known.has(u));
      if (n.length) return n;
    }
    return [];
  };
  (async () => {
    const known = srcs();  // ads and the player already open: not this button's
    const active = buttons.find(b => /\b(active|on|selected|current)\b/.test(b.className));
    for (const b of buttons) {
      const label = (b.getAttribute('title') || b.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
      b.click();
      let urls = await wait(known, %(wait_click)d);
      if (!urls.length) {
        const play = [...document.querySelectorAll(PLAY)].find(visible);
        if (play) { play.click(); urls = await wait(known, %(wait_play)d); }
      }
      urls.forEach(u => known.add(u));
      r.items.push({label, urls});
    }
    if (active && buttons.length > 1) active.click();  // back to the server the page had chosen
  })().catch(e => { r.error = String(e); }).finally(() => { r.done = true; });
  return buttons.length;
})()''' % {'sel': repr(','.join(f'[{a}]' for a in SWITCH_ATTRS)), 'play': repr(PLAY_SELECTOR),
           'max': MAX_BUTTONS, 'wait_click': WAIT_CLICK_MS, 'wait_play': WAIT_PLAY_MS}

RESULT = 'JSON.stringify(window.__mvdlServers || null)'


def players(result):
    """[(label, player URL)] from RESULT's value."""
    out = []
    for item in (result or {}).get('items') or []:
        for u in item.get('urls') or []:
            out.append((item.get('label') or '', u))
    return out
