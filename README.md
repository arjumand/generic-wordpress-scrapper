# Article Scraper GUI

A Streamlit front-end for the article scraper: paste or upload URLs, scrape
title/author/date/image/content, watch live progress, review results in a
table, download as CSV, and (optionally) push each row to NocoDB.

## Setup

```bash
pip install -r requirements.txt

# Only needed if you plan to use the Playwright browser-fallback option:
playwright install chromium
```

## Run

```bash
streamlit run app.py
```

This opens the app in your browser (usually `http://localhost:8501`).

## First-time configuration

Open the **Settings** panel in the sidebar and fill in:

- **NocoDB**: base URL, table ID, API key
- **Proxy** (optional): Webshare host/port/username/password
- **Scrape behavior**: whether to fall back to a real browser (Playwright)
  for pages that block plain HTTP requests, and the delay between requests

Click **Test connection** / **Test proxy** to sanity-check each before
running a full scrape, then **Save settings**.

Settings are saved to `config.json` next to `app.py`, in plaintext, on your
own machine — the same way the original script had them hardcoded, just
out of the source file and editable from the UI instead. **Don't commit or
share `config.json`** (a `.gitignore` is included that excludes it).

## Using it

1. Go to the **Run** tab.
2. Paste URLs (one per line) or upload a `.txt` file of URLs.
3. Leave "Upload each result to NocoDB" checked if you want rows pushed as
   they're scraped, or uncheck it to just build a table/CSV.
4. Click **Start scraping**. You'll see a progress bar, a live log, and the
   results table fill in as each URL finishes.
5. Switch to the **Results** tab any time to review everything scraped so
   far and download it as `articles.csv`.

## Files

- `app.py` — the Streamlit UI
- `scraper_core.py` — extraction/fetch/upload logic (same behavior as the
  original CLI script, just parameterized instead of hardcoded)
- `config.json` — created on first "Save settings" click, holds your
  credentials locally (gitignored)
- `requirements.txt` — Python dependencies
