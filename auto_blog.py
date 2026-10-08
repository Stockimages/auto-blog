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
from PIL import Image, ImageDraw, ImageFont

# ---- Required secrets / env vars (set these as GitHub Actions secrets) ----
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BLOGGER_BLOG_ID = os.environ["BLOGGER_BLOG_ID"]
SITE_URL = "https://decorvibeto.com"
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]
# Optional second photo source, used when every Pexels candidate is rejected.
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")


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


try:
    import video_template   # the black-bars/yellow-words template (video_template.py next to this file)
except ImportError:
    video_template = None


# Voice: Gemini "Zephyr" first, Edge-TTS as automatic fallback; VOICE_ENGINE=edge skips Gemini.
USE_GEMINI_VOICE = video_template is not None and os.environ.get("VOICE_ENGINE", "gemini").strip().lower() != "edge"


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
    everything that was only ever needed ONCE — Pinterest fetches the
    file from its R2 URL and keeps its own copy, so once posting is done there's nothing left pointing at it.
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
if RUN_TYPE == "video" and video_template is None:
    print("video_template.py is missing — running as an image run instead of a video run.")
    RUN_TYPE = "image"

# Model name — Google updates these periodically. If a run starts failing
# with a 404 "model not found" error, check the current name in Google AI
# Studio and update below (or set GEMINI_TEXT_MODEL as an env var/config value).
TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.8-flash")

# If TEXT_MODEL is overloaded/unavailable across all its retries, we fall
# back through these proven models in order rather than failing the run.
FALLBACK_TEXT_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.7-flash")
FALLBACK_TEXT_MODEL_2 = os.environ.get("GEMINI_FALLBACK_MODEL_2", "gemini-3.6-flash")
FALLBACK_TEXT_MODEL_3 = os.environ.get("GEMINI_FALLBACK_MODEL_3", "gemini-3.5-flash")
FALLBACK_TEXT_MODEL_4 = os.environ.get("GEMINI_FALLBACK_MODEL_4", "gemini-3.5-flash-lite")
FALLBACK_TEXT_MODEL_5 = os.environ.get("GEMINI_FALLBACK_MODEL_5", "gemini-3.1-flash-lite")

HISTORY_FILE = "topics_history.json"
MAX_TOPIC_ATTEMPTS = 4  # tries to get a non-duplicate topic before skipping the run
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


# Words that appear in nearly every title on this blog (the format itself),
# so they must not count as "same topic" evidence when comparing titles.
_TITLE_FILLER = {
    "a", "an", "the", "and", "or", "of", "to", "into", "in", "on", "for", "with",
    "your", "you", "my", "i", "this", "that", "it", "its", "is", "are", "how",
    "turn", "make", "made", "making", "diy", "thrifted", "thrift", "store",
    "vintage", "flip", "makeover", "look", "looks", "like", "now", "new",
    "cheap", "budget", "easy", "simple", "under", "from", "that", "one",
    "weekend", "hour", "hours", "step", "guide", "ideas", "idea", "real",
    "heres", "here", "spent", "only", "just", "about", "best", "way",
    # seasonal / list-post words that many different posts legitimately share
    "ways", "tips", "things", "mistakes", "hacks", "secrets", "decor", "decorating",
    "home", "fall", "autumn", "cozy", "winter", "summer", "spring", "christmas",
    "holiday", "holidays", "halloween", "thanksgiving", "easter", "season", "seasonal",
    # words of the post FORMATS themselves ("... in 4 Simple Layers" is a template, not a topic)
    "layer", "layers", "simple", "style", "styled", "styling", "elegant", "perfect", "beautiful",
    "stunning", "gorgeous", "ultimate", "complete", "essential",
    "designer", "fraction", "price", "cheap", "expensive", "less", "worth",
}


def _stem(word):
    """Crude singular form, so 'box' and 'boxes', 'candle' and 'candles' match."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("es") and word[:-2].endswith(("x", "s", "z", "ch", "sh")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _title_keywords(title):
    words = re.findall(r"[a-z]+", title.lower().replace("'", ""))
    return {_stem(w) for w in words if w not in _TITLE_FILLER and len(w) > 2}


def find_duplicate_title(title, history, threshold=0.5):
    """
    Returns the existing title that `title` is too similar to, or None.
    Compares topic keywords (the format words every title shares are
    ignored, as are dollar amounts), so "$12 Thrifted Globe into a $450
    Vintage English Terrestrial Globe" is caught as a repeat of the same
    globe project even if the prices or wording change.
    """
    new_kw = _title_keywords(title)
    if not new_kw:
        return None
    for h in history:
        old_title = h.get("title", "")
        old_kw = _title_keywords(old_title)
        if not old_kw:
            continue
        shared = len(new_kw & old_kw)
        overlap = shared / len(new_kw | old_kw)
        # Two shared topic words are needed (or identical one-word topics): a single
        # shared word such as "table" doesn't make a bedside table a Thanksgiving table.
        if (shared >= 2 and overlap >= threshold) or (overlap == 1.0 and shared >= 1):
            return old_title
    return None


# Phrases that claim a personal experience the blog doesn't actually have
# (the posts use stock photos and the projects weren't done by the author).
_EXPERIENCE_CLAIMS = re.compile(
    r"\bI(?:'ve| have)? (?:tried|spent|made|bought|found|paid|painted|built|used|did|picked|"
    r"grabbed|scored|snagged|tested|ended up|was skeptical|learned|started|decided|thought|hated|"
    r"loved)\b"
    r"|\b(?:my|our) (?:husband|wife|partner|kids|son|daughter|mom|dad|home|house|kitchen|"
    r"apartment|garage|basement|porch|dresser|mantel|living room|bedroom|bathroom)\b"
    r"|\bwhen I (?:made|built|did|tried|first)\b"
    r"|\b(?:we|our team)\s+(?:tested|tried|found|spent|made|built|baked|painted|bought)\b"
    r"|\b(?:in|under|after|during)\s+(?:our\s+|real\s+|hands-on\s+)?testing\b"
    r"|\btesting\s+(?:reveals|revealed|shows|showed|proves|proved|confirms|confirmed)\b"
    r"|\bour\s+tests?\b",
    re.IGNORECASE,
)


_BRAND_NAMES = [
    # high-end / designer retailers
    "anthropologie", "studio mcgee", "mcgee & co", "mcgee and co", "west elm", "pottery barn",
    "restoration hardware", "crate & barrel", "crate and barrel", "cb2", "williams sonoma",
    "williams-sonoma", "ballard designs", "serena & lily", "serena and lily", "ethan allen",
    "lulu and georgia", "arhaus", "rh modern",
    # mainstream stores, marketplaces and thrift chains
    "ikea", "walmart", "etsy", "ebay", "wayfair", "home depot", "lowe's", "lowes",
    "hobby lobby", "dollar tree", "dollar general", "salvation army",
    "habitat for humanity", "homegoods", "tj maxx", "tjmaxx",
    "costco", "world market", "kirkland's", "bed bath & beyond", "pier 1",
    "facebook marketplace", "craigslist", "offerup", "poshmark",
    # branded craft / paint / tool products
    "mod podge", "rust-oleum", "rustoleum", "krylon", "annie sloan", "behr", "sherwin-williams",
    "sherwin williams", "benjamin moore", "valspar", "minwax", "varathane", "dremel", "cricut",
    "sharpie", "velcro", "gorilla glue", "elmer's", "e6000", "dixie belle", "general finishes",
    "command strips", "command hooks", "command hook",
]
_BRAND_NAMES_CAPITALIZED = ["Goodwill", "Amazon", "Michaels", "Marshalls", "Joann"]
_BRAND_RE_CAP = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(_BRAND_NAMES_CAPITALIZED) + r")(?![A-Za-z0-9])"
)
_BRAND_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(b) for b in sorted(_BRAND_NAMES, key=len, reverse=True)) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def find_quality_problems(draft, min_words=600):
    """
    Returns a list of reasons this draft shouldn't be published (empty list =
    fine). Prompts are only requests, so these are checked in code, the same
    way duplicate topics are: (1) too thin to be useful, (2) claims of
    personal experience that didn't happen.
    """
    body_text = re.sub(r"<[^>]+>", " ", draft.get("html", ""))
    problems = []
    words = len(body_text.split())
    if words < min_words:
        problems.append(f"too short ({words} words, minimum {min_words})")
    for label, text in (("title", draft.get("title", "")), ("article", body_text)):
        m = _EXPERIENCE_CLAIMS.search(text)
        if m:
            problems.append(f"claims a personal experience in the {label}: \"{m.group(0)}\"")
            break

    # Brand / retailer / product names (a post naming real brands with prices
    # was unpublished by Blogger). Checked everywhere text can end up public.
    searchable = " ".join([
        draft.get("title", ""), body_text, draft.get("pin_hook", "") or "",
        draft.get("pin_description", "") or "", json.dumps(draft.get("faq", []), ensure_ascii=False),
    ])
    brand = _BRAND_RE.search(searchable) or _BRAND_RE_CAP.search(searchable)
    if brand:
        problems.append(f"names a brand/store/product: \"{brand.group(0)}\"")

    # Prices in the title must agree with the article's own table (a title said
    # "$12 Thrifted Plastic Basket" while the table priced the basket at $6).
    title = draft.get("title", "")
    rows = _table_rows(draft.get("html", ""))
    table_amounts = {float(x) for row in rows for cell in row for x in re.findall(r"\$\s?(\d+(?:\.\d+)?)", cell)}
    if table_amounts:
        for m in re.finditer(r"\$\s?(\d+(?:\.\d+)?)\s+thrift(?:ed)?\b", title, re.IGNORECASE):
            n = float(m.group(1))
            if n not in table_amounts:
                problems.append(f"title says \"${n:g} thrifted...\" but the table never shows ${n:g}")
    if draft.get("_format") not in ("listicle", "mistakes and fixes"):
        totals = [float(x) for x in re.findall(r"\$\s?(\d+(?:\.\d+)?)", str(draft.get("total_cost", "")))]
        for m in re.finditer(r"(?:under|below|less than)\s+\$\s?(\d+(?:\.\d+)?)", title, re.IGNORECASE):
            n = float(m.group(1))
            if totals and n < max(totals):
                problems.append(f"title promises \"under ${n:g}\" but the article's total is ${max(totals):g}")
    return problems


TITLE_STYLES = [
    "RESULT-FIRST: lead with the transformation, e.g. \"This $9 Thrifted Lamp Now Looks Like a $300 Designer Piece\"",
    "QUESTION: a curious question the project answers, e.g. \"Can a $12 Thrifted Mirror Really Pass for Antique Brass?\"",
    "COST-LED: lead with the budget, e.g. \"A Hanging Planter From a Thrifted Colander for Under $20\"",
    "PLAIN DIY: a clear search-friendly tutorial title, e.g. \"DIY Aged Brass Boot Tray From a Thrifted Metal Tray\"",
    "BEFORE-AND-AFTER: e.g. \"From Thrift Store Colander to Zinc Planter: A $18 Makeover\"",
    "BUDGET ANGLE: lead with the saving, e.g. \"A $20 Entryway Bench That Looks Like It Cost $400\"",
]


# ---------------------------------------------------------------------------
# Content variety: every post gets a randomly-picked POST FORMAT (so the site
# isn't 100 near-identical tutorials), a seasonal theme matching the time of
# year (Pinterest users plan holidays weeks ahead) about 2 posts in 3, and
# sometimes a decor-style "lens". The last few formats/themes used are
# remembered in topics_history.json so the same one doesn't come up twice in
# a row. NOTE: this uses a seasonal calendar, not live trend data.
# ---------------------------------------------------------------------------
POST_FORMATS = [
    {"name": "project tutorial", "weight": 3, "use_title_styles": True,
     "instruction": "A single start-to-finish thrift-flip or DIY project, explained step by step.",
     "needs_list": True,
     "table_hint": "a materials and cost breakdown with columns Item, Price"},
    {"name": "listicle", "weight": 3, "use_title_styles": False,
     "instruction": "A numbered list post of 5-9 ideas. Each idea gets its own h2/h3 with a concrete tip, a rough cost and why it works. NOT a single step-by-step project and NOT the 'turn X into Y' framing. Costs are PER IDEA: total_cost must be the range of the per-idea costs in your table (for example \"$6-$30 per idea\"), and the closing paragraph must state that same per-idea range, never a single total that contradicts the table.",
     "table_hint": "a summary table with columns Idea, Approx. cost, Time",
     "title_hint": "number-led and specific, e.g. '7 Budget Ways to Make a Small Bedroom Feel Bigger'"},
    {"name": "room refresh plan", "weight": 2, "use_title_styles": False,
     "instruction": "A whole-room (or whole-area) refresh plan on a budget: what to change first, what to skip, and the order to do it in.",
     "table_hint": "a shopping list with columns Item, Where to buy, Price",
     "title_hint": "budget-led, e.g. 'A Cozy Living Room Refresh for Under $150'"},
    {"name": "thrift buying guide", "weight": 2, "use_title_styles": False,
     "instruction": "A guide to buying a type of item second-hand: what to look for, red flags, fair prices, where to find it.",
     "table_hint": "a table with columns What to look for, Fair price, Red flags",
     "title_hint": "practical, e.g. 'What to Look For When Thrifting a Solid Wood Dresser'"},
    {"name": "mistakes and fixes", "weight": 2, "use_title_styles": False,
     "instruction": "Common mistakes that make a room look cheap, cluttered or dated, each paired with a specific, inexpensive fix. Costs are PER FIX: total_cost must be the range of the per-fix costs in your table, and any total you state in the text must equal the sum of the table.",
     "table_hint": "a table with columns Mistake, Quick fix, Cost",
     "title_hint": "e.g. '6 Mistakes That Make Your Entryway Look Cluttered (and How to Fix Them)'. Never use the words 'cheap' twice in the title."},
    {"name": "project walkthrough with pitfalls", "weight": 2, "use_title_styles": True, "needs_list": True,
     "instruction": "A walkthrough of one makeover that is honest about what can go wrong: the common pitfalls at each step, how to avoid them, and what the finished result should look like. Written as a guide ('you'), NOT as a personal story.",
     "table_hint": "a table of what you bought and what it cost, columns Item, Price"},
    {"name": "designer look for less", "weight": 2, "use_title_styles": False,
     "instruction": "Recreate a high-end look with budget or thrifted alternatives. Describe the pricey version in general terms (never name a brand, store or product) and compare it to the budget version piece by piece.",
     "table_hint": "a comparison table with columns High-end version (described, not named), Budget version, Typical savings (a range)",
     "title_hint": "e.g. 'The Designer Mantel Look for a Fraction of the Price'"},
    {"name": "styling guide", "weight": 2, "use_title_styles": False,
     "instruction": "How to style ONE spot (mantel, shelf, entry table, coffee table, nightstand, porch) in simple layers with easy rules a beginner can follow.",
     "table_hint": "a table with columns Layer, What to use, Approx. cost",
     "title_hint": "e.g. 'How to Style a Mantel in 4 Simple Layers'"},
    {"name": "myth-busting", "weight": 1, "use_title_styles": False,
     "instruction": "Examine a popular budget decor trick or product type (chalk paint, peel-and-stick, thrifted rugs, thrifted lamps) and explain honestly, from how the materials generally behave, what usually works, what usually doesn't and why. Do NOT claim anything was tested: never write 'we tested', 'in testing', 'testing revealed' or 'our tests'. Use 'usually', 'often' and 'most people find'.",
     "table_hint": "a table with columns Method, What usually happens, Verdict",
     "title_hint": "a curious question or honest verdict, e.g. 'Does Peel-and-Stick Backsplash Really Last? What Actually Happens Over Time'. Never write the title in the first person (no 'I' or 'my')."},
]

# month -> (themes for right now, themes for roughly the next 4-6 weeks).
# US audience; holidays are planned ahead on Pinterest, so "upcoming" matters.
SEASONAL_THEMES = {
    1: (["New Year home reset and decluttering on a budget", "cozy winter warmth with textiles, candles and lighting", "small-space organization with thrifted baskets and shelves"],
        ["Valentine's Day budget decor", "winter-to-spring refresh"]),
    2: (["Valentine's Day budget decor and tablescapes", "cozy late-winter bedroom refresh"],
        ["spring refresh decor", "Easter and spring table ideas", "St. Patrick's Day simple decor"]),
    3: (["spring refresh on a budget", "spring decluttering and reset", "Easter table and decor ideas"],
        ["Mother's Day gift and styling ideas", "porch and patio prep"]),
    4: (["Easter and spring decor", "fresh spring porch and entryway", "Mother's Day DIY gifts from thrifted finds"],
        ["patio and balcony setup", "Memorial Day and early-summer entertaining"]),
    5: (["patio and balcony makeovers", "Memorial Day outdoor entertaining decor", "graduation and Mother's Day party decor"],
        ["summer living room refresh", "4th of July decor"]),
    6: (["summer porch and patio styling", "4th of July decor on a budget", "light, bright summer living room"],
        ["dorm and back-to-school room ideas", "late-summer outdoor dining"]),
    7: (["4th of July and summer entertaining", "dorm and college apartment decor on a budget", "light airy bedroom to beat the heat"],
        ["early fall transition decor", "back-to-school organization"]),
    8: (["dorm and first-apartment decor", "back-to-school organization nooks", "late-summer to fall transition styling"],
        ["cozy fall decor", "fall porch and entryway"]),
    9: (["cozy fall decor on a budget", "fall porch, entryway and mantel", "autumn tablescapes and candles"],
        ["Halloween decor", "Thanksgiving table"]),
    10: (["Halloween decor that looks expensive but isn't", "cozy fall living room and bedroom", "Thanksgiving table and hosting on a budget"],
         ["Christmas and holiday decor", "DIY holiday gifts from thrifted finds"]),
    11: (["Thanksgiving tablescapes and hosting", "Christmas decor on a budget", "DIY gifts from thrifted finds", "holiday entryway and mantel"],
         ["Christmas tree and mantel styling", "New Year's party decor"]),
    12: (["Christmas decor: trees, mantels and tablescapes", "last-minute DIY gifts", "New Year's Eve party decor", "cozy winter home"],
         ["New Year home reset", "cozy winter warmth"]),
}

# Holiday themes are dropped once their date is too close to still help
# (Google indexes slowly and Pinterest users plan weeks ahead). (month, day)
# is the last day such a theme can still be picked. Easter moves each year,
# so it has no cutoff here.
THEME_EXPIRY = [
    ("valentine", (2, 7)), ("st. patrick", (3, 10)), ("mother's day", (5, 5)),
    ("memorial day", (5, 20)), ("4th of july", (7, 1)), ("halloween", (10, 8)),
    ("thanksgiving", (11, 15)), ("christmas", (12, 15)),
    ("new year's party", (12, 26)), ("new year's eve", (12, 26)),
]


def theme_expired(theme, today):
    t = theme.lower()
    return any(key in t and (today.month, today.day) > cutoff for key, cutoff in THEME_EXPIRY)


# Share of posts that follow the season, by month: highest in Oct-Dec, when
# Pinterest holiday searches (and ad rates, generally) peak; lower in the rest
# of the year so more evergreen posts build up for Google over time.
SEASONAL_SHARE = {1: 0.45, 2: 0.50, 3: 0.50, 4: 0.50, 5: 0.50, 6: 0.50,
                  7: 0.55, 8: 0.60, 9: 0.65, 10: 0.70, 11: 0.70, 12: 0.70}


STYLE_LENSES = [
    "modern farmhouse", "japandi / warm minimalism", "boho", "mid-century modern",
    "french country", "coastal", "cottagecore", "grandmillennial / vintage maximalist",
    "industrial", "scandinavian", "rustic cabin", "eclectic",
]


def pick_post_plan(history, today=None):
    """
    Decides this post's format, seasonal theme and optional style lens.
    Avoids repeating the last 3 formats and the last 8 themes.
    """
    today = today or datetime.now(timezone.utc)

    recent_formats = {h.get("format") for h in history[-3:]}
    pool = [f for f in POST_FORMATS if f["name"] not in recent_formats] or POST_FORMATS
    fmt = random.choices(pool, weights=[f["weight"] for f in pool], k=1)[0]

    theme = None
    if random.random() < SEASONAL_SHARE.get(today.month, 0.6):  # the rest are evergreen
        now_themes, upcoming = SEASONAL_THEMES[today.month]
        used = {h.get("theme") for h in history[-8:]}
        live_now = [t for t in now_themes if not theme_expired(t, today)]
        live_upcoming = [t for t in upcoming if not theme_expired(t, today)]
        # Themes about events 4-8 weeks away are weighted higher: that's when
        # people search for them, and when a new post still has time to rank.
        weighted = live_now * 2 + live_upcoming * 3
        candidates = [t for t in weighted if t not in used] or weighted or live_now + live_upcoming
        theme = random.choice(candidates) if candidates else None

    if theme:
        season_line = (
            f"SEASON / TIMING: today is {today.strftime('%B %d, %Y')}. Build this post around this "
            f"seasonal theme: \"{theme}\". People plan these weeks ahead on Pinterest, so it "
            f"should be useful right now. Make the season clear in the title and the content."
        )
    else:
        season_line = (
            "SEASON / TIMING: make this one an EVERGREEN post that works any time of year — "
            "do not make it seasonal or holiday-themed."
        )

    style = random.choice(STYLE_LENSES) if random.random() < 0.5 else None
    style_line = (
        f"STYLE LENS (optional flavor): lean into {style} style where it fits naturally; "
        f"don't force it." if style else ""
    )
    return {"fmt": fmt, "theme": theme, "season_line": season_line, "style_line": style_line}


# Models that answered 429 (rate limit / quota) are skipped for the rest of the
# run, so retries after a rejected draft don't re-knock on doors that are shut.
_DEAD_MODELS = set()


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

    # Titles had become samey ("How to Turn a $X Thrifted Y into a $Z ..."),
    # so each run is handed one randomly-picked title style to follow.
    title_style = random.choice(TITLE_STYLES)
    plan = pick_post_plan(history)
    fmt = plan["fmt"]
    needs_line = (
        "   Right after the hook, add <h2>What You'll Need</h2> with a short <ul> of the\n"
        "   materials and tools, so readers can scan it before the steps.\n"
        if fmt.get("needs_list") else ""
    )
    if fmt["use_title_styles"]:
        title_line = (
            f"TITLE STYLE for THIS post (the site's titles had become too similar to each\n"
            f"other, so follow this one): {title_style}. Do NOT start the title with\n"
            f'"How to Turn" and do not use the pattern "Thrifted X into a $Y Z for $W".'
        )
    else:
        title_line = (
            f"TITLE for THIS post: {fmt['title_hint']}. Do NOT start the title with\n"
            f'"How to Turn" and do not use the pattern "Thrifted X into a $Y Z for $W".'
        )

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

    prompt = f"""You are an experienced, down-to-earth writer for a {niche} blog. You know
these projects well and explain them clearly, on a real budget.
Posts are shared to Pinterest automatically the moment they're published, so
the opening line has to earn a click — then the article has to actually
deliver, like a knowledgeable friend explaining exactly how it's done.

NO BRANDS (important — a post with brand names was removed by Blogger):
- Never name any brand, retailer, store, marketplace, designer or branded
  product: no IKEA, Target, Walmart, Amazon, Etsy, Goodwill, Home Depot,
  Anthropologie, West Elm, Pottery Barn, Mod Podge, Rust-Oleum, Command hooks,
  paint brands, and so on. Use generic words instead: "a thrift store", "a
  big-box store", "a high-end retailer", "an online marketplace", "chalk
  paint", "spray paint", "adhesive hooks", "decoupage glue".
- Never quote a specific real product or its price. When comparing to an
  expensive look, DESCRIBE the pricey version in general terms and give
  price ranges ("similar pieces often cost $150-$250"), never a named item.

HONESTY RULES (important):
- Do NOT claim experiences that didn't happen: no "I tried", "I spent", "in my
  home", "my husband", "when I made this". Speak to the reader ("you") or in
  general terms ("most people find...", "a common mistake is...").
- PRICES are estimates, never receipts. Use "about", "around", "under" or a
  range ("$15-$25"); thrift prices vary by store and region. Never state an
  inflated retail value as fact: say "looks like a piece that could cost $400"
  or "similar pieces often sell for $300-$450". In titles prefer "under $20"
  or "for about $20" over an exact figure.

Topics already covered (do NOT repeat these or anything too similar to them):
{json.dumps(recent_titles, ensure_ascii=False)}

Pick ONE fresh, specific, practical angle on {niche} that is not in that list.
Two posts about the same kind of main object or project (for example two about a
thrifted wooden box, or two about candle holders) count as repeats even when the
titles differ, so choose a clearly different object and project from every
recent title above.
PHOTOGRAPHABLE TOPICS: this blog's pictures come from generic stock-photo
libraries, so choose a project or idea that a stock photo of a common home scene
can illustrate (a mantel, shelf, table, bed, entryway, planter, candles,
curtains, a dresser, a bathroom vanity). Avoid projects whose key object is an
unusual hybrid that no stock photo would show (for example a leather-wrapped
glass lantern).

POST FORMAT for THIS post (required — the site was turning into many near-identical
tutorials, so this post MUST follow this format): {fmt["name"].upper()}: {fmt["instruction"]}

{plan["season_line"]}
{plan["style_line"]}

{title_line}

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
- Be specific and concrete everywhere: real product TYPES, generic places to shop
  ("a thrift store", "a big-box store", "a home improvement store", "an online
  marketplace"), real price ranges, real tools, brand-agnostic techniques.
- A light, opinionated aside is fine ("honestly, skip this step if you're short on time") as long as it doesn't claim a personal experience.
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
{needs_line}
2. A few h2/h3 sections walking through the real project or tips, using
   <ul> for independent tips/ideas and <ol> for sequential step-by-step
   instructions — pick whichever actually fits each section.
3. Include ONE real <table> somewhere natural in the article: {fmt["table_hint"]}.
   Use realistic prices wherever prices appear, and mention the total or a
   typical budget in the text near the table (e.g. "All in, this came out
   to about $X").
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
   (e.g. "$26", not 26; not a list). If this post is NOT a single project
   (a list, guide or plan), total_cost is the typical total budget to do
   everything in the post (a range like "$30-$60" is fine), time_estimate is
   how long it would take, and difficulty is how hard the whole thing is.

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
- "pin_description": 2-3 plain sentences (max 300 characters) written for
  Pinterest SEARCH: say plainly what this post is, who it's for, and the
  room / style / budget, using the natural keywords people actually search
  (e.g. "budget living room ideas", "thrift store makeover"). No hashtags, no
  emoji, no quotation marks, no personal-experience claims.
- "pin_hook": a punchy, benefit- or curiosity-driven phrase, 5-8 words max,
  written like Pinterest pin text (e.g. "10 Thrift Flips That Look Expensive"),
  NOT a full sentence, no ending punctuation.
- "image_prompt": REQUIRED — 2-4 simple search keywords (not a sentence) for
  the vertical HERO photo (this is the one shown on Pinterest AND the reel's
  opening shot): the main OBJECT plus the room or setting, e.g. "glass vase
  living room" or "taper candles mantel". No brand names, no people's faces,
  no text. Keep it SHORT: stock-photo search matches a few clear nouns far
  better than a long string of adjectives. Use at most ONE styling word
  (e.g. "cozy", "rustic", "warm light"), and don't put colours or finishes
  ("matte", "black", "gold") in the query unless the colour is the whole
  point. This field must always be present in your JSON response.
- "section_images": a list matching your [[IMG_n]] placeholders, each with a
  "token" (e.g. "IMG_1") and a "query" (2-4 keyword search terms for a real,
  horizontal photo matching that section of the article, written the same
  short, noun-first way as image_prompt above — no people's faces, no text).
- "reel_script": a short spoken-word voiceover script for a ~18-22 second
  vertical video (a Pinterest video pin), 45-65 words total, written to be
  read aloud by an AI voice — NOT the article text, and do NOT include any
  call-to-action or "link"/"website"/"bio" line (that's added separately).
  Structure:
  1. A punchy 1-sentence hook that would make someone stop scrolling — lean
     hard into the most specific, surprising claim of THIS post: the
     price-transformation shock for a project, or a concrete number,
     mistake or result for a list/guide post — a specific, concrete
     curiosity gap (what it actually is, not "you won't believe this").
     Specific and unexpected beats generic every time: "This $9 thrift-store
     lazy Susan looks like a $380 stone counter riser" beats "I made an amazing
     upgrade for cheap."
  2. 2 short, concrete tip/step sentences pulled from the real project — the
     single most surprising or useful specific detail (the trick, the
     material swap, the exact technique), not a generic summary. In ONE of
     these, naturally mention the total cost and time using the actual
     numbers (e.g. "It only takes about an hour and costs around $20.") — write
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
  "pin_description": "...",
  "pin_hook": "...",
  "hashtag_tags": ["8-12 short descriptive style/content tags for Pinterest/Tumblr hashtags only, e.g. thrift flip, diy, budget decor, home makeover, thrifted finds — these do NOT affect the site's category"],
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

    max_attempts = 1  # one try per model, then immediately move to the next — with 6 different
    # models in the list now, each burning its own separate small daily quota, spending 2-3
    # retries on ONE model before moving on wastes quota that a fresh, different model would
    # rather use for a real attempt.
    wait_seconds = []  # no in-model retry delay needed when max_attempts is 1

    # If the primary model is overloaded/unavailable, fall back through a chain of models
    # rather than failing the run — each model has its own separate free-tier daily quota, so
    # trying 6 different ones is far more likely to land a working one than retrying a single
    # model repeatedly. The two "-lite" models at the end have a much higher daily quota than
    # the regular flash models, so they're kept as the last resort.
    top_models = []
    for m in [TEXT_MODEL, FALLBACK_TEXT_MODEL, FALLBACK_TEXT_MODEL_2, FALLBACK_TEXT_MODEL_3]:
        if m and m not in top_models:
            top_models.append(m)
    lite_models = [m for m in [FALLBACK_TEXT_MODEL_4, FALLBACK_TEXT_MODEL_5] if m and m not in top_models]
    # "High demand" (HTTP 503) spikes usually pass within a minute, and the lite models write
    # noticeably shorter, less obedient articles, so the top models get a second pass after a
    # short wait before falling back to the lite ones.
    models_to_try = top_models + ["__WAIT__"] + top_models + lite_models

    last_error = None
    num_cycles = 1  # one pass through the whole model list — with 6 models to try, each
    # contributing its own separate quota, there's no need to loop back and re-try the exact
    # same 6 models again; that would just burn quota with attempts already known to fail.
    cycle_wait = 0

    for cycle in range(1, num_cycles + 1):
        is_last_cycle = cycle == num_cycles

        for model_index, model in enumerate(models_to_try):
            if model == "__WAIT__":
                if any(m not in _DEAD_MODELS for m in top_models):
                    wait = int(os.environ.get("GEMINI_TOP_RETRY_SECONDS", "45"))
                    print(f"The top Gemini models are busy — waiting {wait}s, then trying them once more...")
                    time.sleep(wait)
                continue
            if model in _DEAD_MODELS and model_index < len(models_to_try) - 1:
                continue
            is_last_model = model_index == len(models_to_try) - 1
            is_final_attempt_ever = is_last_model and is_last_cycle  # only raise once we're truly out of options

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
                    if is_last_attempt_for_model and is_final_attempt_ever:
                        raise RuntimeError(f"Gemini request failed on all models/attempts/cycles: {last_error}")
                    if not is_last_attempt_for_model:
                        delay = wait_seconds[attempt - 1]
                        print(f"[{model}] Gemini request failed ({e}), retrying in {delay}s "
                              f"(attempt {attempt}/{max_attempts})...")
                        time.sleep(delay)
                    else:
                        print(f"[{model}] network error ({str(e)[:120]}), switching to fallback model...")
                    continue

                if res.ok:
                    try:
                        text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                    except (KeyError, IndexError, TypeError, ValueError):
                        # a 200 with no usable text (blocked / empty answer): treat like any other failure
                        last_error = f"empty or blocked answer: {res.text[:150]}"
                        if is_last_attempt_for_model and is_final_attempt_ever:
                            raise RuntimeError(f"Gemini gave no usable answer on all models: {last_error}")
                        print(f"[{model}] gave no usable answer ({last_error[:120]}), switching to fallback model...")
                        continue
                    text = text.replace("```json", "").replace("```", "").strip()
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            parsed["_format"] = fmt["name"]
                            parsed["_theme"] = plan["theme"]
                            parsed["_model"] = model
                        print(f"Article written by {model}.")
                        return parsed
                    except json.JSONDecodeError as e:
                        last_error = f"invalid JSON: {e}"
                        if is_last_attempt_for_model and is_final_attempt_ever:
                            raise RuntimeError(f"Gemini returned invalid JSON on all models/attempts/cycles: {last_error}")
                        if not is_last_attempt_for_model:
                            delay = wait_seconds[attempt - 1]
                            print(f"[{model}] Gemini returned invalid JSON ({e}), retrying in {delay}s "
                                  f"(attempt {attempt}/{max_attempts})...")
                            time.sleep(delay)
                        continue

                # Retry only on transient errors (overloaded / rate-limited / server hiccup).
                # Fail immediately on anything else (e.g. bad API key, bad request) — those
                # won't be fixed by waiting, on this cycle or the next.
                transient = res.status_code in (429, 500, 502, 503, 504)
                last_error = f"HTTP {res.status_code}: {res.text[:200]}"

                if not transient:
                    raise RuntimeError(f"Gemini text generation failed ({res.status_code}): {res.text}")

                if is_last_attempt_for_model and is_final_attempt_ever:
                    raise RuntimeError(f"Gemini text generation failed on all models/attempts/cycles: {last_error}")

                if not is_last_attempt_for_model:
                    delay = wait_seconds[attempt - 1]
                    print(f"[{model}] Gemini text generation failed ({res.status_code}), retrying in {delay}s "
                          f"(attempt {attempt}/{max_attempts})...")
                    time.sleep(delay)
                else:
                    print(f"[{model}] exhausted all attempts ({last_error[:150]}), switching to fallback model...")
                if res.status_code == 429:
                    _DEAD_MODELS.add(model)

        if not is_last_cycle:
            print(f"All models exhausted on cycle {cycle}/{num_cycles} — Gemini may be having a "
                  f"broader rough patch. Waiting {cycle_wait}s, then trying the whole model list again...")
            time.sleep(cycle_wait)


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

    # Optional: Pinterest-search description. If missing/malformed, the old
    # behavior (excerpt of the opening paragraph) is used instead.
    if not isinstance(draft.get("pin_description"), str):
        draft["pin_description"] = ""

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


# Words in a Pexels query that describe the *look* rather than the subject;
# they're ignored when matching a photo's description to the query.
_QUERY_STYLE_WORDS = {
    "styled", "cozy", "rustic", "close", "closeup", "up", "warm", "light", "lighting",
    "vignette", "aesthetic", "modern", "vintage", "diy", "decor", "home", "interior",
    "budget", "thrifted", "thrift", "idea", "ideas", "photo", "image", "style",
    # colours / finishes: "matte black" matched a black car's description
    "matte", "black", "white", "dark", "gold", "golden", "silver", "moody", "soft",
    "bright", "natural", "minimalist", "boho", "farmhouse", "glossy", "shiny",
    "mood", "dim", "glow", "glowing", "aesthetic", "elegant", "luxury", "luxurious",
}

# Photos whose description mentions these are obviously not home decor.
_OFFTOPIC_WORDS = {
    "car", "cars", "vehicle", "vehicles", "suv", "truck", "trucks", "tesla", "motorcycle",
    "motorbike", "bicycle", "airplane", "aircraft", "highway", "traffic", "laptop",
    "smartphone", "runway", "makeup", "wedding", "sports", "football", "soccer",
}

VISION_CHECKS_PER_RUN = 36      # cap on Gemini photo-check calls per run (a video run makes ~15-20)
_vision_state = {"used": 0, "disabled": False}


_PEOPLE_WORDS = {
    "woman", "women", "man", "men", "girl", "boy", "person", "people", "child", "children",
    "kid", "kids", "baby", "couple", "family", "portrait", "smiling", "model", "lady", "guy",
    "mother", "father", "bride", "groom", "friends", "teen", "toddler", "selfie", "face",
    "faces", "male", "female", "santa", "wearing",
}


# A fall post got a photo with "Merry Christmas" pillows. Photos showing a
# holiday are skipped unless the keywords or the post itself are about it.
_HOLIDAY_WORDS = {
    "christmas": {"christmas", "xmas", "santa", "reindeer", "jingle", "snowman", "ornament", "ornaments"},
    "halloween": {"halloween", "spooky", "witch", "skeleton", "jack", "lantern"},
}
_photo_context = {"text": ""}   # set per post in main(): title + seasonal theme


def _blocked_holiday_words(query):
    text = f"{query} {_photo_context['text']}".lower()
    blocked = set()
    for holiday, words in _HOLIDAY_WORDS.items():
        mentioned = holiday in text or (holiday == "christmas" and ("holiday" in text or "winter" in text))
        if not mentioned:
            blocked |= words
    return blocked


def _photo_is_offtopic(photo):
    alt = set(re.findall(r"[a-z]+", (photo.get("alt") or "").lower()))
    return bool(alt & _OFFTOPIC_WORDS)


def _photo_has_people(photo):
    alt = set(re.findall(r"[a-z]+", (photo.get("alt") or "").lower()))
    return bool(alt & _PEOPLE_WORDS)


def _query_words(text):
    return [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2 and w not in _QUERY_STYLE_WORDS]


def _photo_match_score(photo, words):
    """How many of the query's subject words appear in the photo's description."""
    alt = set(re.findall(r"[a-z]+", (photo.get("alt") or "").lower()))
    return sum(1 for w in words if w in alt)


def _gemini_pick_best_photo(query, photos, ideal=None, subject=None):
    """
    Shows up to 5 small thumbnails to Gemini and asks which one genuinely
    matches `query`. Returns the chosen photo, 0-based, or:
      None  -> couldn't check (quota/network/bad answer); caller falls back
      False -> Gemini says none of them fit
    Never raises.
    """
    if _vision_state["disabled"] or _vision_state["used"] >= VISION_CHECKS_PER_RUN:
        return None
    try:
        context_line = ""
        if subject:
            context_line += (f" The photo's MAIN SUBJECT must be: {subject} — clearly visible and the "
                             f"focus of the picture, not just something small in the background or a "
                             f"similar-looking object. If no photo clearly shows it, answer 0.")
        if ideal:
            context_line += f" The ideal photo for this part of the article: {ideal}"
        if _photo_context["text"].strip():
            context_line += f" The blog post is titled: \"{_photo_context['text'].strip()}\"."
        parts = [{"text": (
            f"You are choosing a photo for a budget home-decor blog. The photo should clearly "
            f"show: \"{query}\".{context_line} Below are {len(photos)} candidate photos, numbered in order. "
            f"Pick the ONE that best matches those keywords and looks like a clean, real interior "
            f"or decor photo (not mostly text or graphics). REJECT any photo where a person or a "
            f"face is visible, any photo with clearly readable words or lettering, and any photo "
            f"showing Christmas or Halloween decor unless the keywords ask for that holiday. "
            f"If none of them genuinely fit, answer 0. "
            f"Reply with ONLY JSON like {{\"best\": 2}}."
        )}]
        for i, p in enumerate(photos, start=1):
            thumb = robust_request("GET", p["src"].get("medium") or p["src"]["small"], timeout=30)
            if not thumb.ok:
                return None
            parts.append({"text": f"Photo {i}:"})
            parts.append({"inline_data": {"mime_type": "image/jpeg",
                                          "data": base64.b64encode(thumb.content).decode()}})
        _vision_state["used"] += 1

        models = [m for m in (os.environ.get("GEMINI_VISION_MODEL"), FALLBACK_TEXT_MODEL_4, FALLBACK_TEXT_MODEL_5,
                              FALLBACK_TEXT_MODEL_3, FALLBACK_TEXT_MODEL_2) if m]
        models = list(dict.fromkeys(models))
        dead = _vision_state.setdefault("dead", set())   # models out of quota (429) for this run
        for attempt_round in range(2):
            # round 2 (after a pause) only retries the first two models, to bound the time a bad spell can cost
            for model in (models if attempt_round == 0 else models[:2]):
                if model in dead:
                    continue
                try:
                    res = requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                        params={"key": GEMINI_API_KEY},
                        json={"contents": [{"parts": parts}]},
                        timeout=45,
                    )
                except Exception as e:
                    print(f"Photo check: {model} timed out or failed ({str(e)[:80]}).")
                    continue
                if res.status_code == 429:
                    print(f"Photo check: quota hit on {model}.")
                    dead.add(model)
                    continue
                if not res.ok:
                    print(f"Photo check failed on {model} ({res.status_code}).")
                    continue
                try:
                    text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                m = re.search(r'"best"\s*:\s*(\d+)', text)
                if not m:
                    continue
                _vision_state["fails"] = 0
                idx = int(m.group(1))
                if idx == 0:
                    return False
                if 1 <= idx <= len(photos):
                    return photos[idx - 1]
            if attempt_round == 0 and any(mm not in dead for mm in models):
                time.sleep(6)   # "high demand" spikes are short: one more round after a pause
        # Nothing answered. 503s are temporary, so the check is only switched off for the rest of
        # the run after 3 calls in a row failed (or when every model is out of quota).
        _vision_state["fails"] = _vision_state.get("fails", 0) + 1
        if _vision_state["fails"] >= 3 or all(mm in dead for mm in models):
            _vision_state["disabled"] = True
        return None
    except Exception as e:
        print(f"Photo check skipped ({e}).")
        return None


def _fetch_pixabay(query, orientation):
    """
    Pixabay photos for `query`, reshaped like Pexels results so the same
    ranking and Gemini check apply. Returns [] on any problem. Pixabay's
    tags serve as the photo "description". Images are downloaded and
    re-hosted by this script (Pixabay forbids permanent hotlinking), and
    only a handful of requests per run are made (limit: 100 per minute).
    """
    if not PIXABAY_API_KEY:
        return []
    try:
        res = robust_request(
            "GET", "https://pixabay.com/api/",
            params={
                "key": PIXABAY_API_KEY, "q": query[:100], "image_type": "photo",
                "orientation": "vertical" if orientation == "portrait" else "horizontal",
                "safesearch": "true", "per_page": 30, "min_width": 900, "lang": "en",
            },
            timeout=30,
        )
        if not res.ok:
            print(f"Pixabay search failed ({res.status_code}) — skipping Pixabay.")
            return []
        return [
            {
                "id": f"pb{h['id']}", "alt": h.get("tags", ""),
                "width": h.get("imageWidth"), "height": h.get("imageHeight"),
                "src": {"medium": h["webformatURL"], "small": h.get("previewURL") or h["webformatURL"],
                        "large2x": h.get("largeImageURL") or h["webformatURL"]},
            }
            for h in res.json().get("hits", []) if h.get("webformatURL")
        ]
    except Exception as e:
        print(f"Pixabay search skipped ({e}).")
        return []


def search_pexels_image(query, orientation="portrait", used_photo_ids=None, target_ratio=None, strict=False, _stage=0, _source="pexels", fallbacks=True, ideal=None, subject=None):
    """
    Finds a Pexels photo matching `query` and returns (image_bytes, photo_id).

    Selection, in order:
      1. Skip photos already used in earlier posts (the same photo twice
         looks like duplicate spam on Pinterest); fall back to the full set
         if every match was used.
      2. For the reel, keep only photos close to the target ratio (e.g. 9:16)
         so the cover-crop stays modest.
      3. Rank what's left by how well each photo's description matches the
         query's subject words (Pexels order breaks ties).
      4. Show the top 5 as thumbnails to Gemini and use the one it says
         matches. If it says none fit, retry with the query's first three
         subject words, then with a generic "home interior decor" query. If
         Gemini can't be reached, take one of the best-ranked by description
         instead. If even the generic query is rejected: with strict=True
         (section/reel photos) raise, so that image is skipped rather than
         filled with an unrelated photo; with strict=False (the hero, which
         is required) use the best description match.

    Order of attempts when Gemini keeps saying "none fit": Pexels with the
    original query -> Pixabay with the same query (only if PIXABAY_API_KEY
    is set) -> Pexels with the shorter query -> Pexels generic query.
    """
    used_photo_ids = used_photo_ids or set()
    if _stage == 0 and _source == "pexels" and len(query.split()) > 5:
        trimmed = " ".join(_query_words(query)[:4])
        if trimmed:
            print(f"Photo query trimmed: '{query}' -> '{trimmed}'.")
            query = trimmed
    words = _query_words(query)

    def rejected_everywhere():
        """Gemini (or an empty result) rejected this attempt: move to the next one."""
        if _source == "pexels" and _stage == 0 and PIXABAY_API_KEY:
            print(f"Photo check: trying Pixabay for '{query}'.")
            return search_pexels_image(query, orientation, used_photo_ids, target_ratio, strict, _stage=0,
                                       _source="pixabay", fallbacks=fallbacks, ideal=ideal, subject=subject)
        if not fallbacks:
            raise RuntimeError(f"No suitable photo found for '{query}'")
        if _stage == 0 and len(words) > 3:
            shorter = " ".join(words[:3])
            print(f"Photo check: none fit '{query}' — retrying with '{shorter}'.")
            return search_pexels_image(shorter, orientation, used_photo_ids, target_ratio, strict, _stage=1, ideal=ideal, subject=subject)
        if _stage <= 1:
            print(f"Photo check: none fit '{query}' — trying a generic home-decor photo instead.")
            return search_pexels_image("home interior decor", orientation, used_photo_ids, target_ratio, strict, _stage=2, ideal=ideal, subject=subject)
        return None   # final attempt: caller decides (raise or best match)

    if _source == "pixabay":
        photos = _fetch_pixabay(query, orientation)
        if not photos:
            return rejected_everywhere()
    else:
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
    pool = unused or photos

    if target_ratio:
        # Prefer photos within ~35% of the target ratio; if none, use the
        # single closest-ratio photo rather than failing the run.
        close_enough = [
            p for p in pool
            if p.get("width") and p.get("height")
            and abs((p["width"] / p["height"]) - target_ratio) / target_ratio < 0.35
        ]
        if close_enough:
            pool = close_enough
        elif any(p.get("width") and p.get("height") for p in pool):
            pool = [min(
                (p for p in pool if p.get("width") and p.get("height")),
                key=lambda p: abs((p["width"] / p["height"]) - target_ratio),
            )]

    # No photos with people/faces: drop any whose description mentions them
    # (if every result has people, keep them all; Gemini's check below
    # still rejects those if it can).
    without_people = [p for p in pool if not _photo_has_people(p)]
    if without_people:
        pool = without_people
    # ...and any whose description is plainly not home decor (cars, laptops...)
    on_topic = [p for p in pool if not _photo_is_offtopic(p)]
    if on_topic:
        pool = on_topic
    # ...and holiday-decor photos when the post isn't about that holiday
    blocked = _blocked_holiday_words(query)
    if blocked:
        no_holiday = [
            p for p in pool
            if not (set(re.findall(r"[a-z]+", (p.get("alt") or "").lower())) & blocked)
        ]
        if no_holiday:
            pool = no_holiday

    order = {p["id"]: i for i, p in enumerate(photos)}          # the source's own relevance order
    ranked = sorted(pool, key=lambda p: (-_photo_match_score(p, words), order[p["id"]]))
    shortlist = ranked[:6]

    photo = None
    verdict = _gemini_pick_best_photo(query, shortlist, ideal, subject) if len(shortlist) > 1 else None
    if verdict:
        photo = verdict
        print(f"Photo check: Gemini picked a match for '{query}'.")
    elif verdict is False:
        nxt = rejected_everywhere()
        if nxt is not None:
            return nxt
        if strict:
            raise RuntimeError(f"No suitable photo found for '{query}' (every candidate was rejected)")
        print(f"Photo check: none fit '{query}' — using the best description match.")
    if photo is None:
        best_score = _photo_match_score(ranked[0], words)
        if verdict is None and strict and words and best_score < min(2, len(words)):
            # The check was unavailable AND the description barely matches: better no photo than
            # an unverified, probably unrelated one (a dining room under "halloween shelf decor").
            raise RuntimeError(f"No verified photo for '{query}' (photo check unavailable, weak description match)")
        top = [p for p in ranked[:3] if _photo_match_score(p, words) == best_score] or ranked[:1]
        photo = random.choice(top)

    image_url = photo["src"]["large2x"]
    image_res = robust_request("GET", image_url, timeout=30)
    if not image_res.ok:
        raise RuntimeError(f"Photo download failed ({image_res.status_code})")
    return image_res.content, photo["id"]


def find_best_photo(queries, ideal, orientation, used_photo_ids, target_ratio=None, strict=False, allow_fallback=True, subject=None):
    """
    Tries several alternative queries (most specific first), each against
    Pexels and then Pixabay, with Gemini checking the candidates. The first
    query that yields an accepted photo wins. Only if every query is
    rejected does the old fallback chain run (shorter query, generic query,
    then best description match / skip).
    """
    queries = [q for q in dict.fromkeys(q.strip() for q in queries if q and q.strip())]
    for q in queries[:4]:
        try:
            return search_pexels_image(q, orientation, used_photo_ids, target_ratio,
                                       strict=True, fallbacks=False, ideal=ideal, subject=subject)
        except RuntimeError as e:
            print(f"Photo search for '{q}' had no accepted photo ({e}) — trying the next query.")
    if not allow_fallback:
        raise RuntimeError("no query produced an accepted photo")
    print("No planned query produced an accepted photo — using the standard fallbacks.")
    return search_pexels_image(queries[0], orientation, used_photo_ids, target_ratio,
                               strict=strict, ideal=ideal, subject=subject)


def plan_photo_queries(draft, n_extra=4):
    """
    One focused Gemini call, made AFTER the article is written, that plans
    every photo: for the hero, each section image and a few extra vertical
    shots for the video, it returns up to 3 alternative search queries plus a
    one-line description of the ideal photo. The article prompt writes the
    article; this prompt only has to think about photos, and it can see the
    finished text. Returns {slot: {"queries": [...], "ideal": "..."}}; {}
    if anything goes wrong (the article's own queries are used then).
    """
    try:
        tokens = [s.get("token") for s in draft.get("section_images", []) if s.get("token")]
        plain = re.sub(r"<[^>]+>", " ", draft.get("html", ""))
        slots = ["- hero: the single vertical cover photo for the whole post."]
        for t in tokens:
            i = plain.find(f"[[{t}]]")
            window = plain[max(0, i - 200): i + 400] if i >= 0 else plain[:500]
            window = re.sub(r"\s+", " ", window.replace(f"[[{t}]]", " <photo goes here> ")).strip()
            slots.append(f'- {t}: a horizontal photo for this part of the article: "{window}"')
        for n in range(1, n_extra + 1):
            slots.append(f"- extra_{n}: an extra VERTICAL photo for a short video; it must show a DIFFERENT "
                         f"object or area of the topic than the hero and the other slots.")
        theme = draft.get("_theme")
        season = (f"Seasonal theme: {theme}. The hero photo should visibly show this season or holiday "
                  f"when that looks natural (for example warm candles, garland, autumn colours), so "
                  f"the cover matches the title." if theme
                  else "This is an evergreen (not seasonal, not holiday) post.")
        prompt = (
            "You choose stock photos for a budget home-decor blog post.\n"
            f"Post title: {draft.get('title', '')}\nCategory: {draft.get('category', '')}\n{season}\n\n"
            "For EACH slot below give up to 3 alternative stock-photo search queries, ordered from most "
            "specific to most general, a one-sentence description of the ideal photo, and the SUBJECT: "
            "the single most important physical object that must be clearly visible as the focus of the "
            "photo (2-3 words, e.g. \"wooden coat stand\"). For the hero the subject is the post's main "
            "object.\n"
            "Query rules: 2-4 words; start with the main physical OBJECT then the room or setting; NO "
            "adjectives about style, colour, mood or finish; no brand names; no people; no text; the photo "
            "must suit the post's season (no Christmas or Halloween items unless the post is about that "
            "holiday).\n\nSlots:\n" + "\n".join(slots) + "\n\n"
            'Return ONLY JSON like {"photos":[{"slot":"hero","queries":["a","b","c"],"ideal":"...","subject":"..."}]} '
            "with one entry per slot above."
        )
        for model in [m for m in (FALLBACK_TEXT_MODEL_4, FALLBACK_TEXT_MODEL_5, FALLBACK_TEXT_MODEL_3) if m]:
            res = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=90,
            )
            if not res.ok:
                print(f"Photo planning: {model} returned {res.status_code}, trying the next model.")
                continue
            text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                continue
            plan = {}
            for item in json.loads(m.group(0)).get("photos", []):
                slot = item.get("slot")
                queries = [q.strip() for q in item.get("queries", [])
                           if isinstance(q, str) and 0 < len(q.split()) <= 6]
                if slot and queries:
                    subject = item.get("subject")
                    plan[slot] = {
                        "queries": queries[:3],
                        "ideal": (item.get("ideal") or "").strip() or None,
                        "subject": subject.strip() if isinstance(subject, str) and subject.strip() else None,
                    }
            if plan:
                print(f"Photo planning: {len(plan)} slot(s) planned by {model}.")
                return plan
        print("Photo planning produced nothing — using the article's own photo queries.")
    except Exception as e:
        print(f"Photo planning skipped ({e}) — using the article's own photo queries.")
    return {}


def _table_rows(html_text):
    """First <table> in the post as a list of rows, each a list of plain-text cells."""
    m = re.search(r"<table.*?</table>", html_text or "", re.DOTALL | re.IGNORECASE)
    if not m:
        return []
    rows = []
    for tr in re.findall(r"<tr.*?</tr>", m.group(0), re.DOTALL | re.IGNORECASE):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.DOTALL | re.IGNORECASE)
        rows.append([re.sub(r"<[^>]+>", " ", c).strip() for c in cells])
    return rows


def _minutes_in(text):
    """'15 mins', '2 hours', '1-2 hours' -> minute values."""
    vals = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)(?:\s*[-–]\s*(\d+(?:\.\d+)?))?\s*(hours?|hrs?|minutes?|mins?)\b", text, re.IGNORECASE):
        mult = 60 if m.group(3).lower().startswith("h") else 1
        vals += [float(g) * mult for g in (m.group(1), m.group(2)) if g]
    return vals


def _fmt_minutes(v):
    return f"{v / 60:g} hour{'s' if v / 60 != 1 else ''}" if v >= 60 else f"{v:g} min"


def fix_listicle_cost(draft):
    """
    The Quick Take box must agree with the article's own table:
      * listicle / mistakes-and-fixes: cost (and, for listicles, time) are PER
        ITEM, so they are rebuilt from the table: "$10-$35 per idea".
      * designer look for less: the cost is the sum of the budget column of
        the comparison table (a post claimed $57 while its table added up to
        $35); the same wrong total is corrected where the text quotes it.
    """
    try:
        fmt = draft.get("_format")
        rows = _table_rows(draft.get("html", ""))
        if not rows:
            return
        amount = lambda t: [float(x) for x in re.findall(r"\$\s?(\d+(?:\.\d+)?)", t)]
        fmt_amt = lambda v: f"${v:g}"

        unit = {"listicle": "per idea", "mistakes and fixes": "per fix"}.get(fmt)
        if unit:
            amounts = [v for row in rows[1:] for cell in row for v in amount(cell)]
            if len(amounts) >= 2:
                lo, hi = min(amounts), max(amounts)
                draft["total_cost"] = (f"{fmt_amt(lo)}-{fmt_amt(hi)} {unit}" if lo != hi else f"{fmt_amt(lo)} {unit}")
            if fmt == "listicle":
                header = [c.lower() for c in rows[0]]
                if any("time" in c for c in header):
                    ti = next(i for i, c in enumerate(header) if "time" in c)
                    mins = [v for row in rows[1:] if len(row) > ti for v in _minutes_in(row[ti])]
                    if len(mins) >= 2:
                        lo, hi = min(mins), max(mins)
                        if hi < 60:
                            draft["time_estimate"] = (f"{lo:g}-{hi:g} minutes {unit}" if lo != hi else f"{lo:g} minutes {unit}")
                        else:
                            draft["time_estimate"] = (f"{_fmt_minutes(lo)} to {_fmt_minutes(hi)} {unit}" if lo != hi
                                                      else f"{_fmt_minutes(lo)} {unit}")
        elif fmt == "designer look for less":
            lows, highs = [], []
            for row in rows[1:]:
                if len(row) >= 3 and amount(row[1]):
                    lows.append(min(amount(row[1])))
                    highs.append(max(amount(row[1])))
            if len(lows) >= 2:
                lo, hi = sum(lows), sum(highs)
                new_total = f"{fmt_amt(lo)}-{fmt_amt(hi)}" if lo != hi else fmt_amt(lo)
                new_words = f"{fmt_amt(lo)} to {fmt_amt(hi)}" if lo != hi else fmt_amt(lo)
                old = amount(str(draft.get("total_cost", "")))
                draft["total_cost"] = new_total
                if old:
                    # rewrite the whole phrase ("about $55", "around $55 to $65") so a range is never
                    # half-replaced into nonsense like "$65 to $55"
                    draft["html"] = re.sub(
                        r"\b(about|around|roughly|only|just|for|under|at)\s+\$" + re.escape(f"{old[0]:g}")
                        + r"(?:\s*(?:to|-|–)\s*\$\d+(?:\.\d+)?)?(?!\d)(?!\.\d)",
                        lambda m: f"{m.group(1)} {new_words}", draft["html"])
    except Exception as e:
        print(f"Cost/time fix skipped ({e}).")


def draft_has_hero_photo(draft, history):
    """
    "Photographability" gate. Some topics (e.g. a leather-wrapped glass
    lantern) are so specific that no stock library has a photo of them, and
    the post ends up with a loosely related picture. Before accepting a
    draft, run the photo planner and look for an accepted HERO photo. If
    Gemini rejects every candidate, the topic is turned down and a more
    photographable one is requested. On success the plan and the photo are
    kept on the draft so they aren't searched for twice. A technical hiccup
    never blocks publishing (returns True).
    """
    try:
        _photo_context["text"] = f"{draft.get('title', '')} {draft.get('_theme') or ''}"
        used = set()
        for h in history:
            used.update(h.get("photo_ids", []))
        plan = plan_photo_queries(draft)
        hero_plan = plan.get("hero", {})
        queries = hero_plan.get("queries", []) + [draft["image_prompt"]]
        raw, photo_id = find_best_photo(queries, hero_plan.get("ideal"), "portrait", used,
                                        strict=True, allow_fallback=False, subject=hero_plan.get("subject"))
        draft["_photo_plan"] = plan
        draft["_hero_photo"] = (raw, photo_id)
        return True
    except RuntimeError:
        return False
    except Exception as e:
        print(f"Photo check for the topic was skipped ({e}).")
        return True


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


TEMPLATE_BOTTOM_LINES = [
    ("WATCH TILL THE END", ["END"]),
    ("SAVE THIS IDEA FOR LATER", ["SAVE"]),
    ("READ THE FULL GUIDE ON OUR SITE", ["GUIDE"]),
    ("TRY THIS THIS WEEKEND", ["WEEKEND"]),
    ("FOLLOW FOR MORE BUDGET IDEAS", ["BUDGET"]),
]


def template_bar_texts(draft):
    """Top bar = the post's hook; bottom bar = a short call to action."""
    top = (draft.get("pin_hook") or draft.get("title") or "").strip()
    bottom, highlight = random.choice(TEMPLATE_BOTTOM_LINES)
    return top, bottom, highlight


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
    if USE_GEMINI_VOICE:
        if video_template.synthesize_gemini_voiceover(script_text, out_path, GEMINI_API_KEY):
            print("Voiceover: Gemini voice (Zephyr).")
            return
        print("Gemini voice unavailable — falling back to Edge-TTS.")
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
    wrapped = textwrap.fill(hook_text.upper(), width=16)
    while True:
        font = _load_bold_font(font_size)
        bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=8, align="center")
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        # shrink until the widest line leaves a margin on both sides
        if (text_w <= w * 0.9 and text_h <= h * 0.3) or font_size <= 24:
            break
        font_size -= 3

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


def extract_pin_description(html, hashtags="", max_length=500, cta="", override=None):
    """
    Pulls plain text from the article's opening <p> (the hook paragraph)
    to use as the Pinterest/Tumblr description — a genuine
    excerpt of the content, not just a repeat of the title or the on-image
    text overlay. Always ends with "...", whether it was truncated for
    length or not, then an optional CTA line, then hashtags (if provided) — all within
    the length limit.
    """
    if override and override.strip():
        text = override
    else:
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


def _bing_submit_url(url):
    """
    One Bing Submission API call. Returns (ok, quota_exhausted).
    Never raises.
    """
    try:
        res = robust_request(
            "POST",
            f"https://ssl.bing.com/webmaster/api.svc/json/SubmitUrl?apikey={BING_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={"siteUrl": SITE_URL, "url": url},
            timeout=30,
        )
        if res.ok:
            return True, False
        print(f"Bing Submission API call failed ({res.status_code}): {res.text}")
        return False, "quota" in res.text.lower()
    except Exception as e:
        print(f"Bing Submission API call failed: {e}")
        return False, False


def submit_to_bing(url):
    """
    Tell Bing to (re)crawl this URL now, via the Bing Webmaster Submission
    API, using the site-linked apikey stored in BING_API_KEY. Bing's index
    also backs Yahoo and DuckDuckGo, so this one call effectively notifies
    all three. Never raises — if this fails or isn't configured, the post
    is still published and still gets indexed eventually on Bing's normal
    sitemap crawl, just slower. Returns True if Bing accepted the URL.
    """
    if not BING_API_KEY:
        print("BING_API_KEY not set — skipping Bing instant indexing "
              "(post will still be found via the sitemap eventually).")
        return False
    ok, _quota = _bing_submit_url(url)
    if ok:
        print("Submitted to Bing Submission API:", url)
    return ok


# ---------------------------------------------------------------------------
# Indexing boost: "all posts" hub page + gradual Bing backlog submission.
# Neither forces Google/Bing to index anything (nothing can) — they make every
# post reachable from one well-linked page and re-notify Bing about older URLs.
# ---------------------------------------------------------------------------
BING_BACKLOG_FILE = "bing_submitted.json"   # URLs already sent to Bing (committed to the repo)
BING_BACKLOG_PER_RUN = 10                   # old URLs to send per run, so the daily quota is never the problem
HUB_PAGE_TITLE = "All Posts: Budget Home Decor & Thrift Flip Ideas"


def _load_url_set(path):
    try:
        with open(path) as f:
            return set(json.load(f))
    except (FileNotFoundError, ValueError):
        return set()


def _save_url_set(path, urls):
    with open(path, "w") as f:
        json.dump(sorted(urls), f, indent=2)


def fetch_all_live_posts(access_token):
    """Every live post on the blog (title, url, labels, published), newest first."""
    posts, page_token = [], None
    while True:
        params = {
            "maxResults": 500, "fetchBodies": "false", "status": "live",
            "fields": "nextPageToken,items(title,url,labels,published)",
        }
        if page_token:
            params["pageToken"] = page_token
        res = robust_request(
            "GET", f"https://www.googleapis.com/blogger/v3/blogs/{BLOGGER_BLOG_ID}/posts",
            headers={"Authorization": f"Bearer {access_token}"}, params=params, timeout=60,
        )
        if not res.ok:
            raise RuntimeError(f"Could not list Blogger posts ({res.status_code}): {res.text}")
        data = res.json()
        posts.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    posts.sort(key=lambda p: p.get("published", ""), reverse=True)
    return posts


def build_hub_html(posts):
    """One page linking every post, grouped by category (newest first)."""
    by_category = {}
    for p in posts:
        category = (p.get("labels") or ["General Decor"])[0]
        by_category.setdefault(category, []).append(p)
    ordered = [c for c in CATEGORIES if c in by_category] + sorted(c for c in by_category if c not in CATEGORIES)

    parts = [
        f"<p>Every DecorVibe project in one place: {len(posts)} budget-friendly home decor "
        f"and thrift-flip guides, grouped by room and style, newest first.</p>"
    ]
    for category in ordered:
        items = by_category[category]
        parts.append(f"<h2>{html.escape(category)} ({len(items)})</h2>")
        parts.append("<ul>")
        for p in items:
            parts.append(f'<li><a href="{html.escape(p["url"], quote=True)}">{html.escape(p["title"])}</a></li>')
        parts.append("</ul>")
    return "\n".join(parts)


def update_hub_page(access_token, posts):
    """Creates the hub page on first run, updates it in place afterwards. Returns its URL."""
    headers = {"Authorization": f"Bearer {access_token}"}
    base = f"https://www.googleapis.com/blogger/v3/blogs/{BLOGGER_BLOG_ID}/pages"
    payload = {"title": HUB_PAGE_TITLE, "content": build_hub_html(posts)}

    res = robust_request("GET", base, headers=headers,
                         params={"fetchBodies": "false", "fields": "items(id,title,url)"}, timeout=30)
    if not res.ok:
        raise RuntimeError(f"Could not list Blogger pages ({res.status_code}): {res.text}")
    existing = next((pg for pg in res.json().get("items", []) if pg.get("title") == HUB_PAGE_TITLE), None)

    if existing:
        r = robust_request("PUT", f"{base}/{existing['id']}", headers=headers, json=payload, timeout=60)
    else:
        r = robust_request("POST", base, headers=headers, json=payload, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Hub page save failed ({r.status_code}): {r.text}")
    return r.json().get("url") or (existing or {}).get("url")


def warn_if_posts_unpublished(access_token, history):
    """
    Blogger can silently unpublish a post (it did once: Community Guidelines).
    Such a post turns into a Draft, but its Pinterest/Tumblr links stay live
    and now lead nowhere. If any auto-generated post (matched by title
    against topics_history.json) is sitting in Draft, send a notification.
    Never raises.
    """
    try:
        res = robust_request(
            "GET", f"https://www.googleapis.com/blogger/v3/blogs/{BLOGGER_BLOG_ID}/posts",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"status": "draft", "maxResults": 50, "fetchBodies": "false",
                    "fields": "items(title,url)"},
            timeout=30,
        )
        if not res.ok:
            return
        auto_titles = {h.get("title") for h in (history or [])}
        flagged = [p["title"] for p in res.json().get("items", []) if p.get("title") in auto_titles]
        if flagged:
            print("WARNING: auto-posts are in Draft (possibly unpublished by Blogger):", flagged)
            send_phone_notification(
                "⚠️ DecorVibe: post(s) unpublished?",
                "These auto-published posts are now Drafts on Blogger (it may have unpublished "
                "them for a guidelines issue). Check the Blogger Posts page for a notice, "
                "then delete or fix them and remove their Pinterest/Tumblr links:\n\n"
                + "\n".join(f"- {t}" for t in flagged),
            )
    except Exception as e:
        print(f"Draft check skipped: {e}")


def boost_indexing(post_url, bing_ok, history=None):
    """
    Runs after publishing. Never raises. (1) Refreshes the all-posts hub
    page, (2) sends a few not-yet-submitted URLs (hub page first, then
    newest-to-oldest posts) to Bing, remembering which were sent.
    """
    submitted = _load_url_set(BING_BACKLOG_FILE)
    if bing_ok and post_url:
        submitted.add(post_url)

    posts, hub_url = [], None
    try:
        token = get_access_token()
        posts = fetch_all_live_posts(token)
        print(f"Blogger reports {len(posts)} live posts.")
        warn_if_posts_unpublished(token, history)
        hub_url = update_hub_page(token, posts)
        print("Hub page updated:", hub_url)
    except Exception as e:
        print(f"Hub page update failed (post is still published fine): {e}")

    if BING_API_KEY:
        try:
            candidates = ([hub_url] if hub_url else []) + [p["url"] for p in posts]
            todo = [u for u in candidates if u and u not in submitted][:BING_BACKLOG_PER_RUN]
            sent, failures = 0, 0
            for u in todo:
                ok, quota_hit = _bing_submit_url(u)
                if ok:
                    submitted.add(u)
                    sent += 1
                    failures = 0
                elif quota_hit:
                    print("Bing daily quota reached — stopping, will continue next run.")
                    break
                else:
                    failures += 1
                    if failures >= 3:
                        print("Bing keeps rejecting URLs — stopping for this run.")
                        break
                time.sleep(1)
            waiting = len([u for u in candidates if u and u not in submitted])
            print(f"Bing backlog: sent {sent} URL(s) this run, {waiting} still waiting.")
        except Exception as e:
            print(f"Bing backlog submission failed (post is still published fine): {e}")

    try:
        _save_url_set(BING_BACKLOG_FILE, submitted)
    except Exception as e:
        print(f"Could not save {BING_BACKLOG_FILE}: {e}")


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


def save_status(blogger_ok, blogger_url, pinterest_ok, tumblr_ok=False):
    """
    Writes a small status.json the control panel reads to show a simple
    green-tick/red-cross per platform for the most recent run. Facebook,
    Instagram and TikTok are no longer posted to; their keys are kept as
    null so anything reading this file doesn't break.
    """
    now = datetime.now(timezone.utc).isoformat()
    status = {
        "blogger": {"success": blogger_ok, "url": blogger_url, "timestamp": now},
        "pinterest": {"success": pinterest_ok, "timestamp": now},
        "tumblr": {"success": tumblr_ok, "timestamp": now},
        "facebook": {"success": None, "timestamp": now},
        "instagram": {"success": None, "timestamp": now},
        "tiktok": {"success": None, "timestamp": now},
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
        # Code-level duplicate guard: the prompt already tells Gemini not to
        # repeat topics, but that is only a request. If the chosen topic is
        # too close to an existing post, ask again (showing the rejected
        # title in the "already covered" list) instead of publishing a repeat.
        prompt_history = list(history)
        for attempt in range(1, MAX_TOPIC_ATTEMPTS + 1):
            draft = normalize_draft(generate_draft(prompt_history, niche))
            duplicate_of = find_duplicate_title(draft["title"], history)
            quality_problems = find_quality_problems(draft)
            if duplicate_of is None and not quality_problems:
                if attempt >= MAX_TOPIC_ATTEMPTS or draft_has_hero_photo(draft, history):
                    break   # (the last attempt skips the photo gate: a post goes out rather than none)
                print(f"Topic '{draft['title']}' rejected (attempt {attempt}/{MAX_TOPIC_ATTEMPTS}): no suitable "
                      f"stock photo exists for it — asking for a more photographable topic.")
                prompt_history = prompt_history + [{"title": draft["title"]}]
                continue
            if duplicate_of is not None:
                print(f"Topic '{draft['title']}' is too similar to existing post "
                      f"'{duplicate_of}' (attempt {attempt}/{MAX_TOPIC_ATTEMPTS}) — asking for a different one.")
                prompt_history = prompt_history + [{"title": draft["title"]}]
            else:
                print(f"Draft '{draft['title']}' rejected (attempt {attempt}/{MAX_TOPIC_ATTEMPTS}): "
                      f"{'; '.join(quality_problems)} — asking again.")
        else:
            raise RuntimeError(
                f"Could not get an acceptable draft after {MAX_TOPIC_ATTEMPTS} attempts "
                f"(last: '{draft['title']}' — {duplicate_of and 'duplicate topic' or '; '.join(quality_problems)}). "
                f"Nothing was published this run."
            )
        fix_listicle_cost(draft)
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
        _photo_context["text"] = f"{draft.get('title', '')} {draft.get('_theme') or ''}"
        photo_plan = draft.get("_photo_plan") or plan_photo_queries(draft)
        print("Finding hero (Pinterest) photo...")
        hero_plan = photo_plan.get("hero", {})
        if draft.get("_hero_photo"):
            raw_hero, hero_photo_id = draft["_hero_photo"]   # already found by the topic check
            print("Hero photo was already found while checking the topic.")
        else:
            raw_hero, hero_photo_id = find_best_photo(
                hero_plan.get("queries", []) + [draft["image_prompt"]], hero_plan.get("ideal"),
                "portrait", used_photo_ids, subject=hero_plan.get("subject"),
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


        # --- Section images (horizontal, no text overlay, one per placeholder) ---
        section_images = draft.get("section_images", [])
        section_urls = {}
        for i, section in enumerate(section_images):
            token = section.get("token", f"IMG_{i+1}")
            query = section.get("query", draft["image_prompt"])
            print(f"Finding section photo for {token}: {query}")
            try:
                section_plan = photo_plan.get(token, {})
                raw_section, section_photo_id = find_best_photo(
                    section_plan.get("queries", []) + [query], section_plan.get("ideal"),
                    "landscape", used_photo_ids, strict=True, subject=section_plan.get("subject"),
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
                # shouldn't crash a run where the hero image and the
                # rest of the article are already done —
                # skip just this section's image; its [[IMG_n]] placeholder
                # gets cleaned up below like any other unmatched token.
                print(f"Section photo for {token} failed, skipping it: {e}")

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
            # The planner's "extra" slots (different objects/areas of the topic)
            # come first; the article's own section queries are the fallback.
            reel_slots = [photo_plan[k] for k in sorted(photo_plan) if k.startswith("extra_") and photo_plan[k].get("queries")]
            reel_slots += [{"queries": [q], "ideal": None, "subject": None} for q in section_queries]
            qi = 0
            while len(real_image_bytes) < n_content_slides:
                slot = reel_slots[qi % len(reel_slots)]
                query = slot["queries"][0]
                qi += 1
                try:
                    raw_bytes, photo_id = find_best_photo(
                        slot["queries"], slot["ideal"], "portrait", used_photo_ids,
                        target_ratio=9 / 16, strict=True, subject=slot.get("subject"),
                    )
                    real_image_bytes.append(raw_bytes)
                    this_run_photo_ids.append(photo_id)
                    used_photo_ids.add(photo_id)
                except Exception as e:
                    print(f"Couldn't fetch an extra portrait image for '{query}': {e}")
                    if qi > len(reel_slots) * 3:
                        break  # give up rather than loop forever if Pexels keeps failing

            n_content_slides = len(real_image_bytes)  # actual count, in case fetches came up short
            content_captions = split_script_into_captions(
                " ".join(content_sentences), n_content_slides
            )

            image_specs = [
                {"bytes": b, "caption": c, "duration": 1}  # duration set below
                for b, c in zip(real_image_bytes, content_captions)
            ]
            # The template has its own call-to-action bar, so the closing slide
            # simply shows the hero photo again.
            image_specs.append({"bytes": real_image_bytes[0], "caption": cta_line, "duration": 1, "is_cta": True})

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

            print(f"Building the Pinterest video (2:3) from {len(image_specs)} real-image slide(s)...")
            tpl_top, tpl_bottom, tpl_highlight = template_bar_texts(draft)
            pinterest_work_dir = os.path.join("images", f"reel-work-pin-{ts}")
            pinterest_video_bytes = video_template.build_template_video(
                image_specs, audio_path, pinterest_work_dir, width=1080, height=1620,
                top_text=tpl_top, bottom_text=tpl_bottom, bottom_highlight=tpl_highlight,
                music_path=pick_background_music(),
            )
            print(f"Pinterest video ready ({len(pinterest_video_bytes) / 1024:.0f} KB).")

            # Pinterest's video-pin cover_image_url rejects WebP (every
            # other image in this script is WebP) — it needs JPG/PNG. Also,
            # this MUST be cropped to the exact same 2:3 shape as the
            # Pinterest video built just above (not just the raw hero
            # photo's own shape, whatever Pexels happened to return that
            # as, and not 9:16 either — that mismatch was the cause of the
            # black letterboxing bars in the first place).
            # The thumbnail is the video's own look as a still (title bar with a
            # yellow word, photo, call to action), so every video pin's cover
            # matches its video instead of showing a plain photo.
            try:
                pin_cover_bytes = video_template.render_cover(
                    raw_hero, tpl_top, tpl_bottom, tpl_highlight, width=1080, height=1620)
            except Exception as e:
                # e.g. an older video_template.py without render_cover: a plain cover is
                # far better than losing the whole post.
                print(f"Template cover failed ({e}) — using the plain hero photo as the cover.")
                plain = crop_to_ratio(Image.open(BytesIO(raw_hero)).convert("RGB"), target_ratio=2 / 3)
                plain = plain.resize((1080, 1620), Image.LANCZOS)
                plain_out = BytesIO()
                plain.save(plain_out, format="JPEG", quality=85)
                pin_cover_bytes = plain_out.getvalue()
            pin_cover_filename = f"decor-{ts}-pin-cover.jpg"
            pin_cover_filepath = os.path.join("images", pin_cover_filename)
            with open(pin_cover_filepath, "wb") as f:
                f.write(pin_cover_bytes)
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

        # Prices in the article are estimates, so say so right under the first
        # table (done here in code, not in the prompt, so it's never skipped).
        if "estimates and vary" not in body_html:
            body_html = body_html.replace(
                "</table>",
                "</table><p><em>Prices are estimates and vary by store and region.</em></p>",
                1,
            )

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
            "Written by the DecorVibe team — budget home-decor ideas and "
            "thrift-store makeovers, researched and written up so you can "
            "recreate them affordably.</div>"
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
        # uses whatever image appears first as the page's og:image.
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
            save_status(blogger_ok=False, blogger_url=None, pinterest_ok=False)
            git_commit_and_push([STATUS_FILE], "Auto post: run failed before publishing")
        except Exception as status_err:
            print(f"Could not save failure status: {status_err}")
        send_phone_notification(
            "❌ DecorVibe run FAILED",
            f"The run failed before publishing anything.\n\nError: {e}",
        )
        raise

    print("Notifying Bing Submission API...")
    bing_ok = submit_to_bing(post_url)

    # Pinterest keeps a modest hashtag count; Tumblr uses a richer set from the same pool.
    pin_hashtags = build_pin_hashtags(draft.get("hashtag_labels", []), max_tags=5)
    tumblr_hashtags = build_pin_hashtags(draft.get("hashtag_labels", []), max_tags=15)

    # History (with URL, for future internal linking) is saved and committed
    # AFTER publishing, now that we actually know the post's URL.
    history.append({
        "title": draft["title"],
        "category": draft.get("category"),
        "format": draft.get("_format"),
        "theme": draft.get("_theme"),
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
                    draft["html"], hashtags=pin_hashtags, override=draft.get("pin_description"),
                    cta="👉 Visit our website for the full guide!",
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
                    draft["html"], hashtags=pin_hashtags, override=draft.get("pin_description"),
                    cta="👉 Visit our website for the full guide!",
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
        hashtags=tumblr_hashtags,
    )

    print("Updating the all-posts hub page + Bing backlog...")
    boost_indexing(post_url, bing_ok, history)

    save_status(blogger_ok=True, blogger_url=post_url, pinterest_ok=pinterest_ok, tumblr_ok=tumblr_ok)
    print("Committing history + status...")
    commit_paths = [HISTORY_FILE, STATUS_FILE]
    if os.path.exists(BING_BACKLOG_FILE):
        commit_paths.append(BING_BACKLOG_FILE)
    git_commit_and_push(commit_paths, f"Auto post history: {draft['title']}")

    def tick(ok):
        return "✅" if ok else "❌"

    send_phone_notification(
        f"{tick(True)} DecorVibe posted: {draft['title'][:60]}",
        f"{draft['title']}\n{post_url}\n\n"
        f"Blogger: {tick(True)}\n"
        f"Pinterest: {tick(pinterest_ok)}\n"
        f"Tumblr: {tick(tumblr_ok)}",
    )

    # Clean up what R2 only held for this run (Pinterest fetched its own copy of the
    # video cover). hero_url stays: Blogger embeds that exact URL permanently.
    print("Cleaning up temporary R2 files...")
    delete_from_r2(pin_cover_filepath)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        # Only when nothing was published (so a retry can't create a duplicate post):
        # Gemini rate limits recover within minutes, so wait and try once more.
        if "Nothing was published" not in str(e):
            raise
        wait_minutes = int(os.environ.get("PREPUBLISH_RETRY_MINUTES", "15"))
        if wait_minutes <= 0:
            raise
        print(f"No post was published ({e}). Waiting {wait_minutes} minutes, then trying once more...")
        time.sleep(wait_minutes * 60)
        _DEAD_MODELS.clear()
        _vision_state.update(used=0, disabled=False, fails=0, dead=set())
        main()
