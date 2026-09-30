#!/usr/bin/env python3
"""Phase-1 retry: refetch weak/missing Reddit packs with fixed recall gates.

Reads retry_list.csv (rank,tconst,primaryTitle,originalTitle,startYear,kind).
Fixes vs batch v2 (reddit_fetch_batch.py):
  - first_word/blob gates are accent-normalized (NFKD strip) on BOTH sides.
    (v2 stripped non-ASCII from the title only, producing phantom words like
    'pokmon'/'sya'/'mmin' that never match real text -> 92% of non-ASCII
    packs came back empty.)
  - queries run against multiple title forms: primary, unaccented primary,
    originalTitle, unaccented originalTitle (deduped).
  - kind=missing (duplicate-title ranks): startYear is added to the query for
    disambiguation, falling back to the plain query per theme if the year
    query yields nothing.
Output: reddit-raw/r{rank}_{slug}.json for kind=missing (rank-prefixed, avoids
slug collisions); reddit-raw/{slug}.json otherwise (overwrites weak pack).
JSON carries canonical_rank + tconst so the importer keys by rank.
Writes reddit-sweep/retry.done when the list is exhausted. Resumable: rows
whose output file exists are skipped.
"""
import base64, csv, json, os, re, time, unicodedata, urllib.parse, urllib.request, urllib.error

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

def unaccent(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', s)
                   if not unicodedata.combining(c))

def slugify(t):
    return re.sub(r'[^A-Za-z0-9]+', '_', t).strip('_')

def first_word(title):
    words = [w for w in re.sub(r'[^a-z0-9 ]', '', unaccent(title).lower()).split()
             if len(w) >= 3 and w not in STOP]
    return words[0] if words else None

def title_forms(primary, original):
    forms = []
    for f in (primary, unaccent(primary), original, unaccent(original)):
        f = (f or '').strip()
        if f and f not in forms:
            forms.append(f)
    return forms

CID, SEC, USER, PW = (os.environ['REDDIT_CID'], os.environ['REDDIT_SECRET'],
                      os.environ['REDDIT_USER'], os.environ['REDDIT_PASS'])
UA = f'reddit_phase1_retry by {USER}'
TOK = ''; TOK_EXP = 0; API_CALLS = 0

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

refresh_token()
try:
    api('/api/v1/me')
except Exception as e:
    print(f'warmup warn (continuing): {e}', flush=True)
time.sleep(3)

def search(q):
    d = api('/search', {'q': q, 'limit': 100, 'sort': 'relevance',
                        't': 'all', 'type': 'link', 'raw_json': 1})
    time.sleep(1.2)
    return (d.get('data') or {}).get('children', [])

def consider(pool, kept_counter, p, kw, fw):
    if p.get('id') in pool or kept_counter[0] >= PER_QUERY_KEEP:
        return
    sub = (p.get('subreddit') or '').lower()
    if sub in DENY_SUBS:
        return
    blob = unaccent(((p.get('title') or '') + ' ' + (p.get('selftext') or '')).lower())
    if fw and fw not in blob:
        return
    if not KW.search(blob):
        return
    pid = p['id']
    if pid in pool:
        pool[pid]['queries_matched'].append(kw)
        return
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
    kept_counter[0] += 1

def fetch_row(row):
    primary, original = row['primaryTitle'], row['originalTitle']
    year, kind = row['startYear'], row['kind']
    forms = title_forms(primary, original)
    fw = first_word(primary)
    pool = {}
    for kw in QUERIES:
        qlist = []
        if kind == 'missing' and year and year != '\\N':
            qlist += [f'"{f}" {year} {kw}' for f in forms]
        qlist += [f'"{f}" {kw}' for f in forms]
        kept = [0]
        for q in qlist:
            n_before = len(pool)
            for k in search(q):
                consider(pool, kept, k['data'], kw, fw)
                if kept[0] >= PER_QUERY_KEEP:
                    break
            if kept[0] >= PER_QUERY_KEEP:
                break
            if kind == 'missing' and year and year != '\\N' and len(pool) == n_before:
                continue  # year query yielded nothing new; try next form/variant
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
    return posts

def out_path(row):
    slug = slugify(row['primaryTitle'])
    if row['kind'] == 'missing':
        return os.path.join(RAWDIR, f"r{row['rank']}_{slug}.json")
    return os.path.join(RAWDIR, slug + '.json')

def main():
    rows = list(csv.DictReader(open(os.path.join(BASE, 'retry_list.csv'), encoding='utf-8')))
    todo = [r for r in rows if not os.path.exists(out_path(r))]
    print(f'retry list {len(rows)}, {len(todo)} to fetch', flush=True)
    t0 = time.time()
    BUDGET = float(os.environ.get('TIME_BUDGET', '2700'))
    done = 0
    for r in todo:
        if time.time() - t0 > BUDGET:
            print(f'BUDGET exceeded after {done} titles, stopping', flush=True)
            break
        try:
            posts = fetch_row(r)
            out = {'title': r['primaryTitle'], 'tconst': r['tconst'],
                   'canonical_rank': int(r['rank']),
                   'method': 'phase1-retry', 'kind': r['kind'],
                   'queries': QUERIES,
                   'posts': posts}
            json.dump(out, open(out_path(r), 'w'), ensure_ascii=False)
            nc = sum(len(p['comments']) for p in posts)
            print(f"[r{r['rank']}] {r['primaryTitle'][:45]}: {len(posts)} posts, {nc} comments",
                  flush=True)
            done += 1
        except Exception as e:
            print(f"  ERROR r{r['rank']} {r['primaryTitle']!r}: {e}", flush=True)
    remaining = [r for r in rows if not os.path.exists(out_path(r))]
    prog = {'completed_ranks': sorted(int(r['rank']) for r in rows if os.path.exists(out_path(r))),
            'remaining': len(remaining), 'api_calls': API_CALLS}
    json.dump(prog, open(os.path.join(SWEEP, 'retry_progress.json'), 'w'))
    if not remaining:
        open(os.path.join(SWEEP, 'retry.done'), 'w').write(
            time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        print('RETRY DONE', flush=True)

if __name__ == '__main__':
    main()
