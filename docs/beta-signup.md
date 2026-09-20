# Beta sign-up

Date: 2026-09-19

Sign-up is one button on the landing page (`site/beta.js`): the tap is recorded, confetti, then an email. No sign-in.
48 hour access page is `site/beta/index.html`. The backend is Supabase project **sorted**
(`woasffpwdbwavwcrtllz`, Mumbai). No server of ours; the static site talks to Supabase with the
publishable key, which is public by design.

## Flow

1. Tap on "Join the beta": `beta_tap(source, user_agent)` inserts a row in `beta_taps`; confetti; the
   email field appears.
2. "Notify me": `beta_join(email, source, user_agent)` validates the shape server-side and upserts
   `beta_waitlist` (one row per email, `taps` counts repeats). Rate limit 60 per 10 minutes.
3. "You are in. We will notify you shortly with the build." That is all; no code, no page.

Both functions are security definer and the only things the publishable key can call; the tables
have RLS on and no policies, so nothing is readable from a browser. Read the list in the Supabase
dashboard (Table editor, `beta_waitlist`) or through the MCP.

The earlier email-code design (`beta_accounts`, `claim_access`) is still in the database but
unused; drop it when convenient.
