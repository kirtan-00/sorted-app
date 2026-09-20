# Site stats

Date: 2026-09-21

Two counters and one page to read them. Page views on the landing site, and installer runs. The
backend is the same Supabase project as the beta sign-up (`sorted`, `woasffpwdbwavwcrtllz`,
Mumbai); the static site talks to it with the publishable key, which is public by design.

## What is counted

**Page views** (`site/track.js`, loaded by `/` and `/download/`). One row per page load in
`site_hits`: path relative to the site root (`/`, `/download/`), the referrer, the browser's user
agent string, and, taken from the request on the server, the IP and the country. Nothing is stored
in the browser: no cookie, no localStorage, no id. A browser with Do Not Track on sends nothing.
The script is under 1 KB, fires once after load and never blocks the page. The server drops hits
above 600 a minute.

**Installs** (`Install sorted.command`, the last step, only after everything else succeeded). One
row per install in `installs`. The installer writes a random 32 hex id to `.install-id` in the
sorted folder the first time and sends `install_ping(id, kind, macOS version, chip name)`: kind
`install` when the id file is new, `update` when it already existed. A re-run bumps `runs` on the
same row. IP and country come from the request. The ping has a 5 second timeout and fails
silently; an offline Mac still gets a working install. The installer prints one line before it:
"Counting this install (one anonymous ping, the app itself never phones home)."

**Beta** (`site/beta.js`, documented in `docs/beta-signup.md`): taps and emails, counted here too.

## What is not counted

The app. Nothing in `photosort/` sends anything, ever, and there is no network code in it to turn
on. The usage log (`docs/usage-log.md`) stays on the Mac until the tester emails it. The
front-page promise (offline, nothing uploaded) holds; the installer ping is the only thing that
leaves a tester's Mac, and it carries no name, no path and no photo.

Also not counted: page views from browsers with Do Not Track on, the stats page itself, and
anything on the site that is not `/` or `/download/`.

## Reading it

`https://kirtan-00.github.io/sorted/stats/` (not linked from the site, `noindex`). The first
visit asks for the key; it is kept in that browser's localStorage under `sorted.stats.key` and a
wrong key clears it and shows the server's message. Then: visits today, 7 days, all time, unique
IPs, installs total and this week, updates, beta taps and emails; a People row and a funnel
(landed, download page, gave an email, installed, ran it again; each stage as a share of the
first); a Visitors table, one row per IP (first and last seen, hits, download page, installs by
IP, newest 200); a 30 day chart of visits and installs; top pages, referrers and countries; the
last 50 visits and installs; the email list with its source. Refresh button, and it refreshes
itself every 60 seconds while the tab is visible.

Reading the funnel: an IP is a person, roughly (one office or one phone hotspot is one IP).
Emails cannot be tied to an IP, so that stage counts every source and the small text says how
many came from the download page. "Installs we also saw on the site" is the join by IP between
`installs` and `site_hits`; a tester who read the page on a phone and installed on a Mac at home
is counted as an install but not as seen on the site.

**The key** lives in the Supabase dashboard: project `sorted`, Table editor, schema `private`,
table `config`, row `stats_key`. Change the value there to rotate it; every browser then asks
again.

## Backend

Three RPC functions, all `security definer`, reachable with the publishable key at
`/rest/v1/rpc/<name>`:

- `site_hit(p_path, p_ref, p_ua)`: insert only.
- `install_ping(p_install_id, p_kind, p_macos, p_chip)`: upsert on the id, which must be 32 hex.
- `site_stats(p_key)`: the JSON the page renders (`visits`, `installs`, `beta`, `funnel`, `people`,
  `daily`, `paths`, `refs`, `countries`, `recent_visits`, `recent_installs`, `emails`); anything
  but the right key is HTTP 400 "wrong key".

The tables (`site_hits`, `installs`) have RLS on and no policies, so nothing is readable from a
browser without the key. Delete test rows in the Table editor or through the MCP.
