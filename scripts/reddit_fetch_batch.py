#!/usr/bin/env python3
"""Batch worker for the GitHub Actions hourly fetch.
Copied from phase1b (same queries/filters/caps); reads the catalog CSV
from the repo, skips titles with existing reddit-raw/<slug>.json, and
stops after TIME_BUDGET seconds (default 2700) so each hourly run fits
inside the job window. Writes FETCH_DONE when the catalog is exhausted,
ABORT after 5 consecutive title errors.

Phase 1b original docstring follows.

Broadened 2026-09-26 per user: +3 queries (fucked-up/disturbing, grooming/predator,
censorship/backlash) and wider keyword gate. Scope stays on the sexual/taboo/
controversy axis; downstream triage judges relevance.

v2 fixes (2026-09-23 EDA verdict: v1 packs were ~99% noise for mainstream titles):
- Keep Reddit's relevance order per query; NO re-sort by raw score (score-sort
  amplified viral memes over niche controversy threads).
- Controversy-keyword gate on post title+selftext BEFORE fetching comments:
  kills PewDiePie/Hellboy-style junk at near-zero API cost.
- Subreddit denylist for meme/fanart subs.
- Per-post query attribution (queries_matched) for future EDA.
- If a title has no keyword-matching posts, record 0 posts: honest signal.

Same one-time-use credentials via env: REDDIT_CID/SECRET/USER/PASS.
"""
import json, os, time, base64, csv, re, sys, urllib.request, urllib.parse

CID = os.environ['REDDIT_CID']; SEC = os.environ['REDDIT_SECRET']
USER = os.environ['REDDIT_USER']; PW = os.environ['REDDIT_PASS']
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAWDIR = os.path.join(BASE, 'reddit-raw')
SWEEP = os.path.join(BASE, 'reddit-sweep')
os.makedirs(RAWDIR, exist_ok=True); os.makedirs(SWEEP, exist_ok=True)

QUERIES = [
    'controversy',
    'sexual content OR sexualization',
    'fanservice OR ecchi OR nudity',
    'rape OR sexual assault OR non-consent',
    'underage OR loli OR shota OR minor',
    'incest OR taboo',
    'problematic OR disgusting',
    'fucked up OR disturbing OR messed up',
    'grooming OR pedophilia OR predator',
    'censorship OR backlash OR boycott',
]
# posts kept per query (relevance order), cap per title
PER_QUERY_KEEP = 3
MAX_POSTS = 12
N_COMMENTS = 50

DENY_SUBS = {'animemes', 'animeme', 'animememes', 'goodanimemes', 'wholesomeanimemes',
             'pewdiepiesubmissions', 'shitposting', 'rule34', 'hentai', 'memes',
             'dankmemes', 'anime_irl', 'funny', 'pics', 'aww', 'topcharactertropes',
             'cartoons'}
KW = re.compile(
    r'controvers|sexual|fanservice|ecchi|nudity|\bnude\b|\bnaked\b|rape|assault|'
    r'harass|molest|problematic|disgust|loli|shota|underage|\bminor\b|incest|taboo|'
    r'sexist|misogyn|grope|\bperv|lewd|non-?con|coerc|groom|\bcreep|objectif|pedo|'
    r'censor|explicit|erotic|\bporn\b|panty|cleavage|\bboob|breast|thigh|\bstrip\b|'
    r'\bbath\b|\bshower\b|fucked|disturbing|predat|backlash|boycott|messed up|statutory', re.I)
STOP = {'the', 'a', 'an'}

def slugify(t):
    return re.sub(r'[^A-Za-z0-9]+', '_', t).strip('_')

def first_word(title):
    words = [w for w in re.sub(r'[^a-z0-9 ]', '', title.lower()).split()
             if len(w) >= 3 and w not in STOP]
    return words[0] if words else None

def api(path, params=None):
    global TOK, API_CALLS
    if time.time() > TOK_EXP - 60:
        refresh_token()
    url = 'https://oauth.reddit.com' + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url)
            req.add_header('Authorization', 'Bearer ' + TOK)
            req.add_header('User-Agent', UA)
            API_CALLS += 1
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) or 500 <= e.code < 600:
                time.sleep(2 ** attempt + 1)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(2 ** attempt + 1)
    raise RuntimeError(f'API failed after retries: {path}')

def refresh_token():
    global TOK, TOK_EXP, API_CALLS
    req = urllib.request.Request(
        'https://www.reddit.com/api/v1/access_token',
        data=urllib.parse.urlencode(
            {'grant_type': 'password', 'username': USER, 'password': PW}).encode())
    req.add_header('Authorization',
                   'Basic ' + base64.b64encode(f'{CID}:{SEC}'.encode()).decode())
    req.add_header('User-Agent', UA)
    API_CALLS += 1
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    TOK = d['access_token']
    TOK_EXP = time.time() + d.get('expires_in', 3600)

UA = f'reddit_phase1_v2 by {USER}'
TOK = ''; TOK_EXP = 0; API_CALLS = 0
refresh_token()
try:
    api('/api/v1/me')
except Exception as e:
    print(f'warmup warn (continuing): {e}', flush=True)
time.sleep(3)

def fetch_title(title):
    fw = first_word(title)
    pool = {}  # id -> post dict (relevance order kept)
    searched = 0
    for qi, kw in enumerate(QUERIES):
        q = f'"{title}" {kw}'
        d = api('/search', {'q': q, 'limit': 100, 'sort': 'relevance',
                            't': 'all', 'type': 'link', 'raw_json': 1})
        kids = (d.get('data') or {}).get('children', [])
        time.sleep(1.2)
        kept_this_q = 0
        for k in kids:
            p = k['data']
            if p.get('id') in pool or kept_this_q >= PER_QUERY_KEEP:
                continue
            searched += 1
            sub = (p.get('subreddit') or '').lower()
            if sub in DENY_SUBS:
                continue
            blob = ((p.get('title') or '') + ' ' + (p.get('selftext') or '')).lower()
            if fw and fw not in blob:
                continue
            if not KW.search(blob):
                continue
            pid = p['id']
            if pid in pool:
                pool[pid]['queries_matched'].append(kw)
                continue
            pool[pid] = {
                'id': pid,
                'title': p.get('title', ''),
                'selftext': (p.get('selftext') or '')[:2000],
                'subreddit': p.get('subreddit', ''),
                'score': p.get('score', 0),
                'num_comments': p.get('num_comments', 0),
                'over_18': bool(p.get('over_18')),
                'permalink': p.get('permalink', ''),
                'created_utc': p.get('created_utc', 0),
                'queries_matched': [kw],
                'comments': [],
            }
            kept_this_q += 1
    posts = list(pool.values())[:MAX_POSTS]
    for p in posts:
        try:
            d = api(f"/comments/{p['id']}",
                    {'sort': 'confidence', 'limit': N_COMMENTS,
                     'depth': 1, 'raw_json': 1})
            listing = d[1]['data']['children'] if isinstance(d, list) and len(d) > 1 else []
            for c in listing:
                cd = c.get('data', {})
                if cd.get('body') in (None, '[deleted]', '[removed]'):
                    continue
                p['comments'].append({
                    'id': cd.get('id', ''),
                    'author': cd.get('author', ''),
                    'body': (cd.get('body') or '')[:1500],
                    'score': cd.get('score', 0),
                    'created_utc': cd.get('created_utc', 0),
                })
                if len(p['comments']) >= N_COMMENTS:
                    break
        except Exception as e:
            print(f'  comment warn {p["id"]}: {e}', flush=True)
        time.sleep(0.8)
    return {'posts': posts, 'searched': searched, 'gate_pass': len(pool)}

def main():
    rows = list(csv.DictReader(open(os.path.join(
        BASE, 'imdb-anime-catalog-4365.csv'), encoding='utf-8-sig')))
    rows.sort(key=lambda r: int(r['canonical_rank']))
    have = set()
    for r in rows:
        title = r['primaryTitle'] or r['originalTitle']
        if os.path.exists(os.path.join(RAWDIR, slugify(title) + '.json')):
            have.add(title)
    todo = [r for r in rows if (r['primaryTitle'] or r['originalTitle']) not in have]
    print(f'skipping {len(have)} already-fetched, {len(todo)} to fetch', flush=True)
    t0 = time.time()
    BUDGET = float(os.environ.get('TIME_BUDGET', '2700'))
    stats = {}
    attempted = 0
    for i, r in enumerate(todo):
        if time.time() - t0 > BUDGET:
            print(f'BUDGET: {BUDGET:.0f}s exceeded after {attempted} titles, stopping', flush=True)
            break
        title = r['primaryTitle'] or r['originalTitle']
        fn = os.path.join(RAWDIR, slugify(title) + '.json')
        if os.path.exists(fn):
            stats[title] = {'skipped': True}
            continue
        attempted += 1
        try:
            res = fetch_title(title)
            out = {'title': title, 'tconst': r['tconst'],
                   'canonical_rank': int(r['canonical_rank']),
                   'method': 'phase1-v2',
                   'queries': QUERIES, 'posts': res['posts']}
            json.dump(out, open(fn, 'w'), ensure_ascii=False)
            stats[title] = {'posts': len(res['posts']),
                            'comments': sum(len(p['comments']) for p in res['posts']),
                            'searched': res['searched']}
            print(f'[{i+1}/{len(todo)}] {title}: {len(res["posts"])} posts, '
                  f'{stats[title]["comments"]} comments '
                  f'(searched {res["searched"]})', flush=True)
            consec = 0
        except Exception as e:
            stats[title] = {'error': str(e)[:120]}
            consec = locals().get('consec', 0) + 1
            print(f'  ERROR title {title!r}: {e}', flush=True)
            if consec >= 5:
                print('ABORT: 5 consecutive title errors', flush=True)
                open(os.path.join(BASE, 'ABORT'), 'w').write(
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ' 5 consecutive title errors')
                break
            time.sleep(5)
        if attempted % 5 == 0:
            json.dump({'attempted': attempted, 'todo': len(todo),
                       'api_calls': API_CALLS,
                       'elapsed_min': round((time.time() - t0) / 60, 1),
                       'stats': stats,
                       'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')},
                      open(os.path.join(SWEEP, 'phase1b-progress.json'), 'w'),
                      ensure_ascii=False)
    else:
        open(os.path.join(BASE, 'FETCH_DONE'), 'w').write(
            'completed ' + time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        print('FETCH_DONE: catalog exhausted', flush=True)
    kept = [s for s in stats.values() if 'posts' in s]
    print(f'DONE: {len(kept)} titles, '
          f'{sum(s["posts"] for s in kept)} posts, '
          f'{sum(s["comments"] for s in kept)} comments, '
          f'{API_CALLS} api calls, {round((time.time()-t0)/60,1)} min', flush=True)
    json.dump({'attempted': attempted, 'todo': len(todo), 'api_calls': API_CALLS,
               'elapsed_min': round((time.time() - t0) / 60, 1),
               'stats': stats,
               'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')},
              open(os.path.join(SWEEP, 'phase1b-progress.json'), 'w'), ensure_ascii=False)

if __name__ == '__main__':
    main()
