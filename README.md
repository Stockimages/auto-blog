# Auto Blog Publisher — setup (one-time)

This repo publishes a new budget-home-decor post to your Blogger blog every
day, fully automatically: Gemini picks the topic, writes the article,
generates a header image; the image is compressed and committed to this
repo; the post goes live on Blogger. No clicking required after setup.

## 1. Get a Gemini API key
Google AI Studio -> Get API key. Copy it.

## 2. Enable Blogger API + create a Desktop OAuth client
1. Google Cloud Console -> select/create a project.
2. APIs & Services -> Library -> enable **Blogger API v3**.
3. APIs & Services -> OAuth consent screen -> set it up (External, add your
   own email as a test user if it stays in "Testing" mode).
4. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
   -> Application type: **Desktop app**. Copy the Client ID and Client Secret.

## 3. Get your Blogger Blog ID
Blogger dashboard -> Settings -> your Blog ID is shown there.

## 4. Get a refresh token (one-time, on your own computer)
```
pip install google-auth-oauthlib
```
Open `get_refresh_token.py`, paste in your Client ID and Client Secret,
then run:
```
python get_refresh_token.py
```
A browser tab opens — log in and approve. The refresh token prints in your
terminal. Copy it.

## 5. Create this repo on GitHub and push these files
```
git init
git add .
git commit -m "Auto blog publisher"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

## 6. Add GitHub Secrets
Repo -> Settings -> Secrets and variables -> Actions -> New repository secret.
Add each of these:

| Secret name            | Value                                  |
|-------------------------|-----------------------------------------|
| `GEMINI_API_KEY`         | from step 1                             |
| `BLOGGER_BLOG_ID`        | from step 3                             |
| `GOOGLE_CLIENT_ID`       | from step 2                             |
| `GOOGLE_CLIENT_SECRET`   | from step 2                             |
| `GOOGLE_REFRESH_TOKEN`   | from step 4                             |

## 7. Turn on GitHub Pages (this gives you the control-panel website)
Repo -> Settings -> Pages -> Source: "Deploy from a branch" -> Branch: `main`,
folder: `/ (root)` -> Save. GitHub gives you a URL like:
```
https://YOUR_USERNAME.github.io/YOUR_REPO/
```
That page is `index.html` — the control panel. Open it.

## 8. Connect the control panel
1. Create a GitHub Personal Access Token: GitHub -> Settings -> Developer
   settings -> Personal access tokens -> Fine-grained tokens -> New token.
   Restrict it to **only this repo**. Under Permissions, set
   **Actions: Read & write** and **Contents: Read & write**.
2. Open your Pages URL from step 7, paste in the token and `owner/repo`,
   click **Connect**.
3. From there: **Start** turns the daily schedule on, **Stop** turns it off,
   **Run once now** triggers an immediate test post, and you can edit the
   blog's focus/niche and posting frequency right from the page. Recent runs
   and their status show at the bottom, with a link to the full GitHub log
   for each one.

The token is stored only in your own browser's local storage — it is never
sent anywhere except directly to GitHub's API.

## Pinterest note
Since your Blogger blog auto-posts to Pinterest, every image now gets a
bold text banner (a short punchy "pin_hook" Gemini writes, like a real
Pinterest pin) added near the top automatically, and the image is generated
in a vertical 2:3 shape — the format Pinterest favors. The article's opening
paragraph is written as a standalone hook too, since Pinterest/search often
show just that first line as the preview. Nothing extra to configure — this
is baked into the generation step.

## Notes
- If a run fails with a "model not found" error, Google renamed a model.
  Check current names in Google AI Studio and update `GEMINI_TEXT_MODEL` /
  `GEMINI_IMAGE_MODEL` (either edit the defaults in `auto_blog.py`, or add
  them as extra repo secrets/variables with those exact names).
- `topics_history.json` grows automatically so Gemini avoids repeating
  itself. Don't delete it.
- Nothing here reviews the post before it goes live. Worth skimming the
  blog every few days, since fully unattended AI content can occasionally
  drift off-topic or repeat a claim oddly even with the topic-history guard.
- Anyone who gets your Personal Access Token could enable/disable the
  workflow or edit files in this repo, so keep it private and scoped to
  just this one repo (fine-grained tokens let you do that).
