# Beta sign-up

Date: 2026-09-19

Sign-up lives on the landing page (`site/beta.js`, rendered into `<section id="beta">`) and the
48 hour access page is `site/beta/index.html`. The backend is Supabase project **sorted**
(`woasffpwdbwavwcrtllz`, Mumbai). No server of ours; the static site talks to Supabase with the
publishable key, which is public by design.

## Flow

1. Name, email, studio, what they shoot, shoot size, Mac, note. Sent as user metadata with
   `auth.signInWithOtp` (six digit code emailed to the address). An address that cannot receive
   the code never becomes an account: that is the "only valid email ids" rule.
2. Code verified with `auth.verifyOtp` (`type: 'email'`). Supabase creates the auth user; the
   trigger `beta_on_auth_user` copies the form into `public.beta_accounts`.
3. The site calls `claim_access()`: sets `access_until = now() + 48 hours` once, returns the row.
   The access page shows the clock, the build note and how to reach us.
4. Returning users sign in from `beta/` with the same code flow (`shouldCreateUser: false`, so an
   unknown address is told to join on the front page).

RLS: the table is insert-by-trigger only, each user can read their own row, nobody else can read
anything with the publishable key. Kirtan reads the list in the Supabase dashboard (Table editor,
`beta_accounts`) or through the MCP.

## One-time dashboard setup (Kirtan, two minutes)

The built-in Supabase mailer sends 2 emails an hour. Not enough for a launch night.

1. **Auth → SMTP settings → Enable custom SMTP**
   host `smtp-relay.brevo.com`, port `587`, user = your Brevo login email, password = a Brevo
   SMTP key (Brevo → SMTP & API → SMTP keys → generate). Sender email `hello@send.clapper.in`
   (already authenticated in Brevo) until sorted has its own domain; sender name `sorted`.
2. **Auth → Email templates → Magic Link**: replace the body with the six digit code, because the
   site verifies a code, not a link. Subject `Your sorted code`. Body:

   ```html
   <p>Your sorted beta code:</p>
   <p style="font-size:28px;letter-spacing:6px;font-weight:600">{{ .Token }}</p>
   <p>It works for ten minutes. If you did not ask for it, ignore this email.</p>
   ```
3. **Auth → Rate limits**: emails per hour to something like 100 for launch night.

Nothing else. Site URL and redirect lists do not matter because no link is ever clicked.
