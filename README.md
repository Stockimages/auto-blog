# DecorVibe Auto Blog Publisher

Publishes budget home-decor posts automatically:

1. **Gemini** picks a topic and writes the article (format and season vary).
2. **Pexels** (and **Pixabay** as a fallback) supply real photos; Gemini checks
   each photo actually matches the post.
3. The post goes live on **Blogger**.
4. A pin goes to **Pinterest** (image pin, or a video pin on video runs).
5. A photo post goes to **Tumblr**.
6. The new URL is sent to **Bing**, and an "All Posts" hub page on the blog is
   refreshed so every post stays linked.

Facebook, Instagram, TikTok and YouTube are **not** used any more.

## Run types

The workflow takes a `run_type` input:

| run_type | What it does |
|---|---|
| `image` | Blogger post + Pinterest **image** pin + Tumblr |
| `video` | Same, but the Pinterest pin is a **video** (2:3) with a Gemini voiceover (voice "Zephyr"), black bars, yellow highlighted words, 41 random photo transitions and a soft swoosh on each photo change |

A scheduler such as cron-job.org triggers the workflow (`workflow_dispatch`) at
the times you choose; you can also use **Actions -> Auto Blog Publisher -> Run
workflow** by hand.

## Files

| File | Purpose |
|---|---|
| `auto_blog.py` | The whole pipeline |
| `video_template.py` | Video look, transitions, swoosh sound, Gemini voice. If it is missing, `video` runs quietly become `image` runs |
| `.github/workflows/auto-blog.yml` | The GitHub Actions workflow (also downloads the free Anton and Poppins fonts) |
| `config.json` | The blog's niche/focus |
| `requirements.txt` | Python packages |
| `topics_history.json` | Every post made so far (topic, format, theme, URL, photo ids). **Don't delete** - it prevents repeats |
| `bing_submitted.json` | URLs already sent to Bing |
| `status.json` | Result of the last run (rewritten every run) |
| `images/` | Older posts' images (before R2). Don't delete: those posts still point here |
| `music/` | Background music for video runs |

## GitHub Secrets

Settings -> Secrets and variables -> Actions -> Secrets:

| Group | Secrets |
|---|---|
| Gemini | `GEMINI_API_KEY` |
| Blogger | `BLOGGER_BLOG_ID`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` |
| Photos | `PEXELS_API_KEY`, `PIXABAY_API_KEY` (optional) |
| Pinterest | `PINTEREST_APP_ID`, `PINTEREST_APP_SECRET`, `PINTEREST_REFRESH_TOKEN`, `PINTEREST_BOARD_ID` |
| Tumblr | `TUMBLR_CONSUMER_KEY`, `TUMBLR_CONSUMER_SECRET`, `TUMBLR_ACCESS_TOKEN`, `TUMBLR_ACCESS_TOKEN_SECRET`, `TUMBLR_BLOG_NAME` |
| Image hosting (Cloudflare R2) | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, `R2_PUBLIC_URL` |
| Bing | `BING_API_KEY` |
| Email summary after each run | `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL` |
| Auto-updating the Pinterest token | `GH_SECRETS_PAT` |

Optional **Variables** (same page, Variables tab):

| Variable | Effect |
|---|---|
| `VOICE_ENGINE` = `edge` | Skip Gemini's voice and use Edge-TTS only |
| `GEMINI_TTS_MODEL` | Override the Gemini text-to-speech model name |

## What the pipeline checks before publishing

A draft is rejected and rewritten (up to 4 tries) if it:
- is too short (under 600 words),
- repeats the topic of an earlier post,
- names a brand, store or branded product,
- claims a personal experience ("I tried...", "my home...") that did not happen,
- has no usable hero photo for its topic.

If nothing could be published, the run waits 15 minutes and tries once more.
You get an email either way.

## Troubleshooting

- **Run failed with "exhausted all attempts"** - Gemini's better models are out
  of quota or overloaded; the log line shows the HTTP error. The lite models
  write shorter articles that the checks above reject. Wait, and avoid many
  manual runs in one day.
- **"model not found"** - Google renamed a model. Update the model names in
  `auto_blog.py` (the `GEMINI_*` defaults near the top).
- **Pinterest token** - refreshed and saved back to `PINTEREST_REFRESH_TOKEN`
  automatically (needs `GH_SECRETS_PAT`).
- **A post disappeared** - Blogger can unpublish a post for a guideline
  issue. You will get an email if an auto-post turns into a Draft.
- Skim the blog every few days: unattended AI content can still drift.
