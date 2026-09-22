"""
Fully automatic Blogger publisher for a budget home-decor blog.

Flow each run:
  1. Read topics_history.json (titles + urls) so Gemini doesn't repeat itself
     and can link to relevant older posts of ours
  2. Ask Gemini for: topic, full article (with internal links + a budget
     table + section-image placeholders), a Pinterest hook, a hero image
     search query, and a search query for each in-article section image
  3. Fetch a vertical hero photo from Pexels (for Pinterest) + a horizontal
     photo per section (for the article body)
  4. Overlay a bold Pinterest-style text hook on the hero image only
  5. Commit all images to this repo (so they get public raw.githubusercontent.com URLs)
  6. Swap the section-image placeholders in the HTML for real <img> tags
  7. Get a fresh Blogger access token from the stored refresh token
  8. Publish the post to Blogger
  9. Save this post's title + URL into history (for future internal linking)
  10. Get a fresh Pinterest access token from the stored refresh token
  11. Create a Pin on Pinterest pointing back to the new post

Meant to be run by the GitHub Actions workflow in .github/workflows/auto-blog.yml,
on a schedule, with no human interaction.
"""

import os
import re
import math
import json
import html
import base64
import subprocess
import textwrap
import random
import time
import asyncio
import smtplib
import urllib.parse
from email.mime.text import MIMEText
from io import BytesIO
from datetime import datetime, timezone

import requests
from requests_oauthlib import OAuth1Session
import edge_tts
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest
from PIL import Image, ImageDraw, ImageFont

# ---- Required secrets / env vars (set these as GitHub Actions secrets) ----
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BLOGGER_BLOG_ID = os.environ["BLOGGER_BLOG_ID"]
SITE_URL = "https://decorvibeto.com"
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]

# Service-account JSON (full contents) for the Google Indexing API — lets us
# tell Google to (re)crawl a new post immediately instead of waiting for it
# to be discovered via the sitemap on its own schedule.
GOOGLE_INDEXING_KEY = os.environ.get("GOOGLE_INDEXING_KEY")

# Bing Webmaster Submission API key — lets us tell Bing to (re)crawl a new
# post immediately. Bing's own index also powers Yahoo and DuckDuckGo
# results, so this one call effectively covers all three.
BING_API_KEY = os.environ.get("BING_API_KEY")

# Personal access token with "Secrets: read and write" permission on this repo
# only — used to auto-update the PINTEREST_REFRESH_TOKEN secret when Pinterest
# rotates it, so no manual copy-paste is ever needed.
GH_SECRETS_PAT = os.environ.get("GH_SECRETS_PAT")

# Pinterest — used to auto-post a Pin right after each Blogger post goes live.
PINTEREST_APP_ID = os.environ["PINTEREST_APP_ID"]
PINTEREST_APP_SECRET = os.environ["PINTEREST_APP_SECRET"]
PINTEREST_REFRESH_TOKEN = os.environ["PINTEREST_REFRESH_TOKEN"]
PINTEREST_BOARD_ID = os.environ["PINTEREST_BOARD_ID"]

# Facebook Page — auto-posts a link to the Page right after each Blogger post.
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")
FACEBOOK_PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN")

# Tumblr — auto-posts a photo pointing back to each new Blogger post.
# Unlike Medium, Tumblr's OAuth 1.0a API is still open/self-service, so this
# is a normal, fully-supported integration (no session cookies, no risk).
TUMBLR_CONSUMER_KEY = os.environ.get("TUMBLR_CONSUMER_KEY")
TUMBLR_CONSUMER_SECRET = os.environ.get("TUMBLR_CONSUMER_SECRET")
TUMBLR_ACCESS_TOKEN = os.environ.get("TUMBLR_ACCESS_TOKEN")
TUMBLR_ACCESS_TOKEN_SECRET = os.environ.get("TUMBLR_ACCESS_TOKEN_SECRET")
TUMBLR_BLOG_NAME = os.environ.get("TUMBLR_BLOG_NAME")

# Phone notification (via Gmail App Password + SMTP) — sends a summary
# email to your own inbox after each run, so a push notification shows up
# on your phone even without touching the Blogger OAuth setup at all.
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", GMAIL_ADDRESS)

# Instagram Business account — auto-posts the hero image right after each
# Blogger post. INSTAGRAM_ACCESS_TOKEN is a Facebook Page Access Token
# (derived from a long-lived user token via Graph API Explorer), which
# doesn't expire on its own — no refresh logic needed, unlike Pinterest.
INSTAGRAM_ACCOUNT_ID = os.environ.get("INSTAGRAM_ACCOUNT_ID")
INSTAGRAM_ACCESS_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN")

# Auto-set by GitHub Actions as "owner/repo". Falls back for local testing.
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "your-username/your-repo")

# --- Cloudflare R2 (image/video hosting) ---
# Images and videos are uploaded here instead of being committed to the
# GitHub repo, so the repo itself never grows — R2 has its own free
# storage (10 GB) and serves files publicly on its own, with no git
# history bloat over time.
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")

_r2_client = None


def get_r2_client():
    """Lazily builds the boto3 S3-compatible client for Cloudflare R2."""
    global _r2_client
    if _r2_client is None:
        import boto3
        from botocore.config import Config as BotoConfig
        _r2_client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=BotoConfig(signature_version="s3v4"),
            region_name="auto",
        )
    return _r2_client


_R2_CONTENT_TYPES = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".mp4": "video/mp4",
}


def upload_to_r2(local_path):
    """
    Uploads a local file to the Cloudflare R2 bucket and returns its public
    URL. This is what hero/section/carousel images and the video (in
    video-mode) use instead of being committed to the git repo.
    """
    key = local_path.replace(os.sep, "/")
    ext = os.path.splitext(local_path)[1].lower()
    content_type = _R2_CONTENT_TYPES.get(ext, "application/octet-stream")
    client = get_r2_client()
    with open(local_path, "rb") as f:
        client.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=f, ContentType=content_type)
    return f"{R2_PUBLIC_URL}/{key}"


def delete_from_r2(local_path):
    """
    Deletes a file from the R2 bucket by the same key upload_to_r2 used
    (the local path, forward-slashed). Called at the end of a run for
    everything that was only ever needed ONCE — Facebook/Instagram/
    Pinterest/TikTok all fetch a file from its R2 URL and keep their own
    copy, so once posting is done there's nothing left pointing at it.
    The hero image is the one exception (Blogger's post keeps embedding
    that exact URL forever), so it's never passed here. Never raises —
    a cleanup failure should never turn a successful run into a failed
    one; it just means that one file lingers in R2 an extra day.
    """
    if not local_path:
        return
    try:
        key = local_path.replace(os.sep, "/")
        get_r2_client().delete_object(Bucket=R2_BUCKET_NAME, Key=key)
        print(f"Cleaned up from R2: {key}")
    except Exception as e:
        print(f"R2 cleanup failed for {local_path} (harmless, continuing): {e}")


# Controls which social-posting mode this run uses. Set via the GitHub
# Actions workflow so the 6:30 AM trigger passes RUN_TYPE=image (current
# carousel/link/image-pin behavior) and the 6:30 PM trigger passes
# RUN_TYPE=video (Reel/native-video/video-pin behavior). Defaults to
# "image" so nothing changes if the workflow doesn't set it.
#
# Video-mode was previously tried and disabled because it used Pexels'
# stock VIDEO library, which is far smaller/more generic than its photo
# library and kept falling back to unrelated generic clips for specific
# decor topics. That pipeline has since been replaced: video-mode now
# builds its slides from the SAME real hero/section images already
# generated for the article (Ken Burns zoom + an AI voiceover via
# Edge-TTS), so the content-matching problem this override existed for no
# longer applies. Re-enabled as of the voiceover rewrite.
RUN_TYPE = os.environ.get("RUN_TYPE", "image").strip().lower()

# Model name — Google updates these periodically. If a run starts failing
# with a 404 "model not found" error, check the current name in Google AI
# Studio and update below (or set GEMINI_TEXT_MODEL as an env var/config value).
TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.8-flash")

# If TEXT_MODEL is overloaded/unavailable across all its retries, we fall
# back through these proven models in order rather than failing the run.
FALLBACK_TEXT_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.7-flash")
FALLBACK_TEXT_MODEL_2 = os.environ.get("GEMINI_FALLBACK_MODEL_2", "gemini-3.6-flash")
FALLBACK_TEXT_MODEL_3 = os.environ.get("GEMINI_FALLBACK_MODEL_3", "gemini-3.5-flash")

HISTORY_FILE = "topics_history.json"
CONFIG_FILE = "config.json"
DEFAULT_NICHE = "budget-friendly home decor"

# Fixed set of categories that back the site's navigation menu (each one is
# a real Blogger label with its own /search/label/<Category> page). Every
# post must be filed under exactly one of these so the menu always leads
# somewhere real. Spelling/capitalization here must exactly match the menu
# links in Blogger's Layout > Top Navigation gadget.
CATEGORIES = [
    "Living Room",
    "Bedroom",
    "Kitchen",
    "Bathroom",
    "Small Spaces",
    "Entryway",
    "Outdoor",
    "General Decor",
]

# Each category pins to its own dedicated Pinterest board (created manually
# in the Pinterest UI, IDs copied from get_pinterest_boards.py) instead of
# everything going to one shared board — better topical discovery on
# Pinterest. PINTEREST_BOARD_ID (the original single board) stays as the
# fallback for any category that's missing here.
CATEGORY_BOARD_IDS = {
    "Living Room": "1123014925773074351",
    "Bedroom": "1123014925773074361",
    "Kitchen": "1123014925773074364",
    "Bathroom": "1123014925773074365",
    "Small Spaces": "1123014925773074366",
    "Entryway": "1123014925773074367",
    "Outdoor": "1123014925773074371",
    "General Decor": "1123014925773074374",
}

# A small, fixed set of emoji used ONLY in Facebook/Instagram captions (never
# in the hook line itself, and never on Pinterest or Blogger — both of those
# are search-driven platforms where 2026 best practice is to stay
# emoji-free and keyword-focused; Facebook/Instagram are scroll-feed
# platforms where 2-3 tasteful emoji measurably help engagement/CTR).
CATEGORY_EMOJIS = {
    "Living Room": "🛋️",
    "Bedroom": "🛏️",
    "Kitchen": "🍽️",
    "Bathroom": "🛁",
    "Small Spaces": "📦",
    "Entryway": "🚪",
    "Outdoor": "🌿",
    "General Decor": "🏠",
}

# Words/phrases that make AI writing sound canned. Gemini is told to avoid these.
BANNED_PHRASES = [
    "elevate", "delve", "unlock", "unleash", "seamless", "seamlessly",
    "game-changer", "game changer", "revolutionize", "boasts", "furthermore",
    "moreover", "in today's world", "in today's day and age", "when it comes to",
    "it's important to note", "in conclusion", "at the end of the day",
    "realm", "tapestry", "testament to", "landscape of", "dive into",
    "unveil", "unveiling", "embark", "embark on a journey", "whether you're",
    "in the world of", "look no further", "let's face it",
]


DEFAULT_WAIT_SECONDS = [15, 30, 60]


def robust_request(method, url, max_attempts=4, wait_seconds=None, retry_statuses=(429, 500, 502, 503, 504), **kwargs):
    """
    A requests.request() wrapper used for every network call in this script.
    Retries on:
      - network-level failures (timeout, connection reset, DNS hiccup — these
        raise before any HTTP response exists, so status_code can't catch them)
      - the given transient HTTP status codes (rate-limited / server hiccups)
    Anything else (4xx auth/bad-request errors) is returned as-is immediately
    for the caller to handle/raise with a specific message.
    """
    wait_seconds = wait_seconds or DEFAULT_WAIT_SECONDS
    last_response = None

    for attempt in range(1, max_attempts + 1):
        is_last_attempt = attempt == max_attempts
        try:
            res = requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException as e:
            if is_last_attempt:
                raise RuntimeError(f"Request to {url} failed after {max_attempts} attempts (network error): {e}")
            delay = wait_seconds[min(attempt - 1, len(wait_seconds) - 1)]
            print(f"Network error calling {url} ({e}), retrying in {delay}s "
                  f"(attempt {attempt}/{max_attempts})...")
            time.sleep(delay)
            continue

        if res.ok or res.status_code not in retry_statuses or is_last_attempt:
            return res

        last_response = res
        delay = wait_seconds[min(attempt - 1, len(wait_seconds) - 1)]
        print(f"{url} returned {res.status_code}, retrying in {delay}s "
              f"(attempt {attempt}/{max_attempts})...")
        time.sleep(delay)

    return last_response


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def get_trending_context(niche):
    """
    Asks Gemini (with Google Search grounding enabled) what's currently
    being searched for / talked about in this niche, so generate_draft can
    nudge its topic choice toward something people are actually looking
    for right now instead of a purely random angle. Deliberately a
    separate, small, free-text call — NOT the same call that generates the
    structured JSON draft — because mixing Google Search grounding into a
    call that must return strict JSON risks breaking that JSON (grounded
    responses tend to add citations/commentary). Returns a short string,
    or "" on any failure — this is a nice-to-have nudge, never something
    the run should fail over.
    """
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{TEXT_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": (
                    f"Using Google Search, check what's currently trending or "
                    f"getting a lot of search interest right now in the "
                    f"{niche} niche — specifically thrifted/budget furniture "
                    f"and decor flips. In 2-3 short sentences, name a couple "
                    f"of specific current angles, items, or styles people "
                    f"seem to be searching for/talking about. Be concrete "
                    f"and brief — no preamble, no markdown."
                )}]}],
                "tools": [{"google_search": {}}],
            },
            timeout=60,
        )
        if not res.ok:
            print(f"Trending-context lookup failed ({res.status_code}) — continuing without it.")
            return ""
        candidates = res.json().get("candidates", [])
        if not candidates:
            return ""
        text = "".join(
            part.get("text", "") for part in candidates[0].get("content", {}).get("parts", [])
        ).strip()
        if text:
            print("Trending context:", text)
        return text
    except Exception as e:
        print(f"Trending-context lookup failed ({e}) — continuing without it.")
        return ""


def generate_draft(history, niche):
    # Duplicate-topic avoidance window: at ~2 posts/day, checking only the
    # last 50 titles covers ~25 days — past that, older topics could start
    # silently repeating since Gemini never sees them again. 300 covers
    # ~5 months before the same widening concern reappears; titles are
    # short so this stays cheap even at that size.
    recent_titles = [h["title"] for h in history[-300:]]

    # Only entries that have a URL (i.e. posts we've actually published since
    # URL-tracking was added) are usable as internal-link candidates.
    linkable = [h for h in history if h.get("url")][-20:]
    linkable_json = json.dumps(
        [{"title": h["title"], "url": h["url"]} for h in linkable],
        ensure_ascii=False,
    )

    banned_list = ", ".join(f'"{w}"' for w in BANNED_PHRASES)

    # Count how many past posts fell in each fixed category so we can nudge
    # Gemini toward whichever categories are under-served, instead of every
    # category naturally drifting toward whatever's easiest to write about.
    category_counts = {c: 0 for c in CATEGORIES}
    for h in history:
        cat = h.get("category")
        if cat in category_counts:
            category_counts[cat] += 1
    categories_by_need = sorted(CATEGORIES, key=lambda c: category_counts[c])
    category_counts_str = ", ".join(f"{c}: {category_counts[c]}" for c in CATEGORIES)

    trending_context = get_trending_context(niche)
    trending_block = (
        f"\nWhat's currently trending in this niche (from a live search just now) — "
        f"lean toward this if it genuinely fits a good topic, but don't force it:\n"
        f"{trending_context}\n"
        if trending_context else ""
    )

    prompt = f"""You are a real person who runs a {niche} blog and personally writes every
post. You've done these projects yourself, in your own home, on a real budget.
Posts are shared to Pinterest automatically the moment they're published, so
the opening line has to earn a click — then the article has to actually
deliver, like a friend explaining exactly how they did something.
{trending_block}
Topics already covered (do NOT repeat these or anything too similar to them):
{json.dumps(recent_titles, ensure_ascii=False)}

Pick ONE fresh, specific, practical angle on {niche} that is not in that list.

CATEGORY (required): every post on this site is filed under exactly ONE of
these fixed categories, which is also the site's navigation menu — pick
whichever one the topic genuinely belongs to:
{json.dumps(CATEGORIES, ensure_ascii=False)}

Current post count per category (so the site stays balanced instead of
piling up in one category): {category_counts_str}.
All else being equal, prefer a topic that fits one of the currently
under-served categories — in order of most-needed first: {json.dumps(categories_by_need, ensure_ascii=False)}.
But NEVER force a topic into the wrong category just to balance the count —
pick the category the topic honestly belongs in, and if nothing fits well,
use "General Decor".

WRITING VOICE — this is the most important instruction:
- Write like a real person talking to a friend, not like a content mill.
- Vary sentence length. Short punchy sentences next to longer ones. Use contractions.
- Be specific and concrete everywhere: real product types, real store names when
  natural (Ikea, Home Depot, Target, thrift stores, Facebook Marketplace), real
  price ranges, real tools, real brand-agnostic techniques.
- It's fine to have a mild personal opinion or aside ("I was skeptical about this one, but...").
- NEVER use these overused AI-sounding words/phrases, in any form: {banned_list}.
- No generic filler sentences that could apply to any home-decor post. Every
  paragraph must teach something specific or move the project forward.

LENGTH: 900-1300 words. Do not pad to hit a word count — if the honest,
specific version of this article is 950 words, that's fine. Every sentence
should earn its place.

STRUCTURE (as HTML, using ONLY these tags: p, h2, h3, ul, ol, li, table, thead,
tbody, tr, th, td, strong, blockquote, a):

CRITICAL JSON-SAFETY RULE: inside the "html" string, use SINGLE quotes for
every HTML attribute value (e.g. <a href='https://...'>, not <a href="https://...">).
Never use a double-quote character anywhere inside the html string — double
quotes are the JSON string delimiter and will break the response.

1. Opening hook paragraph (standalone, curiosity or a specific promise —
   this is what Pinterest/Google show as the preview).
2. A few h2/h3 sections walking through the real project or tips, using
   <ul> for independent tips/ideas and <ol> for sequential step-by-step
   instructions — pick whichever actually fits each section.
3. Include ONE real <table> somewhere natural in the article: a budget /
   materials breakdown with columns like Item, Price. Use realistic prices
   that add up to a sensible total, and mention the total in the text near
   the table (e.g. "All in, this came out to about $X").
4. INTERNAL LINKS: here are {len(linkable)} of our own previously published
   posts (title + real URL): {linkable_json}
   If (and only if) 1-3 of them are genuinely relevant to THIS article's
   topic, link to them naturally in-line inside a sentence using
   <a href="the exact URL">relevant anchor text</a> — never a bare "read
   more" list, never a fabricated URL, never forced if nothing fits.
5. IMAGE PLACEHOLDERS: after the intro and after 2-3 of the major sections
   (never as the very first thing), insert an image placeholder on its own
   line: [[IMG_1]], then [[IMG_2]], then [[IMG_3]] if the article is long
   enough — sequential numbering, 2-3 total. Do not add more placeholders
   than you provide section_images for.

6. QUICK FACTS: also provide total_cost (the final total from your budget
   table, e.g. "$26"), time_estimate (e.g. "1 hour", "A weekend"), and
   difficulty ("Easy", "Moderate", or "Advanced") — used for a quick-take
   summary box at the top of the post. All three MUST be plain strings
   (e.g. "$26", not 26; not a list).

7. FAQ: write exactly 3 short, genuinely specific reader questions about
   THIS project (not generic decor questions) with concise 1-2 sentence
   answers. No quotation marks inside the question/answer text.

   STRICT FORMAT — "faq" MUST be a list of exactly 3 JSON objects, each with
   a "question" key and an "answer" key, both plain strings. Do NOT return
   plain strings, arrays of two strings, or any other shape. Example of the
   ONLY acceptable shape (using this article's own type of topic):
   "faq": [
     {{"question": "Will this work on a stand that already has some rust",
       "answer": "Yes, light surface rust is fine — scrub it off with steel wool before priming so the paint has a clean surface to grip."}},
     {{"question": "Do I need to remove the wire mesh before painting",
       "answer": "No, leave it in place — it gives the spray paint something to grip and keeps the stand's original shape intact."}},
     {{"question": "How long before the finish can handle daily use",
       "answer": "Let it cure a full 48 hours before placing anything in it, even though it feels dry to the touch after a few hours."}}
   ]

Also write:
- "pin_hook": a punchy, benefit- or curiosity-driven phrase, 5-8 words max,
  written like Pinterest pin text (e.g. "10 Thrift Flips That Look Expensive"),
  NOT a full sentence, no ending punctuation.
- "image_prompt": REQUIRED — 3-5 simple search keywords (not a sentence) for
  the vertical HERO photo (this is the one shown on Pinterest AND the reel's
  opening shot) — e.g. "thrifted glass vase living room". No brand names, no
  people's faces, no text. Bias toward a STYLED, finished-look shot rather
  than a plain product photo: add a styling word when it fits naturally
  (e.g. "styled", "cozy", "rustic", "close-up", "warm light", "vignette") so
  the search leans toward an aesthetic, magazine-style result instead of a
  flat catalog photo — this is what makes someone stop and think "how did
  they make that?" instead of scrolling past. This field must always be
  present in your JSON response.
- "section_images": a list matching your [[IMG_n]] placeholders, each with a
  "token" (e.g. "IMG_1") and a "query" (3-5 keyword search terms for a real,
  horizontal photo matching that section of the article, with the same
  styled/aesthetic bias as image_prompt above — no people's faces, no text).
- "reel_script": a short spoken-word voiceover script for a ~18-22 second
  vertical video (Instagram Reel / TikTok), 45-65 words total, written to be
  read aloud by an AI voice — NOT the article text, and do NOT include any
  call-to-action or "link"/"website"/"bio" line (that's added separately).
  Structure:
  1. A punchy 1-sentence hook that would make someone stop scrolling — lean
     hard into the price-transformation shock or a specific, concrete
     curiosity gap (what it actually is, not "you won't believe this").
     Specific and unexpected beats generic every time: "This $9 thrift-store
     lazy Susan is now a $380 stone counter riser" beats "I made an amazing
     upgrade for cheap."
  2. 2 short, concrete tip/step sentences pulled from the real project — the
     single most surprising or useful specific detail (the trick, the
     material swap, the exact technique), not a generic summary. In ONE of
     these, naturally mention the total cost and time using the actual
     numbers (e.g. "It only took about an hour and cost me $20.") — write
     dollar amounts as "$20" (not spelled out); the TTS voice reads these
     correctly, and it lets the on-screen caption highlight the price.
  Write it like natural spoken American English: short punchy sentences,
  contractions, no filler, no markdown, no quotation marks inside the
  string. Every sentence should earn its place — cut anything that doesn't
  add curiosity or a concrete, specific detail.

Return ONLY valid JSON. No markdown fences, no commentary before or after.
{{
  "title": "a specific, honest, clickable title. Plain text only — no emoji (this is an SEO title indexed by Google, and keyword clarity matters more than decoration there)",
  "category": "EXACTLY one of the fixed categories listed above",
  "pin_hook": "...",
  "hashtag_tags": ["8-12 short descriptive style/content tags for social hashtags only (Instagram/Facebook use more of these than Pinterest does), e.g. thrift flip, diy, budget decor, home makeover, thrifted finds — these do NOT affect the site's category"],
  "total_cost": "e.g. $26",
  "time_estimate": "e.g. 1 hour",
  "difficulty": "Easy, Moderate, or Advanced",
  "faq": [
    {{"question": "...", "answer": "..."}},
    {{"question": "...", "answer": "..."}},
    {{"question": "...", "answer": "..."}}
  ],
  "html": "full article body as HTML, following every rule above",
  "image_prompt": "...",
  "section_images": [
    {{"token": "IMG_1", "query": "..."}},
    {{"token": "IMG_2", "query": "..."}}
  ],
  "reel_script": "..."
}}"""

    max_attempts = 3
    wait_seconds = [20, 45]  # delay before attempts 2, 3 (per model)

    # If the primary model is overloaded/unavailable across all its retries,
    # fall back to a second, proven-stable model rather than failing the run.
    models_to_try = [TEXT_MODEL]
    if FALLBACK_TEXT_MODEL and FALLBACK_TEXT_MODEL not in models_to_try:
        models_to_try.append(FALLBACK_TEXT_MODEL)
    if FALLBACK_TEXT_MODEL_2 and FALLBACK_TEXT_MODEL_2 not in models_to_try:
        models_to_try.append(FALLBACK_TEXT_MODEL_2)
    if FALLBACK_TEXT_MODEL_3 and FALLBACK_TEXT_MODEL_3 not in models_to_try:
        models_to_try.append(FALLBACK_TEXT_MODEL_3)

    last_error = None
    for model_index, model in enumerate(models_to_try):
        is_last_model = model_index == len(models_to_try) - 1

        for attempt in range(1, max_attempts + 1):
            is_last_attempt_for_model = attempt == max_attempts

            try:
                res = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": GEMINI_API_KEY},
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                    timeout=150,
                )
            except requests.exceptions.RequestException as e:
                last_error = f"network error: {e}"
                if is_last_attempt_for_model and is_last_model:
                    raise RuntimeError(f"Gemini request failed on all models/attempts: {last_error}")
                if not is_last_attempt_for_model:
                    delay = wait_seconds[attempt - 1]
                    print(f"[{model}] Gemini request failed ({e}), retrying in {delay}s "
                          f"(attempt {attempt}/{max_attempts})...")
                    time.sleep(delay)
                continue

            if res.ok:
                text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                text = text.replace("```json", "").replace("```", "").strip()
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:
                    last_error = f"invalid JSON: {e}"
                    if is_last_attempt_for_model and is_last_model:
                        raise RuntimeError(f"Gemini returned invalid JSON on all models/attempts: {last_error}")
                    if not is_last_attempt_for_model:
                        delay = wait_seconds[attempt - 1]
                        print(f"[{model}] Gemini returned invalid JSON ({e}), retrying in {delay}s "
                              f"(attempt {attempt}/{max_attempts})...")
                        time.sleep(delay)
                    continue

            # Retry only on transient errors (overloaded / rate-limited / server hiccup).
            # Fail immediately on anything else (e.g. bad API key, bad request).
            transient = res.status_code in (429, 500, 502, 503, 504)
            last_error = f"HTTP {res.status_code}: {res.text[:200]}"

            if not transient:
                raise RuntimeError(f"Gemini text generation failed ({res.status_code}): {res.text}")

            if is_last_attempt_for_model and is_last_model:
                raise RuntimeError(f"Gemini text generation failed on all models/attempts: {last_error}")

            if not is_last_attempt_for_model:
                delay = wait_seconds[attempt - 1]
                print(f"[{model}] Gemini text generation failed ({res.status_code}), retrying in {delay}s "
                      f"(attempt {attempt}/{max_attempts})...")
                time.sleep(delay)
            else:
                print(f"[{model}] exhausted all attempts, switching to fallback model...")


def normalize_draft(draft):
    """
    Single place that guarantees every field the rest of this script reads
    from `draft` is present and in a sane shape — so a future run where
    Gemini omits or malforms ANY field degrades gracefully (falls back to a
    safe default) instead of crashing the whole run with a KeyError or
    AttributeError somewhere downstream.

    This exists because that's exactly what happened twice already: once
    with a malformed "faq" item, once with a missing "image_prompt". Rather
    than adding a one-off guard at each crash site as new fields get added
    to the prompt over time, every field is checked here, in one place,
    the moment the draft comes back from Gemini. Add new fields to THIS
    function when the prompt grows, instead of patching a crash later.

    "title" and "html" are the only two fields that can't be sensibly
    defaulted — without them there's no article at all — so those two
    still raise if missing, surfacing a clear error instead of publishing
    an empty post.
    """
    if not draft.get("title"):
        raise RuntimeError("Gemini response is missing required field 'title'.")
    if not draft.get("html"):
        raise RuntimeError("Gemini response is missing required field 'html'.")

    def warn(field, fallback_desc):
        print(f"Gemini response missing/invalid '{field}' — using {fallback_desc}.")

    if not draft.get("pin_hook"):
        warn("pin_hook", "the title")
        draft["pin_hook"] = draft["title"]

    if not draft.get("image_prompt"):
        warn("image_prompt", "the title as the search query")
        draft["image_prompt"] = draft["title"]

    if not draft.get("reel_script") or not isinstance(draft.get("reel_script"), str):
        warn("reel_script", "a generated fallback built from the title/pin_hook")
        cost_line = f"It only cost {draft.get('total_cost', 'a few dollars')}. " if draft.get("total_cost") else ""
        draft["reel_script"] = (
            f"{draft.get('pin_hook', draft['title'])}. "
            f"Here's how to get the look for way less. "
            f"{cost_line}"
        ).strip()

    if not isinstance(draft.get("section_images"), list):
        warn("section_images", "no section images")
        draft["section_images"] = []
    else:
        # Each item needs at least a usable "query" — drop any that don't,
        # rather than letting a malformed item crash the image-fetch loop.
        draft["section_images"] = [
            s for s in draft["section_images"]
            if isinstance(s, dict) and s.get("query")
        ]

    if draft.get("category") not in CATEGORIES:
        warn("category", "'General Decor'")
        draft["category"] = "General Decor"

    if not isinstance(draft.get("hashtag_tags"), list):
        warn("hashtag_tags", "an empty tag list")
        draft["hashtag_tags"] = []
    else:
        draft["hashtag_tags"] = [str(t) for t in draft["hashtag_tags"] if t]

    for field, fallback in [
        ("total_cost", "See breakdown below"),
        ("time_estimate", "A weekend"),
        ("difficulty", "Easy"),
    ]:
        value = draft.get(field)
        if not value or not isinstance(value, (str, int, float)):
            warn(field, repr(fallback))
            draft[field] = fallback
        else:
            draft[field] = str(value)

    if not isinstance(draft.get("faq"), list):
        warn("faq", "no FAQ section")
        draft["faq"] = []
    else:
        draft["faq"] = [
            f for f in draft["faq"]
            if isinstance(f, dict) and f.get("question") and f.get("answer")
        ][:3]

    return draft


def search_pexels_image(query, orientation="portrait", used_photo_ids=None, target_ratio=None):
    """
    Finds a Pexels photo matching `query`. If `used_photo_ids` is given,
    photos we've already used in previous posts are skipped — two posts
    with similar queries would otherwise land on the exact same photo,
    which looks like duplicate spam on Pinterest in particular. Falls back
    to the full result set if every match has already been used, so a run
    never fails just because a query's results are exhausted.

    `orientation="portrait"` only guarantees height > width — Pexels still
    returns a mix of actual ratios within that (a near-square 4:5 photo
    and a tall 1:2 photo both count as "portrait"). For the reel video,
    which needs to fill an exact 9:16 frame, a photo whose real ratio is
    far from that needs a much more aggressive cover-crop to fill the
    frame, which is what was making some slides look oddly tight/zoomed-in
    compared to the hero shot. Passing `target_ratio` (width/height, e.g.
    9/16) makes this prefer candidates reasonably close to that ratio
    instead of picking any portrait photo at random.

    Returns (image_bytes, photo_id) so the caller can record the ID.
    """
    used_photo_ids = used_photo_ids or set()

    res = robust_request(
        "GET", "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "orientation": orientation, "per_page": 30},
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(f"Pexels search failed ({res.status_code}): {res.text}")

    photos = res.json().get("photos", [])
    if not photos:
        res = robust_request(
            "GET", "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": "home decor", "orientation": orientation, "per_page": 30},
            timeout=30,
        )
        if not res.ok:
            raise RuntimeError(f"Pexels fallback search failed ({res.status_code}): {res.text}")
        photos = res.json().get("photos", [])
        if not photos:
            raise RuntimeError(f"No Pexels photos found for query: {query}")

    unused = [p for p in photos if p["id"] not in used_photo_ids]
    if not unused:
        print(f"All Pexels results for '{query}' were already used — reusing one anyway.")
    candidates = unused or photos

    if target_ratio:
        # Prefer photos within ~35% of the target ratio (e.g. 9:16 for the
        # reel) so the later cover-crop only trims a normal amount instead
        # of zooming into a thin sliver of an oddly-shaped source photo.
        # Falls back to the single closest-ratio photo if nothing is close
        # enough, rather than failing the whole run over it.
        close_enough = [
            p for p in candidates
            if p.get("width") and p.get("height")
            and abs((p["width"] / p["height"]) - target_ratio) / target_ratio < 0.35
        ]
        if close_enough:
            photo = random.choice(close_enough)
        elif any(p.get("width") and p.get("height") for p in candidates):
            photo = min(
                (p for p in candidates if p.get("width") and p.get("height")),
                key=lambda p: abs((p["width"] / p["height"]) - target_ratio),
            )
        else:
            photo = random.choice(candidates)
    else:
        photo = random.choice(candidates)

    image_url = photo["src"]["large2x"]
    image_res = robust_request("GET", image_url, timeout=30)
    if not image_res.ok:
        raise RuntimeError(f"Pexels image download failed ({image_res.status_code})")
    return image_res.content, photo["id"]


def compress_image(image_bytes, max_width=1200, quality=78):
    img = Image.open(BytesIO(image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="WEBP", quality=quality)
    return out.getvalue()


FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_bold_font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def search_pexels_video(query, orientation="portrait", min_duration=3, max_duration=25):
    """
    Finds a real Pexels stock video clip matching `query` — generic
    topic-matching b-roll (not footage of this specific fictional project,
    same honesty scope as the stock photos used elsewhere in this script)
    — and downloads the smallest file that's still at least 720p, to keep
    runs fast.
    """
    res = robust_request(
        "GET", "https://api.pexels.com/videos/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "orientation": orientation, "per_page": 15},
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(f"Pexels video search failed ({res.status_code}): {res.text}")

    videos = [
        v for v in res.json().get("videos", [])
        if min_duration <= v.get("duration", 0) <= max_duration
    ]
    if not videos:
        res = robust_request(
            "GET", "https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": "home decor", "orientation": orientation, "per_page": 15},
            timeout=30,
        )
        if not res.ok:
            raise RuntimeError(f"Pexels video fallback search failed ({res.status_code}): {res.text}")
        videos = [
            v for v in res.json().get("videos", [])
            if min_duration <= v.get("duration", 0) <= max_duration
        ]
        if not videos:
            raise RuntimeError(f"No suitable Pexels videos found for query: {query}")

    video = random.choice(videos)
    # Pick the smallest file that's still HD (720p+), to keep downloads and
    # ffmpeg processing fast — we re-encode everything anyway, so starting
    # resolution beyond 1080p is wasted bandwidth.
    hd_files = [f for f in video["video_files"] if (f.get("height") or 0) >= 720]
    candidates = sorted(hd_files or video["video_files"], key=lambda f: f.get("width", 0))
    file_info = candidates[0]

    video_res = robust_request("GET", file_info["link"], timeout=60)
    if not video_res.ok:
        raise RuntimeError(f"Pexels video download failed ({video_res.status_code})")
    return video_res.content


def build_caption_overlay_png(text, width=1080, position="top", is_cta=False):
    """
    Renders a caption as a transparent PNG with word-wrapping, drawn word
    by word (not PIL's built-in multiline_text) so dollar amounts like
    "$20" can be highlighted in gold while the rest of the line stays
    white — the price/transformation number is this niche's biggest
    scroll-stopper, so it needs to visually pop, not blend in.

    is_cta=True renders the closing "visit our website" slide with a
    bigger font and a solid accent background bar instead of the usual
    semi-transparent one, so it reads as a clear call-to-action rather
    than just another step caption.

    Vertical position is nudged down from the very top edge (rather than
    flush against it) to stay clear of Instagram/TikTok's own UI chrome
    (status area, sound name) — the "safe zone" for on-screen text.
    """
    price_re = re.compile(r"\$[\d,]+(?:\.\d+)?")
    display_text = text.upper() if (position == "top" and not is_cta) else text

    font_size = 58 if is_cta else (50 if position == "top" else 44)
    font = _load_bold_font(font_size)
    accent_color = (255, 205, 60, 255)   # gold — for price highlights
    text_color = (20, 20, 20, 255) if is_cta else (255, 255, 255, 255)
    bg_color = (255, 205, 60, 235) if is_cta else (0, 0, 0, 150)

    dummy_img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dummy_img)
    space_w = draw.textlength(" ", font=font)

    # Manual word-wrap so we can track each word's color individually.
    max_line_width = width - 100
    words = display_text.split()
    lines, current_line, current_w = [], [], 0
    for word in words:
        w = draw.textlength(word, font=font)
        if current_line and current_w + space_w + w > max_line_width:
            lines.append(current_line)
            current_line, current_w = [], 0
        current_line.append(word)
        current_w += (space_w if len(current_line) > 1 else 0) + w
    if current_line:
        lines.append(current_line)

    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    spacing = 10
    text_h = len(lines) * line_h + (len(lines) - 1) * spacing
    pad_v = 30 if is_cta else 26
    bar_h = text_h + pad_v * 2

    img = Image.new("RGBA", (width, bar_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (width, bar_h)], fill=bg_color)

    y = pad_v
    for line in lines:
        line_w = sum(draw.textlength(w, font=font) for w in line) + space_w * (len(line) - 1)
        x = (width - line_w) / 2
        for word in line:
            color = accent_color if (price_re.fullmatch(word.strip(".,!?")) and not is_cta) else text_color
            draw.text((x, y), word, font=font, fill=color)
            x += draw.textlength(word, font=font) + space_w
        y += line_h + spacing

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def build_watermark_overlay_png(brand_text="DecorVibe", canvas_size=(1080, 1920)):
    """
    Small, permanent, semi-transparent brand watermark composited onto
    every frame of the reel — placed top-right, away from Instagram/
    TikTok's own bottom UI chrome (caption/username/audio strip) and away
    from the main caption text (top-center), so it never collides with
    either. Travels with the video if it's ever reposted or screen-
    recorded without credit.
    """
    font = _load_bold_font(30)
    img = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    text = brand_text
    text_w = draw.textlength(text, font=font)
    margin = 36
    x = canvas_size[0] - text_w - margin
    y = margin
    # Faint shadow for legibility over any background, then the text itself.
    draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 110))
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 170))
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()



REEL_VOICES = ["en-US-AvaMultilingualNeural", "en-US-EmmaMultilingualNeural", "en-US-JennyNeural"]

# Rotates randomly per video (see the video-mode block in main()) instead
# of always using the same line, so posts don't feel repetitive over time.
# Deliberately confident/direct rather than asking a favor ("please visit")
# — matches how the rest of this niche's successful creators talk.
REEL_CTA_LINES = [
    "Want to try this on a budget too? Full guide's on our website.",
    "If you want to recreate this for cheap, the full guide's on our website.",
    "Want the budget version? Full guide's on our website.",
]
MUSIC_DIR = "music"  # optional: drop royalty-free .mp3 tracks here to enable background music


def pick_background_music():
    """
    Returns a random .mp3 path from MUSIC_DIR, or None if the folder
    doesn't exist or is empty — background music is optional polish, so a
    missing folder should never break a run, just fall back to
    voice-only (which is exactly today's behavior until music is added).
    """
    if not os.path.isdir(MUSIC_DIR):
        return None
    tracks = [os.path.join(MUSIC_DIR, f) for f in os.listdir(MUSIC_DIR) if f.lower().endswith(".mp3")]
    return random.choice(tracks) if tracks else None


def synthesize_voiceover(script_text, out_path, voice=None):
    """
    Converts the reel script to speech via Microsoft Edge-TTS (free,
    unlimited, no API key). Randomly picks one of a few natural female
    American voices (REEL_VOICES) when none is specified — this niche's
    audience skews heavily female, and a female voice fits it better than
    a male one. The newer "MultilingualNeural" voices (Ava, Emma) sound
    noticeably less robotic/more expressive than the older classic Neural
    voices, so those are favored, with JennyNeural (also one of the more
    natural classic voices) as a third option for variety.

    Also nudges the delivery to sound less flat/robotic: slightly slower
    than default (-4%, reads as more deliberate/natural for how-to content
    rather than rushed) and a small per-run random pitch offset (so
    back-to-back videos using the same voice don't all sound identically
    monotone). Writes an mp3 to out_path.
    """
    voice = voice or random.choice(REEL_VOICES)
    pitch_offset = random.randint(-15, 5)  # Hz
    print(f"Using voice: {voice} (rate=-4%, pitch={pitch_offset:+d}Hz)")

    async def _run():
        communicate = edge_tts.Communicate(
            script_text, voice, rate="-4%", pitch=f"{pitch_offset:+d}Hz"
        )
        await communicate.save(out_path)

    asyncio.run(_run())


def get_audio_duration_seconds(path):
    """Reads a media file's duration (seconds, float) via ffprobe."""
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(res.stdout.strip())


def split_script_into_captions(script_text, n_parts):
    """
    Splits the voiceover script into n_parts caption chunks, breaking on
    sentence boundaries (not mid-sentence) so each on-screen caption reads
    as a complete thought. Falls back to even word-count chunks if there
    aren't enough sentences to fill n_parts groups.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", script_text.strip()) if s.strip()]
    if len(sentences) >= n_parts:
        groups = [[] for _ in range(n_parts)]
        for i, sentence in enumerate(sentences):
            idx = min(i * n_parts // len(sentences), n_parts - 1)
            groups[idx].append(sentence)
        return [" ".join(g).strip() or sentences[min(i, len(sentences) - 1)] for i, g in enumerate(groups)]
    else:
        words = script_text.split()
        per = max(1, len(words) // n_parts)
        chunks = [" ".join(words[i:i + per]) for i in range(0, len(words), per)]
        while len(chunks) < n_parts:
            chunks.append(chunks[-1] if chunks else script_text)
        return chunks[:n_parts]


def build_reel_video(image_specs, audio_path, work_dir, width=1080, height=1920):
    """
    Builds a vertical MP4 (default 1080x1920, 9:16 — Instagram/Facebook's
    required reel shape; pass width=1080, height=1620 for a 2:3 version,
    which is Pinterest's own best-performing ratio) from real project
    images (hero + section photos + a closing CTA card), each with a slow
    Ken Burns zoom, a synced on-screen caption, and short fade in/out
    transitions — narrated by an AI voiceover (see synthesize_voiceover)
    instead of being silent.

    `image_specs` is a list of dicts:
      {"bytes": <image bytes>, "caption": str or None, "duration": seconds}
    Durations should already sum to ~the voiceover's length (the caller
    computes this from get_audio_duration_seconds + word-weighted splits).

    Called twice per video-mode run — once at the default 9:16 for
    Instagram Reels/Facebook video, once at 2:3 for the Pinterest video
    pin (Pinterest fully supports 9:16 too, but 2:3 is its own officially
    best-performing ratio, and forcing the 9:16 file into a 2:3 pin
    container was the cause of the black letterboxing bars). Returns the
    final MP4 bytes. Raises on any ffmpeg failure (caller decides the
    fallback).
    """
    os.makedirs(work_dir, exist_ok=True)
    fps = 30
    segment_paths = []
    scale_w, scale_h = width * 3, height * 3  # upscale factor before zoompan, same ratio as the target

    watermark_path = os.path.join(work_dir, "watermark.png")
    with open(watermark_path, "wb") as f:
        f.write(build_watermark_overlay_png(canvas_size=(width, height)))

    for i, spec in enumerate(image_specs):
        img_path = os.path.join(work_dir, f"img_{i}.png")
        with open(img_path, "wb") as f:
            f.write(spec["bytes"])

        duration = max(0.8, spec["duration"])
        frames = max(1, int(round(duration * fps)))
        fade_dur = min(0.3, duration / 4)
        is_cta = bool(spec.get("is_cta"))
        is_first_slide = (i == 0)

        # Zoom rate is calculated PER SLIDE (target zoom reached, right at
        # the slide's own last frame) rather than a fixed rate — a fixed
        # rate reaches its zoom cap early on any longer slide and then
        # visibly freezes/holds still for the remainder, which is exactly
        # what looked "stuck" before. This keeps the pan/zoom continuously
        # moving for the slide's entire on-screen duration, however long
        # or short that slide happens to be.
        target_zoom = 1.15
        zoom_rate = (target_zoom - 1.0) / frames
        zoom_vf = (
            # Real photos rarely come in exactly the 9:16 (0.5625) ratio
            # this video needs — a suitcase photo above was 2:3 (0.667),
            # for example. Forcing "scale=W:H" to an exact target size
            # ignores the source's own ratio and stretches/squishes it.
            # "force_original_aspect_ratio=increase" instead scales up
            # UNIFORMLY until the image at least covers the 9:16 box, then
            # "crop" trims the overflow to the exact box — same idea as
            # object-fit: cover in CSS. No distortion, whatever the
            # source photo's original shape was.
            f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=increase,"
            f"crop={scale_w}:{scale_h},"
            f"zoompan=z='min(zoom+{zoom_rate:.8f},{target_zoom})':d={frames}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps},"
            + ("" if is_first_slide else f"fade=t=in:st=0:d={fade_dur},")
            + f"fade=t=out:st={max(0, duration - fade_dur)}:d={fade_dur}"
        )

        seg_path = os.path.join(work_dir, f"seg_{i}.mp4")
        caption_png_path = None
        if spec.get("caption"):
            caption_png_path = os.path.join(work_dir, f"caption_{i}.png")
            with open(caption_png_path, "wb") as f:
                f.write(build_caption_overlay_png(spec["caption"], is_cta=is_cta))

        # Safe-zone caption position: nudged below the very top edge for
        # normal step captions; the closing CTA card gets its (bigger,
        # bolder) caption centered vertically so it reads as a clear final
        # call-to-action rather than just another step. Either way it
        # stays clear of Instagram/TikTok's own bottom UI chrome
        # (caption/username/audio strip), which is the part most likely to
        # cover on-screen text if it's placed too low.
        caption_y = "H*0.42" if is_cta else "H*0.12"

        if caption_png_path:
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-i", img_path,
                "-i", caption_png_path, "-i", watermark_path, "-t", str(duration),
                "-filter_complex",
                f"[0:v]{zoom_vf}[bg];"
                f"[bg][1:v]overlay=0:{caption_y}[bg2];"
                f"[bg2][2:v]overlay=0:0[out]",
                "-map", "[out]", "-an",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23", seg_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-i", img_path,
                "-i", watermark_path, "-t", str(duration),
                "-filter_complex", f"[0:v]{zoom_vf}[bg];[bg][1:v]overlay=0:0[out]",
                "-map", "[out]", "-an",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23", seg_path,
            ]
        subprocess.run(cmd, check=True, capture_output=True)
        segment_paths.append(seg_path)

    concat_list_path = os.path.join(work_dir, "concat.txt")
    with open(concat_list_path, "w") as f:
        for p in segment_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    silent_video_path = os.path.join(work_dir, "silent.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path,
         "-c:v", "libx264", "-preset", "fast", "-crf", "23", silent_video_path],
        check=True, capture_output=True,
    )

    final_path = os.path.join(work_dir, "final.mp4")
    music_path = pick_background_music()

    if music_path:
        # Loop the track to at least cover the voiceover's length, then mix
        # it in well below the voice (0.12x) so it's felt as ambience, not
        # heard as competing audio — the voice must always stay the clear,
        # dominant track since it carries the actual information.
        print(f"Mixing in background music: {os.path.basename(music_path)}")
        subprocess.run(
            ["ffmpeg", "-y",
             "-i", silent_video_path, "-i", audio_path,
             "-stream_loop", "-1", "-i", music_path,
             "-filter_complex",
             "[2:a]volume=0.12[music];[1:a][music]amix=inputs=2:duration=first:dropout_transition=0[mixed]",
             "-map", "0:v", "-map", "[mixed]",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
             "-shortest", "-movflags", "+faststart",
             final_path],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            ["ffmpeg", "-y",
             "-i", silent_video_path, "-i", audio_path,
             "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
             "-shortest", "-movflags", "+faststart",
             final_path],
            check=True, capture_output=True,
        )

    with open(final_path, "rb") as f:
        return f.read()



def crop_to_ratio(img, target_ratio=2 / 3):
    """
    Center-crops an image to a fixed width:height ratio (default 2:3, Pinterest's
    recommended portrait ratio). Ensures every hero image has identical
    proportions regardless of what shape photo Pexels returned, so link
    previews on Facebook/Instagram/etc. crop it consistently every time.
    """
    w, h = img.size
    current_ratio = w / h
    if current_ratio > target_ratio:
        # Image is too wide for the target ratio — crop the sides.
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        # Image is too tall for the target ratio — crop top/bottom.
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    return img


def add_pin_text(image_bytes, hook_text):
    """Overlay a bold Pinterest-style text banner near the top of the image."""
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img, "RGBA")

    font_size = max(28, int(w * 0.085))
    font = _load_bold_font(font_size)

    wrapped = textwrap.fill(hook_text.upper(), width=16)
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=8, align="center")
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    pad_x, pad_y = 36, 28
    band_top = int(h * 0.05)
    band_bottom = band_top + text_h + pad_y * 2
    draw.rectangle([0, band_top, w, band_bottom], fill=(15, 15, 15, 165))

    x = (w - text_w) / 2 - bbox[0]
    y = band_top + pad_y - bbox[1]
    draw.multiline_text((x, y), wrapped, font=font, fill="white", align="center", spacing=8)

    return img


def finalize_pin_image(raw_image_bytes, hook_text, max_width=1200, quality=78, target_ratio=2 / 3):
    img = Image.open(BytesIO(raw_image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img = crop_to_ratio(img, target_ratio)
    cropped_bytes_io = BytesIO()
    img.save(cropped_bytes_io, format="PNG")  # lossless intermediate before text overlay

    img_with_text = add_pin_text(cropped_bytes_io.getvalue(), hook_text)
    if img_with_text.width > max_width:
        ratio = max_width / img_with_text.width
        img_with_text = img_with_text.resize(
            (max_width, int(img_with_text.height * ratio)), Image.LANCZOS
        )
    out = BytesIO()
    img_with_text.save(out, format="WEBP", quality=quality)
    return out.getvalue()


def build_text_card(lines, size=(1080, 1350), bg_color=(45, 38, 32), accent_color=(176, 141, 87)):
    """
    Builds a plain solid-background slide with centered text — used for the
    Instagram carousel's "quick take" and "link in bio" info slides. No
    photo needed (no extra Pexels call), just PIL drawing text on a card.
    `lines` is a list of (text, is_title) tuples; title lines get a larger
    bold font and an accent-colored underline beneath them.
    """
    img = Image.new("RGB", size, bg_color)
    draw = ImageDraw.Draw(img)

    title_font = _load_bold_font(int(size[0] * 0.09))
    body_font = _load_bold_font(int(size[0] * 0.05))

    blocks = []
    total_h = 0
    for text, is_title in lines:
        font = title_font if is_title else body_font
        wrapped = textwrap.fill(text.upper() if is_title else text, width=18)
        bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=10, align="center")
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        blocks.append((wrapped, font, bbox, w, h, is_title))
        total_h += h + 40

    y = (size[1] - total_h) / 2
    for wrapped, font, bbox, w, h, is_title in blocks:
        x = (size[0] - w) / 2 - bbox[0]
        draw.multiline_text((x, y - bbox[1]), wrapped, font=font, fill="white", align="center", spacing=10)
        if is_title:
            underline_y = y + h + 12
            draw.rectangle(
                [(size[0] - w) / 2, underline_y, (size[0] + w) / 2, underline_y + 4],
                fill=accent_color,
            )
        y += h + 40

    out = BytesIO()
    img.save(out, format="WEBP", quality=82)
    return out.getvalue()


def git_commit_and_push(paths, message, max_attempts=3):
    subprocess.run(["git", "config", "user.email", "auto-blog-bot@users.noreply.github.com"], check=True)
    subprocess.run(["git", "config", "user.name", "auto-blog-bot"], check=True)
    subprocess.run(["git", "add", *paths], check=True)
    result = subprocess.run(["git", "commit", "-m", message])
    if result.returncode != 0:
        # Nothing to commit — not an error, just means these paths had no changes.
        return

    for attempt in range(1, max_attempts + 1):
        push_result = subprocess.run(["git", "push"])
        if push_result.returncode == 0:
            return
        is_last_attempt = attempt == max_attempts
        if is_last_attempt:
            raise RuntimeError("git push failed after retries — see logs above for git's error output.")
        print(f"git push failed (attempt {attempt}/{max_attempts}), "
              f"pulling latest changes and retrying...")
        # Unshallow first: this repo is checked out with fetch-depth=1 for
        # speed, but `git pull --rebase` needs real history to rebase onto
        # and can fail unpredictably on a shallow clone. This only costs
        # extra time on the rare occasion a retry is actually needed (e.g.
        # a manual run overlapping the scheduled one), never on a normal run.
        subprocess.run(["git", "fetch", "--unshallow"], check=False)
        subprocess.run(["git", "pull", "--rebase"], check=True)


def get_access_token():
    """Google/Blogger access token, refreshed from the stored Google refresh token."""
    res = robust_request(
        "POST", "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": GOOGLE_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(f"Could not refresh Google access token: {res.text}")
    return res.json()["access_token"]


def publish_post(access_token, title, html, labels, search_description=None):
    payload = {"title": title, "content": html, "labels": labels}
    if search_description:
        # Blogger's "search description" becomes the page's meta description
        # (and og:description), which is what shows up as the snippet in
        # Google search results — without this, Blogger just guesses one.
        payload["searchDescription"] = search_description[:150]
    res = robust_request(
        "POST", f"https://www.googleapis.com/blogger/v3/blogs/{BLOGGER_BLOG_ID}/posts/",
        headers={"Authorization": f"Bearer {access_token}"},
        json=payload,
        timeout=60,
    )
    if not res.ok:
        raise RuntimeError(f"Blogger publish failed ({res.status_code}): {res.text}")
    return res.json()


def update_github_secret(secret_name, secret_value):
    """
    Updates a GitHub Actions repository secret via the API, so tokens that
    rotate (like Pinterest's refresh_token) never need manual copy-pasting.
    Requires GH_SECRETS_PAT (a fine-grained PAT scoped to this repo with
    "Secrets: read and write"). If that's not set, this just prints instead —
    it never raises, so a missing PAT never breaks the actual publish run.
    """
    if not GH_SECRETS_PAT:
        print(f"GH_SECRETS_PAT not set — could not auto-update {secret_name}. "
              f"New value (update it manually):")
        print(secret_value)
        return

    try:
        from nacl import encoding, public

        headers = {
            "Authorization": f"Bearer {GH_SECRETS_PAT}",
            "Accept": "application/vnd.github+json",
        }

        key_res = robust_request(
            "GET", f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/public-key",
            headers=headers, timeout=30,
        )
        key_res.raise_for_status()
        key_data = key_res.json()

        public_key = public.PublicKey(key_data["key"].encode("utf-8"), encoding.Base64Encoder())
        sealed_box = public.SealedBox(public_key)
        encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
        encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

        put_res = robust_request(
            "PUT", f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/{secret_name}",
            headers=headers,
            json={"encrypted_value": encrypted_b64, "key_id": key_data["key_id"]},
            timeout=30,
        )
        if put_res.status_code in (201, 204):
            print(f"Auto-updated GitHub secret: {secret_name}")
        else:
            print(f"Failed to auto-update {secret_name} ({put_res.status_code}): {put_res.text}")
    except Exception as e:
        print(f"Could not auto-update {secret_name} (new value below, update manually): {e}")
        print(secret_value)


def get_pinterest_access_token():
    """
    Pinterest access token, refreshed from the stored Pinterest refresh token.
    Runs fresh every time this script runs, so the 30-day access-token expiry
    never matters — only the refresh token's own (longer) expiry does.

    If Pinterest ever returns a *new* refresh_token in the response (some
    providers rotate it), this prints a warning so you know to update the
    PINTEREST_REFRESH_TOKEN GitHub secret manually.
    """
    basic_auth = base64.b64encode(
        f"{PINTEREST_APP_ID}:{PINTEREST_APP_SECRET}".encode()
    ).decode()

    res = robust_request(
        "POST", "https://api.pinterest.com/v5/oauth/token",
        headers={
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": PINTEREST_REFRESH_TOKEN,
        },
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(f"Could not refresh Pinterest access token: {res.text}")

    data = res.json()
    new_refresh_token = data.get("refresh_token")
    if new_refresh_token and new_refresh_token != PINTEREST_REFRESH_TOKEN:
        print("Pinterest issued a new refresh_token — updating GitHub secret...")
        update_github_secret("PINTEREST_REFRESH_TOKEN", new_refresh_token)

    return data["access_token"]


def build_pin_hashtags(labels, max_tags=5):
    """Turns article labels into hashtags, e.g. 'thrift flip' -> '#ThriftFlip'."""
    tags = []
    for label in labels[:max_tags]:
        tag = re.sub(r"[^a-zA-Z0-9 ]", "", label).title().replace(" ", "")
        if tag and f"#{tag}" not in tags:
            tags.append(f"#{tag}")
    return " ".join(tags)


def extract_pin_description(html, hashtags="", max_length=500, cta=""):
    """
    Pulls plain text from the article's opening <p> (the hook paragraph)
    to use as the Pinterest/Facebook/Instagram description — a genuine
    excerpt of the content, not just a repeat of the title or the on-image
    text overlay. Always ends with "...", whether it was truncated for
    length or not, then an optional CTA line (matching the "Visit our
    website" line already used in the Facebook/Instagram captions — added
    here too for consistency), then hashtags (if provided) — all within
    the length limit.
    """
    match = re.search(r"<p>(.*?)</p>", html, re.IGNORECASE | re.DOTALL)
    text = match.group(1) if match else html
    text = re.sub(r"<[^>]+>", "", text)  # strip any remaining HTML tags
    text = re.sub(r"\s+", " ", text).strip()

    cta_part = f"\n\n{cta}" if cta else ""
    suffix = f"{cta_part}\n\n{hashtags}" if hashtags else cta_part
    # Reserve room for the "..." ending plus the CTA/hashtag suffix.
    excerpt_limit = max_length - len(suffix) - 3
    if len(text) > excerpt_limit:
        text = text[:excerpt_limit].rsplit(" ", 1)[0]
    text = text.rstrip(" .…") + "..."
    return text + suffix


def create_pinterest_pin(access_token, board_id, title, description, link, image_url):
    res = robust_request(
        "POST", "https://api.pinterest.com/v5/pins",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={
            "board_id": board_id,
            "title": title[:100],
            "description": description[:500],
            "link": link,
            "media_source": {
                "source_type": "image_url",
                "url": image_url,
            },
        },
        timeout=60,
    )
    if not res.ok:
        raise RuntimeError(f"Pinterest pin creation failed ({res.status_code}): {res.text}")
    return res.json()


def create_pinterest_video_pin(access_token, board_id, title, description, link,
                                video_bytes, cover_image_url):
    """
    Creates a Pinterest VIDEO pin (used only for RUN_TYPE=video runs).
    Unlike the image pin (which just points at a URL), Pinterest's video
    pins require directly uploading the file bytes in three steps:
      1. Register an upload -> get a media_id + presigned upload_url/fields
      2. POST the video bytes to that presigned URL
      3. Poll the media_id until Pinterest finishes processing it
    Then the pin itself references that media_id. Raises on failure — the
    caller wraps this in try/except like the image pin call already is.
    """
    register_res = robust_request(
        "POST", "https://api.pinterest.com/v5/media",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"media_type": "video"},
        timeout=30,
    )
    if not register_res.ok:
        raise RuntimeError(f"Pinterest media registration failed ({register_res.status_code}): {register_res.text}")
    media_info = register_res.json()
    media_id = media_info["media_id"]
    upload_url = media_info["upload_url"]
    upload_fields = media_info["upload_parameters"]

    upload_res = requests.post(
        upload_url, data=upload_fields, files={"file": ("video.mp4", video_bytes)}, timeout=120,
    )
    if not upload_res.ok:
        raise RuntimeError(f"Pinterest video upload failed ({upload_res.status_code}): {upload_res.text}")

    for attempt in range(15):
        time.sleep(8)
        status_res = robust_request(
            "GET", f"https://api.pinterest.com/v5/media/{media_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        status = status_res.json().get("status") if status_res.ok else None
        print(f"Pinterest video processing status (attempt {attempt + 1}/15): {status}")
        if status == "succeeded":
            break
        if status == "failed":
            raise RuntimeError("Pinterest video processing failed (status=failed).")
    else:
        raise RuntimeError("Pinterest video never finished processing in time.")

    pin_res = robust_request(
        "POST", "https://api.pinterest.com/v5/pins",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={
            "board_id": board_id,
            "title": title[:100],
            "description": description[:500],
            "link": link,
            "media_source": {
                "source_type": "video_id",
                "cover_image_url": cover_image_url,
                "media_id": media_id,
            },
        },
        timeout=60,
    )
    if not pin_res.ok:
        raise RuntimeError(f"Pinterest video pin creation failed ({pin_res.status_code}): {pin_res.text}")
    return pin_res.json()


def submit_url_for_indexing(url):
    """
    Tell Google to (re)crawl this URL now, via the Indexing API, using the
    service-account key stored in GOOGLE_INDEXING_KEY. Never raises — if this
    fails or isn't configured, the post is still published and still gets
    indexed eventually via the normal sitemap crawl, just slower.
    """
    if not GOOGLE_INDEXING_KEY:
        print("GOOGLE_INDEXING_KEY not set — skipping instant indexing "
              "(post will still be found via the sitemap eventually).")
        return

    try:
        key_info = json.loads(GOOGLE_INDEXING_KEY)
        credentials = service_account.Credentials.from_service_account_info(
            key_info, scopes=["https://www.googleapis.com/auth/indexing"]
        )
        credentials.refresh(GoogleAuthRequest())

        res = robust_request(
            "POST", "https://indexing.googleapis.com/v3/urlNotifications:publish",
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Content-Type": "application/json",
            },
            json={"url": url, "type": "URL_UPDATED"},
            timeout=30,
        )
        if res.ok:
            print("Submitted to Google Indexing API:", url)
        else:
            print(f"Indexing API call failed ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"Indexing API submission failed (post still published fine): {e}")


def submit_to_bing(url):
    """
    Tell Bing to (re)crawl this URL now, via the Bing Webmaster Submission
    API, using the site-linked apikey stored in BING_API_KEY. Bing's index
    also backs Yahoo and DuckDuckGo, so this one call effectively notifies
    all three. Never raises — if this fails or isn't configured, the post
    is still published and still gets indexed eventually on Bing's normal
    sitemap crawl, just slower.
    """
    if not BING_API_KEY:
        print("BING_API_KEY not set — skipping Bing instant indexing "
              "(post will still be found via the sitemap eventually).")
        return

    try:
        res = robust_request(
            "POST",
            f"https://ssl.bing.com/webmaster/api.svc/json/SubmitUrl?apikey={BING_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={"siteUrl": SITE_URL, "url": url},
            timeout=30,
        )
        if res.ok:
            print("Submitted to Bing Submission API:", url)
        else:
            print(f"Bing Submission API call failed ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"Bing Submission API call failed (post still published fine): {e}")


def check_meta_token_health():
    """
    Quick pre-flight check for the Facebook/Instagram Page Access Token,
    run before attempting to post. Catches an expired/invalidated token
    early with a clear, actionable message — instead of only finding out
    via a buried OAuthException deep in the Facebook/Instagram post calls.
    """
    if not FACEBOOK_PAGE_ACCESS_TOKEN:
        print("[token health] FACEBOOK_PAGE_ACCESS_TOKEN not set — Facebook/Instagram will be skipped.")
        return False
    try:
        res = requests.get(
            "https://graph.facebook.com/me",
            params={"fields": "id,name", "access_token": FACEBOOK_PAGE_ACCESS_TOKEN},
            timeout=15,
        )
        if res.ok:
            print(f"[token health] Facebook/Instagram Page token OK ({res.json().get('name')}).")
            return True
        print(f"[token health] Facebook/Instagram Page token looks INVALID: {res.text}")
        print("[token health] Fix: Graph API Explorer -> generate a new User Token with the usual "
              "7 permissions -> Extend Access Token in the Access Token Debugger -> "
              "run me/accounts?fields=name,access_token,instagram_business_account with that extended "
              "token -> copy the returned access_token into BOTH FACEBOOK_PAGE_ACCESS_TOKEN and "
              "INSTAGRAM_ACCESS_TOKEN secrets.")
        return False
    except Exception as e:
        print(f"[token health] Could not verify Facebook/Instagram token: {e}")
        return False


def post_to_facebook_page(message, image_url, link):
    """
    Posts a native photo to the Facebook Page (reusing the same 4:5 image
    made for Instagram — also Facebook's own recommended feed ratio as of
    2026), then adds the blog link as a follow-up comment. `message`
    (built by the caller) uses a plain CTA phrase rather than the raw URL,
    since a literal link in the caption text costs organic reach even
    without using the API's "link" field — the actual clickable URL lives
    only in the comment, which is guaranteed visible since it's the post's
    first (usually only) comment, even though programmatically PINNING a
    comment isn't reliably supported by the Graph API.
    Never raises — if this fails or isn't configured, the post is still
    published everywhere else fine. Returns True/False for the dashboard.
    """
    if not FACEBOOK_PAGE_ID or not FACEBOOK_PAGE_ACCESS_TOKEN:
        print("FACEBOOK_PAGE_ID / FACEBOOK_PAGE_ACCESS_TOKEN not set — skipping Facebook post.")
        return False

    try:
        res = robust_request(
            "POST", f"https://graph.facebook.com/v26.0/{FACEBOOK_PAGE_ID}/photos",
            data={
                "url": image_url,
                "caption": message,
                "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
            },
            timeout=30,
        )
        if not res.ok:
            print(f"Facebook post failed ({res.status_code}): {res.text}")
            return False

        result = res.json()
        post_id = result.get("post_id") or result.get("id")
        print("Posted to Facebook:", post_id)

        # Also add the link as a comment — wrapped separately so a comment
        # failure doesn't undo the fact that the photo post itself (with
        # the link already in its caption) already succeeded.
        try:
            comment_res = robust_request(
                "POST", f"https://graph.facebook.com/v26.0/{post_id}/comments",
                data={"message": link, "access_token": FACEBOOK_PAGE_ACCESS_TOKEN},
                timeout=30,
            )
            if comment_res.ok:
                print("Added link comment:", comment_res.json().get("id"))
            else:
                print(f"Facebook link-comment failed ({comment_res.status_code}): {comment_res.text}")
        except Exception as e:
            print(f"Facebook link-comment failed (post itself is still published fine): {e}")

        return True
    except Exception as e:
        print(f"Facebook post failed (blog post is still published fine): {e}")
        return False


def post_to_tumblr(title, intro, total_cost, time_estimate, difficulty,
                    image_url, link, hashtags):
    """
    Posts a richly-formatted native post to the Tumblr blog via Tumblr's
    official OAuth 1.0a API, using proper Neue Post Format blocks (heading,
    paragraph, a bulleted "Quick Take" checklist, a styled link block, and
    hashtags) instead of one flat text blob — matches how well-performing
    decor/DIY Tumblr accounts structure their posts.
    Never raises — if this fails or isn't configured, the post is still
    published everywhere else fine.
    """
    if not all([TUMBLR_CONSUMER_KEY, TUMBLR_CONSUMER_SECRET,
                TUMBLR_ACCESS_TOKEN, TUMBLR_ACCESS_TOKEN_SECRET, TUMBLR_BLOG_NAME]):
        print("Tumblr credentials not fully set — skipping Tumblr post.")
        return False

    try:
        oauth = OAuth1Session(
            TUMBLR_CONSUMER_KEY,
            client_secret=TUMBLR_CONSUMER_SECRET,
            resource_owner_key=TUMBLR_ACCESS_TOKEN,
            resource_owner_secret=TUMBLR_ACCESS_TOKEN_SECRET,
        )
        content = [
            {"type": "image", "media": [{"url": image_url}]},
            {"type": "text", "text": f"✨ {title} ✨", "subtype": "heading1"},
            {"type": "text", "text": intro},
            {"type": "text", "text": "Quick Take:", "subtype": "heading2"},
            {"type": "text", "text": f"💰 Cost: {total_cost}", "subtype": "unordered-list-item"},
            {"type": "text", "text": f"⏱️ Time: {time_estimate}", "subtype": "unordered-list-item"},
            {"type": "text", "text": f"📊 Difficulty: {difficulty}", "subtype": "unordered-list-item"},
            {"type": "link", "url": link, "display_url": link,
             "title": "Read the Full Post on DecorVibe"},
            {"type": "text", "text": hashtags},
        ]
        res = oauth.post(
            f"https://api.tumblr.com/v2/blog/{TUMBLR_BLOG_NAME}/posts",
            json={"content": content},
            timeout=30,
        )
        if res.ok:
            print("Posted to Tumblr:", res.json().get("response", {}).get("id"))
            return True
        else:
            print(f"Tumblr post failed ({res.status_code}): {res.text}")
            return False
    except Exception as e:
        print(f"Tumblr post failed (blog post is still published fine): {e}")
        return False
def post_facebook_video(description, video_url, link):
    """
    Posts a native video to the Facebook Page (used only for RUN_TYPE=video
    runs) — this is a plain video post, NOT the clickable link-card that
    post_to_facebook_page() makes, so it's posted as an ADDITIONAL post
    alongside the usual link post rather than replacing it, to avoid losing
    the click-through traffic the link card drives. Also adds the blog link
    as a comment (same as the image-mode post), since a native video post's
    description text isn't clickable either.
    Never raises — returns True/False for the dashboard.
    """
    if not FACEBOOK_PAGE_ID or not FACEBOOK_PAGE_ACCESS_TOKEN:
        print("FACEBOOK_PAGE_ID / FACEBOOK_PAGE_ACCESS_TOKEN not set — skipping Facebook video post.")
        return False
    try:
        res = robust_request(
            "POST", f"https://graph.facebook.com/v26.0/{FACEBOOK_PAGE_ID}/videos",
            data={
                "file_url": video_url,
                "description": description,
                "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
            },
            timeout=120,
        )
        if not res.ok:
            print(f"Facebook video post failed ({res.status_code}): {res.text}")
            return False

        post_id = res.json().get("id")
        print("Posted Facebook video:", post_id)

        # Also add the link as a comment — wrapped separately so a comment
        # failure doesn't undo the fact that the video post itself already
        # succeeded. Native video posts process ASYNCHRONOUSLY on
        # Facebook's side (the id above comes back before the video is
        # fully attached to a commentable post), so the very first attempt
        # can land too early and get rejected even though the post is
        # completely fine — retrying with a short wait fixes that.
        #
        # IMPORTANT: this retry must be idempotent. robust_request() already
        # retries on its own on a network timeout/5xx — and posting a
        # comment isn't idempotent (each successful POST creates a NEW
        # comment), so if a POST actually succeeded on Facebook's side but
        # the response back to us was lost (timeout), a naive retry posts
        # a SECOND copy. That's exactly what was causing the double-comment
        # (and, on the runs where every attempt genuinely failed,
        # missing-comment) inconsistency. So before each retry, check
        # whether our comment is already there first, instead of just
        # blindly posting again.
        comment_posted = False
        for attempt in range(4):
            if attempt > 0:
                wait_s = 10 * attempt  # 10s, 20s, 30s
                print(f"Checking/retrying link comment in {wait_s}s (attempt {attempt + 1}/4)...")
                time.sleep(wait_s)
                try:
                    existing = robust_request(
                        "GET", f"https://graph.facebook.com/v26.0/{post_id}/comments",
                        params={"access_token": FACEBOOK_PAGE_ACCESS_TOKEN},
                        timeout=30,
                    )
                    if existing.ok and any(
                        c.get("message") == link for c in existing.json().get("data", [])
                    ):
                        print("Link comment already present from an earlier attempt — not re-posting.")
                        comment_posted = True
                        break
                except Exception as e:
                    print(f"Couldn't check for an existing comment ({e}) — trying to post anyway.")
            try:
                comment_res = robust_request(
                    "POST", f"https://graph.facebook.com/v26.0/{post_id}/comments",
                    data={"message": link, "access_token": FACEBOOK_PAGE_ACCESS_TOKEN},
                    timeout=30,
                )
                if comment_res.ok:
                    print("Added link comment:", comment_res.json().get("id"))
                    comment_posted = True
                    break
                else:
                    print(f"Facebook video link-comment attempt {attempt + 1} failed "
                          f"({comment_res.status_code}): {comment_res.text}")
            except Exception as e:
                print(f"Facebook video link-comment attempt {attempt + 1} failed: {e}")
        if not comment_posted:
            print("Giving up on the Facebook video link comment (video post still published fine).")

        return True
    except Exception as e:
        print(f"Facebook video post failed (blog post is still published fine): {e}")
        return False


def post_to_instagram(caption, image_url):
    """
    Creates an Instagram media container from a public image URL, then
    publishes it. Uses the Facebook Graph API with a Page Access Token
    (non-expiring). Never raises — if this fails or isn't configured, the
    post is still published everywhere else fine. Returns True/False so the
    caller can record status for the dashboard.
    """
    if not INSTAGRAM_ACCOUNT_ID or not INSTAGRAM_ACCESS_TOKEN:
        print("INSTAGRAM_ACCOUNT_ID / INSTAGRAM_ACCESS_TOKEN not set — skipping Instagram post.")
        return False

    try:
        create_res = robust_request(
            "POST", f"https://graph.facebook.com/v26.0/{INSTAGRAM_ACCOUNT_ID}/media",
            data={
                "image_url": image_url,
                "caption": caption,
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
            timeout=60,
        )
        if not create_res.ok:
            print(f"Instagram media creation failed ({create_res.status_code}): {create_res.text}")
            return False
        creation_id = create_res.json()["id"]

        # Give Instagram a moment to finish processing the image before publishing.
        time.sleep(10)

        publish_res = robust_request(
            "POST", f"https://graph.facebook.com/v26.0/{INSTAGRAM_ACCOUNT_ID}/media_publish",
            data={"creation_id": creation_id, "access_token": INSTAGRAM_ACCESS_TOKEN},
            timeout=60,
        )
        if publish_res.ok:
            print("Posted to Instagram:", publish_res.json().get("id"))
            return True
        else:
            print(f"Instagram publish failed ({publish_res.status_code}): {publish_res.text}")
            return False
    except Exception as e:
        print(f"Instagram post failed (blog post is still published fine): {e}")
        return False


def _create_ig_carousel_child(image_url):
    """Creates one carousel slide's media container (no caption on children)."""
    res = robust_request(
        "POST", f"https://graph.facebook.com/v26.0/{INSTAGRAM_ACCOUNT_ID}/media",
        data={
            "image_url": image_url,
            "is_carousel_item": "true",
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=60,
    )
    if not res.ok:
        raise RuntimeError(f"Carousel child creation failed ({res.status_code}): {res.text}")
    return res.json()["id"]


def post_to_instagram_carousel(caption, image_urls):
    """
    Posts a multi-slide Instagram carousel (2-10 public image URLs). Falls
    back to a single-image post via post_to_instagram() using the first
    image if fewer than 2 URLs are given, or if anything in the carousel
    flow fails — so a carousel hiccup never costs the Instagram post
    entirely, same philosophy as the rest of this script's social posting.
    """
    if not INSTAGRAM_ACCOUNT_ID or not INSTAGRAM_ACCESS_TOKEN:
        print("INSTAGRAM_ACCOUNT_ID / INSTAGRAM_ACCESS_TOKEN not set — skipping Instagram post.")
        return False

    if len(image_urls) < 2:
        return post_to_instagram(caption, image_urls[0]) if image_urls else False

    try:
        child_ids = []
        for url in image_urls:
            child_ids.append(_create_ig_carousel_child(url))
            time.sleep(2)

        # Let all children finish processing before assembling the carousel.
        time.sleep(8)

        parent_res = robust_request(
            "POST", f"https://graph.facebook.com/v26.0/{INSTAGRAM_ACCOUNT_ID}/media",
            data={
                "media_type": "CAROUSEL",
                "children": ",".join(child_ids),
                "caption": caption,
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
            timeout=60,
        )
        if not parent_res.ok:
            raise RuntimeError(f"Carousel container failed ({parent_res.status_code}): {parent_res.text}")
        creation_id = parent_res.json()["id"]

        time.sleep(10)

        publish_res = robust_request(
            "POST", f"https://graph.facebook.com/v26.0/{INSTAGRAM_ACCOUNT_ID}/media_publish",
            data={"creation_id": creation_id, "access_token": INSTAGRAM_ACCESS_TOKEN},
            timeout=60,
        )
        if not publish_res.ok:
            raise RuntimeError(f"Carousel publish failed ({publish_res.status_code}): {publish_res.text}")

        print("Posted Instagram carousel:", publish_res.json().get("id"))
        return True
    except Exception as e:
        print(f"Instagram carousel failed ({e}), falling back to single-image post...")
        return post_to_instagram(caption, image_urls[0])


def post_instagram_reel(caption, video_url):
    """
    Posts a Reel (used only for RUN_TYPE=video runs), replacing the usual
    carousel for that run. Reels take longer to process than images, so
    this polls the container's status_code until FINISHED (capped attempts)
    before publishing, instead of a fixed sleep like the image/carousel
    paths use. Never raises — returns True/False for the dashboard.
    """
    if not INSTAGRAM_ACCOUNT_ID or not INSTAGRAM_ACCESS_TOKEN:
        print("INSTAGRAM_ACCOUNT_ID / INSTAGRAM_ACCESS_TOKEN not set — skipping Instagram Reel.")
        return False
    try:
        create_res = robust_request(
            "POST", f"https://graph.facebook.com/v26.0/{INSTAGRAM_ACCOUNT_ID}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
            timeout=60,
        )
        if not create_res.ok:
            print(f"Instagram Reel container failed ({create_res.status_code}): {create_res.text}")
            return False
        creation_id = create_res.json()["id"]

        # Poll for processing to finish (video takes longer than an image).
        for attempt in range(15):
            time.sleep(10)
            status_res = robust_request(
                "GET", f"https://graph.facebook.com/v26.0/{creation_id}",
                params={"fields": "status_code", "access_token": INSTAGRAM_ACCESS_TOKEN},
                timeout=30,
            )
            status_code = status_res.json().get("status_code") if status_res.ok else None
            print(f"Reel processing status (attempt {attempt + 1}/15): {status_code}")
            if status_code == "FINISHED":
                break
            if status_code == "ERROR":
                print("Instagram Reel processing failed (ERROR status).")
                return False
        else:
            print("Instagram Reel never finished processing in time — skipping publish.")
            return False

        publish_res = robust_request(
            "POST", f"https://graph.facebook.com/v26.0/{INSTAGRAM_ACCOUNT_ID}/media_publish",
            data={"creation_id": creation_id, "access_token": INSTAGRAM_ACCESS_TOKEN},
            timeout=60,
        )
        if publish_res.ok:
            print("Posted Instagram Reel:", publish_res.json().get("id"))
            return True
        else:
            print(f"Instagram Reel publish failed ({publish_res.status_code}): {publish_res.text}")
            return False
    except Exception as e:
        print(f"Instagram Reel failed (blog post is still published fine): {e}")
        return False


STATUS_FILE = "status.json"


def send_phone_notification(subject, body):
    """
    Emails a short run summary to your own inbox via Gmail SMTP (App
    Password), so a push notification shows up on your phone through the
    Gmail app. Never raises — a notification failure should never break
    or fail the actual posting run.
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD or not NOTIFY_EMAIL:
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set — skipping phone notification.")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = NOTIFY_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, [NOTIFY_EMAIL], msg.as_string())
        print("Phone notification email sent.")
    except Exception as e:
        print(f"Could not send phone notification (non-fatal): {e}")


def save_status(blogger_ok, blogger_url, facebook_ok, pinterest_ok, instagram_ok, tumblr_ok=False):
    """
    Writes a small status.json the control panel reads to show a simple
    green-tick/red-cross per platform for the most recent run, with when
    it happened — instead of parsing raw workflow logs.
    """
    now = datetime.now(timezone.utc).isoformat()
    status = {
        "blogger": {"success": blogger_ok, "url": blogger_url, "timestamp": now},
        "facebook": {"success": facebook_ok, "timestamp": now},
        "pinterest": {"success": pinterest_ok, "timestamp": now},
        "instagram": {"success": instagram_ok, "timestamp": now},
        "tumblr": {"success": tumblr_ok, "timestamp": now},
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)
    return status


def main():
    config = load_config()
    niche = config.get("niche", DEFAULT_NICHE)

    global TEXT_MODEL
    TEXT_MODEL = config.get("text_model", TEXT_MODEL)

    history = load_history()

    try:
        print(f"Niche: {niche}")
        print("Asking Gemini for a topic + article...")
        draft = generate_draft(history, niche)
        draft = normalize_draft(draft)
        print("Topic chosen:", draft["title"])

        category = draft["category"]

        # Every post gets exactly ONE Blogger label: its category. This keeps
        # the breadcrumb, the thumbnail badge, and the nav menu always in
        # sync — no second "style" label (e.g. "Thrift Flip") that could make
        # the breadcrumb show something other than the category a visitor
        # just clicked into.
        #
        # Social hashtags are kept separate and richer on purpose: they use
        # the category PLUS Gemini's descriptive "hashtag_tags" (e.g. "thrift
        # flip", "diy"), so Pinterest/Instagram hashtag variety doesn't drop
        # just because Blogger's on-site labeling was simplified.
        draft["hashtag_labels"] = [category] + [t for t in draft["hashtag_tags"] if t != category]
        draft["labels"] = [category]
        print("Category:", category)

        # Quick-take fields — normalize_draft() already guarantees these are
        # plain, non-empty strings. Raw values go into the image slide (PIL
        # just draws plain text); escaped versions go into the HTML box.
        plain_word_count = len(re.sub(r"<[^>]+>", " ", draft["html"]).split())
        reading_minutes = max(1, round(plain_word_count / 200))
        total_cost_raw = draft["total_cost"]
        time_estimate_raw = draft["time_estimate"]
        difficulty_raw = draft["difficulty"]
        total_cost = html.escape(total_cost_raw)
        time_estimate = html.escape(time_estimate_raw)
        difficulty = html.escape(difficulty_raw)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        os.makedirs("images", exist_ok=True)

        # Photos used in previous posts, so this run can pick different ones
        # (two posts with similar queries would otherwise land on the exact
        # same photo, which reads as duplicate spam on Pinterest). Tracked
        # per-run in history; older entries simply have no photo_ids key.
        used_photo_ids = set()
        for h in history:
            used_photo_ids.update(h.get("photo_ids", []))
        this_run_photo_ids = []

        # --- Hero image (vertical, with the Pinterest text hook baked in) ---
        print("Finding hero (Pinterest) photo...")
        raw_hero, hero_photo_id = search_pexels_image(
            draft["image_prompt"], orientation="portrait", used_photo_ids=used_photo_ids
        )
        this_run_photo_ids.append(hero_photo_id)
        used_photo_ids.add(hero_photo_id)
        pin_hook = draft.get("pin_hook", draft["title"])
        hero_compressed = finalize_pin_image(raw_hero, pin_hook)
        hero_filename = f"decor-{ts}-hero.webp"
        hero_filepath = os.path.join("images", hero_filename)
        with open(hero_filepath, "wb") as f:
            f.write(hero_compressed)
        hero_url = upload_to_r2(hero_filepath)
        print(f"Hero image compressed to {len(hero_compressed) / 1024:.1f} KB")

        # --- Instagram-optimized image (4:5, Instagram's recommended feed
        # ratio) — cropped from the same source photo, with its own text
        # overlay sized/positioned for this canvas rather than just cropping
        # the already-finished 2:3 hero (which would risk cutting the banner).
        print("Preparing Instagram-optimized image (4:5)...")
        ig_compressed = finalize_pin_image(raw_hero, pin_hook, target_ratio=4 / 5)
        ig_filename = f"decor-{ts}-instagram.webp"
        ig_filepath = os.path.join("images", ig_filename)
        with open(ig_filepath, "wb") as f:
            f.write(ig_compressed)
        ig_image_url = upload_to_r2(ig_filepath)
        print(f"Instagram image compressed to {len(ig_compressed) / 1024:.1f} KB")

        # --- Quick-take card (used two ways): a carousel slide in
        # image-mode, or one of the slideshow slides in video-mode. Built
        # once either way — no extra Pexels call, just drawn text.
        ig_quicktake_compressed = build_text_card([
            ("Quick Take", True),
            (f"Cost: {total_cost_raw}", False),
            (f"Time: {time_estimate_raw}", False),
            (f"Difficulty: {difficulty_raw}", False),
        ])
        ig_slide2_filename = f"decor-{ts}-ig-quicktake.webp"
        ig_slide2_filepath = os.path.join("images", ig_slide2_filename)
        with open(ig_slide2_filepath, "wb") as f:
            f.write(ig_quicktake_compressed)
        ig_slide2_url = upload_to_r2(ig_slide2_filepath)

        # --- CTA card (used two ways): last slide of the image-mode
        # carousel, OR the closing slide of the video-mode slideshow.
        # Built once either way — no extra Pexels call, just drawn text.
        ig_cta_compressed = build_text_card([
            ("Want The Full Guide", True),
            ("Tap the link in our bio", False),
            ("for the full step-by-step", False),
        ])
        ig_slide4_filename = f"decor-{ts}-ig-cta.webp"
        ig_slide4_filepath = os.path.join("images", ig_slide4_filename)
        with open(ig_slide4_filepath, "wb") as f:
            f.write(ig_cta_compressed)
        ig_slide4_url = upload_to_r2(ig_slide4_filepath)

        ig_slide3_filepath = None
        if RUN_TYPE != "video":
            # --- Image-mode (default/morning run): the 3rd carousel slide
            # (a second hook photo, reusing raw_hero — no extra Pexels call).
            ig_slide3_compressed = finalize_pin_image(
                raw_hero, "See The Full Tutorial", target_ratio=4 / 5
            )
            ig_slide3_filename = f"decor-{ts}-ig-tutorial.webp"
            ig_slide3_filepath = os.path.join("images", ig_slide3_filename)
            with open(ig_slide3_filepath, "wb") as f:
                f.write(ig_slide3_compressed)
            ig_slide3_url = upload_to_r2(ig_slide3_filepath)
            print("Instagram carousel slides ready.")

        # Facebook now reuses the same hero image used on Pinterest/Instagram
        # (see below) — no separate Facebook-specific image is generated
        # anymore, since Facebook posting switched from a link-share (which
        # needed its own landscape preview image) to a native photo post
        # with the blog link moved to a comment.



        # --- Section images (horizontal, no text overlay, one per placeholder) ---
        section_images = draft.get("section_images", [])
        section_urls = {}
        for i, section in enumerate(section_images):
            token = section.get("token", f"IMG_{i+1}")
            query = section.get("query", draft["image_prompt"])
            print(f"Finding section photo for {token}: {query}")
            try:
                raw_section, section_photo_id = search_pexels_image(
                    query, orientation="landscape", used_photo_ids=used_photo_ids
                )
                this_run_photo_ids.append(section_photo_id)
                used_photo_ids.add(section_photo_id)
                section_compressed = compress_image(raw_section)
                section_filename = f"decor-{ts}-{token.lower()}.webp"
                section_filepath = os.path.join("images", section_filename)
                with open(section_filepath, "wb") as f:
                    f.write(section_compressed)
                section_url = upload_to_r2(section_filepath)
                section_urls[token] = (section_url, query)
            except Exception as e:
                # One section photo failing (rare network/API hiccup)
                # shouldn't crash a run where the hero image, Facebook
                # image, and the rest of the article are already done —
                # skip just this section's image; its [[IMG_n]] placeholder
                # gets cleaned up below like any other unmatched token.
                print(f"Section photo for {token} failed, skipping it: {e}")

        reel_video_filepath = None
        pin_cover_filepath = None
        if RUN_TYPE == "video":
            # --- Video-mode: build a narrated vertical video using the
            # SAME real images already generated for the article (hero +
            # section photos + the CTA card) — not generic mismatched stock
            # video clips — each with a slow Ken Burns zoom, synced
            # on-screen captions, and an AI voiceover (Edge-TTS, free)
            # reading Gemini's short reel_script. This matches what the
            # article is actually about and gives the reel real audio
            # instead of being silent.
            print("Video-mode run: synthesizing voiceover...")
            cta_line = random.choice(REEL_CTA_LINES)
            reel_script = f"{draft['reel_script'].strip()} {cta_line}"
            work_dir = os.path.join("images", f"reel-work-{ts}")
            os.makedirs(work_dir, exist_ok=True)
            audio_path = os.path.join(work_dir, "voiceover.mp3")
            synthesize_voiceover(reel_script, audio_path)
            audio_duration = get_audio_duration_seconds(audio_path)
            print(f"Voiceover ready: {audio_duration:.1f}s")

            # Everything except the CTA line (added above, and always the
            # last sentence) becomes the content captions.
            content_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", draft["reel_script"].strip()) if s.strip()]
            content_words = sum(len(s.split()) for s in content_sentences)
            total_words_incl_cta = content_words + len(cta_line.split())
            content_duration_est = audio_duration * (content_words / max(1, total_words_incl_cta))

            # Cap how long any single image can hold the screen — a longer
            # caption used to mean a longer hold on one image, which (even
            # with the zoom fixed above) still reads as "stuck" compared to
            # a normal reel's pacing. Past this cap, use MORE images with
            # shorter holds instead of stretching one out.
            MAX_SLIDE_SECONDS = 3.5
            n_content_slides = max(2, math.ceil(content_duration_est / MAX_SLIDE_SECONDS))

            # Real, VERTICAL images only. The hero is already portrait
            # (search_pexels_image defaults to orientation="portrait"). The
            # article's own section photos are LANDSCAPE (they're made for
            # the horizontal blog layout) — cropping those into a 9:16 reel
            # frame was cutting them down into an oddly narrow, stretched-
            # looking strip. So instead of reusing them here, do fresh
            # portrait-orientation Pexels searches using the same topical
            # queries (still on-topic for this specific post, and still
            # whatever Gemini's own section_images queries described) —
            # cycling through those queries again if more slides are
            # needed than there are distinct queries.
            real_image_bytes = [raw_hero]
            section_queries = [q for _, q in section_urls.values()] or [draft["image_prompt"]]
            qi = 0
            while len(real_image_bytes) < n_content_slides:
                query = section_queries[qi % len(section_queries)]
                qi += 1
                try:
                    raw_bytes, photo_id = search_pexels_image(
                        query, orientation="portrait", used_photo_ids=used_photo_ids,
                        target_ratio=9 / 16,
                    )
                    real_image_bytes.append(raw_bytes)
                    this_run_photo_ids.append(photo_id)
                    used_photo_ids.add(photo_id)
                except Exception as e:
                    print(f"Couldn't fetch an extra portrait image for '{query}': {e}")
                    if qi > len(section_queries) * 3:
                        break  # give up rather than loop forever if Pexels keeps failing

            n_content_slides = len(real_image_bytes)  # actual count, in case fetches came up short
            content_captions = split_script_into_captions(
                " ".join(content_sentences), n_content_slides
            )

            image_specs = [
                {"bytes": b, "caption": c, "duration": 1}  # duration set below
                for b, c in zip(real_image_bytes, content_captions)
            ]
            # A blank branded background (no baked-in text) — NOT
            # ig_cta_compressed, which already has "Want the full guide /
            # link in our bio" text drawn into the image for the Instagram
            # carousel. Reusing that here would double up with the CTA
            # caption overlay below, showing two overlapping messages.
            video_cta_bg = build_text_card([], size=(1080, 1920))
            image_specs.append({"bytes": video_cta_bg, "caption": cta_line, "duration": 1, "is_cta": True})

            # Word-weighted duration so a longer caption gets more screen
            # time than a short one, proportioned to the voiceover's total
            # length — with a floor so no slide flashes by unreadably fast,
            # and the MAX_SLIDE_SECONDS cap so no slide holds too long
            # (that "extra" time is given to the closing CTA card instead,
            # which is fine to sit a little longer).
            word_counts = [max(1, len(spec["caption"].split())) for spec in image_specs]
            total_words = sum(word_counts)
            overflow = 0.0
            for spec, wc in zip(image_specs, word_counts):
                raw_duration = audio_duration * (wc / total_words)
                if spec.get("is_cta"):
                    spec["duration"] = max(1.5, raw_duration)
                else:
                    capped = min(raw_duration, MAX_SLIDE_SECONDS)
                    overflow += raw_duration - capped
                    spec["duration"] = max(1.5, capped)
            image_specs[-1]["duration"] += overflow  # CTA card absorbs the difference

            print(f"Building video from {len(image_specs)} real-image slide(s)...")
            reel_video_bytes = build_reel_video(
                image_specs, audio_path, work_dir=work_dir
            )
            reel_video_filename = f"decor-{ts}-reel.mp4"
            reel_video_filepath = os.path.join("images", reel_video_filename)
            with open(reel_video_filepath, "wb") as f:
                f.write(reel_video_bytes)
            reel_video_url = upload_to_r2(reel_video_filepath)
            print(f"Video ready ({len(reel_video_bytes) / 1024:.0f} KB).")

            # A SECOND video, just for the Pinterest pin — same images,
            # captions, and voiceover, re-rendered at 2:3 instead of 9:16.
            # Pinterest fully supports 9:16 video pins, but 2:3 is its own
            # officially best-performing ratio (and reusing the 9:16 file
            # for both was the underlying cause of the black letterboxing
            # bars Pinterest was adding to reconcile the mismatch with its
            # expected pin shape). The Instagram/Facebook video above is
            # untouched by this — it stays exactly 9:16 as required.
            print("Building a 2:3 version for the Pinterest pin...")
            pinterest_work_dir = os.path.join("images", f"reel-work-pin-{ts}")
            pinterest_video_bytes = build_reel_video(
                image_specs, audio_path, work_dir=pinterest_work_dir,
                width=1080, height=1620,
            )
            print(f"Pinterest video ready ({len(pinterest_video_bytes) / 1024:.0f} KB).")

            # Pinterest's video-pin cover_image_url rejects WebP (every
            # other image in this script is WebP) — it needs JPG/PNG. Also,
            # this MUST be cropped to the exact same 2:3 shape as the
            # Pinterest video built just above (not just the raw hero
            # photo's own shape, whatever Pexels happened to return that
            # as, and not 9:16 either — that mismatch was the cause of the
            # black letterboxing bars in the first place).
            pin_cover_img = Image.open(BytesIO(raw_hero)).convert("RGB")
            pin_cover_img = crop_to_ratio(pin_cover_img, target_ratio=2 / 3)
            pin_cover_img = pin_cover_img.resize((1080, 1620), Image.LANCZOS)
            pin_cover_out = BytesIO()
            pin_cover_img.save(pin_cover_out, format="JPEG", quality=85)
            pin_cover_filename = f"decor-{ts}-pin-cover.jpg"
            pin_cover_filepath = os.path.join("images", pin_cover_filename)
            with open(pin_cover_filepath, "wb") as f:
                f.write(pin_cover_out.getvalue())
            pin_cover_url = upload_to_r2(pin_cover_filepath)

        # Images/video are already uploaded to R2 individually above — no
        # git commit needed for them (that's the whole point of moving off
        # GitHub-hosted images: the repo no longer grows with every post).


        # Section images are real <img> tags with proper alt text — this
        # used to be a CSS background-image div instead, specifically to
        # dodge Pinterest's RSS auto-publish scraper (which pinned every
        # <img> it found in the post body). That RSS feature has since been
        # fully deleted, so the workaround is no longer needed, and a real
        # <img alt="..."> is what actually gets section photos indexed in
        # Google Images (a CSS background-image on a div is invisible to
        # Google's image search).
        body_html = draft["html"]
        for token, (url, query) in section_urls.items():
            alt_text = html.escape(query)
            img_tag = (
                f'<img src="{url}" alt="{alt_text}" loading="lazy" '
                f'style="width:100%;max-width:100%;aspect-ratio:4/3;'
                f'object-fit:cover;border-radius:10px;'
                f'box-shadow:0 2px 10px rgba(0,0,0,0.12);margin:20px 0;" />'
            )
            body_html = re.sub(rf"\[\[{re.escape(token)}\]\]", img_tag, body_html)
        # Remove any leftover placeholders Gemini added without a matching section_images entry.
        body_html = re.sub(r"\[\[IMG_\d+\]\]", "", body_html)

        # --- Quick-take summary box (cost/time/difficulty/reading time) ---
        quick_take_html = (
            '<div style="background:#f7f3ee;border-left:4px solid #b08d57;'
            'padding:15px 20px;margin:15px 0;border-radius:6px;">'
            f'<strong>Quick Take:</strong> Total cost: {total_cost} &bull; '
            f'Time: {time_estimate} &bull; Difficulty: {difficulty} &bull; '
            f'{reading_minutes} min read</div>'
        )

        # --- FAQ section + FAQPage schema (for Google rich-result eligibility) ---
        # normalize_draft() already guarantees this is a clean list of valid
        # {"question", "answer"} dicts (or an empty list) — no re-validation
        # needed here.
        faq_items = draft["faq"]
        faq_html = ""
        faq_schema_html = ""
        if faq_items:
            faq_parts = ["<h2>Frequently Asked Questions</h2>"]
            for item in faq_items:
                q = html.escape(str(item["question"]))
                a = html.escape(str(item["answer"]))
                faq_parts.append(f"<h3>{q}</h3><p>{a}</p>")
            faq_html = "\n".join(faq_parts)

            faq_schema = {
                "@context": "https://schema.org",
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": item["question"],
                        "acceptedAnswer": {"@type": "Answer", "text": item["answer"]},
                    }
                    for item in faq_items
                ],
            }
            faq_schema_html = (
                '<script type="application/ld+json">'
                f"{json.dumps(faq_schema, ensure_ascii=False)}</script>"
            )

        # --- Related posts (same category first, then fill remaining slots
        # with the most recent posts overall so this section is never empty
        # just because a category is new/thin). Still fully static (baked
        # into the HTML at publish time) — no extra JS/network request on
        # page load, so there's no page-speed trade-off.
        same_category = [
            h for h in history
            if h.get("category") == category and h.get("url")
        ]
        related = list(reversed(same_category[-3:]))
        if len(related) < 3:
            related_urls = {h["url"] for h in related}
            most_recent = [
                h for h in reversed(history)
                if h.get("url") and h["url"] not in related_urls
            ]
            related = related + most_recent[: 3 - len(related)]
        related_posts_html = ""
        if related:
            items = "".join(
                f'<li><a href="{h["url"]}">{html.escape(h["title"])}</a></li>'
                for h in related
            )
            related_posts_html = f"<h2>You Might Also Like</h2><ul>{items}</ul>"

        # --- Static author/trust bio (E-E-A-T signal, same on every post) ---
        author_bio_html = (
            '<div style="margin-top:30px;padding-top:20px;border-top:1px solid #ddd;'
            'font-size:0.9em;color:#555;"><strong>About the Author:</strong> '
            "Written by the DecorVibe team — real budget home-decor flips and "
            "thrifted finds, tested and written up so you can recreate them "
            "affordably.</div>"
        )

        # --- Article/BlogPosting schema (JSON-LD) — separate from the FAQ
        # schema above, this is what lets Google show a thumbnail image and
        # published date alongside the search result for the post itself.
        published_iso = datetime.now(timezone.utc).isoformat()
        article_schema = {
            "@context": "https://schema.org",
            "@type": "BlogPosting",
            "headline": draft["title"],
            "image": [hero_url],
            "datePublished": published_iso,
            "dateModified": published_iso,
            "author": {"@type": "Organization", "name": "DecorVibe"},
            "publisher": {"@type": "Organization", "name": "DecorVibe"},
        }
        article_schema_html = (
            '<script type="application/ld+json">'
            f"{json.dumps(article_schema, ensure_ascii=False)}</script>"
        )

        # --- Breadcrumb schema (JSON-LD) — mirrors the visible on-site
        # breadcrumb (Home > Category > Post title). The last item is the
        # current page itself, so it deliberately has no "item" URL (Google's
        # own guidance: the final breadcrumb entry doesn't need one).
        category_url = f"{SITE_URL}/search/label/{urllib.parse.quote(category)}"
        breadcrumb_schema = {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Home", "item": SITE_URL},
                {"@type": "ListItem", "position": 2, "name": category, "item": category_url},
                {"@type": "ListItem", "position": 3, "name": draft["title"]},
            ],
        }
        breadcrumb_schema_html = (
            '<script type="application/ld+json">'
            f"{json.dumps(breadcrumb_schema, ensure_ascii=False)}</script>"
        )

        # The hero image is simply the first <img> in the post — Blogger
        # uses whatever image appears first as the page's og:image, which
        # used to require a separate hidden landscape image specifically
        # for Facebook's link-preview card. That's no longer needed since
        # Facebook posting switched to a native photo post (see below),
        # which doesn't generate a link-preview card at all.
        full_html = (
            f'<img src="{hero_url}" alt="{draft["title"]}" style="max-width:100%;height:auto;" />\n'
            f'{quick_take_html}\n{body_html}\n{related_posts_html}\n{faq_html}\n'
            f'{author_bio_html}\n{faq_schema_html}\n{article_schema_html}\n{breadcrumb_schema_html}'
        )
        social_description = extract_pin_description(draft["html"])

        print("Publishing to Blogger...")
        access_token = get_access_token()
        result = publish_post(
            access_token, draft["title"], full_html, draft.get("labels", []),
            search_description=social_description,
        )
        post_url = result.get("url")
        print("Published:", post_url)
    except Exception as e:
        # Blogger/generation itself failed — nothing got posted anywhere this
        # run. Still record it so the dashboard shows a red cross for today
        # instead of silently keeping yesterday's green tick.
        print(f"Run failed before publishing: {e}")
        try:
            save_status(blogger_ok=False, blogger_url=None, facebook_ok=False, pinterest_ok=False, instagram_ok=False)
            git_commit_and_push([STATUS_FILE], "Auto post: run failed before publishing")
        except Exception as status_err:
            print(f"Could not save failure status: {status_err}")
        send_phone_notification(
            "❌ DecorVibe run FAILED",
            f"The run failed before publishing anything.\n\nError: {e}",
        )
        raise

    print("Notifying Google Indexing API...")
    submit_url_for_indexing(post_url)

    print("Notifying Bing Submission API...")
    submit_to_bing(post_url)

    # Pinterest keeps a modest hashtag count (its own norms lean lighter);
    # Instagram/Facebook use a richer set from the same tag pool, since more
    # hashtags there genuinely helps discovery rather than looking spammy.
    pin_hashtags = build_pin_hashtags(draft.get("hashtag_labels", []), max_tags=5)
    social_hashtags = build_pin_hashtags(draft.get("hashtag_labels", []), max_tags=15)

    meta_token_ok = check_meta_token_health()
    if not meta_token_ok:
        print("Skipping Facebook + Instagram posting this run — see the health check message above.")
        facebook_ok = False
        instagram_ok = False
    elif RUN_TYPE == "video":
        # Video-mode (evening run): a native Facebook video post (not a
        # clickable link-card — accepted trade-off) and an Instagram Reel,
        # instead of the morning run's link-post + carousel.
        print("Posting Facebook video...")
        category_emoji = CATEGORY_EMOJIS.get(category, "🏠")
        fb_message = (
            f"{pin_hook}\n\n{category_emoji} {draft['title']}\n\n{social_description}\n\n"
            f"👉 Visit our website for the full step-by-step guide!\n\n{social_hashtags}"
        )
        facebook_ok = post_facebook_video(fb_message, reel_video_url, post_url)

        print("Posting Instagram Reel...")
        ig_caption = f"{pin_hook}\n\n{category_emoji} {draft['title']}\n\n{social_description}\n\nFull post: link in bio 🔗\n\n{social_hashtags}"
        instagram_ok = post_instagram_reel(ig_caption, reel_video_url)
    else:
        # Image-mode (morning run, default): native photo post reusing the
        # same 4:5 image made for Instagram (Facebook's own recommended
        # feed ratio too, as of 2026). The caption uses a plain CTA phrase
        # instead of the raw URL — a literal link in the caption text still
        # costs reach even without the "link" API field — while the actual
        # clickable link goes in a follow-up comment (see
        # post_to_facebook_page) for guaranteed one-tap access. Captions
        # lead with the same punchy "pin_hook" line used on the image
        # itself — Facebook/Instagram only show the first 1-2 lines before
        # "See more".
        print("Posting to Facebook Page...")
        category_emoji = CATEGORY_EMOJIS.get(category, "🏠")
        fb_message = (
            f"{pin_hook}\n\n{category_emoji} {draft['title']}\n\n{social_description}\n\n"
            f"👉 Visit our website for the full step-by-step guide!\n\n{social_hashtags}"
        )
        facebook_ok = post_to_facebook_page(fb_message, ig_image_url, post_url)

        print("Posting to Instagram (carousel)...")
        ig_caption = f"{pin_hook}\n\n{category_emoji} {draft['title']}\n\n{social_description}\n\nFull post: link in bio 🔗\n\n{social_hashtags}"
        instagram_ok = post_to_instagram_carousel(
            ig_caption, [ig_image_url, ig_slide2_url, ig_slide3_url, ig_slide4_url]
        )

    # History (with URL, for future internal linking) is saved and committed
    # AFTER publishing, now that we actually know the post's URL.
    history.append({
        "title": draft["title"],
        "category": draft.get("category"),
        "date": datetime.now(timezone.utc).isoformat(),
        "url": post_url,
        "photo_ids": this_run_photo_ids,
    })
    save_history(history)

    # Pinterest is posted last and wrapped in try/except on purpose: if this
    # fails for any reason, the Blogger post has already gone live and should
    # NOT be rolled back or treated as a failed run.
    print("Posting to Pinterest...")
    pinterest_ok = False
    try:
        pinterest_token = get_pinterest_access_token()
        board_id = CATEGORY_BOARD_IDS.get(category, PINTEREST_BOARD_ID)
        if RUN_TYPE == "video":
            pin_result = create_pinterest_video_pin(
                pinterest_token,
                board_id=board_id,
                title=draft["title"],
                description=extract_pin_description(
                    draft["html"], hashtags=pin_hashtags,
                    cta="👉 Visit our website for the full step-by-step guide!",
                ),
                link=post_url,
                video_bytes=pinterest_video_bytes,
                cover_image_url=pin_cover_url,
            )
        else:
            pin_result = create_pinterest_pin(
                pinterest_token,
                board_id=board_id,
                title=draft["title"],
description=extract_pin_description(
                    draft["html"], hashtags=pin_hashtags,
                    cta="👉 Visit our website for the full step-by-step guide!",
                ),
                link=post_url,
                image_url=hero_url,
            )
        print("Pinned:", pin_result.get("id"), "-> board:", board_id)
        pinterest_ok = True
    except Exception as e:
        print(f"Pinterest post failed (blog post is still published fine): {e}")

    print("Posting to Tumblr...")
    tumblr_ok = post_to_tumblr(
        title=draft["title"],
        intro=social_description,
        total_cost=draft["total_cost"],
        time_estimate=draft["time_estimate"],
        difficulty=draft["difficulty"],
        image_url=hero_url,
        link=post_url,
        hashtags=social_hashtags,
    )

    save_status(
        blogger_ok=True, blogger_url=post_url,
        facebook_ok=facebook_ok, pinterest_ok=pinterest_ok, instagram_ok=instagram_ok,
        tumblr_ok=tumblr_ok,
    )
    print("Committing history + status...")
    git_commit_and_push([HISTORY_FILE, STATUS_FILE], f"Auto post history: {draft['title']}")

    def tick(ok):
        return "✅" if ok else "❌"

    send_phone_notification(
        f"{tick(True)} DecorVibe posted: {draft['title'][:60]}",
        f"{draft['title']}\n{post_url}\n\n"
        f"Blogger: {tick(True)}\n"
        f"Facebook: {tick(facebook_ok)}\n"
        f"Instagram: {tick(instagram_ok)}\n"
        f"Pinterest: {tick(pinterest_ok)}\n"
        f"Tumblr: {tick(tumblr_ok)}",
    )

    # Clean up everything in R2 that was only ever needed to get through
    # THIS run's posting (Facebook/Instagram/Pinterest/Tumblr have all
    # fetched their own copies by now) — keeps R2 storage from growing
    # forever, especially now that video-mode uploads a couple of MB per
    # run instead of a few KB. hero_url is deliberately NOT included:
    # Blogger's post keeps embedding that exact URL permanently.
    print("Cleaning up temporary R2 files...")
    for path in [ig_filepath, ig_slide2_filepath, ig_slide3_filepath,
                 ig_slide4_filepath, reel_video_filepath, pin_cover_filepath]:
        delete_from_r2(path)


if __name__ == "__main__":
    main()
