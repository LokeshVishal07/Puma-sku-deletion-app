# Marketplace SKU Deletion Finder

Finds SKUs listed on **Lazada / Shopee / Zalora / TikTok** (SG, MY, PH) that
qualify for delisting because they:

1. Had **zero stock** in Jan 2025, Jun 2025, Dec 2025, **and** today's
   inventory, **AND**
2. Are **not tracked** in the Content file **or** not tracked in the ZeCom
   master tracker.

Output: one row per Seller SKU with Platform Product ID, Article No, Product
Title, stock at each checkpoint, and a `Delete = YES / NO / REVIEW` verdict
with a plain-English reason — downloadable as an Excel file with a separate
sheet per marketplace (plus a Summary sheet of counts).

## Why it's built this way

Your export files are messy in a very consistent way: real headers sit a
few rows below title/promo banners, template files (Lazada, TikTok) sandwich
"Mandatory/Optional" instruction rows between the header and the actual
data, and column names shift slightly release to release. So instead of
hardcoding column positions, the app:

- **Auto-detects the header row** for every uploaded file (you can override
  it if it guesses wrong — there's a number input right above the preview).
- **Auto-skips instruction/blank rows** between the header and first real
  data row.
- **Auto-guesses which column plays which role** (EAN, Seller SKU, Article
  No, Stock, Status) and lets you confirm or fix the mapping in an
  expandable panel before anything is used.

This means it should keep working even as file names and exact column
headers drift over time — you just re-check the mapping panel if a guess
looks off.

## Running it locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open the local URL Streamlit prints (usually `http://localhost:8501`).

## Using the app

Go through the sidebar steps in order:

1. **Master files** — upload the Content file (EAN ↔ Article/Color No,
   shared across all 3 regions) and the ZeCom trackers (PH is its own
   workbook; MY and SG are separate sheets inside one workbook — upload the
   same file twice and pick the matching sheet each time).
2. **Marketplace files** — per region tab, upload Lazada / Shopee / Zalora
   (Shopee arrives as a `.zip` of many small `.xlsx` files — just upload the
   zip as-is, the app extracts and consolidates them). For MY, also upload
   the two TikTok exports (active listings + inactive listings) — each gets
   tagged with its status automatically.
3. **Stock files** — per region, upload the current inventory file and the
   Jan/Jun/Dec 2025 snapshots as you get them. Any checkpoint you haven't
   uploaded yet just shows up as "REVIEW — missing data" in the results
   instead of being silently assumed to be zero.
4. **Run & results** — pick a region, click **Run analysis**, review the
   table (filterable by decision and platform), and download the Excel
   report.

Column-mapping choices and uploaded data stay in memory only for the
current browser session — nothing is written to disk or persisted between
runs.

## ⚠️ Before you push this to GitHub

**Do not commit your actual Puma inventory/marketplace files to the repo.**
They contain commercially sensitive stock and pricing data. The included
`.gitignore` already blocks `.xlsx` / `.xls` / `.csv` / `.zip` from being
committed by accident, but double-check `git status` before your first
push if you've been testing locally in this folder.

If you plan to deploy this via **Streamlit Community Cloud** (free, and the
easiest option since there's no server to manage), the repo can be public
or private — either way, files are uploaded fresh by whoever opens the app
in their browser; nothing about your data lives in the repo itself.

## Pushing to your own GitHub

This folder is already a git repo with one commit. From your machine:

```bash
cd puma-sku-deletion-app
git remote add origin https://github.com/<your-username>/<your-repo-name>.git
git branch -M main
git push -u origin main
```

(Create the empty repo on github.com first — "New repository", no README/
.gitignore initialized there, so the push doesn't conflict.)

## Deploying so your team can use it without installing anything

1. Push the repo to GitHub (above).
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with
   GitHub, click **New app**, pick this repo/branch, and set the main file
   to `app.py`.
3. Deploy — you'll get a shareable URL.

## Project structure

```
app.py                  Streamlit UI — the 4 steps described above
modules/
  io_utils.py            Header detection, column-role guessing, file loading
  matching.py            The delete-decision engine (EAN/Article No joins, zero-stock logic)
requirements.txt
```

## Extending it later

- **New platform / new region**: add it to `PLATFORMS_BY_REGION` in
  `app.py`; the upload UI and matching logic are already generic.
- **A file's headers changed shape entirely**: the mapping panel lets you
  re-point any role to the new column name without touching code.
- **Want the ZeCom "found" check to also compare launch-status flags**
  (Lazada/Shopee/Zalora/TikTok Yes/No columns in the tracker) rather than
  just presence/absence: that data is already being read in, it's just not
  wired into the decision yet — extend `evaluate()` in `modules/matching.py`.
