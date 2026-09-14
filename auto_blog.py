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
import json
import html
import base64
import subprocess
import textwrap
import random
import time
import smtplib
from email.mime.text import MIMEText
from io import BytesIO
from datetime import datetime, timezone

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest
from PIL import Image, ImageDraw, ImageFont

# ---- Required secrets / env vars (set these as GitHub Actions secrets) ----
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BLOGGER_BLOG_ID = os.environ["BLOGGER_BLOG_ID"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]

# Service-account JSON (full contents) for the Google Indexing API — lets us
# tell Google to (re)crawl a new post immediately instead of waiting for it
# to be discovered via the sitemap on its own schedule.
GOOGLE_INDEXING_KEY = os.environ.get("GOOGLE_INDEXING_KEY")

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

# Controls which social-posting mode this run uses. Set via the GitHub
# Actions workflow so the 6:30 AM trigger passes RUN_TYPE=image (current
# carousel/link/image-pin behavior) and the 6:30 PM trigger passes
# RUN_TYPE=video (Reel/native-video/video-pin behavior). Defaults to
# "image" so nothing changes if the workflow doesn't set it.
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


def generate_draft(history, niche):
    recent_titles = [h["title"] for h in history[-50:]]

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

    prompt = f"""You are a real person who runs a {niche} blog and personally writes every
post. You've done these projects yourself, in your own home, on a real budget.
Posts are shared to Pinterest automatically the moment they're published, so
the opening line has to earn a click — then the article has to actually
deliver, like a friend explaining exactly how they did something.

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
  the vertical HERO photo (this is the one shown on Pinterest) — e.g.
  "thrifted glass vase living room". No brand names, no people's faces, no
  text. This field must always be present in your JSON response.
- "section_images": a list matching your [[IMG_n]] placeholders, each with a
  "token" (e.g. "IMG_1") and a "query" (3-5 keyword search terms for a real,
  horizontal photo matching that section of the article — no people's faces,
  no text).

Return ONLY valid JSON. No markdown fences, no commentary before or after.
{{
  "title": "a specific, honest, clickable title",
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
  ]
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


def search_pexels_image(query, orientation="portrait"):
    res = robust_request(
        "GET", "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "orientation": orientation, "per_page": 15},
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(f"Pexels search failed ({res.status_code}): {res.text}")

    photos = res.json().get("photos", [])
    if not photos:
        res = robust_request(
            "GET", "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": "home decor", "orientation": orientation, "per_page": 15},
            timeout=30,
        )
        if not res.ok:
            raise RuntimeError(f"Pexels fallback search failed ({res.status_code}): {res.text}")
        photos = res.json().get("photos", [])
        if not photos:
            raise RuntimeError(f"No Pexels photos found for query: {query}")

    photo = random.choice(photos)
    image_url = photo["src"]["large2x"]
    image_res = robust_request("GET", image_url, timeout=30)
    if not image_res.ok:
        raise RuntimeError(f"Pexels image download failed ({image_res.status_code})")
    return image_res.content


def search_pexels_video(query, orientation="portrait", min_duration=3, max_duration=20):
    """
    Finds a real Pexels stock video clip matching `query` (generic b-roll,
    not footage of this specific fictional project — same honesty scope as
    the stock photos already used elsewhere in this script) and downloads
    the smallest file that's still at least 720p, to keep runs fast.
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


def build_hook_overlay_png(text, width=1080):
    """
    Renders the hook text as a transparent PNG with proper word-wrapping
    and a semi-transparent background bar sized to fit the wrapped text —
    reuses the same wrapping approach as build_text_card(), so long hooks
    wrap onto multiple lines instead of overflowing past the frame edges
    (which is what a raw ffmpeg drawtext string did before this fix).
    """
    font = _load_bold_font(58)
    dummy_img = Image.new("RGBA", (width, 10), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dummy_img)
    wrapped = textwrap.fill(text.upper(), width=22)
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=12, align="center")
    text_h = bbox[3] - bbox[1]

    pad_v, pad_h = 30, 40
    bar_h = text_h + pad_v * 2
    img = Image.new("RGBA", (width, bar_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (width, bar_h)], fill=(0, 0, 0, 140))
    text_w = bbox[2] - bbox[0]
    x = (width - text_w) / 2 - bbox[0]
    draw.multiline_text((x, pad_v - bbox[1]), wrapped, font=font, fill="white",
                         align="center", spacing=12)

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def build_reel_video(clip_bytes_list, hook_text, cta_card_bytes, work_dir,
                      clip_duration=4):
    """
    Stitches 2-4 raw Pexels stock video clips + a static CTA card into one
    vertical (1080x1920) MP4 short, with the pin_hook text overlaid (as a
    pre-wrapped PNG, not raw ffmpeg drawtext — see build_hook_overlay_png)
    on the first clip. Used for Instagram Reels, Facebook video, and
    Pinterest video pins — same file, three destinations. Returns the
    final MP4 bytes. Raises on any ffmpeg failure (caller decides fallback).
    """
    os.makedirs(work_dir, exist_ok=True)
    segment_paths = []

    hook_png_path = os.path.join(work_dir, "hook_overlay.png")
    with open(hook_png_path, "wb") as f:
        f.write(build_hook_overlay_png(hook_text))

    for i, clip_bytes in enumerate(clip_bytes_list):
        raw_path = os.path.join(work_dir, f"raw_{i}.mp4")
        with open(raw_path, "wb") as f:
            f.write(clip_bytes)

        trimmed_path = os.path.join(work_dir, f"seg_{i}.mp4")
        base_vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30"
        if i == 0:
            # Composite the pre-wrapped hook PNG onto the scaled/cropped
            # clip via overlay — this is what fixes the text getting cut
            # off at the frame edges (drawtext had no word-wrap).
            cmd = [
                "ffmpeg", "-y", "-i", raw_path, "-i", hook_png_path,
                "-t", str(clip_duration),
                "-filter_complex",
                f"[0:v]{base_vf}[bg];[bg][1:v]overlay=0:H*0.08[out]",
                "-map", "[out]", "-an",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23", trimmed_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", raw_path, "-t", str(clip_duration),
                "-vf", base_vf, "-an",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23", trimmed_path,
            ]
        subprocess.run(cmd, check=True, capture_output=True)
        segment_paths.append(trimmed_path)

    # CTA end-card: turn the static "link in bio" card into a video clip.
    cta_img_path = os.path.join(work_dir, "cta.png")
    with open(cta_img_path, "wb") as f:
        f.write(cta_card_bytes)
    cta_video_path = os.path.join(work_dir, "seg_cta.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loop", "1", "-i", cta_img_path, "-t", "3",
         "-vf", "scale=1080:1920,fps=30", "-an",
         "-c:v", "libx264", "-preset", "fast", "-crf", "23", cta_video_path],
        check=True, capture_output=True,
    )
    segment_paths.append(cta_video_path)

    # Concatenate all segments, and add a silent audio track — Instagram
    # Reels / Facebook expect an audio stream present even if it's silent.
    concat_list_path = os.path.join(work_dir, "concat.txt")
    with open(concat_list_path, "w") as f:
        for p in segment_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    final_path = os.path.join(work_dir, "final.mp4")
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "concat", "-safe", "0", "-i", concat_list_path,
         "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
         "-shortest",
         "-c:v", "libx264", "-preset", "fast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
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


def extract_pin_description(html, hashtags="", max_length=500):
    """
    Pulls plain text from the article's opening <p> (the hook paragraph)
    to use as the Pinterest/Facebook/Instagram description — a genuine
    excerpt of the content, not just a repeat of the title or the on-image
    text overlay. Always ends with "..." (a "read more" cue), whether it
    was truncated for length or not. Hashtags (if provided) are appended
    after that, within the length limit.
    """
    match = re.search(r"<p>(.*?)</p>", html, re.IGNORECASE | re.DOTALL)
    text = match.group(1) if match else html
    text = re.sub(r"<[^>]+>", "", text)  # strip any remaining HTML tags
    text = re.sub(r"\s+", " ", text).strip()

    suffix = f" {hashtags}" if hashtags else ""
    # Reserve room for the "..." ending plus the hashtag suffix.
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


def post_to_facebook_page(message, link):
    """
    Posts a link to the Facebook Page's feed — the whole preview card
    (image + headline) is clickable straight through to the blog post,
    which matters more here than exact image control since driving clicks
    is the whole point. Facebook builds the card from the page's og:image,
    which we control separately via a hidden square image placed first in
    the post's HTML (see main()).
    Never raises — if this fails or isn't configured, the post is still
    published everywhere else fine. Returns True/False for the dashboard.
    """
    if not FACEBOOK_PAGE_ID or not FACEBOOK_PAGE_ACCESS_TOKEN:
        print("FACEBOOK_PAGE_ID / FACEBOOK_PAGE_ACCESS_TOKEN not set — skipping Facebook post.")
        return False

    try:
        res = robust_request(
            "POST", f"https://graph.facebook.com/v26.0/{FACEBOOK_PAGE_ID}/feed",
            data={
                "message": message,
                "link": link,
                "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
            },
            timeout=30,
        )
        if res.ok:
            print("Posted to Facebook:", res.json().get("id"))
            return True
        else:
            print(f"Facebook post failed ({res.status_code}): {res.text}")
            return False
    except Exception as e:
        print(f"Facebook post failed (blog post is still published fine): {e}")
        return False


def post_facebook_video(description, video_url):
    """
    Posts a native video to the Facebook Page (used only for RUN_TYPE=video
    runs) — this is a plain video post, NOT the clickable link-card that
    post_to_facebook_page() makes, so it's posted as an ADDITIONAL post
    alongside the usual link post rather than replacing it, to avoid losing
    the click-through traffic the link card drives.
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
        if res.ok:
            print("Posted Facebook video:", res.json().get("id"))
            return True
        else:
            print(f"Facebook video post failed ({res.status_code}): {res.text}")
            return False
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


def save_status(blogger_ok, blogger_url, facebook_ok, pinterest_ok, instagram_ok):
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
        committed_paths = []

        # --- Hero image (vertical, with the Pinterest text hook baked in) ---
        print("Finding hero (Pinterest) photo...")
        raw_hero = search_pexels_image(draft["image_prompt"], orientation="portrait")
        pin_hook = draft.get("pin_hook", draft["title"])
        hero_compressed = finalize_pin_image(raw_hero, pin_hook)
        hero_filename = f"decor-{ts}-hero.webp"
        hero_filepath = os.path.join("images", hero_filename)
        with open(hero_filepath, "wb") as f:
            f.write(hero_compressed)
        committed_paths.append(hero_filepath)
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
        committed_paths.append(ig_filepath)
        print(f"Instagram image compressed to {len(ig_compressed) / 1024:.1f} KB")

        # --- CTA card (used two ways): last slide of the image-mode
        # carousel, OR the closing segment of the video-mode Reel/video.
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
        committed_paths.append(ig_slide4_filepath)

        reel_video_filepath = None
        if RUN_TYPE == "video":
            # --- Video-mode: fetch 3 real Pexels stock clips (generic
            # topic-matching b-roll, same honesty scope as the stock photos
            # used everywhere else in this script) and stitch them + the
            # CTA card into one vertical Reel/video via ffmpeg. 3 clips (was
            # 2) for more visual variety — a 2-clip video felt too sparse.
            print("Video-mode run: fetching Pexels stock video clips...")
            clip_queries = [draft["image_prompt"]] + [
                s.get("query", draft["image_prompt"]) for s in draft.get("section_images", [])
            ][:2]
            clip_bytes_list = []
            for q in clip_queries[:3]:
                try:
                    clip_bytes_list.append(search_pexels_video(q))
                except Exception as e:
                    print(f"Pexels video search failed for '{q}': {e}")
            if not clip_bytes_list:
                # Last-resort generic query, so a run never fails purely
                # because one specific search came back empty.
                clip_bytes_list.append(search_pexels_video("home decor"))

            print(f"Building Reel/video from {len(clip_bytes_list)} clip(s)...")
            reel_video_bytes = build_reel_video(
                clip_bytes_list, pin_hook, ig_cta_compressed,
                work_dir=os.path.join("images", f"reel-work-{ts}"),
            )
            reel_video_filename = f"decor-{ts}-reel.mp4"
            reel_video_filepath = os.path.join("images", reel_video_filename)
            with open(reel_video_filepath, "wb") as f:
                f.write(reel_video_bytes)
            committed_paths.append(reel_video_filepath)
            print(f"Reel/video ready ({len(reel_video_bytes) / 1024:.0f} KB).")

            # Pinterest's video-pin cover_image_url rejects WebP (every
            # other image in this script is WebP) — it needs JPG/PNG. Build
            # a one-off JPEG copy of the hero image just for this.
            pin_cover_img = Image.open(BytesIO(raw_hero)).convert("RGB")
            pin_cover_out = BytesIO()
            pin_cover_img.save(pin_cover_out, format="JPEG", quality=85)
            pin_cover_filename = f"decor-{ts}-pin-cover.jpg"
            pin_cover_filepath = os.path.join("images", pin_cover_filename)
            with open(pin_cover_filepath, "wb") as f:
                f.write(pin_cover_out.getvalue())
            committed_paths.append(pin_cover_filepath)
        else:
            # --- Image-mode (default/morning run): the usual 2 extra
            # carousel slides (quick-take card + second hook photo).
            print("Preparing Instagram carousel slides (quick-take + CTA cards)...")
            ig_slide2_compressed = build_text_card([
                ("Quick Take", True),
                (f"Cost: {total_cost_raw}", False),
                (f"Time: {time_estimate_raw}", False),
                (f"Difficulty: {difficulty_raw}", False),
            ])
            ig_slide2_filename = f"decor-{ts}-ig-quicktake.webp"
            ig_slide2_filepath = os.path.join("images", ig_slide2_filename)
            with open(ig_slide2_filepath, "wb") as f:
                f.write(ig_slide2_compressed)
            committed_paths.append(ig_slide2_filepath)

            ig_slide3_compressed = finalize_pin_image(
                raw_hero, "See The Full Tutorial", target_ratio=4 / 5
            )
            ig_slide3_filename = f"decor-{ts}-ig-tutorial.webp"
            ig_slide3_filepath = os.path.join("images", ig_slide3_filename)
            with open(ig_slide3_filepath, "wb") as f:
                f.write(ig_slide3_compressed)
            committed_paths.append(ig_slide3_filepath)
            print("Instagram carousel slides ready.")

        # --- Facebook-optimized image (1.91:1 landscape — Facebook's actual
        # recommended link-preview ratio). Cropping the tall portrait hero
        # down to this ratio would leave only a thin strip, so we fetch a
        # genuinely landscape source photo instead, with its own text overlay.
        print("Preparing Facebook-optimized image (1.91:1)...")
        raw_fb = search_pexels_image(draft["image_prompt"], orientation="landscape")
        fb_compressed = finalize_pin_image(raw_fb, pin_hook, target_ratio=1.91)
        fb_filename = f"decor-{ts}-fb.webp"
        fb_filepath = os.path.join("images", fb_filename)
        with open(fb_filepath, "wb") as f:
            f.write(fb_compressed)
        committed_paths.append(fb_filepath)
        print(f"Facebook image compressed to {len(fb_compressed) / 1024:.1f} KB")



        # --- Section images (horizontal, no text overlay, one per placeholder) ---
        section_images = draft.get("section_images", [])
        section_urls = {}
        for i, section in enumerate(section_images):
            token = section.get("token", f"IMG_{i+1}")
            query = section.get("query", draft["image_prompt"])
            print(f"Finding section photo for {token}: {query}")
            raw_section = search_pexels_image(query, orientation="landscape")
            section_compressed = compress_image(raw_section)
            section_filename = f"decor-{ts}-{token.lower()}.webp"
            section_filepath = os.path.join("images", section_filename)
            with open(section_filepath, "wb") as f:
                f.write(section_compressed)
            committed_paths.append(section_filepath)
            section_urls[token] = (
                f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{section_filepath}",
                query,
            )

        print("Committing images to the repo...")
        git_commit_and_push(committed_paths, f"Auto post images: {draft['title']}")
        time.sleep(8)

        hero_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{hero_filepath}"
        ig_image_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{ig_filepath}"
        if RUN_TYPE == "video":
            reel_video_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{reel_video_filepath}"
            pin_cover_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{pin_cover_filepath}"
        else:
            ig_slide2_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{ig_slide2_filepath}"
            ig_slide3_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{ig_slide3_filepath}"
            ig_slide4_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{ig_slide4_filepath}"
        fb_image_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{fb_filepath}"


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

        # Hidden square image placed FIRST so it becomes the page's og:image
        # (Blogger uses the first <img> in the post body for that) — this is
        # what Facebook's link-share card shows. display:none keeps it
        # invisible to actual readers, who see only the normal hero below.
        hidden_og_img = f'<img src="{fb_image_url}" alt="" style="display:none;" />\n'
        full_html = (
            hidden_og_img +
            f'<img src="{hero_url}" alt="{draft["title"]}" style="max-width:100%;height:auto;" />\n'
            f'{quick_take_html}\n{body_html}\n{related_posts_html}\n{faq_html}\n'
            f'{author_bio_html}\n{faq_schema_html}'
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
        fb_message = f"{pin_hook}\n\n{draft['title']}\n\n{social_description}\n\n{social_hashtags}"
        facebook_ok = post_facebook_video(fb_message, reel_video_url)

        print("Posting Instagram Reel...")
        ig_caption = f"{pin_hook}\n\n{draft['title']}\n\n{social_description}\n\nFull post: link in bio 🔗\n\n{social_hashtags}"
        instagram_ok = post_instagram_reel(ig_caption, reel_video_url)
    else:
        # Image-mode (morning run, default): the usual clickable link-post
        # + carousel. Captions lead with the same punchy "pin_hook" line
        # used on the image itself — Facebook/Instagram only show the first
        # 1-2 lines before "See more", so the hook belongs first.
        print("Posting to Facebook Page...")
        fb_message = f"{pin_hook}\n\n{draft['title']}\n\n{social_description}\n\n{social_hashtags}"
        facebook_ok = post_to_facebook_page(fb_message, post_url)

        print("Posting to Instagram (carousel)...")
        ig_caption = f"{pin_hook}\n\n{draft['title']}\n\n{social_description}\n\nFull post: link in bio 🔗\n\n{social_hashtags}"
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
            with open(reel_video_filepath, "rb") as f:
                reel_video_bytes_for_pin = f.read()
            pin_result = create_pinterest_video_pin(
                pinterest_token,
                board_id=board_id,
                title=draft["title"],
                description=extract_pin_description(draft["html"], hashtags=pin_hashtags),
                link=post_url,
                video_bytes=reel_video_bytes_for_pin,
                cover_image_url=pin_cover_url,
            )
        else:
            pin_result = create_pinterest_pin(
                pinterest_token,
                board_id=board_id,
                title=draft["title"],
                description=extract_pin_description(draft["html"], hashtags=pin_hashtags),
                link=post_url,
                image_url=hero_url,
            )
        print("Pinned:", pin_result.get("id"), "-> board:", board_id)
        pinterest_ok = True
    except Exception as e:
        print(f"Pinterest post failed (blog post is still published fine): {e}")

    save_status(
        blogger_ok=True, blogger_url=post_url,
        facebook_ok=facebook_ok, pinterest_ok=pinterest_ok, instagram_ok=instagram_ok,
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
        f"Pinterest: {tick(pinterest_ok)}",
    )


if __name__ == "__main__":
    main()
