# SEO for the sorted landing site

What the site does for search engines, what changed on 21 September 2026, and the three things
only Kirtan can do. Site: https://kirtan-00.github.io/sorted/ (GitHub Pages, published by
`site-publish.sh`).

## What is in place

- **Titles and descriptions.** Landing: "sorted: search a whole shoot on your Mac, photos and
  video" (58 chars) with a 154 character description. Download and credits pages have their own.
- **Canonical, theme-color, Open Graph and Twitter cards** on all three pages, all pointing at
  `brand/og-image.png` (1200 by 630, the "Sorts out post." card). `og:site_name` sorted,
  `og:type` website.
- **Structured data** on the landing page, two JSON-LD blocks in the head: `SoftwareApplication`
  (name sorted, macOS, MultimediaApplication, price 0 "public beta", author Organization
  "sorted", download URL, feature list) and `FAQPage` mirroring the visible "Before you ask."
  section. Every FAQ answer is a sentence that already existed on the page, the download page or
  docs/sorted-concept.md. Both blocks parse (`python3 -c "import json"`).
- **Semantics.** One h1 per page, h2/h3 in order, `<main id="main">` with a skip link, `<nav
  aria-label>`, lang=en, `rel="noopener"` on the external links, the credits page linked as
  "Photo credits". The app replica's toolbar mark was an empty `<h2>`; it is a div now.
- **Keywords**, only where the copy already said it: "photo culling" and "video search" in the
  description and the FAQ, "find every photo of a person" as an FAQ question, "wedding photo
  sorting", "documentary footage search", "on-device", "Apple silicon" in the FAQ answers.
  British spelling stays on the page (organise, colour, recognise); Google treats
  organizer/organiser as the same query.
- **Performance.** `preconnect` for fonts.googleapis, fonts.gstatic and cdnjs (GSAP);
  `font-display: swap` was already there. The LCP element is the h1 at both widths.
- **Crawler files** in `site/`: `robots.txt`, `sitemap.xml` (three pages), `llms.txt` (a plain
  text summary for LLM crawlers, same facts as docs/sorted-concept.md), `humans.txt`.
- **main.js** no longer overwrites the `<title>` with plain "sorted" after load (it did, so a
  rendering crawler saw the old title), and the line reveal puts a space between lines so the DOM
  text of a split heading reads "to the client" rather than "tothe client". The smooth-scroll
  handler leaves the skip link alone so it moves focus. Wordmark spans carry `role="img"` with
  their aria-label, which is what Lighthouse wanted. Commits 053cdb9, d381999, f992cdf.

## What Kirtan must do himself

1. **Google Search Console.** https://search.google.com/search-console, add a property of type
   "URL prefix" with `https://kirtan-00.github.io/sorted/`. Pick the "HTML tag" method. Copy the
   `<meta name="google-site-verification" content="...">` line it shows. In `site/index.html`
   there is a marked comment in the head (search for `google-site-verification`); replace the
   commented placeholder with the real tag, uncomment it, publish with `site-publish.sh`, then
   press Verify. Then Sitemaps, add `sitemap.xml`. Submit it there directly: on a project-path
   site robots.txt is not at the origin root, so Google will not discover the sitemap from it.
2. **Bing Webmaster Tools.** https://www.bing.com/webmasters. Easiest: "Import from Google Search
   Console" once step 1 is done. Otherwise add the site and paste its `msvalidate.01` meta tag
   next to the Google one. Submit the sitemap there too.
3. **A custom domain would help.** `kirtan-00.github.io/sorted/` is fine to start, but: the
   site's robots.txt is at `/sorted/robots.txt`, which crawlers ignore (they read
   `kirtan-00.github.io/robots.txt`, which does not exist), so the `Disallow: /stats/` line and
   the Sitemap line are inert until the site has its own domain. The stats page is protected by
   its own `noindex` meta, which does work. A domain also gives a clean canonical, its own
   Search Console property and social previews without "github.io" in them. When the domain
   lands: change the canonical, og:url, sitemap and robots URLs (grep for `kirtan-00.github.io`
   in `site/`), add the CNAME file, and set up a redirect from the github.io URL if GitHub allows
   it for the project page.

## Decisions to know about

- The download page had `<meta name="robots" content="noindex">` from the installer task. The
  SEO brief gives it a description and a sitemap entry, which only make sense if it is indexed,
  so the noindex is gone. Put it back (one line in `site/download/index.html`) if the beta
  download should stay out of search.
- `Offer.priceCurrency` is required for the SoftwareApplication schema to validate. The site
  names no currency; it says USD with price 0. Change it when there is a price.
- The author is `Organization` "sorted", because the site never names a person.
- FAQPage markup no longer earns a rich result on Google (dropped for all but government and
  health sites in 2023). It is kept because it describes the page honestly for every crawler;
  do not expect dropdowns under the listing.

## Known limits, not changed

- Every Pexels photo is a CSS background image (`--u:url(...)` on inline styles), not an
  `<img>`. So `loading="lazy"`, `fetchpriority` and alt text do not apply to them; all 38
  images (about 1.1 MB) load on first visit at every width. Converting them to `<img>` would
  rewrite the animations. If that matters later, the honest route is smaller tiles (the grid
  shows them at about 200 px wide; several files are 60 KB) rather than lazy loading.
- The hero is revealed by GSAP after `document.fonts.ready`, so the LCP (the h1) waits for the
  CSS, the Archivo font, the two GSAP scripts from cdnjs and a 1.1 s reveal. Lighthouse mobile
  (simulated slow 4G) reports LCP about 5.9 s before and after this pass; the pass did not touch
  the animation. The Google Fonts stylesheet blocks first paint and must keep doing so: the line
  reveal measures line breaks and needs the real font in place first. The three Source Sans 3
  files are 109 KB each; a Latin subset would cut roughly two thirds of that.
- In motion mode every `[data-reveal]` block sits at `opacity: 0` until scrolled into view.
  Googlebot indexes such text but may weight it lower. If rankings matter more than the reveal
  one day, a fallback that reveals everything after a few seconds is the fix.
- Lighthouse still flags colour contrast on the story step h3s and one app button; that is the
  design, not touched.

## Numbers from this pass (local Playwright, Chromium, no throttling; Lighthouse 12 mobile)

| | before | after |
|---|---|---|
| title | "sorted" (and overwritten by JS) | "sorted: search a whole shoot on your Mac, photos and video" |
| description | 225 chars | 154 chars |
| canonical / OG / Twitter / JSON-LD | none | all three pages / 2 blocks on the landing page |
| h1 count, empty headings | 1, one empty h2 | 1, none |
| skip link, main id | no | yes |
| requests, landing | 50 | 52 (track.js and its ping, from the stats task) |
| page weight, landing | 1,719 KB | 1,733 KB (HTML 51 to 59 KB: meta, JSON-LD, FAQ) |
| LCP element | h1, 332 ms at 1440, 212 ms at 390 | h1, 316 ms at 1440, 228 ms at 390 |
| Lighthouse perf / a11y / best practices / SEO | 72 / 92 / 100 / 100 | 72 / 96 / 100 / 100 |
| Lighthouse FCP / LCP / CLS | 3.2 s / 5.9 s / 0 | 3.2 s / 5.9 s / 0 |
