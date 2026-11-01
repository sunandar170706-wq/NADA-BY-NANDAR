import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
from flask import Flask, Response, jsonify, request
from ytmusicapi import YTMusic

app = Flask(__name__)

# ───────────────────────── YouTube Music (lazy + thread-safe) ─────────────────────────

_yt = None
_yt_lock = threading.Lock()


def get_yt():
    """YTMusic dibuat saat dibutuhkan, jadi server tetap bisa start walau internet sedang putus."""
    global _yt
    with _yt_lock:
        if _yt is None:
            _yt = YTMusic()
        return _yt


# ───────────────────────── Cache kecil dengan TTL ─────────────────────────

class TTLCache:
    def __init__(self, max_items=400):
        self._d = {}
        self._lock = threading.Lock()
        self._max = max_items

    def get(self, key):
        with self._lock:
            item = self._d.get(key)
            if not item:
                return None
            if item[0] < time.time():
                self._d.pop(key, None)
                return None
            return item[1]

    def set(self, key, value, ttl):
        with self._lock:
            if len(self._d) >= self._max:
                now = time.time()
                for k in [k for k, v in self._d.items() if v[0] < now]:
                    self._d.pop(k, None)
                if len(self._d) >= self._max:
                    self._d.pop(next(iter(self._d)), None)
            self._d[key] = (time.time() + ttl, value)


search_cache = TTLCache()
stream_cache = TTLCache()
lyrics_cache = TTLCache()

VIDEO_ID_RE = re.compile(r'^[\w-]{11}$')


# ───────────────────────── Helpers data lagu ─────────────────────────

def hi_res(url):
    if not url:
        return ''
    if 'googleusercontent.com' in url or 'ggpht.com' in url:
        return re.sub(r'=[wsh]\d+.*$', '=w544-h544-l90-rj', url)
    return url


def to_song(item):
    vid = item.get('videoId')
    if not vid:
        return None
    thumbs = item.get('thumbnails') or []
    artists = item.get('artists') or []
    names = [a.get('name') for a in artists[:2] if a.get('name')]
    return {
        'videoId': vid,
        'title': item.get('title') or 'Tanpa judul',
        'artist': ', '.join(names) or 'Unknown',
        'thumb': hi_res(thumbs[-1]['url']) if thumbs else '',
    }


def search_songs(query, limit=20):
    key = query.strip().lower()
    cached = search_cache.get(key)
    if cached is not None:
        return cached
    raw = get_yt().search(query, filter='songs', limit=limit)
    out, seen = [], set()
    for item in raw:
        song = to_song(item)
        if song and song['videoId'] not in seen:
            seen.add(song['videoId'])
            out.append(song)
    if out:
        search_cache.set(key, out, 600)
    return out


def resolve_audio(video_id):
    """Ambil URL audio langsung. Coba beberapa client YouTube sampai ada yang berhasil."""
    attempts = [['android', 'web'], ['ios'], ['tv'], None]
    for clients in attempts:
        opts = {
            # m4a (AAC) paling kompatibel dengan Safari / iOS
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'skip_download': True,
            'socket_timeout': 8,
            'retries': 1,
        }
        if clients:
            opts['extractor_args'] = {'youtube': {'player_client': clients}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
            url = info.get('url')
            if not url:
                for f in (info.get('requested_formats') or []):
                    if f.get('url'):
                        url = f['url']
                        break
            if url:
                return url
        except Exception:
            continue
    return None


# ───────────────────────── API ─────────────────────────

@app.route('/api/home')
def api_home():
    queries = ['Indonesia Viral Hits', 'Lagu Indonesia Terpopuler']
    results = []
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        futures = [pool.submit(search_songs, q) for q in queries]
        for f in futures:
            try:
                results.append(f.result(timeout=20))
            except Exception:
                results.append([])
    merged, seen = [], set()
    for i in range(max((len(r) for r in results), default=0)):
        for r in results:
            if i < len(r) and r[i]['videoId'] not in seen:
                seen.add(r[i]['videoId'])
                merged.append(r[i])
    if not merged:
        return jsonify({'error': 'unavailable'}), 502
    return jsonify(merged[:24])


@app.route('/api/search')
def api_search():
    q = (request.args.get('query') or '').strip()
    if not q:
        return jsonify([])
    try:
        return jsonify(search_songs(q[:100]))
    except Exception:
        return jsonify({'error': 'unavailable'}), 502


@app.route('/api/stream')
def api_stream():
    vid = request.args.get('videoId') or ''
    if not VIDEO_ID_RE.match(vid):
        return jsonify({'error': 'invalid'}), 400
    fresh = request.args.get('fresh') == '1'
    if not fresh:
        cached = stream_cache.get(vid)
        if cached:
            return jsonify({'url': cached})
    url = resolve_audio(vid)
    if not url:
        return jsonify({'error': 'unavailable'}), 502
    stream_cache.set(vid, url, 50 * 60)
    resp = jsonify({'url': url})
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _parse_lyrics(data):
    """Normalisasi hasil ytmusicapi (lirik bertimestamp / teks biasa) ke satu format."""
    raw = data.get('lyrics') if isinstance(data, dict) else None
    lines, synced = [], False
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, dict):
                text, start = x.get('text'), x.get('start_time')
            else:
                text, start = getattr(x, 'text', ''), getattr(x, 'start_time', None)
            if start is not None:
                synced = True
            lines.append({'text': (text or '').strip(), 'time': (start / 1000.0) if start is not None else None})
        if synced:
            lines = [l for l in lines if l['time'] is not None]
            lines.sort(key=lambda l: l['time'])
        else:
            lines = [{'text': l['text']} for l in lines]
    elif isinstance(raw, str):
        prev_blank = True
        for t in raw.splitlines():
            t = t.strip()
            if not t:
                if not prev_blank:
                    lines.append({'text': ''})
                prev_blank = True
            else:
                lines.append({'text': t})
                prev_blank = False
        while lines and not lines[-1]['text']:
            lines.pop()
    return {'synced': synced, 'lines': lines}


@app.route('/api/lyrics')
def api_lyrics():
    vid = request.args.get('videoId') or ''
    empty = {'synced': False, 'lines': []}
    if not VIDEO_ID_RE.match(vid):
        return jsonify(empty)
    cached = lyrics_cache.get(vid)
    if cached is not None:
        return jsonify(cached)
    result = empty
    try:
        yt = get_yt()
        browse_id = yt.get_watch_playlist(videoId=vid, limit=1).get('lyrics')
        if browse_id:
            data = None
            try:
                data = yt.get_lyrics(browse_id, timestamps=True)
            except Exception:
                data = None
            parsed = _parse_lyrics(data) if data else empty
            if not parsed['lines']:
                try:
                    parsed = _parse_lyrics(yt.get_lyrics(browse_id))
                except Exception:
                    parsed = empty
            result = parsed
        lyrics_cache.set(vid, result, 3600)
    except Exception:
        pass  # jangan di-cache supaya bisa dicoba lagi
    return jsonify(result)


@app.route('/favicon.ico')
def favicon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#fa2d48"/><g fill="#fff">'
           '<rect x="6.5" y="12" width="3.4" height="8" rx="1.7"/><rect x="11.9" y="7.5" width="3.4" height="17" rx="1.7"/>'
           '<rect x="17.3" y="10" width="3.4" height="12" rx="1.7"/><rect x="22.7" y="13.5" width="3.4" height="5" rx="1.7"/></g></svg>')
    return Response(svg, mimetype='image/svg+xml')


@app.route('/')
def home():
    resp = Response(PAGE, mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


# ───────────────────────── Tampilan (HTML + CSS + JS) ─────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="NADA">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<link rel="icon" href="/favicon.ico">
<title>NADA</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
:root{
  color-scheme:light dark;
  --bg:#fff;--bg2:#f2f2f7;--grp:#fff;
  --tx:#000;--tx2:rgba(60,60,67,.6);--tx3:rgba(60,60,67,.3);
  --sep:rgba(60,60,67,.16);--fill:rgba(118,118,128,.12);
  --mat:rgba(249,249,249,.8);--accent:#fa2d48;--red:#ff3b30;
  --tab:50px;--sab:env(safe-area-inset-bottom,0px);--sat:env(safe-area-inset-top,0px);
  --ease:cubic-bezier(.32,.72,0,1);--artline:rgba(0,0,0,.12);
}
@media(prefers-color-scheme:dark){:root{
  --bg:#000;--bg2:#1c1c1e;--grp:#2c2c2e;
  --tx:#fff;--tx2:rgba(235,235,245,.6);--tx3:rgba(235,235,245,.3);
  --sep:rgba(84,84,88,.55);--fill:rgba(118,118,128,.24);
  --mat:rgba(29,29,31,.8);--accent:#ff375f;--red:#ff453a;--artline:rgba(255,255,255,.1);
}}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;background:var(--bg2);overscroll-behavior:none}
body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display",Inter,"Helvetica Neue",system-ui,sans-serif;
  color:var(--tx);-webkit-font-smoothing:antialiased;-webkit-text-size-adjust:100%;
  -webkit-user-select:none;user-select:none;-webkit-touch-callout:none;overflow:hidden}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;-webkit-appearance:none;appearance:none}
input{font:inherit;color:inherit;-webkit-user-select:text;user-select:text}
img{display:block;-webkit-user-drag:none}
:focus{outline:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
.ic{display:block;flex:none}

#app{position:fixed;top:0;bottom:0;left:50%;width:100%;max-width:520px;transform:translateX(-50%);
  overflow:hidden;background:var(--bg);contain:layout paint}
@media(min-width:560px){#app{box-shadow:0 0 0 .5px var(--sep),0 0 70px rgba(0,0,0,.22)}}

/* ── Halaman & navbar ── */
.view{position:absolute;inset:0;overflow-y:auto;overflow-x:hidden;display:none;overscroll-behavior:contain;
  -webkit-overflow-scrolling:touch;padding-bottom:calc(var(--tab) + var(--sab) + 96px);scrollbar-width:none}
.view::-webkit-scrollbar{display:none}
.view.on{display:block}
.nav{position:sticky;top:0;z-index:6;height:calc(44px + var(--sat));padding-top:var(--sat);
  display:flex;align-items:center;justify-content:center;transition:background .2s,box-shadow .2s}
.view.scrolled .nav{background:var(--mat);-webkit-backdrop-filter:blur(24px) saturate(180%);backdrop-filter:blur(24px) saturate(180%);box-shadow:0 .5px 0 var(--sep)}
.nt{font-size:17px;font-weight:600;opacity:0;transition:opacity .2s;max-width:56%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.view.scrolled .nt{opacity:1}
.nl,.nr{position:absolute;bottom:0;height:44px;display:flex;align-items:center;gap:2px;color:var(--accent);font-size:17px}
.nl{left:6px;padding:0 8px 0 4px}
.nr{right:8px;padding:0 10px}
.nr.red{color:var(--red)}
.lt{font-size:34px;line-height:41px;font-weight:700;letter-spacing:.37px;padding:2px 20px 8px}
.sh{font-size:22px;font-weight:700;letter-spacing:.35px;padding:22px 20px 8px}

/* ── Artwork ── */
.art{position:relative;overflow:hidden;background:var(--fill);border-radius:6px;flex:none}
.art::after{content:"";position:absolute;inset:0;border-radius:inherit;box-shadow:inset 0 0 0 .5px var(--artline);pointer-events:none;z-index:3}
.art img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.art .ph{position:absolute;inset:0;display:grid;place-items:center;color:var(--tx3)}
.art .ph svg{width:36%;height:36%}
.art.fav{background:var(--accent)}
.art.fav .ph{color:#fff}
.eq{position:absolute;inset:0;display:none;align-items:center;justify-content:center;gap:2px;background:rgba(0,0,0,.42)}
.eq i{width:3px;height:14px;border-radius:2px;background:#fff;transform-origin:center;animation:eq .9s ease-in-out infinite}
.eq i:nth-child(2){animation-delay:-.35s}.eq i:nth-child(3){animation-delay:-.7s}
@keyframes eq{0%,100%{transform:scaleY(.3)}50%{transform:scaleY(1)}}
.cur.playing .eq{display:flex}

/* ── Baris lagu ── */
.row{position:relative;display:flex;align-items:center;gap:12px;padding:6px 6px 6px 20px;cursor:pointer;transition:background .12s}
.row:active{background:var(--fill)}
.row::after{content:"";position:absolute;left:84px;right:0;bottom:0;height:.5px;background:var(--sep)}
.list>.row:last-child::after{display:none}
.row .art{width:52px;height:52px}
.txt{flex:1;min-width:0}
.t{font-size:16px;line-height:21px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.s{font-size:14px;line-height:19px;color:var(--tx2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row.cur .t{color:var(--accent)}
.more,.chev{width:44px;height:44px;display:grid;place-items:center;color:var(--tx2);flex:none}
.chev{width:28px;color:var(--tx3)}

/* ── Carousel ── */
.hs{display:flex;gap:14px;overflow-x:auto;padding:0 20px 6px;scroll-snap-type:x mandatory;scroll-padding:0 20px;scrollbar-width:none}
.hs::-webkit-scrollbar{display:none}
.cc{flex:0 0 158px;scroll-snap-align:start;cursor:pointer}
.cc .art{width:158px;height:158px;border-radius:10px;box-shadow:0 4px 14px rgba(0,0,0,.14);transition:transform .35s var(--ease)}
.cc:active .art{transform:scale(.96)}
.cc .t{margin-top:8px;font-size:15px;line-height:20px}
.cc .s{font-size:15px}
.cc.cur .t{color:var(--accent)}

.brand{display:flex;align-items:center;gap:13px;padding:4px 20px 14px}
.brand svg{width:46px;height:46px;border-radius:11px;box-shadow:0 4px 14px rgba(250,45,72,.28)}
.brand small{display:block;font-size:14px;line-height:18px;color:var(--tx2)}
.brand b{display:block;font-size:34px;line-height:38px;font-weight:800;letter-spacing:.5px}
.hero{position:relative;margin:2px 20px 0;aspect-ratio:16/11;border-radius:18px;overflow:hidden;background:var(--fill);cursor:pointer;
  box-shadow:0 12px 32px rgba(0,0,0,.22),0 0 0 .5px var(--artline);transition:transform .4s var(--ease)}
.hero:active{transform:scale(.975)}
.hero img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.hero::after{content:"";position:absolute;inset:0;background:linear-gradient(transparent 42%,rgba(0,0,0,.74))}
.hero .hi{position:absolute;left:18px;right:78px;bottom:16px;z-index:2;color:#fff}
.hero .hi b{display:block;font-size:22px;line-height:27px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hero .hi span{display:block;font-size:15px;line-height:20px;color:rgba(255,255,255,.78);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hero .hp{position:absolute;right:16px;bottom:16px;z-index:2;width:46px;height:46px;border-radius:50%;color:#fff;display:grid;place-items:center;
  background:rgba(255,255,255,.24);-webkit-backdrop-filter:blur(16px);backdrop-filter:blur(16px)}
.hero .ip{display:none}.hero.playing .ip{display:block}.hero.playing .is{display:none}
.intro #homebody>*{animation:up .8s var(--ease) both}
.intro #homebody>*:nth-child(2){animation-delay:.08s}.intro #homebody>*:nth-child(3){animation-delay:.16s}
.intro #homebody>*:nth-child(4){animation-delay:.24s}.intro #homebody>*:nth-child(5){animation-delay:.32s}
.intro #homebody>*:nth-child(n+6){animation-delay:.4s}
@keyframes up{from{opacity:0;transform:translateY(16px)}}

/* ── Splash ── */
#splash{position:absolute;inset:0;z-index:600;background:#000;color:#fff;display:grid;place-items:center;
  transition:opacity .6s ease,transform .9s var(--ease),visibility 0s .9s}
#splash.out{opacity:0;visibility:hidden;transform:scale(1.07);pointer-events:none}
.sp-in{display:flex;flex-direction:column;align-items:center;gap:30px;transition:transform .6s var(--ease)}
.wave{display:flex;align-items:center;gap:6px;height:72px}
.wave i{display:block;width:5px;height:100%;border-radius:3px;background:var(--accent);opacity:var(--o);
  transform:scaleY(calc(var(--e) * .26));animation:wv var(--t) ease-in-out infinite;animation-delay:var(--d)}
@keyframes wv{0%,100%{transform:scaleY(calc(var(--e) * .26))}50%{transform:scaleY(var(--e))}}
.word{display:flex;font-size:44px;line-height:1;font-weight:800;letter-spacing:.3em;margin-right:-.3em}
.word span{opacity:0;transform:translateY(12px);animation:ltr .8s var(--ease) forwards;animation-delay:calc(.3s + var(--i) * .1s)}
@keyframes ltr{to{opacity:1;transform:none}}

/* ── Tombol Putar / Acak ── */
.pb{display:flex;gap:12px;padding:4px 20px 10px}
.pbtn{flex:1;height:46px;border-radius:10px;background:var(--fill);color:var(--accent);font-size:17px;font-weight:600;display:flex;align-items:center;justify-content:center;gap:8px;transition:opacity .12s}
.pbtn:active{opacity:.6}

/* ── Pencarian ── */
.sbar{position:sticky;top:calc(44px + var(--sat));z-index:5;background:var(--bg);padding:0 16px 10px;display:flex;align-items:center;gap:12px}
.sf{flex:1;height:36px;border-radius:10px;background:var(--fill);display:flex;align-items:center;gap:6px;padding:0 8px;color:var(--tx2)}
.sf input{flex:1;min-width:0;height:100%;background:none;border:0;outline:0;font-size:17px;color:var(--tx)}
.sf input::placeholder{color:var(--tx2)}
.sf input::-webkit-search-cancel-button,.sf input::-webkit-search-decoration{-webkit-appearance:none;display:none}
.clr{display:none;width:20px;height:20px;place-items:center;color:var(--tx2)}
.sf.has .clr{display:grid}
.cancel{display:none;color:var(--accent);font-size:17px}
.sbar.act .cancel{display:block}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:0 20px}
.tile{position:relative;overflow:hidden;height:92px;border-radius:10px;padding:12px;color:#fff;font-size:17px;font-weight:700;text-align:left;display:flex;align-items:flex-end;transition:opacity .12s}
.tile:active{opacity:.75}
.tile span{position:relative;z-index:1}
.tile .tn{position:absolute;right:-10px;bottom:-12px;width:56px;height:56px;transform:rotate(22deg);opacity:.24;color:#fff}
.tile .tn svg{width:100%;height:100%}

/* ── State kosong / error / skeleton ── */
.empty{padding:72px 36px;text-align:center}
.empty p{font-size:20px;font-weight:700;margin-bottom:6px}
.empty span{display:block;font-size:15px;color:var(--tx2);line-height:21px;margin-bottom:18px}
.empty .pbtn{display:inline-flex;flex:none;padding:0 24px;height:44px}
.sk{display:flex;align-items:center;gap:12px;padding:6px 20px}
.sk .art{width:52px;height:52px}
.sk .bars{flex:1}
.sk .art,.sk .bars i{background:linear-gradient(100deg,var(--fill) 30%,rgba(128,128,128,.06) 50%,var(--fill) 70%);background-size:220% 100%;animation:shim 1.4s linear infinite}
.sk .bars i{display:block;height:12px;border-radius:6px;margin:5px 0}
.sk .bars i:last-child{width:55%}
@keyframes shim{from{background-position:120% 0}to{background-position:-120% 0}}

/* ── Pustaka ── */
.prow .art{width:56px;height:56px}
.dh{display:flex;flex-direction:column;align-items:center;padding:6px 20px 12px;text-align:center}
.dh .art{width:216px;height:216px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.25)}
.dh h1{font-size:22px;line-height:28px;font-weight:700;margin-top:16px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dh p{font-size:15px;color:var(--tx2);margin-top:2px}

/* ── Mini player ── */
#mini{position:absolute;left:8px;right:8px;bottom:calc(var(--tab) + var(--sab) + 8px);height:60px;border-radius:14px;
  background:var(--mat);-webkit-backdrop-filter:blur(28px) saturate(180%);backdrop-filter:blur(28px) saturate(180%);
  box-shadow:0 0 0 .5px var(--sep),0 6px 24px rgba(0,0,0,.16);display:none;align-items:center;padding:0 4px 0 8px;z-index:20}
#mini.show{display:flex}
.mi{flex:1;min-width:0;display:flex;align-items:center;gap:12px;height:100%;cursor:pointer}
.mi .art{width:44px;height:44px}
.mi .txt .t{font-size:15px}
#mini .mp{position:absolute;left:14px;right:14px;bottom:0;height:2px;border-radius:1px;background:var(--fill);overflow:hidden}
#miniFill{display:block;height:100%;width:0;background:var(--accent)}
.mb{width:44px;height:44px;display:grid;place-items:center;position:relative}
.mb:active{opacity:.5}

/* ── Tab bar ── */
#tabs{position:absolute;left:0;right:0;bottom:0;height:calc(var(--tab) + var(--sab));padding-bottom:var(--sab);z-index:30;
  background:var(--mat);-webkit-backdrop-filter:blur(28px) saturate(180%);backdrop-filter:blur(28px) saturate(180%);
  box-shadow:0 -.5px 0 var(--sep);display:flex}
.tabb{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;font-size:10px;font-weight:500;letter-spacing:.1px;color:#8e8e93}
.tabb.on{color:var(--accent)}
.tabb.on svg{stroke-width:2.2}

/* ── Sedang Diputar ── */
#np{position:absolute;inset:0;z-index:100;color:#fff;background:#1c1c1e;overflow:hidden;
  transform:translateY(100%);visibility:hidden;transition:transform .5s var(--ease),visibility 0s .5s}
#np.on{transform:none;visibility:visible;transition:transform .5s var(--ease)}
#np{background:#2c2c2e}
.npbg{position:absolute;inset:-30%;background:#3a3a3c center/cover no-repeat;filter:blur(60px) saturate(1.5) brightness(.62);transform:translateZ(0);opacity:0;transition:opacity .9s ease}
.npbg.on{opacity:1}
#np .shade{position:absolute;inset:0;background:linear-gradient(rgba(0,0,0,.08),rgba(0,0,0,.32))}
.npi{position:relative;height:100%;display:flex;flex-direction:column;padding:calc(var(--sat) + 4px) 28px calc(var(--sab) + 18px)}
.npt{height:32px;display:grid;place-items:center;flex:none;cursor:pointer;touch-action:none}
.npt i{width:36px;height:5px;border-radius:3px;background:rgba(255,255,255,.42)}
.stage{flex:1;min-height:0;position:relative;display:flex;align-items:center;justify-content:center;touch-action:none}
.cover{width:clamp(150px,calc(100dvh - 400px),400px);max-width:100%;aspect-ratio:1;border-radius:12px;
  box-shadow:0 22px 54px rgba(0,0,0,.45);transform:scale(.82);transition:transform .55s var(--ease),opacity .25s}
.cover.art{border-radius:12px}
#np.playing .cover{transform:scale(1)}
.lyr{position:absolute;inset:0;overflow-y:auto;display:none;padding:34% 0;scrollbar-width:none;touch-action:pan-y;
  -webkit-mask-image:linear-gradient(transparent,#000 14%,#000 86%,transparent);mask-image:linear-gradient(transparent,#000 14%,#000 86%,transparent)}
.lyr::-webkit-scrollbar{display:none}
#np.lyr-on .lyr{display:block}
#np.lyr-on .cover{opacity:0;pointer-events:none}
.ll{font-size:clamp(22px,6.4vw,30px);line-height:1.22;font-weight:700;padding:9px 2px;color:rgba(255,255,255,.38);cursor:pointer;
  transform-origin:left center;transform:scale(.94);transition:color .35s,transform .35s var(--ease)}
.ll.on{color:#fff;transform:none}
.lyr.plain .ll{font-size:20px;line-height:1.35;color:rgba(255,255,255,.88);cursor:default;transform:none;padding:4px 2px}
.ll.sp{padding:10px 0}
.lmsg{text-align:center;color:rgba(255,255,255,.6);font-size:17px;font-weight:600;padding-top:20px}
.npm{display:flex;align-items:center;gap:12px;margin-top:14px;flex:none}
.npm .txt h2{font-size:21px;line-height:26px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.npm .txt p{font-size:18px;line-height:24px;color:rgba(255,255,255,.62);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cb{width:34px;height:34px;border-radius:50%;background:rgba(255,255,255,.2);display:grid;place-items:center;flex:none;transition:transform .15s}
.cb:active{transform:scale(.88)}
.cb.on{color:var(--accent);background:#fff}
.cb.pop{animation:pop .3s var(--ease)}
@keyframes pop{40%{transform:scale(1.25)}}
.scrub{height:30px;margin-top:12px;display:flex;align-items:center;touch-action:none;cursor:pointer;flex:none}
.track{flex:1;height:5px;border-radius:3px;background:rgba(255,255,255,.28);overflow:hidden;transition:height .18s var(--ease)}
.scrub.drag .track{height:11px}
#fill{height:100%;width:0;background:rgba(255,255,255,.92);border-radius:3px}
.times{display:flex;justify-content:space-between;font-size:12px;color:rgba(255,255,255,.58);font-variant-numeric:tabular-nums;margin-top:-3px;flex:none}
.tr{display:flex;align-items:center;justify-content:center;gap:clamp(24px,11vw,52px);margin-top:8px;flex:none}
.tb{width:64px;height:64px;display:grid;place-items:center;position:relative;transition:transform .12s,opacity .12s}
.tb:active{transform:scale(.86);opacity:.7}
.bot{display:flex;justify-content:space-around;margin-top:8px;flex:none}
.bb{width:48px;height:40px;border-radius:10px;display:grid;place-items:center;color:rgba(255,255,255,.6);transition:background .2s,color .2s}
.bb.on{background:rgba(255,255,255,.22);color:#fff}
.loading svg{display:none}
.loading::after{content:"";width:24px;height:24px;border-radius:50%;border:3px solid currentColor;border-right-color:transparent;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}

/* ── Sheet ── */
#sheetwrap{position:absolute;inset:0;z-index:200;background:rgba(0,0,0,.4);opacity:0;visibility:hidden;transition:opacity .3s,visibility 0s .4s}
#sheetwrap.on{opacity:1;visibility:visible;transition:opacity .3s}
.sheet{position:absolute;left:0;right:0;bottom:0;max-height:86%;display:flex;flex-direction:column;background:var(--bg2);
  border-radius:14px 14px 0 0;padding-bottom:var(--sab);transform:translateY(100%);transition:transform .45s var(--ease)}
#sheetwrap.on .sheet{transform:none}
.grab{width:36px;height:5px;border-radius:3px;background:var(--tx3);margin:6px auto 0;flex:none}
.shh{display:flex;align-items:center;justify-content:space-between;padding:10px 16px 12px;flex:none}
.shh b{font-size:17px;font-weight:600}
.shh span{width:64px}
.done{width:64px;text-align:right;color:var(--accent);font-size:17px;font-weight:600}
.sbody{overflow-y:auto;padding-bottom:16px;overscroll-behavior:contain}
.group{background:var(--grp);border-radius:12px;margin:0 16px 14px;overflow:hidden}
.srow{display:flex;align-items:center;gap:12px;padding:8px 14px;min-height:60px;position:relative;cursor:pointer}
.srow .art{width:44px;height:44px}
.srow+.srow::before{content:"";position:absolute;top:0;left:70px;right:0;height:.5px;background:var(--sep)}
.srow:active{background:var(--fill)}
.chk{color:var(--accent);opacity:0;transition:opacity .15s}
.chk.on{opacity:1}
.newrow{display:flex;align-items:center;gap:8px;padding:6px 8px 6px 16px;min-height:52px}
.newrow input{flex:1;min-width:0;height:40px;background:none;border:0;outline:0;font-size:17px}
.newrow input::placeholder{color:var(--tx2)}
.newrow button{height:34px;padding:0 14px;border-radius:17px;background:var(--accent);color:#fff;font-size:15px;font-weight:600}
.newrow button:active{opacity:.7}

/* ── Alert ── */
#alertwrap{position:absolute;inset:0;z-index:300;background:rgba(0,0,0,.4);display:grid;place-items:center;opacity:0;visibility:hidden;transition:opacity .2s,visibility 0s .2s}
#alertwrap.on{opacity:1;visibility:visible;transition:opacity .2s}
.alert{width:270px;border-radius:14px;background:var(--mat);-webkit-backdrop-filter:blur(30px) saturate(180%);backdrop-filter:blur(30px) saturate(180%);
  text-align:center;overflow:hidden;transform:scale(1.08);transition:transform .25s var(--ease)}
#alertwrap.on .alert{transform:none}
.alert .ab{padding:18px 16px}
.alert h3{font-size:17px;font-weight:600;line-height:22px}
.alert p{font-size:13px;line-height:18px;margin-top:2px}
.alert .bt{display:flex;border-top:.5px solid var(--sep)}
.alert .bt button{flex:1;height:44px;font-size:17px;color:var(--accent)}
.alert .bt button+button{border-left:.5px solid var(--sep);font-weight:600;color:var(--red)}
.alert .bt button:active{background:var(--fill)}

/* ── Toast ── */
#toast{position:absolute;left:50%;top:calc(var(--sat) + 10px);transform:translate(-50%,-14px);max-width:86%;z-index:400;
  padding:11px 18px;border-radius:22px;font-size:15px;font-weight:500;text-align:center;pointer-events:none;opacity:0;
  background:var(--mat);-webkit-backdrop-filter:blur(28px) saturate(180%);backdrop-filter:blur(28px) saturate(180%);
  box-shadow:0 0 0 .5px var(--sep),0 8px 28px rgba(0,0,0,.22);transition:opacity .25s,transform .35s var(--ease)}
#toast.on{opacity:1;transform:translate(-50%,0)}
.hide{display:none!important}

@media(prefers-reduced-motion:reduce){*{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
</style>
</head>
<body>
<div id="app">

  <section class="view on" id="v-home">
    <div class="nav"><span class="nt">NADA</span></div>
    <div id="homebody"></div>
  </section>

  <section class="view" id="v-search">
    <div class="nav"><span class="nt">Cari</span></div>
    <div class="lt">Cari</div>
    <div class="sbar" id="sbar">
      <label class="sf" id="sf">
        <span data-ic="search" data-s="17"></span>
        <input id="q" type="search" enterkeyhint="search" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="Lagu atau artis" aria-label="Cari lagu atau artis">
        <button class="clr" data-do="clear" aria-label="Hapus teks" type="button"><span data-ic="clear" data-s="18"></span></button>
      </label>
      <button class="cancel" data-do="cancel" type="button">Batal</button>
    </div>
    <div id="sbody"></div>
  </section>

  <section class="view" id="v-lib">
    <div class="nav" id="libnav"></div>
    <div id="libbody"></div>
  </section>

  <div id="mini">
    <div class="mi" data-do="opennp" role="button" tabindex="0" aria-label="Buka pemutar">
      <div class="art" id="miniArt"></div>
      <div class="txt"><div class="t" id="miniT"></div><div class="s" id="miniA"></div></div>
    </div>
    <button class="mb" id="miniPlay" data-do="toggle" aria-label="Putar atau jeda"></button>
    <button class="mb" data-do="next" aria-label="Lagu berikutnya"><span data-ic="next" data-s="26"></span></button>
    <div class="mp"><i id="miniFill"></i></div>
  </div>

  <nav id="tabs" aria-label="Navigasi utama">
    <button class="tabb on" data-do="tab" data-tab="home"><span data-ic="home" data-s="26"></span>Beranda</button>
    <button class="tabb" data-do="tab" data-tab="search"><span data-ic="search" data-s="26"></span>Cari</button>
    <button class="tabb" data-do="tab" data-tab="lib"><span data-ic="lib" data-s="26"></span>Pustaka</button>
  </nav>

  <div id="np" aria-label="Sedang diputar">
    <div class="npbg on" id="npBgA"></div><div class="npbg" id="npBgB"></div><div class="shade"></div>
    <div class="npi">
      <div class="npt" id="npt" data-do="closenp" role="button" aria-label="Tutup pemutar"><i></i></div>
      <div class="stage" id="stage">
        <div class="art cover" id="npArt"></div>
        <div class="lyr" id="lyr"></div>
      </div>
      <div class="npm">
        <div class="txt"><h2 id="npT"></h2><p id="npA"></p></div>
        <button class="cb" id="npHeart" data-do="heart" aria-label="Favorit"></button>
        <button class="cb" data-do="npmore" aria-label="Simpan ke playlist"><span data-ic="more" data-s="20"></span></button>
      </div>
      <div class="scrub" id="scrub"><div class="track"><div id="fill"></div></div></div>
      <div class="times"><span id="tCur">0:00</span><span id="tRem">-0:00</span></div>
      <div class="tr">
        <button class="tb" data-do="prev" aria-label="Sebelumnya"><span data-ic="prev" data-s="36"></span></button>
        <button class="tb" id="npPlay" data-do="toggle" aria-label="Putar atau jeda"></button>
        <button class="tb" data-do="next" aria-label="Berikutnya"><span data-ic="next" data-s="36"></span></button>
      </div>
      <div class="bot">
        <button class="bb" id="bShuf" data-do="shuffle" aria-label="Acak"></button>
        <button class="bb" id="bLyr" data-do="lyrics" aria-label="Lirik"></button>
        <button class="bb" id="bRep" data-do="repeat" aria-label="Ulangi"></button>
      </div>
    </div>
  </div>

  <div id="sheetwrap">
    <div class="sheet" role="dialog" aria-modal="true">
      <div class="grab"></div>
      <div class="shh"><span></span><b id="shT">Simpan ke Playlist</b><button class="done" data-do="sheetclose">Selesai</button></div>
      <div class="sbody">
        <div class="group" id="shSong"></div>
        <div class="group newrow">
          <input id="plname" maxlength="40" placeholder="Playlist baru" autocomplete="off" enterkeyhint="done" aria-label="Nama playlist baru">
          <button data-do="create" type="button">Buat</button>
        </div>
        <div class="group" id="shList"></div>
      </div>
    </div>
  </div>

  <div id="alertwrap">
    <div class="alert" role="alertdialog">
      <div class="ab"><h3 id="alT"></h3><p id="alM"></p></div>
      <div class="bt"><button data-do="alcancel">Batal</button><button data-do="alok" id="alOk">Hapus</button></div>
    </div>
  </div>

  <div id="toast" role="status" aria-live="polite"></div>

  <div id="splash" aria-hidden="true"><div class="sp-in"><div class="wave"><i style="--e:0.34;--o:0.5;--d:-.9s;--t:1.0s"></i><i style="--e:0.5;--o:0.62;--d:-.3s;--t:1.2s"></i><i style="--e:0.7;--o:0.75;--d:-.6s;--t:.95s"></i><i style="--e:0.88;--o:0.88;--d:-.1s;--t:1.15s"></i><i style="--e:1;--o:1;--d:-.75s;--t:1.05s"></i><i style="--e:0.88;--o:0.88;--d:-.45s;--t:1.2s"></i><i style="--e:0.7;--o:0.75;--d:-.2s;--t:.9s"></i><i style="--e:0.5;--o:0.62;--d:-.8s;--t:1.1s"></i><i style="--e:0.34;--o:0.5;--d:-.5s;--t:1.0s"></i></div><div class="word"><span style="--i:0">N</span><span style="--i:1">A</span><span style="--i:2">D</span><span style="--i:3">A</span></div></div></div>
</div>
<audio id="audio" preload="auto" playsinline></audio>

<script>
(()=>{
'use strict';
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>Array.from(r.querySelectorAll(s));
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/* ───────── Ikon ───────── */
const HEART='M12 20.6s-7.6-4.7-9.5-9.4C1.3 8.2 3.2 5 6.6 5c2 0 3.6 1.1 5.4 3.2C13.8 6.1 15.4 5 17.4 5c3.4 0 5.3 3.2 4.1 6.2-1.9 4.7-9.5 9.4-9.5 9.4z';
const F={
  play:'<path d="M7.2 4.9v14.2a.9.9 0 0 0 1.4.8l11.2-7.1a.9.9 0 0 0 0-1.5L8.6 4.1a.9.9 0 0 0-1.4.8z"/>',
  pause:'<rect x="5.5" y="4" width="4.6" height="16" rx="1.4"/><rect x="13.9" y="4" width="4.6" height="16" rx="1.4"/>',
  next:'<path d="M4.5 6.2v11.6a.8.8 0 0 0 1.3.6l7.3-5.8a.8.8 0 0 0 0-1.2L5.8 5.6a.8.8 0 0 0-1.3.6z"/><rect x="15" y="5" width="3.6" height="14" rx="1.2"/>',
  prev:'<path d="M19.5 6.2v11.6a.8.8 0 0 1-1.3.6l-7.3-5.8a.8.8 0 0 1 0-1.2l7.3-5.8a.8.8 0 0 1 1.3.6z"/><rect x="5.4" y="5" width="3.6" height="14" rx="1.2"/>',
  heartF:'<path d="'+HEART+'"/>',
  more:'<circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/>',
  note:'<path d="M9 17.5V6.4a1 1 0 0 1 .8-1l9-1.8a1 1 0 0 1 1.2 1v11.4"/><circle cx="6.6" cy="17.6" r="3"/><circle cx="17.6" cy="15.6" r="3"/>'.replace(/<path/,'<path fill="none" stroke="currentColor" stroke-width="1.6"').replace(/<circle/g,'<circle'),
  clear:'<path d="M12 2.5a9.5 9.5 0 1 0 0 19 9.5 9.5 0 0 0 0-19zm3.9 12.4-1 1L12 13l-2.9 2.9-1-1L11 12 8.1 9.1l1-1L12 11l2.9-2.9 1 1L13 12z"/>'
};
const P={
  home:'<path d="M3.5 11 12 4l8.5 7v8a1.5 1.5 0 0 1-1.5 1.5h-3.5V15h-7v5.5H5A1.5 1.5 0 0 1 3.5 19z"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m20 20-4.9-4.9"/>',
  lib:'<path d="M9 18V5.6l11-2V16"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
  heart:'<path d="'+HEART+'"/>',
  shuffle:'<path d="M3 7h3.2c1.5 0 2.9.8 3.7 2l3.9 6c.8 1.2 2.2 2 3.7 2H21"/><path d="m18 14.5 3 2.5-3 2.5"/><path d="M3 17h3.2c1.2 0 2.3-.5 3.1-1.3"/><path d="M13.7 8.3c.8-.8 1.9-1.3 3.1-1.3H21"/><path d="m18 4.5 3 2.5-3 2.5"/>',
  repeat:'<path d="M4 11V9.5A2.5 2.5 0 0 1 6.5 7H19"/><path d="m16 4 3 3-3 3"/><path d="M20 13v1.5a2.5 2.5 0 0 1-2.5 2.5H5"/><path d="m8 20-3-3 3-3"/>',
  repeat1:'<path d="M4 11V9.5A2.5 2.5 0 0 1 6.5 7H19"/><path d="m16 4 3 3-3 3"/><path d="M20 13v1.5a2.5 2.5 0 0 1-2.5 2.5H5"/><path d="m8 20-3-3 3-3"/><text x="12" y="14.7" font-size="7.5" font-weight="700" text-anchor="middle" fill="currentColor" stroke="none">1</text>',
  lyrics:'<path d="M4 6a2.5 2.5 0 0 1 2.5-2.5h11A2.5 2.5 0 0 1 20 6v8.5a2.5 2.5 0 0 1-2.5 2.5H11l-4.5 3.5V17A2.5 2.5 0 0 1 4 14.5z"/><path d="M8 8.3h8M8 12h5"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  check:'<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  chev:'<path d="m9 5 7 7-7 7"/>',
  chevl:'<path d="m15 5-7 7 7 7"/>',
  trash:'<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12.1A1.5 1.5 0 0 0 8.5 20.5h7a1.5 1.5 0 0 0 1.5-1.4L18 7M9 7V4.5h6V7"/>'
};
function ic(n,s){
  const sz=s?` width="${s}" height="${s}"`:'';
  if(F[n]) return `<svg class="ic" viewBox="0 0 24 24"${sz} fill="currentColor" aria-hidden="true">${F[n]}</svg>`;
  return `<svg class="ic" viewBox="0 0 24 24"${sz} fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[n]}</svg>`;
}

/* ───────── State ───────── */
const audio=$('#audio');
const SILENT='data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAIARKwAABCxAgAEABAAZGF0YQQAAAAAAA==';
const FAV='Favorit';
const S={tab:'home',cur:null,ctx:[],queue:[],pos:-1,shuffle:false,repeat:0,loading:false,ready:false,
         fails:0,errTried:false,npOpen:false,lyrics:false,libOpen:null,sheetSong:null};
const LISTS={};
let token=0,unlocked=false,dragging=false,dragPct=0;
let L={id:null,lines:[],synced:false,i:-1,els:[]},lyrToken=0,userScroll=false,userScrollT=0;

const isSilent=()=>audio.src.startsWith('data:');
const fmt=t=>{if(!isFinite(t)||t<0)t=0;const m=Math.floor(t/60),s=Math.floor(t%60);return m+':'+String(s).padStart(2,'0')};
const shuffleArr=a=>{for(let i=a.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[a[i],a[j]]=[a[j],a[i]]}return a};
const compact=s=>({videoId:s.videoId,title:s.title,artist:s.artist,thumb:s.thumb});

/* ───────── Penyimpanan playlist (kompatibel dengan data lama) ───────── */
function getPl(){
  let p=null;try{p=JSON.parse(localStorage.getItem('playlists')||'null')}catch(e){}
  if(!p||typeof p!=='object'||Array.isArray(p))p={};
  for(const k in p)if(!Array.isArray(p[k]))p[k]=[];
  if(!p[FAV])p[FAV]=[];
  return p;
}
function savePl(p){try{localStorage.setItem('playlists',JSON.stringify(p));return true}catch(e){toast('Penyimpanan penuh, perubahan tidak tersimpan');return false}}
const plNames=p=>[FAV,...Object.keys(p).filter(k=>k!==FAV)];
const inPl=(p,n,id)=>(p[n]||[]).some(x=>x.videoId===id);

/* ───────── Toast & Alert ───────── */
let toastT;
function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('on');clearTimeout(toastT);toastT=setTimeout(()=>t.classList.remove('on'),2300)}
let alertRes=null;
function confirmBox(title,msg,ok){
  return new Promise(res=>{
    $('#alT').textContent=title;$('#alM').textContent=msg;$('#alOk').textContent=ok;
    $('#alertwrap').classList.add('on');alertRes=res;
  });
}
function closeAlert(v){$('#alertwrap').classList.remove('on');if(alertRes){alertRes(v);alertRes=null}}

/* ───────── Template ───────── */
const eqHTML='<span class="eq"><i></i><i></i><i></i></span>';
function artHTML(thumb,cls,extra){
  return `<div class="art ${cls||''}"><span class="ph">${ic('note')}</span>${thumb?`<img src="${esc(thumb)}" alt="" loading="lazy" decoding="async" draggable="false">`:''}${extra||''}</div>`;
}
function rowHTML(key,s,i){
  return `<div class="row" role="button" tabindex="0" data-list="${key}" data-i="${i}" data-vid="${esc(s.videoId)}">${artHTML(s.thumb,'',eqHTML)}<div class="txt"><div class="t">${esc(s.title)}</div><div class="s">${esc(s.artist)}</div></div><button class="more" data-do="more" aria-label="Simpan ke playlist" tabindex="-1">${ic('more',20)}</button></div>`;
}
function cardHTML(key,s,i){
  return `<div class="cc" role="button" tabindex="0" data-list="${key}" data-i="${i}" data-vid="${esc(s.videoId)}">${artHTML(s.thumb,'',eqHTML)}<div class="t">${esc(s.title)}</div><div class="s">${esc(s.artist)}</div></div>`;
}
const actionsHTML=key=>`<div class="pb"><button class="pbtn" data-do="playall" data-list="${key}">${ic('play',18)}Putar</button><button class="pbtn" data-do="shuffleall" data-list="${key}">${ic('shuffle',20)}Acak</button></div>`;
const skel=n=>Array.from({length:n},()=>'<div class="sk"><div class="art"></div><div class="bars"><i></i><i></i></div></div>').join('');
const emptyHTML=(t,m,btn)=>`<div class="empty"><p>${esc(t)}</p><span>${esc(m)}</span>${btn||''}</div>`;

/* ───────── Beranda ───────── */
const LOGO='<svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" fill="#fa2d48"/><g fill="#fff"><rect x="6.5" y="12" width="3.4" height="8" rx="1.7"/><rect x="11.9" y="7.5" width="3.4" height="17" rx="1.7"/><rect x="17.3" y="10" width="3.4" height="12" rx="1.7"/><rect x="22.7" y="13.5" width="3.4" height="5" rx="1.7"/></g></svg>';
function greet(){const h=new Date().getHours();return h<4?'Selamat dini hari':h<11?'Selamat pagi':h<15?'Selamat siang':h<18?'Selamat sore':'Selamat malam'}
const brandHTML=()=>`<div class="brand">${LOGO}<div><small>${greet()}</small><b>NADA</b></div></div>`;
function heroHTML(s){
  return `<div class="hero" role="button" tabindex="0" data-list="home" data-i="0" data-vid="${esc(s.videoId)}">${s.thumb?`<img src="${esc(s.thumb)}" alt="">`:''}<div class="hi"><b>${esc(s.title)}</b><span>${esc(s.artist)}</span></div><span class="hp"><i class="is">${ic('play',22)}</i><i class="ip">${ic('pause',22)}</i></span></div>`;
}
let homeBusy=false;
async function loadHome(){
  if(homeBusy)return;homeBusy=true;
  const lt=brandHTML();
  $('#homebody').innerHTML=lt+skel(7);
  try{
    const r=await fetch('/api/home');if(!r.ok)throw 0;
    const d=await r.json();if(!Array.isArray(d)||!d.length)throw 0;
    LISTS.home=d;
    const feat=d.slice(1,9),from=d.length>9?9:1;
    $('#homebody').innerHTML=lt+heroHTML(d[0])
      +`<div class="sh">Pilihan Untukmu</div><div class="hs">${feat.map((s,i)=>cardHTML('home',s,i+1)).join('')}</div>`
      +`<div class="sh">Populer Sekarang</div>${actionsHTML('home')}<div class="list">${d.slice(from).map((s,j)=>rowHTML('home',s,j+from)).join('')}</div>`;
    updateRows();
  }catch(e){
    $('#homebody').innerHTML=lt+emptyHTML('Tidak bisa memuat lagu','Periksa koneksi internet, lalu coba lagi.',`<button class="pbtn" data-do="reloadhome">Coba Lagi</button>`);
  }finally{homeBusy=false}
}

/* ───────── Cari ───────── */
const GEN=[['Pop Indonesia','pop indonesia','#ff375f'],['Hip-Hop','hip hop indonesia','#ff9f0a'],['Indie','indie indonesia','#30b0c7'],
  ['Dangdut','dangdut','#5e5ce6'],['Rock','rock indonesia','#a2845e'],['Jazz','jazz','#0a84ff'],['Lo-fi','lofi','#34c759'],
  ['K-Pop','kpop','#bf5af2'],['Akustik','akustik indonesia','#ff6482'],['Galau','lagu galau','#64748b']];
const qi=$('#q');
let sCtl=null,sTimer=0,lastQ='';
function showBrowse(){
  $('#sbody').innerHTML=`<div class="sh">Telusuri</div><div class="tiles">${GEN.map(g=>`<button class="tile" style="background:${g[2]}" data-do="q" data-q="${esc(g[1])}"><span>${esc(g[0])}</span><i class="tn">${ic('note')}</i></button>`).join('')}</div>`;
}
function syncSearchUI(){
  const has=!!qi.value;
  $('#sf').classList.toggle('has',has);
  $('#sbar').classList.toggle('act',has||document.activeElement===qi);
}
async function runSearch(q){
  lastQ=q;clearTimeout(sTimer);
  if(sCtl)sCtl.abort();
  const ctl=sCtl=new AbortController();
  $('#sbody').innerHTML=skel(7);
  try{
    const r=await fetch('/api/search?query='+encodeURIComponent(q),{signal:ctl.signal});
    if(!r.ok)throw 0;
    const d=await r.json();
    if(ctl.signal.aborted)return;
    if(!Array.isArray(d)||!d.length){LISTS.search=[];$('#sbody').innerHTML=emptyHTML('Tidak ada hasil',`Tidak menemukan lagu untuk “${q}”. Coba kata kunci lain.`);return}
    LISTS.search=d;
    $('#sbody').innerHTML=actionsHTML('search')+`<div class="list">${d.map((s,i)=>rowHTML('search',s,i)).join('')}</div>`;
    updateRows();
  }catch(e){
    if(ctl.signal.aborted)return;
    $('#sbody').innerHTML=emptyHTML('Pencarian gagal','Periksa koneksi internet, lalu coba lagi.',`<button class="pbtn" data-do="retrysearch">Coba Lagi</button>`);
  }
}
function onSearchInput(){
  syncSearchUI();
  const q=qi.value.trim();
  clearTimeout(sTimer);
  if(!q){if(sCtl)sCtl.abort();lastQ='';showBrowse();return}
  sTimer=setTimeout(()=>runSearch(q),380);
}

/* ───────── Pustaka ───────── */
function renderLib(){
  const p=getPl(),nav=$('#libnav'),body=$('#libbody');
  if(S.libOpen&&!(S.libOpen in p))S.libOpen=null;
  if(!S.libOpen){
    nav.innerHTML=`<span class="nt">Pustaka</span><button class="nr" data-do="newpl" aria-label="Playlist baru">${ic('plus',24)}</button>`;
    body.innerHTML='<div class="lt">Pustaka</div><div class="list">'+plNames(p).map(n=>{
      const c=p[n].length;
      const cover=n===FAV?`<div class="art fav"><span class="ph">${ic('heartF')}</span></div>`:artHTML(c?p[n][0].thumb:'');
      return `<div class="row prow" role="button" tabindex="0" data-do="openpl" data-name="${esc(n)}">${cover}<div class="txt"><div class="t">${esc(n)}</div><div class="s">${c} lagu</div></div><span class="chev">${ic('chev',18)}</span></div>`;
    }).join('')+'</div>';
    return;
  }
  const name=S.libOpen,songs=p[name];LISTS.lib=songs;
  nav.innerHTML=`<button class="nl" data-do="libback">${ic('chevl',22)}<span>Pustaka</span></button><span class="nt">${esc(name)}</span>`
    +(name!==FAV?`<button class="nr red" data-do="delpl" aria-label="Hapus playlist">${ic('trash',22)}</button>`:'');
  const cover=name===FAV?`<div class="art fav"><span class="ph">${ic('heartF')}</span></div>`:artHTML(songs.length?songs[0].thumb:'');
  body.innerHTML=`<div class="dh">${cover}<h1>${esc(name)}</h1><p>${songs.length} lagu</p></div>`
    +(songs.length?actionsHTML('lib')+`<div class="list">${songs.map((s,i)=>rowHTML('lib',s,i)).join('')}</div>`
      :emptyHTML('Belum ada lagu','Ketuk tombol ••• di lagu mana pun, lalu simpan ke playlist ini.'));
  updateRows();
}

/* ───────── Sheet playlist ───────── */
function openSheet(song){
  if(!song)return;
  S.sheetSong=song;
  $('#shT').textContent='Simpan ke Playlist';
  $('#shSong').classList.remove('hide');$('#shList').classList.remove('hide');
  $('#shSong').innerHTML=`<div class="srow" style="cursor:default">${artHTML(song.thumb)}<div class="txt"><div class="t">${esc(song.title)}</div><div class="s">${esc(song.artist)}</div></div></div>`;
  renderSheetList();
  $('#plname').value='';
  $('#sheetwrap').classList.add('on');
}
function openNewPl(){
  S.sheetSong=null;
  $('#shT').textContent='Playlist Baru';
  $('#shSong').classList.add('hide');$('#shList').classList.add('hide');
  $('#plname').value='';
  $('#sheetwrap').classList.add('on');
  setTimeout(()=>$('#plname').focus(),380);
}
function closeSheet(){$('#sheetwrap').classList.remove('on');$('#plname').blur();S.sheetSong=null}
function renderSheetList(){
  const p=getPl(),s=S.sheetSong;if(!s)return;
  $('#shList').innerHTML=plNames(p).map(n=>{
    const cover=n===FAV?`<div class="art fav"><span class="ph">${ic('heartF')}</span></div>`:artHTML(p[n][0]?p[n][0].thumb:'');
    return `<div class="srow" role="button" tabindex="0" data-do="tgl" data-name="${esc(n)}">${cover}<div class="txt"><div class="t">${esc(n)}</div><div class="s">${p[n].length} lagu</div></div><span class="chk ${inPl(p,n,s.videoId)?'on':''}">${ic('check',22)}</span></div>`;
  }).join('');
}
function refreshSaved(){
  updateHeart();
  if(S.tab==='lib')renderLib();
}
function togglePl(name){
  const s=S.sheetSong;if(!s)return;
  const p=getPl();if(!p[name])p[name]=[];
  const i=p[name].findIndex(x=>x.videoId===s.videoId);
  if(i>-1){p[name].splice(i,1);toast(`Dihapus dari ${name}`)}else{p[name].push(compact(s));toast(`Ditambahkan ke ${name}`)}
  if(savePl(p)){renderSheetList();refreshSaved()}
}
function createPl(){
  const inp=$('#plname'),n=inp.value.trim().replace(/\s+/g,' ');
  if(!n){inp.focus();return}
  const p=getPl();
  if(Object.keys(p).some(k=>k.toLowerCase()===n.toLowerCase())){toast('Nama playlist sudah dipakai');return}
  p[n]=[];
  const s=S.sheetSong;
  if(s)p[n].push(compact(s));
  if(!savePl(p))return;
  inp.value='';inp.blur();
  if(s){renderSheetList();toast(`Ditambahkan ke ${n}`)}else{closeSheet();toast('Playlist dibuat')}
  refreshSaved();
}
async function deletePl(){
  const n=S.libOpen;if(!n||n===FAV)return;
  const ok=await confirmBox(`Hapus “${n}”?`,'Playlist ini akan dihapus dari perangkat ini.','Hapus');
  if(!ok)return;
  const p=getPl();delete p[n];
  if(savePl(p)){S.libOpen=null;renderLib();$('#v-lib').scrollTop=0;updateHeart();toast('Playlist dihapus')}
}
function toggleFav(){
  const s=S.cur;if(!s)return;
  const p=getPl(),i=p[FAV].findIndex(x=>x.videoId===s.videoId);
  if(i>-1){p[FAV].splice(i,1);toast('Dihapus dari Favorit')}else{p[FAV].push(compact(s));toast('Ditambahkan ke Favorit')}
  if(!savePl(p))return;
  const b=$('#npHeart');b.classList.remove('pop');void b.offsetWidth;b.classList.add('pop');
  if(navigator.vibrate)try{navigator.vibrate(10)}catch(e){}
  refreshSaved();
}
function updateHeart(){
  if(!S.cur)return;
  const f=inPl(getPl(),FAV,S.cur.videoId),b=$('#npHeart');
  b.innerHTML=ic(f?'heartF':'heart',18);b.classList.toggle('on',f);b.setAttribute('aria-pressed',f);
}

/* ───────── Tab ───────── */
function showTab(t){
  if(S.tab===t){
    const v=$('#v-'+t);
    if(t==='lib'&&S.libOpen){S.libOpen=null;renderLib();v.scrollTop=0}
    else if(t==='search'&&(!v.scrollTop||v.scrollTop<4))qi.focus();
    else v.scrollTo({top:0,behavior:'smooth'});
    return;
  }
  S.tab=t;
  $$('.view').forEach(v=>v.classList.toggle('on',v.id==='v-'+t));
  $$('.tabb').forEach(b=>{const on=b.dataset.tab===t;b.classList.toggle('on',on);on?b.setAttribute('aria-current','page'):b.removeAttribute('aria-current')});
  if(t==='lib')renderLib();
}

/* ───────── Antrian ───────── */
function buildQueue(first){
  const ctx=S.ctx,at=ctx.findIndex(x=>x.videoId===first.videoId);
  if(at<0){S.queue=[first];S.pos=0;return}
  if(S.shuffle){const rest=ctx.filter((_,i)=>i!==at);shuffleArr(rest);S.queue=[ctx[at],...rest];S.pos=0}
  else{S.queue=ctx.slice();S.pos=at}
}
function playList(list,i){
  const first=list[i];if(!first)return;
  S.ctx=list.slice();buildQueue(first);S.fails=0;playSong(first);
}
function playFromList(key,i){
  const l=LISTS[key],s=l&&l[i];if(!s)return;
  if(S.cur&&S.cur.videoId===s.videoId&&S.ready){
    if(audio.paused)audio.play().catch(()=>{});else openNP();
    return;
  }
  playList(l,i);
}
function step(dir,auto){
  const n=S.queue.length;if(!n)return;
  let p=S.pos+dir;
  if(dir>0){
    if(p>=n){
      if(S.repeat===1||!auto)p=0;
      else{audio.pause();try{audio.currentTime=0}catch(e){}syncPlayState();return}
    }
  }else{
    if(audio.currentTime>3){audio.currentTime=0;return}
    if(p<0)p=S.repeat===1?n-1:0;
  }
  S.pos=p;S.fails=auto?S.fails:0;playSong(S.queue[p]);
}
function setShuffle(on){S.shuffle=on;if(S.cur&&S.ctx.length)buildQueue(S.cur);updateModes()}
function updateModes(){
  const sh=$('#bShuf'),rp=$('#bRep'),ly=$('#bLyr');
  sh.innerHTML=ic('shuffle',22);sh.classList.toggle('on',S.shuffle);sh.setAttribute('aria-pressed',S.shuffle);
  rp.innerHTML=ic(S.repeat===2?'repeat1':'repeat',22);rp.classList.toggle('on',S.repeat>0);
  rp.setAttribute('aria-label',['Ulangi: mati','Ulangi semua','Ulangi satu lagu'][S.repeat]);
  ly.innerHTML=ic('lyrics',22);ly.classList.toggle('on',S.lyrics);ly.setAttribute('aria-pressed',S.lyrics);
  audio.loop=S.repeat===2;
}

/* ───────── Streaming ───────── */
const streamCache=new Map(),inflight=new Map();
function getStream(id,force){
  if(!force){
    const c=streamCache.get(id);
    if(c&&c.exp>Date.now())return Promise.resolve(c.url);
    if(inflight.has(id))return inflight.get(id);
  }
  const p=fetch('/api/stream?videoId='+encodeURIComponent(id)+(force?'&fresh=1':''))
    .then(r=>{if(!r.ok)throw new Error('stream');return r.json()})
    .then(d=>{if(!d.url)throw new Error('nourl');streamCache.set(id,{url:d.url,exp:Date.now()+45*60e3});return d.url})
    .finally(()=>{if(inflight.get(id)===p)inflight.delete(id)});
  inflight.set(id,p);return p;
}
function unlock(){
  if(unlocked)return;unlocked=true;
  try{audio.src=SILENT;const p=audio.play();if(p&&p.catch)p.catch(()=>{})}catch(e){}
}
function loadAndPlay(url,startAt){
  audio.src=url;
  if(startAt>0)audio.addEventListener('loadedmetadata',()=>{try{audio.currentTime=startAt}catch(e){}},{once:true});
  return audio.play();
}
async function startPlayback(song,my,opt){
  opt=opt||{};
  try{
    const url=await getStream(song.videoId,opt.force);
    if(my!==token)return;
    await loadAndPlay(url,opt.startAt||0);
  }catch(err){
    if(my!==token)return;
    const name=err&&err.name;
    if(name==='AbortError'){setLoading(false);syncPlayState();return}
    if(name==='NotAllowedError'){setLoading(false);syncPlayState();toast('Ketuk putar untuk memulai');return}
    if(!opt.force)return startPlayback(song,my,{force:true,startAt:opt.startAt});
    fail(song,my);
  }
}
function playSong(song){
  const my=++token;
  S.cur=song;S.ready=false;S.errTried=false;
  unlock();
  try{audio.pause()}catch(e){}
  setNowUI(song);setLoading(true);
  if(S.lyrics)loadLyrics(song);else L={id:null,lines:[],synced:false,i:-1,els:[]};
  startPlayback(song,my);
}
function fail(song,my){
  if(my!==token)return;
  S.ready=false;setLoading(false);syncPlayState();S.fails++;
  toast(`Tidak bisa memutar “${song.title}”`);
  if(S.fails<3&&S.queue.length>1)setTimeout(()=>{if(token===my)step(1,true)},1100);
}
function toggle(){
  if(!S.cur||S.loading)return;
  if(!audio.paused){audio.pause();return}
  if(S.ready&&!isSilent())audio.play().catch(()=>{});
  else playSong(S.cur);
}
function prefetch(){
  const n=S.queue.length;if(n<2||!S.cur)return;
  const nx=S.queue[(S.pos+1)%n];
  if(nx&&nx.videoId!==S.cur.videoId)getStream(nx.videoId).catch(()=>{});
}
audio.addEventListener('playing',()=>{if(isSilent())return;S.ready=true;S.fails=0;setLoading(false);syncPlayState();prefetch()});
audio.addEventListener('play',()=>{if(!isSilent())syncPlayState()});
audio.addEventListener('pause',()=>{if(!isSilent())syncPlayState()});
audio.addEventListener('waiting',()=>{if(!isSilent()&&!audio.paused&&S.ready)setLoading(true)});
audio.addEventListener('canplay',()=>{if(!isSilent())setLoading(false)});
audio.addEventListener('timeupdate',()=>{if(!isSilent())paint()});
audio.addEventListener('durationchange',()=>{if(!isSilent())paint()});
audio.addEventListener('seeked',()=>{if(!isSilent())paint()});
audio.addEventListener('ended',()=>{if(isSilent())return;step(1,true)});
audio.addEventListener('error',()=>{
  // Gagal di tengah lagu (mis. URL kedaluwarsa): ambil URL baru lalu lanjutkan dari posisi terakhir
  if(isSilent()||!S.cur||!S.ready)return;
  const my=token;
  if(!S.errTried){S.errTried=true;S.ready=false;setLoading(true);startPlayback(S.cur,my,{force:true,startAt:audio.currentTime||0})}
  else fail(S.cur,my);
});

/* ───────── UI pemutar ───────── */
function setLoading(b){
  S.loading=b;
  $('#miniPlay').classList.toggle('loading',b);$('#npPlay').classList.toggle('loading',b);
}
function syncPlayState(){
  const playing=!audio.paused&&!isSilent()&&!!S.cur;
  const html=ic(playing?'pause':'play',playing?28:28);
  $('#miniPlay').innerHTML=html;
  $('#npPlay').innerHTML=ic(playing?'pause':'play',46);
  $('#np').classList.toggle('playing',playing);
  S.playing=playing;
  updateRows();
  if('mediaSession' in navigator)try{navigator.mediaSession.playbackState=playing?'playing':'paused'}catch(e){}
}
function updateRows(){
  const id=S.cur&&S.cur.videoId;
  $$('[data-vid]').forEach(el=>{
    const c=el.dataset.vid===id;
    el.classList.toggle('cur',c);el.classList.toggle('playing',c&&S.playing);
  });
}
function setNowUI(s){
  $('#mini').classList.add('show');
  $('#miniArt').innerHTML=`<span class="ph">${ic('note')}</span>`+(s.thumb?`<img src="${esc(s.thumb)}" alt="">`:'');
  $('#miniT').textContent=s.title;$('#miniA').textContent=s.artist;
  $('#npArt').innerHTML=`<span class="ph">${ic('note')}</span>`+(s.thumb?`<img src="${esc(s.thumb)}" alt="">`:'');
  setBg(s.thumb);
  $('#npT').textContent=s.title;$('#npA').textContent=s.artist;
  document.title=`${s.title} — NADA`;
  updateHeart();setProg(0,0);updateRows();
  if('mediaSession' in navigator)try{
    navigator.mediaSession.metadata=new MediaMetadata({title:s.title,artist:s.artist,artwork:s.thumb?[{src:s.thumb,sizes:'544x544'}]:[]});
  }catch(e){}
}
let bgFlip=false;
function setBg(u){
  const a=$('#npBgA'),b=$('#npBgB'),nx=bgFlip?a:b,pv=bgFlip?b:a;bgFlip=!bgFlip;
  nx.style.backgroundImage=u?`url(${JSON.stringify(u)})`:'none';nx.classList.add('on');pv.classList.remove('on');
}
function setProg(t,d){
  const ok=isFinite(d)&&d>0;
  const pc=(ok?Math.min(100,t/d*100):0)+'%';
  $('#fill').style.width=pc;$('#miniFill').style.width=pc;
  $('#tCur').textContent=fmt(t);
  $('#tRem').textContent=ok?'-'+fmt(Math.max(0,d-t)):'-0:00';
}
let lastPos=0;
function paint(){
  const t=audio.currentTime,d=audio.duration;
  if(S.npOpen&&!dragging)setProg(t,d);
  else if(!S.npOpen)$('#miniFill').style.width=(isFinite(d)&&d>0?Math.min(100,t/d*100):0)+'%';
  paintLyric();
  if('mediaSession' in navigator&&isFinite(d)&&d>0&&Date.now()-lastPos>1000){
    lastPos=Date.now();
    try{navigator.mediaSession.setPositionState({duration:d,playbackRate:audio.playbackRate||1,position:Math.min(t,d)})}catch(e){}
  }
}
function openNP(){
  if(!S.cur||S.npOpen)return;
  S.npOpen=true;$('#np').classList.add('on');
  history.pushState({np:1},'');
  setProg(audio.currentTime,audio.duration);
}
function closeNP(fromPop){
  if(!S.npOpen)return;
  S.npOpen=false;$('#np').classList.remove('on');
  if(!fromPop&&history.state&&history.state.np)history.back();
}
window.addEventListener('popstate',()=>{if(S.npOpen)closeNP(true)});

/* Geser ke bawah untuk menutup */
let drag=null;
function dragStart(e){
  if(e.target.closest('.lyr'))return;
  if(e.currentTarget.id==='stage'&&S.lyrics)return;
  drag={y:e.clientY,dy:0,el:e.currentTarget};
  try{e.currentTarget.setPointerCapture(e.pointerId)}catch(x){}
  $('#np').style.transition='none';
}
function dragMove(e){
  if(!drag)return;
  drag.dy=Math.max(0,e.clientY-drag.y);
  $('#np').style.transform=`translateY(${drag.dy}px)`;
}
function dragEnd(){
  if(!drag)return;
  const dy=drag.dy;drag=null;
  const np=$('#np');np.style.transition='';np.style.transform='';
  if(dy>110)closeNP();
}
['#npt','#stage'].forEach(sel=>{
  const el=$(sel);
  el.addEventListener('pointerdown',dragStart);el.addEventListener('pointermove',dragMove);
  el.addEventListener('pointerup',dragEnd);el.addEventListener('pointercancel',dragEnd);
});

/* Bar progres */
const scrub=$('#scrub');
const pctOf=e=>{const r=scrub.getBoundingClientRect();return Math.min(1,Math.max(0,(e.clientX-r.left)/r.width))};
scrub.addEventListener('pointerdown',e=>{
  if(!isFinite(audio.duration)||audio.duration<=0)return;
  dragging=true;scrub.classList.add('drag');
  try{scrub.setPointerCapture(e.pointerId)}catch(x){}
  dragPct=pctOf(e);setProg(dragPct*audio.duration,audio.duration);
});
scrub.addEventListener('pointermove',e=>{if(!dragging)return;dragPct=pctOf(e);setProg(dragPct*audio.duration,audio.duration)});
const scrubEnd=e=>{
  if(!dragging)return;
  dragging=false;scrub.classList.remove('drag');
  if(e.type==='pointerup')try{audio.currentTime=dragPct*audio.duration}catch(x){}
  setProg(audio.currentTime,audio.duration);
};
scrub.addEventListener('pointerup',scrubEnd);scrub.addEventListener('pointercancel',scrubEnd);

/* ───────── Lirik ───────── */
async function loadLyrics(song){
  const my=++lyrToken;
  L={id:song.videoId,lines:[],synced:false,i:-1,els:[]};
  $('#lyr').className='lyr';$('#lyr').innerHTML='<div class="lmsg">Memuat lirik…</div>';
  try{
    const r=await fetch('/api/lyrics?videoId='+encodeURIComponent(song.videoId));
    const d=await r.json();
    if(my!==lyrToken)return;
    L.lines=Array.isArray(d.lines)?d.lines:[];L.synced=!!d.synced;
  }catch(e){if(my!==lyrToken)return;L.lines=[]}
  renderLyrics();
}
function renderLyrics(){
  const el=$('#lyr');
  if(!L.lines.length){el.className='lyr';el.innerHTML='<div class="lmsg">Lirik tidak tersedia</div>';return}
  el.className='lyr'+(L.synced?'':' plain');
  el.innerHTML=L.lines.map((l,i)=>l.text?`<div class="ll" data-k="${i}">${esc(l.text)}</div>`:(L.synced?`<div class="ll" data-k="${i}">♪</div>`:'<div class="ll sp"></div>')).join('');
  L.els=$$('.ll',el);L.i=-1;el.scrollTop=0;paintLyric(true);
}
function paintLyric(force){
  if(!S.lyrics||!L.synced||!L.els.length)return;
  const t=audio.currentTime+0.25;
  let lo=0,hi=L.lines.length-1,idx=-1;
  while(lo<=hi){const m=(lo+hi)>>1;if(L.lines[m].time<=t){idx=m;lo=m+1}else hi=m-1}
  if(idx===L.i&&!force)return;
  L.i=idx;
  L.els.forEach((e,k)=>e.classList.toggle('on',k===idx));
  const e=L.els[idx],box=$('#lyr');
  if(e&&!userScroll)box.scrollTo({top:e.offsetTop-box.clientHeight/2+e.offsetHeight/2,behavior:force?'auto':'smooth'});
}
function toggleLyrics(){
  S.lyrics=!S.lyrics;
  $('#np').classList.toggle('lyr-on',S.lyrics);
  updateModes();
  if(S.lyrics&&S.cur){if(L.id!==S.cur.videoId)loadLyrics(S.cur);else paintLyric(true)}
}
const lyrBox=$('#lyr');
lyrBox.addEventListener('pointerdown',e=>e.stopPropagation());
['touchstart','wheel'].forEach(ev=>lyrBox.addEventListener(ev,()=>{userScroll=true;clearTimeout(userScrollT);userScrollT=setTimeout(()=>userScroll=false,3000)},{passive:true}));
lyrBox.addEventListener('click',e=>{
  const k=e.target.closest('.ll');if(!k||!L.synced)return;
  const l=L.lines[+k.dataset.k];
  if(l&&isFinite(l.time)){try{audio.currentTime=l.time}catch(x){}userScroll=false}
});

/* ───────── Aksi ───────── */
const songOf=el=>{const it=el.closest('[data-list]');const l=it&&LISTS[it.dataset.list];return l&&l[+it.dataset.i]};
const ACT={
  tab:el=>showTab(el.dataset.tab),
  more:el=>openSheet(songOf(el)),
  playall:el=>{const l=LISTS[el.dataset.list];if(l&&l.length){S.shuffle=false;updateModes();playList(l,0)}},
  shuffleall:el=>{const l=LISTS[el.dataset.list];if(l&&l.length){S.shuffle=true;updateModes();playList(l,Math.floor(Math.random()*l.length))}},
  opennp:()=>openNP(),closenp:()=>closeNP(),
  toggle:()=>toggle(),next:()=>step(1,false),prev:()=>step(-1,false),
  shuffle:()=>setShuffle(!S.shuffle),
  repeat:()=>{S.repeat=(S.repeat+1)%3;updateModes()},
  lyrics:toggleLyrics,heart:toggleFav,npmore:()=>openSheet(S.cur),
  sheetclose:closeSheet,create:createPl,newpl:openNewPl,tgl:el=>togglePl(el.dataset.name),
  openpl:el=>{S.libOpen=el.dataset.name;renderLib();$('#v-lib').scrollTop=0},
  libback:()=>{S.libOpen=null;renderLib();$('#v-lib').scrollTop=0},
  delpl:deletePl,
  q:el=>{qi.value=el.dataset.q;syncSearchUI();runSearch(el.dataset.q)},
  clear:()=>{qi.value='';onSearchInput();qi.focus()},
  cancel:()=>{qi.value='';qi.blur();onSearchInput()},
  retrysearch:()=>{if(lastQ)runSearch(lastQ)},
  reloadhome:()=>loadHome(),
  alok:()=>closeAlert(true),alcancel:()=>closeAlert(false)
};
const app=$('#app');
app.addEventListener('click',e=>{
  if(e.target.id==='sheetwrap'){closeSheet();return}
  const d=e.target.closest('[data-do]');
  if(d){const f=ACT[d.dataset.do];if(f){e.preventDefault();f(d,e)}return}
  const it=e.target.closest('[data-list][data-i]');
  if(it)playFromList(it.dataset.list,+it.dataset.i);
});
app.addEventListener('keydown',e=>{
  if(e.target.matches&&e.target.matches('[role=button]')&&(e.key==='Enter'||e.key===' ')){e.preventDefault();e.target.click()}
});
qi.addEventListener('input',onSearchInput);
qi.addEventListener('focus',syncSearchUI);
qi.addEventListener('blur',syncSearchUI);
qi.addEventListener('keydown',e=>{
  if(e.key==='Enter'){const q=qi.value.trim();if(q)runSearch(q);qi.blur()}
});
$('#plname').addEventListener('keydown',e=>{if(e.key==='Enter')createPl()});
$$('.view').forEach(v=>v.addEventListener('scroll',()=>v.classList.toggle('scrolled',v.scrollTop>40),{passive:true}));

/* Gambar rusak → tampilkan placeholder */
document.addEventListener('error',e=>{if(e.target&&e.target.tagName==='IMG')e.target.style.display='none'},true);

/* Kontrol dari lock screen / headset */
if('mediaSession' in navigator){
  const h=(a,f)=>{try{navigator.mediaSession.setActionHandler(a,f)}catch(e){}};
  h('play',()=>{if(audio.paused)toggle()});
  h('pause',()=>audio.pause());
  h('previoustrack',()=>step(-1,false));
  h('nexttrack',()=>step(1,false));
  h('seekto',d=>{if(d&&isFinite(d.seekTime))try{audio.currentTime=d.seekTime}catch(e){}});
}

/* Pintasan keyboard (desktop) */
document.addEventListener('keydown',e=>{
  const tag=(e.target.tagName||'').toLowerCase();
  if(tag==='input'||tag==='textarea')return;
  if(e.code==='Space'&&!e.target.closest('button,[role=button]')){e.preventDefault();toggle()}
  else if(e.key==='ArrowRight'&&e.shiftKey)step(1,false);
  else if(e.key==='ArrowLeft'&&e.shiftKey)step(-1,false);
  else if(e.key==='Escape'){if($('#sheetwrap').classList.contains('on'))closeSheet();else closeNP()}
});

/* ───────── Mulai ───────── */
$$('[data-ic]').forEach(el=>{el.innerHTML=ic(el.dataset.ic,+el.dataset.s||22)});
$('#miniPlay').innerHTML=ic('play',28);$('#npPlay').innerHTML=ic('play',46);
$('#npHeart').innerHTML=ic('heart',18);
$('#npArt').innerHTML=`<span class="ph">${ic('note')}</span>`;
try{history.replaceState(null,'')}catch(e){}
updateModes();showBrowse();
/* Splash: tampil minimal ~2 detik, atau sampai beranda siap */
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
Promise.race([Promise.all([loadHome(),sleep(2000)]),sleep(8000)]).then(()=>{
  const sp=$('#splash');if(!sp)return;
  app.classList.add('intro');sp.classList.add('out');
  setTimeout(()=>{sp.remove();app.classList.remove('intro')},1700);
});
})();
</script>
</body>
</html>
"""


if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000)),
        debug=os.environ.get('NADA_DEBUG') == '1',
        threaded=True,
    )
