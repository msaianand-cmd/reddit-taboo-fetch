# reddit-taboo-fetch

Hourly GitHub Actions batch fetch of Reddit controversy/discussion packs for a
4,365-title anime catalog (sexual content, fanservice, nudity, coercion,
under-18 discourse, incest/taboo, disturbing content, grooming allegations,
censorship/backlash).

- `scripts/reddit_fetch_batch.py` — batch worker (stdlib only). Reads
  `imdb-anime-catalog-4365.csv`, skips titles with an existing
  `reddit-raw/<slug>.json`, runs 10 controversy/taboo queries per title via the
  Reddit API (password grant), keeps keyword-matching posts + up to 50 comments
  each. Stops after `TIME_BUDGET` seconds (default 2700).
- `.github/workflows/fetch.yml` — hourly schedule + manual dispatch. Commits
  new packs back to the repo each run.
- `reddit-raw/` — one JSON pack per title: `{title, tconst, canonical_rank,
  method, queries, posts[]}` with per-post comments.
- `FETCH_DONE` appears when the catalog is exhausted; `ABORT` on repeated errors.

Reddit API credentials live in repo Secrets
(`REDDIT_CID/REDDIT_SECRET/REDDIT_USER/REDDIT_PASS`) and never in the repo.
