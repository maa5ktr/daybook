# Daybook

Daybook is a mobile-friendly spending and income tracker. The original local version remains available at the project root (`run.bat` and `server.py`). The GitHub Pages version is in `docs/` and stores account data in a Supabase Postgres database.

## GitHub Pages and Supabase

The GitHub Pages site is publicly reachable, so do not treat its URL as a password. Supabase Auth signs users in, Postgres Row Level Security confines every transaction to its account, and signup requires a valid one-use invitation. The Supabase publishable/anon key is designed to be public; never publish a Supabase secret or service-role key.

### 1. Create the Supabase project

Create a project at <https://supabase.com/dashboard>. In **SQL Editor**, run all of [`supabase/schema.sql`](supabase/schema.sql). It creates the tables, invite functions, signup trigger, and per-account RLS policies.

### 2. Create the owner before publishing

Before enabling GitHub Pages, open **Authentication → Users → Add user** in Supabase and create your owner with an email and password. The database trigger makes the first account the owner. Creating it before the site is public prevents a stranger from claiming the first-account slot. Keep your Supabase project's owner login separate from your Daybook account.

The simplest signup flow is **Authentication → Providers → Email → Confirm email: off**. Invite-only database checks still gate every additional account, but email addresses are not verified and password recovery by email will not work. If you keep email confirmations on, configure a custom SMTP provider and set the deployed site as an allowed redirect URL.

### 3. Publish the static app

Create a GitHub repository. Upload this project, including the hidden `.github/workflows/deploy-pages.yml` folder. In the repository, open **Settings → Secrets and variables → Actions → Variables** and add:

- `SUPABASE_URL`: your project URL from Supabase **Project Settings → API**.
- `SUPABASE_ANON_KEY`: the publishable key (or legacy anon key) from that same API page. This key is public by design; RLS is the protection.

Under **Settings → Pages**, choose **GitHub Actions** as the build and deployment source. Commit the project to the `main` branch. The included workflow deploys `docs/` and generates `supabase-config.js` during the build; that generated file and the local database are ignored by Git.

The site address will look like `https://YOUR-GITHUB-NAME.github.io/YOUR-REPOSITORY/`. If email confirmation is enabled, add that site URL (and its signup redirect path) to Supabase **Authentication → URL Configuration → Redirect URLs**.

### 4. Invite friends

Sign in as the owner on the Pages site and choose **Invite**. Send the one-use code privately; it expires in seven days. A friend opens the site, chooses **Create account with an invite**, and registers with their own email and password. Their income and expense records are private to their account.

### 5. Import existing PC data

Keep a backup copy of `daybook.sqlite3` before migrating. The importer reads the SQLite file in read-only mode, signs into Supabase as the owner, and upserts transactions using their original IDs, so rerunning it will not duplicate records. It does not delete or modify the local database.

From the project folder, run:

```powershell
C:\Users\ASUS\.local\bin\uv.exe run --python 3.12 --no-project python scripts\migrate_sqlite_to_supabase.py
```

The script prompts for the Supabase project URL, publishable/anon key, owner email, and password. Password entry is hidden and no credentials are written to files. Check the dashboard totals after importing before relying on the cloud copy.

## Backups

Use **Export** in each account to download a personal CSV of its transactions regularly. Supabase database backup availability and retention depend on your Supabase plan; a Pages link alone is not a data backup. Keep the original `daybook.sqlite3` as an additional local backup until you have confirmed the cloud import and your CSV exports.

## Local-only version

For the original PC-only SQLite app, double-click `run.bat`; it opens <http://127.0.0.1:8765>. The server remains bound to your PC and does not require GitHub or Supabase. That version's records are in `daybook.sqlite3`.
