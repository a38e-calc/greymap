# Sitemap watch (with page text)

Checks a list of sitemaps once a day and records which page URLs are new (and which
have gone). It then visits each new URL and saves the cleaned text of the page.
Runs on GitHub Actions; results are browsed and downloaded from a GitHub Pages site.

## Setting it up

1. Create a new GitHub repository and upload everything in this folder, keeping the
   folder structure (including the hidden `.github` folder).
2. In the repository, go to Settings, then Pages. Set the source to "Deploy from a
   branch", choose the `main` branch and the `/docs` folder, and save.
3. Go to Settings, then Actions, then General. Under "Workflow permissions" choose
   "Read and write permissions" and save.
4. Go to the Actions tab, open "Daily sitemap check" and press "Run workflow". This
   first run saves the starting point for each sitemap. No page text is fetched
   yet, because nothing counts as new until the second run.
5. After that it runs by itself every evening at about 18:37 UTC.

## Adding sitemaps

Edit `sitemaps.txt` on GitHub and add the address on a new line. The first run for a
new line saves its starting point; new pages are reported from the run after that.
Sitemaps that point to other sitemaps (an index) are followed automatically.

## What is stored

- `docs/data/new_urls.csv` is the single running file of differences. Each day's new
  and removed URLs are added to the end. Columns: date, site, change (added or
  removed), url.
- `docs/data/page_text/YYYY-MM.csv` holds the cleaned text, one file per month so that
  no single file grows too large. Columns: date_found, fetched_at, site, url, status,
  title, word_count, text. Join it to `new_urls.csv` on the url column.
- `snapshots/` holds one plain list of URLs per sitemap (the copy from the last run).
- `state/pending_urls.tsv` is the queue of new URLs still waiting to be visited.
- `docs/data/status.json` feeds the web page.

The text keeps some structure: headings start with `#` and list items with `-`.
Menus, footers, sidebars, scripts, cookie banners and similar are removed, and if the
page has a `<main>` or `<article>` section, only the text inside it is kept.

## Using the text in KNIME or elsewhere

Every file on the Pages site has a plain web address, for example
`https://YOUR-NAME.github.io/YOUR-REPO/data/new_urls.csv`, so a CSV Reader node can
read it straight from the URL. The text files use standard CSV quoting, with line
breaks inside the text field. Excel cells hold at most about 32,000 characters, so a
very long page will be cut off if you open the file in Excel; KNIME and most other
tools do not have that limit.

## The status column in the text files

- `ok`: text was saved.
- `ok_thin`: very little text came back, so the page probably builds its content with
  JavaScript. This tool does not run JavaScript.
- `ok_truncated`: the page was extremely long and the text was cut at one million
  characters.
- `blocked_by_robots`: the site's robots.txt asks automated visitors to stay away from
  that page, so it was not fetched.
- `http_403` (or another number): the site refused the request. It is recorded and left
  alone, and no attempt is made to get round it.
- `not_html`: the address is a PDF, image or other file type.
- `error (...)`: the download failed for another reason, with a short explanation.
- `robots_unreadable`: the site's robots.txt could not be read on five separate runs.

## How it behaves

- At most 300 pages are visited per run, one second apart, and the page step stops
  starting new pages after 30 minutes. Anything left over waits in the queue for the
  next run.
- If one sitemap reports more than 500 new URLs in a single run, that looks like a site
  rebuild. Its URLs are still recorded in `new_urls.csv`, but their pages are not
  fetched, and a notice appears on the web page. The limits are set at the top of
  `monitor.py` and `pagetext.py`.
- If a sitemap cannot be read, its saved copy is left alone, the problem shows as a
  warning in the run log and in a banner on the web page, and the other sitemaps carry
  on. The run only fails if every sitemap failed.
- If a run starts late, its results are still dated by the evening it was scheduled for.
- Repository size: page text adds up over months. Check the repository size now and
  then, and if it grows large, older monthly text files can be moved elsewhere.
