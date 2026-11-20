# Search / SEO setup for the Capture site

The CMS is the source of truth. Webstudio only *binds* to JSON in R2, so after the one-time
bindings below, every SEO text is edited in the CMS (🌐 Website) and never in the builder.

## New JSON fields (R2)

`stories/<slug>.json` and `stories/index.json` now also carry:

| field | meaning | who writes it |
|---|---|---|
| `summary` | what is in the photo ("an elderly man and his grandson") | you / AI draft |
| `location` | "Pune, India" (already existed, was empty) | you |
| `captured` | year / date text ("2023") | you |
| `subtitle` | derived: "An elderly man and his grandson, Pune, India, 2023." | CMS |
| `seo_title` | derived `<title>`: "Generations: an elderly man and his grandson, Pune, India" | CMS |
| `description` | one **full** sentence ≤ 160 chars (cut-off `…` is now blocked) | you / AI draft |
| `images[].alt` | describes the photo; equal-to-title is now blocked | you / AI draft |

`site.json` (new, edited in 🌐 Website → "Site & search settings"): `site_name`, `home_title`,
`home_description`, `about_title`, `about_description`, `location`, `author`.

Old JSON keeps working: every binding below falls back when a field is missing.

## 1. Deploy the CMS change (cloud server)

```bash
cd <cms dir> && git pull && sudo systemctl restart <cms service>   # see DEPLOYMENT.md
```

## 2. Fix the 30 existing stories

```bash
python seo_backfill.py draft --location "<City, Country>" --captured <year>   # only if true for ALL stories
python seo_backfill.py draft                                                  # otherwise leave them blank
#   → seo_review.csv: Gemini proposes alt / summary / full-sentence description.
#     Open it in a spreadsheet, check every row, fill location + captured, save as CSV.
python seo_backfill.py apply --dry-run     # same validation as the editor; lists rows that would be skipped
python seo_backfill.py apply               # writes to R2 (backs up each story JSON in seo_backup/)
```

Then in 🌐 Website → Site & search settings, fill the home/About titles and descriptions.
Suggested home title: `Arinmay Datta — street, portrait & nature photography from <city>`.

## 3. Webstudio bindings (project `capture_cms_new`, once)

Stories already use the `story` resource (`https://media.adatta.in/stories/<slug>.json`).

**Story page (`/:slug`)**
1. Page Settings → *Title*: `$story.data.seo_title ?? $story.data.title`
2. Under the `Title` heading add a `Text` (paragraph) bound to `$story.data.subtitle`
   (hide it when empty: Show/hide → `$story.data.subtitle`).
3. *Description* is already `$story.data.description`; image `alt` is already bound — both now carry real text.

**Home page and About page**
1. Add a Resource variable `site` (GET `https://media.adatta.in/site.json`) on each page.
2. Home → *Title* `$site.data.home_title`, *Description* `$site.data.home_description`.
3. About → *Title* `$site.data.about_title`, *Description* `$site.data.about_description`.
4. About body: add one sentence with the base location
   ("I'm a street, portrait and nature photographer based in <city>.") and an `alt` for the portrait.

Publish the build. The sitemap and pages refresh within ~10 min of a CMS save.

## 4. Google Search Console

1. search.google.com/search-console → *Add property* → **Domain** → `adatta.in` (covers `capture.`) →
   add the TXT record it shows in Cloudflare DNS → Verify.
2. *Sitemaps* → submit **both** `https://capture.adatta.in/sitemap.xml` and
   `https://capture.adatta.in/sitemap-stories.xml`.
   (robots.txt only lists `sitemap.xml`, which has Home + About but **not** the stories — the stories sitemap
   must be submitted separately.)
3. *URL inspection* → paste `https://capture.adatta.in/` → *Request indexing*. Repeat for a few stories.
4. Check back in 1–2 weeks: *Pages* (indexed vs not) and *Performance* (queries).

## 5. Categories & the filter menu

A story's **first tag** is its category (`tags[0]` → the card's `data-category`). Rename one everywhere with
`python seo_backfill.py rename-tag life Anima` (add `--dry-run` first), or edit tags per story in the CMS.

The filter menu (Bottom nav → *Filter dropdown* → *Filter menu*) is a list of items you own in the builder, kept in sync with the cards by
`webstudio_snippets/bottom-nav-logic.html` (paste it into the *Bottom nav logic* HTML Embed once):
- **All** always shows; an item whose category no card has any more is hidden automatically;
- a category that has no item yet is appended at the end;
- **order and label** are whatever you set in the builder: drag items in the Navigator to reorder, edit the text to relabel.

To pin the order of the new categories: in the builder duplicate an item (right-click → Duplicate), set its text
(`Anima`) and its attribute `data-filter` = `anima` (lower-case, matches the tag), then drag it to the spot you want.
