# Accounts (sign in / guest mode)

beesells.xyz works for everyone as a guest. Signing in (email + password, or Google) adds **My dashboard**: your own
coins list with live prices, a note per coin, "what's happening on my coins today" (channel calls, H&S patterns,
long-term breakouts, channel status, Bee's Shortlist, today's signals), a start page and a display name.

## How it is built

| Piece | Where |
|---|---|
| Sign-in and users | Supabase Auth (PKCE flow), email + password and Google |
| Saved data | Supabase Postgres: `profiles`, `watchlist`, `saved_views` (`supabase/migrations/20261027000000_accounts.sql`) |
| Page | `web/index.html` loads `supabase-js` 2.117.3 from unpkg with an SRI hash, only when accounts are configured |
| Config | `web/auth.json`: project URL + **publishable (anon) key** only, injected as `<meta name="auth-config">` |

Security:
- Row Level Security on every table: a signed-in user reads and writes only their own rows; anonymous visitors get nothing.
- Limits in the database: 300 coins, 50 saved views, 500-character notes, 16 KB settings.
- The page never holds a secret. `publish_site.py` refuses a secret / service-role key in `web/auth.json`.
- CSP opens `connect-src` to exactly the project origin; nothing else changes.
- Users can export their data and delete their account (cascade) from My dashboard. Privacy notice and terms are
  published as `privacy.html` and `terms.html` (set `contact_email` in `site.json` to add a contact line).
- With `web/auth.json` empty, the site is exactly as before: no sign-in button, no welcome screen, no extra requests.

## One-time setup (Bilal)

1. **Supabase project**: supabase.com, New project (pick the region closest to your users). Keep the database password
   to yourself.
2. **Run the migration**: SQL Editor, paste `supabase/migrations/20261027000000_accounts.sql`, Run.
3. **Auth, URL configuration**: Site URL `https://beesells.xyz`; Redirect URLs `https://beesells.xyz/**`.
4. **Auth, Email provider**: on, "Confirm email" on, minimum password length 8.
   Optional but recommended later: custom SMTP (Supabase's built-in sender is rate-limited).
5. **Google**: Google Cloud Console, APIs & Services, Credentials, Create OAuth client ID (Web application).
   Authorized redirect URI: `https://<project-ref>.supabase.co/auth/v1/callback`. OAuth consent screen: app name
   "Bee's Dashboard", support email, authorized domain `beesells.xyz`. Copy the Client ID and Client Secret into
   Supabase, Authentication, Providers, Google, and enable it. The secret goes only into Supabase.
6. **Publish**: put the Project URL and the **publishable key** (Project Settings, API Keys; `sb_publishable_...`,
   or the legacy `anon` key) into `web/auth.json` and push. The next publish turns accounts on.

Never put the `service_role` / `sb_secret_...` key anywhere in this project.
