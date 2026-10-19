""""""
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
from datetime import datetime, timedelta, timezone

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
    "fix", "fixes", "them", "cluttered", "clutter", "space", "spaces", "before", "after",
    # pure template words of the newer formats (renter-friendly ideas, quick fixes, checklists, palettes...).
    # Topic words such as gift, refresh, plan, small, clean or care are deliberately NOT here: two posts
    # about the same thing must still be recognised as duplicates.
    "renter", "renters", "friendly", "quick", "minute", "minutes", "swap", "swaps", "versus", "vs", "which",
    "checklist", "palette", "shopping", "list", "splurge", "save", "rules", "rule", "formula", "formulas",
    "solutions", "solution",
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


def find_repeated_text(html_text):
    """
    Returns a short snippet of the first sentence that is repeated (word for word, or with only a
    couple of words changed) elsewhere in the article, else None. Only long sentences count (10+
    words) and the match must be close (the two sentences share 30%+ of their 4-word runs), so
    normal prose never triggers it. A fully reworded repeat can't be caught by code; the prompt
    forbids that.
    """
    text = re.sub(r"</(?:p|li|h2|h3|td|th|tr|blockquote)>|<br\s*/?>", "\n", html_text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    sentences = []
    for block in text.split("\n"):
        for sent in re.split(r"(?<=[.!?])\s+", block):
            words = re.findall(r"[a-z0-9']+", sent.lower())
            if len(words) >= 10:
                sentences.append((sent.strip(), {tuple(words[i:i + 4]) for i in range(len(words) - 3)}))
    for i in range(len(sentences)):
        for j in range(i + 1, len(sentences)):
            a, b = sentences[i][1], sentences[j][1]
            if a and b and len(a & b) / len(a | b) >= 0.3:
                return sentences[j][0][:70]
    return None


def find_quality_problems(draft, min_words=400):
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
    if not subject_followed(draft):
        entry = next((e for e in SUBJECT_POOL if e[0] == draft.get("_subject")), None)
        problems.append(f"ignored the assigned subject ({entry[2] if entry else draft.get('_subject')})")
    repeated = find_repeated_text(draft.get("html", ""))
    if repeated:
        problems.append(f"repeats itself, padding with repeated text (e.g. \"{repeated}...\")")
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

    examples = {
        e.strip().lower()
        for text in [f.get("title_hint", "") for f in POST_FORMATS] + list(TITLE_STYLES)
        for e in re.findall(r"""['"]([^'"]{20,})['"]""", text)
    }
    if draft.get("title", "").strip().lower() in examples:
        problems.append("title was copied word for word from an example in the prompt")
    if re.match(r"^[A-Z][A-Z0-9 \-]{3,}:\s", draft.get("title", "")):
        problems.append("title starts with a capitalised label such as \"BEFORE-AND-AFTER:\"")

    # No prices anywhere (they change all the time and every total was a place for a mistake). The text is cleaned
    # by strip_prices(); a price that is still in the TITLE cannot be cleaned, so such a draft is rejected.
    if PRICE_RE.search(draft.get("title", "")):
        problems.append("the title contains a price (prices are not allowed anywhere)")
    elif PRICE_RE.search(draft.get("html", "")):
        problems.append("the article contains a price (prices are not allowed anywhere)")
    return problems


TITLE_STYLES = [
    "lead with the transformation, e.g. \"This Thrifted Lamp Now Looks Like a Designer Piece\"",
    "a curious question the project answers, e.g. \"Can a Thrifted Mirror Really Pass for Antique Brass?\"",
    "lead with the budget in words, e.g. \"A Hanging Planter From a Thrifted Colander on a Tiny Budget\"",
    "a clear, search-friendly tutorial title, e.g. \"DIY Aged Brass Boot Tray From a Thrifted Metal Tray\"",
    "a from-to makeover title, e.g. \"From Thrift Store Colander to Zinc Planter\"",
    "lead with the look, e.g. \"A Budget Entryway Bench That Looks Expensive\"",
    "a beginner-friendly project title, e.g. \"An Easy Beginner Project: Painted Wooden Bead Garland\"",
    "a time-led title, e.g. \"A Thrifted Frame Gallery Wall You Can Finish in One Afternoon\"",
    "a skip-the-store title, e.g. \"Skip the Store: A Thrifted Crate Side Table on a Budget\"",
    "a materials-led title, e.g. \"Wood, Rope and Paint: A Simple DIY Wall Shelf\"",
    "a no-tools title, e.g. \"A No-Drill Thrifted Frame Gallery for Renters\"",
    "a small-detail title, e.g. \"The Little Trick That Makes Plain Candle Holders Look Aged\"",
    "a plain how-to title, e.g. \"How to Make a Rope Handle Basket Look Custom\"",
    "a before-you-start title, e.g. \"What to Know Before You Paint a Thrifted Wooden Tray\"",
    "a for-whom title, e.g. \"A Simple Nightstand Refresh for Small Bedrooms\"",
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
    {"name": 'project tutorial', "weight": 3, "use_title_styles": True, "needs_list": True,
     "instruction": 'A single start-to-finish thrift-flip or DIY project, explained in simple numbered steps. Name every material once in the list at the top and use only those. Never mention a price or any dollar amount.'},
    {"name": 'listicle', "weight": 3, "use_title_styles": False,
     "instruction": 'A numbered list post of 5-6 ideas. Each idea gets its own h2/h3 with one concrete tip and why it works. NOT a single step-by-step project. Never mention a price or any dollar amount.',
     "title_hint": "number-led and specific, in your own words, for example '7 Budget Ways to Make a Small Bedroom Feel Bigger' or '5 Easy Swaps That Make a Rental Kitchen Look Custom' or '9 Thrift Finds That Look Expensive on a Shelf'"},
    {"name": 'room refresh plan', "weight": 2, "use_title_styles": False,
     "instruction": 'A refresh plan for ONE room or area on a budget: what to change first, what to skip and the order to do it in. Keep it to 4-5 simple steps. Never mention a price or any dollar amount.',
     "title_hint": "in your own words, for example 'A Cozy Living Room Refresh on a Real Budget' or 'How to Refresh a Tiny Bathroom Without Overspending' or 'A Weekend Entryway Makeover on a Budget'"},
    {"name": 'thrift buying guide', "weight": 2, "use_title_styles": False,
     "instruction": 'A guide to buying ONE kind of item second-hand: what to look for, the red flags and where to find it. Never mention a price or any dollar amount.',
     "title_hint": "practical and specific, in your own words, for example 'What to Look For When Thrifting Wooden Bowls' or 'Buying Second-Hand Mirrors: What to Check First'"},
    {"name": 'mistakes and fixes', "weight": 2, "use_title_styles": False,
     "instruction": 'Common mistakes that make a room look cheap, cluttered or dated, each paired with one specific, simple fix. Never mention a price or any dollar amount.',
     "title_hint": "number-led, in your own words, for example '5 Porch Mistakes That Make Your Home Look Uninviting' or '6 Shelf Styling Mistakes and How to Fix Them'"},
    {"name": 'project walkthrough with pitfalls', "weight": 2, "use_title_styles": True, "needs_list": True,
     "instruction": "A walkthrough of one makeover that is honest about what can go wrong: the common pitfalls at each step, how to avoid them and what the finished result should look like. Written as a guide ('you'), NOT as a personal story. Name every material once in the list at the top. Never mention a price or any dollar amount."},
    {"name": 'designer look for less', "weight": 2, "use_title_styles": False,
     "instruction": 'Recreate a high-end look with budget or thrifted alternatives. Describe the pricey version in general terms (never a brand, store or product) and compare it to the budget version piece by piece, in words only. Never mention a price or any dollar amount.',
     "title_hint": "in your own words, for example 'The Designer Mantel Look Without the Designer Budget' or 'A Boutique Hotel Bedroom Look on a Budget'"},
    {"name": 'styling guide', "weight": 2, "use_title_styles": False,
     "instruction": 'How to style ONE spot (mantel, shelf, entry table, coffee table, nightstand, porch) in simple layers with easy rules a beginner can follow. Never mention a price or any dollar amount.',
     "title_hint": "in your own words, for example 'How to Style a Mantel in 4 Simple Layers' or 'A Simple Way to Style Your Coffee Table Like a Designer'"},
    {"name": 'myth-busting', "weight": 1, "use_title_styles": False,
     "instruction": 'Pick 4-5 common decorating or thrifting myths and say honestly what is true, with the reason, in plain words. Base it on how materials and rooms generally behave. Never mention a price or any dollar amount.',
     "title_hint": "in your own words, for example '5 Thrifting Myths That Cost You Good Finds' or 'Does Peel-and-Stick Really Damage Walls?'"},
    {"name": 'renter-friendly ideas', "weight": 2, "use_title_styles": False,
     "instruction": 'A numbered list post of 5-6 ideas that need no drilling, no painting and nothing permanent, so a renter can undo every change and keep the deposit. Each idea gets its own h2/h3 with a concrete tip and how it comes off again. Never mention a price or any dollar amount.',
     "title_hint": "number-led and specific, in your own words, for example '6 Renter-Friendly Ways to Warm Up a Plain Living Room' or '5 No-Drill Ideas That Make a Rental Bathroom Feel Finished'"},
    {"name": 'quick fixes under an hour', "weight": 2, "use_title_styles": False,
     "instruction": 'A numbered list post of 5-6 quick improvements that each take 10-60 minutes. Each gets its own h2/h3 with the exact steps in two or three sentences and how long it takes. Never mention a price or any dollar amount.',
     "title_hint": "number-led and specific, in your own words, for example '7 Fifteen-Minute Fixes That Make a Room Look Finished' or '6 Quick Entryway Upgrades You Can Do Tonight'"},
    {"name": 'diy gift ideas', "weight": 1, "use_title_styles": False,
     "instruction": 'A numbered list post of 5-6 handmade or thrift-flip gift ideas for a specific kind of person or occasion. Each gets its own h2/h3 with what to make, the supplies and a simple way to wrap it. Never mention a price or any dollar amount.',
     "title_hint": "number-led and specific, in your own words, for example '5 Thoughtful Gifts to Make From Thrift Store Finds' or '6 Simple Handmade Gifts for People Who Love Their Kitchen'"},
    {"name": 'this vs that', "weight": 1, "use_title_styles": False,
     "instruction": 'An honest side-by-side comparison of two budget options for the same job (for example paint vs peel-and-stick): effort, look, durability and who should pick which. Base it on how the materials generally behave; never claim you tested them. End with a clear recommendation for each type of reader. Never mention a price or any dollar amount.',
     "title_hint": "a clear comparison, in your own words, for example 'Paint or Peel-and-Stick: Which Is Better for a Rental Wall?' or 'Thrifted Baskets or New Ones: Which Is Worth It?'"},
    {"name": 'seasonal checklist', "weight": 1, "use_title_styles": False,
     "instruction": 'A practical checklist for getting ONE space ready for the season or an event, grouped into what to do first, next and last, with roughly how long each task takes. Never mention a price or any dollar amount.',
     "title_hint": "in your own words, for example 'A Simple Checklist for Getting Your Entryway Ready for Guests' or 'A Weekend Checklist to Make a Guest Room Feel Welcoming'"},
    {"name": 'color palette guide', "weight": 1, "use_title_styles": False,
     "instruction": 'How to choose and build a cohesive colour palette for one room on a budget: how many colours, the 60-30-10 idea, where each colour shows up in the room and a cheap way to add each one. Use generic colour names only. Never mention a price or any dollar amount.',
     "title_hint": "in your own words, for example 'How to Build a Calm Neutral Palette for a Bedroom on a Budget' or 'Choosing Warm Colors for a Cozy Living Room Without Repainting'"},
    {"name": 'small space solutions', "weight": 2, "use_title_styles": False,
     "instruction": 'A problem-and-solution post for ONE kind of small space (tiny bathroom, studio, narrow hallway, small bedroom): 5-6 specific problems, each paired with one inexpensive solution. Never mention a price or any dollar amount.',
     "title_hint": "number-led and specific, in your own words, for example '6 Ways to Fit More Storage Into a Tiny Bathroom' or '5 Smart Fixes for a Narrow Hallway'"},
    {"name": 'care and cleaning guide', "weight": 1, "use_title_styles": False,
     "instruction": 'How to clean, refresh or safely repair ONE kind of second-hand item or material (wood, brass, glass, ceramic, wicker, textiles): what to test first, the gentle method and what to avoid. Written from how materials generally behave; never claim you tried it. Never mention a price or any dollar amount.',
     "title_hint": "practical and specific, in your own words, for example 'How to Clean and Refresh Thrifted Brass Without Ruining It' or 'Giving Old Wicker Baskets a Second Life: What Works'"},
    {"name": 'thrift shopping list', "weight": 2, "use_title_styles": False,
     "instruction": 'A curated shopping list for ONE room or look: what to hunt for on a thrift-store trip and what to check on each piece before buying it. Never mention a price or any dollar amount.',
     "title_hint": "in your own words, for example 'A Thrift Store Shopping List for a Cozy Reading Nook' or 'What to Hunt for When Thrifting a Warm Entryway'"},
    {"name": 'splurge or save', "weight": 1, "use_title_styles": False,
     "instruction": 'Where to spend a little more and where to save when decorating ONE room, piece by piece, with a clear reason for each choice. Say it in words (spend a bit more on..., save on...). Never mention a price or any dollar amount.',
     "title_hint": "in your own words, for example 'Where to Splurge and Where to Save When Decorating a Living Room' or 'Spend or Save: A Practical Guide to Furnishing a Small Bedroom'"},
    {"name": 'decor rules', "weight": 1, "use_title_styles": False,
     "instruction": 'Simple designer rules of thumb (rule of three, odd numbers, 60-30-10, eye level, one big statement piece) applied to ONE spot, with a beginner-friendly example for each rule. Never mention a price or any dollar amount.',
     "title_hint": "number-led and specific, in your own words, for example '5 Easy Decorating Rules That Make a Shelf Look Styled' or '4 Simple Rules for a Mantel That Looks Balanced'"},
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

# extra non-holiday themes per month (no holiday names, so they never expire), for more variety
_EXTRA_SEASONAL_THEMES = {
    1: ["cozy winter reading nook", "winter entryway with boot storage", "bedroom refresh with warm layers"],
    2: ["late-winter organization and reset", "cozy bathroom spa corner", "bright winter-to-spring windowsill"],
    3: ["spring entryway refresh", "light and fresh bedroom for spring", "spring cleaning organization nooks"],
    4: ["fresh kitchen refresh for spring", "balcony herb and flower planters", "bright spring bedroom"],
    5: ["porch and patio planters", "light summer bedroom", "outdoor dining on a budget"],
    6: ["small balcony and apartment patio", "summer kitchen and entertaining", "beach-inspired bathroom and bedroom"],
    7: ["cool and airy summer bedroom", "summer porch lighting", "organized pantry and kitchen reset"],
    8: ["first apartment kitchen basics", "entryway drop zone for back-to-school", "end-of-summer porch refresh"],
    9: ["cozy fall bedroom", "fall kitchen and pantry", "warm fall entryway"],
    10: ["cozy fall bathroom and guest room", "hosting-ready living room", "warm lighting and candles for dark evenings"],
    11: ["hosting-ready guest room", "cozy bedroom for winter", "kitchen and dining table for gatherings"],
    12: ["cozy winter bedroom and bath", "organized entryway for guests", "warm lighting for long evenings"],
}
for _m, _extra in _EXTRA_SEASONAL_THEMES.items():
    SEASONAL_THEMES[_m] = (SEASONAL_THEMES[_m][0] + _extra, SEASONAL_THEMES[_m][1])

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


def _utcnow():
    return datetime.now(timezone.utc)


def post_out_of_season(h, today=None):
    """
    True for an older holiday post that isn't worth showing right now (e.g. a Halloween post after
    8 October). A post counts as in season during the 90 days before its holiday's last-useful date
    (THEME_EXPIRY), judged from its title + saved theme. Posts that mention no holiday are never
    out of season. If it mentions several holidays, it stays as long as any one of them is in season.
    """
    today = today or _utcnow()
    text = f"{h.get('theme') or ''} {h.get('title') or ''}".lower()
    matched = [(key, cutoff) for key, cutoff in THEME_EXPIRY if key in text]
    if not matched:
        return False
    for key, (month, day) in matched:
        for year in (today.year - 1, today.year, today.year + 1):
            end = datetime(year, month, day, tzinfo=timezone.utc)
            if end - timedelta(days=90) <= today <= end + timedelta(days=1):
                return False
    return True


def pick_related_posts(history, category, today=None, limit=3):
    """Up to `limit` posts for the "You Might Also Like" box: same category first, then the newest
    posts overall, never repeating a title and never an out-of-season holiday post."""
    related, seen_titles = [], set()

    def add_related(candidates):
        for h in candidates:
            key = re.sub(r"\W+", " ", h.get("title", "").lower()).strip()
            if h.get("url") and key and key not in seen_titles and len(related) < limit:
                seen_titles.add(key)
                related.append(h)

    in_season = [h for h in history if not post_out_of_season(h, today)]
    add_related(reversed([h for h in in_season if h.get("category") == category]))
    add_related(reversed(in_season))
    return related


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
STYLE_LENSES += [
    "transitional", "traditional / classic", "dark academia", "coastal grandmother", "organic modern", "retro 70s",
    "english country", "maximalist", "desert modern / southwest", "minimalist", "art deco touches", "global / well-travelled",
]


# Specific, stock-photo-friendly subjects. The script hands ONE of them to Gemini for each post (the one
# used least so far), so the model no longer has to invent a topic that is different from ~150 older posts.
# (id, lowercase keywords that count as "already used" when found in an old title, how it is described)
SUBJECT_POOL = [
    ("mantel", ("mantel", "fireplace"), "a fireplace mantel"),
    ("coffee-table", ("coffee table",), "a coffee table"),
    ("bookshelf", ("bookshelf", "bookcase", "book shelf"), "a bookshelf or bookcase"),
    ("console-table", ("console table", "sofa table"), "a console table behind a sofa"),
    ("throw-pillows", ("pillow", "cushion"), "throw pillows and sofa cushions"),
    ("floor-lamp", ("floor lamp",), "a floor lamp corner"),
    ("side-table", ("side table", "end table"), "a side table next to a sofa or chair"),
    ("blanket-basket", ("blanket",), "a basket of throw blankets"),
    ("gallery-wall", ("gallery wall", "wall art"), "a gallery wall of frames"),
    ("tv-stand", ("tv stand", "media console", "tv wall"), "a TV stand or media console"),
    ("area-rug", ("area rug", "living room rug"), "an area rug"),
    ("curtains", ("curtain", "drape"), "window curtains"),
    ("window-sill", ("window sill", "windowsill"), "a window sill"),
    ("pouf", ("pouf", "ottoman"), "an ottoman or pouf"),
    ("accent-chair", ("accent chair", "armchair"), "an accent chair corner"),
    ("reading-nook", ("reading nook",), "a reading nook"),
    ("open-shelves", ("open shelf", "open shelves", "kitchen shelf"), "open kitchen shelves"),
    ("kitchen-island", ("kitchen island",), "a kitchen island"),
    ("spice-rack", ("spice",), "spice storage"),
    ("cutting-boards", ("cutting board", "pastry board", "charcuterie"), "wooden cutting boards"),
    ("utensil-crock", ("utensil",), "a utensil crock by the stove"),
    ("fruit-bowl", ("fruit bowl", "fruit basket"), "a fruit bowl on the counter"),
    ("herb-garden", ("herb",), "a kitchen window herb garden"),
    ("pantry", ("pantry",), "pantry shelves and jars"),
    ("cabinet-hardware", ("cabinet knob", "cabinet hardware", "cabinet pull", "cabinet door"), "cabinet knobs and handles"),
    ("kitchen-cart", ("kitchen cart", "bar cart"), "a kitchen or bar cart"),
    ("serving-tray", ("serving tray",), "a serving tray"),
    ("mug-display", ("mug",), "a mug display"),
    ("cake-stand", ("cake stand",), "a cake stand"),
    ("dining-table", ("dining table", "centerpiece"), "a dining table centerpiece"),
    ("table-setting", ("table runner", "placemat", "tablescape"), "a table setting with runner and placemats"),
    ("vanity-tray", ("vanity",), "a bathroom vanity tray"),
    ("towel-storage", ("towel",), "bathroom towel storage"),
    ("shower-curtain", ("shower curtain",), "a shower curtain"),
    ("bathroom-mirror", ("bathroom mirror",), "a bathroom mirror"),
    ("soap-dispenser", ("soap",), "soap dispensers and bath accessories"),
    ("bath-mat", ("bath mat", "bathmat", "bath rug"), "a bath mat"),
    ("bathroom-shelf", ("over the toilet", "over-the-toilet", "bathroom shelf"), "a shelf above the toilet"),
    ("bathroom-plant", ("bathroom plant",), "plants in a bathroom"),
    ("bathroom-baskets", ("bathroom basket",), "bathroom storage baskets"),
    ("headboard", ("headboard",), "a headboard"),
    ("nightstand", ("nightstand", "bedside"), "a nightstand"),
    ("bedding", ("bedding", "duvet", "comforter"), "layered bedding"),
    ("dresser-top", ("dresser",), "a dresser top"),
    ("above-bed", ("above the bed", "above your bed", "above a bed", "above bed", "over the bed", "over your bed"), "the wall above a bed"),
    ("bed-bench", ("foot of the bed", "bed bench"), "a bench at the foot of the bed"),
    ("jewelry-display", ("jewelry",), "a jewelry display"),
    ("bedroom-curtains", ("blackout", "bedroom curtain"), "bedroom window treatments"),
    ("closet", ("closet", "wardrobe"), "a closet or wardrobe"),
    ("shoe-storage", ("shoe",), "shoe storage by the door"),
    ("wall-hooks", ("hooks", "coat rack"), "wall hooks or a coat rack"),
    ("key-tray", ("key tray", "mail", "key holder"), "a key tray and mail station"),
    ("entry-mirror", ("entryway mirror", "entry mirror"), "a mirror by the front door"),
    ("umbrella-stand", ("umbrella",), "an umbrella stand"),
    ("entry-bench", ("entryway bench", "entry bench", "mudroom"), "an entryway bench or mudroom corner"),
    ("doormat", ("doormat", "welcome mat"), "a doormat"),
    ("porch-planters", ("porch planter", "planter"), "porch planters"),
    ("string-lights", ("string light", "fairy light"), "outdoor string lights"),
    ("outdoor-rug", ("outdoor rug",), "an outdoor rug"),
    ("garden-pots", ("garden pot", "terracotta", "flower pot"), "garden pots and terracotta"),
    ("bird-feeder", ("bird",), "a bird feeder or bird bath"),
    ("house-numbers", ("house number", "mailbox"), "house numbers and a mailbox"),
    ("door-wreath", ("wreath", "front door"), "a front door wreath"),
    ("patio-table", ("patio table", "outdoor table"), "an outdoor table setting"),
    ("outdoor-seating", ("outdoor cushion", "patio seating", "patio furniture"), "outdoor seating and cushions"),
    ("window-boxes", ("window box",), "window boxes"),
    ("lanterns", ("lantern",), "lanterns"),
    ("garden-path", ("garden path", "stepping stone", "walkway"), "a garden path"),
    ("room-divider", ("room divider",), "a room divider"),
    ("floating-shelves", ("floating shelf", "floating shelves"), "floating shelves"),
    ("over-door", ("over-the-door", "over the door"), "over-the-door storage"),
    ("under-bed", ("under-bed", "under bed"), "under-bed storage"),
    ("corner-shelf", ("corner shelf",), "a corner shelf"),
    ("desk-nook", ("wall desk", "desk"), "a small desk nook"),
    ("folding-table", ("folding table", "drop-leaf"), "a fold-down or drop-leaf table"),
    ("candles", ("candle",), "candles and candle holders"),
    ("vases", ("vase",), "vases with fresh or faux stems"),
    ("baskets", ("basket",), "decorative baskets"),
    ("wall-mirrors", ("round mirror", "wall mirror", "leaning mirror"), "a decorative wall mirror"),
    ("picture-frames", ("picture frame", "photo frame"), "picture frames"),
    ("faux-plants", ("faux plant", "fake plant", "houseplant", "succulent"), "faux plants or houseplants"),
    ("wall-clock", ("clock",), "a wall clock"),
    ("table-lamp", ("lampshade", "lamp shade", "table lamp"), "a table lamp and lampshade"),
    ("wooden-crates", ("crate",), "wooden crates"),
    ("glass-jars", ("jar", "apothecary"), "glass jars and apothecary jars"),
    ("book-stacks", ("stack of books", "book stack"), "stacked books as decor"),
    ("terrarium", ("terrarium", "cloche"), "a terrarium or glass cloche"),
    ("decor-trays", ("decorative tray",), "decorative trays"),
    ("picture-ledge", ("picture ledge",), "a picture ledge"),
    ("woven-wall", ("macrame", "woven wall"), "woven or macrame wall hangings"),
    ("dried-flowers", ("pampas", "dried flower", "dried stems"), "dried flowers and pampas grass"),
]


SUBJECT_POOL += [
    ("fireplace-screen", ("fireplace screen", "firewood"), "a fireplace screen and firewood basket"),
    ("wall-sconces", ("sconce",), "wall sconces"),
    ("plant-stand", ("plant stand",), "a plant stand"),
    ("sofa-styling", ("sofa styling", "styling a sofa", "couch"), "styling a sofa or couch"),
    ("window-seat", ("bay window", "window seat"), "a window seat or bay window"),
    ("built-in-shelves", ("built-in", "built in"), "built-in shelves"),
    ("accent-wall", ("accent wall",), "an accent wall"),
    ("wallpaper", ("peel-and-stick", "peel and stick", "wallpaper"), "peel-and-stick wallpaper"),
    ("chandelier", ("chandelier", "pendant light", "light fixture"), "a chandelier or pendant light"),
    ("layered-lighting", ("lighting layers", "layered lighting"), "layered lighting"),
    ("rug-layering", ("rug layering", "layered rug"), "layered rugs"),
    ("shelf-styling", ("shelfie", "shelf styling"), "a styled wall shelf"),
    ("sideboard", ("sideboard", "buffet table"), "a sideboard or buffet"),
    ("display-cabinet", ("china cabinet", "display cabinet"), "a display cabinet"),
    ("dining-chairs", ("dining chair",), "dining chairs"),
    ("home-bar", ("home bar", "bar area", "drink station"), "a home bar or drink station"),
    ("coffee-station", ("coffee station", "coffee bar"), "a coffee station"),
    ("breakfast-nook", ("breakfast nook",), "a breakfast nook"),
    ("wine-storage", ("wine rack", "wine storage"), "a wine rack or wine storage"),
    ("bread-box", ("bread box", "counter storage"), "a bread box and counter storage"),
    ("pot-rack", ("pot rack", "pan rack", "hanging pot"), "a pot rack"),
    ("dish-rack", ("dish rack", "dish drying"), "a dish drying area"),
    ("sink-area", ("sink area", "kitchen sink", "dish soap"), "the area around the kitchen sink"),
    ("backsplash", ("backsplash",), "a backsplash"),
    ("kitchen-rug", ("kitchen rug", "runner rug"), "a kitchen rug or runner"),
    ("kitchen-window", ("kitchen window",), "a kitchen window"),
    ("countertop-styling", ("countertop",), "countertop styling"),
    ("tea-station", ("tea station", "tea cart", "tea bar"), "a tea station or tea cart"),
    ("laundry-nook", ("laundry",), "a laundry nook"),
    ("hamper", ("hamper",), "a laundry hamper"),
    ("linen-closet", ("linen closet", "linen shelf"), "a linen closet"),
    ("blanket-ladder", ("towel ladder", "blanket ladder"), "a blanket or towel ladder"),
    ("toilet-paper", ("toilet paper",), "toilet paper storage"),
    ("bathroom-window", ("bathroom window",), "a bathroom window"),
    ("bathroom-rug", ("bathroom rug",), "a bathroom rug"),
    ("bath-tray", ("bathtub tray", "bath tray", "bathtub"), "a bathtub tray and bath setup"),
    ("medicine-cabinet", ("medicine cabinet",), "a medicine cabinet"),
    ("bathroom-lighting", ("vanity light", "bathroom light"), "bathroom lighting"),
    ("powder-room", ("guest bathroom", "powder room"), "a powder room"),
    ("bedside-lamp", ("bedside lamp",), "a bedside lamp"),
    ("bed-canopy", ("canopy",), "a bed canopy"),
    ("vanity-table", ("vanity table", "makeup station", "makeup"), "a vanity table or makeup station"),
    ("nursery", ("nursery",), "a nursery corner"),
    ("kids-room", ("kids room", "kid's room", "toy storage", "playroom"), "a kids room or toy storage"),
    ("dorm-room", ("teen room", "dorm room", "dorm"), "a dorm or teen room"),
    ("guest-room", ("guest room", "guest bedroom"), "a guest room"),
    ("bedroom-wall", ("bedroom wall",), "a bedroom wall"),
    ("bedroom-rug", ("bedroom rug",), "a bedroom rug"),
    ("bedroom-storage", ("bedroom storage",), "bedroom storage"),
    ("pillow-layering", ("pillow arrangement", "pillow layering"), "bed pillow arrangement"),
    ("quilt-display", ("quilt",), "a quilt display"),
    ("home-office", ("home office", "work from home"), "a home office corner"),
    ("desk-setup", ("desk setup", "desk organization", "desk organizer"), "a desk setup"),
    ("cord-hiding", ("cord", "cable"), "hiding cords and cables"),
    ("pegboard", ("pegboard",), "a pegboard wall"),
    ("cork-board", ("bulletin board", "cork board", "memo board"), "a cork board or memo board"),
    ("command-center", ("calendar", "command center"), "a family command center"),
    ("craft-storage", ("craft storage", "craft room"), "craft storage"),
    ("closet-organizer", ("closet organizer", "closet organization"), "closet organization"),
    ("mudroom-lockers", ("lockers", "cubbies"), "mudroom lockers or cubbies"),
    ("hallway", ("hallway", "hall runner"), "a hallway runner or hallway wall"),
    ("staircase", ("staircase", "stair wall", "stairs"), "a staircase wall"),
    ("stair-landing", ("stair landing", "landing nook", "landing piece", "the landing"), "a stair landing nook"),
    ("foyer-table", ("foyer", "entry table", "entryway table"), "a foyer or entry table"),
    ("coat-closet", ("coat closet",), "a coat closet"),
    ("porch-swing", ("porch swing",), "a porch swing"),
    ("balcony", ("balcony",), "a small balcony"),
    ("patio-shade", ("patio umbrella", "umbrella shade"), "patio shade"),
    ("garden-bench", ("garden bench",), "a garden bench"),
    ("raised-beds", ("raised bed",), "raised garden beds"),
    ("herb-planter", ("herb planter", "herb pot"), "an outdoor herb planter"),
    ("hanging-baskets", ("hanging basket", "hanging planter"), "hanging baskets"),
    ("curb-appeal", ("front yard", "curb appeal", "flower bed"), "curb appeal and flower beds"),
    ("welcome-sign", ("welcome sign",), "a welcome sign"),
    ("porch-chairs", ("porch chair", "rocking chair"), "porch chairs"),
    ("solar-lighting", ("solar light", "path light", "landscape lighting"), "solar and path lighting"),
    ("outdoor-dining", ("outdoor dining", "backyard dinner"), "an outdoor dining setup"),
    ("tall-planters", ("patio pot", "tall planter"), "tall patio planters"),
    ("trellis", ("trellis",), "a garden trellis"),
    ("hammock", ("hammock",), "a hammock corner"),
    ("garden-signs", ("garden sign", "garden marker"), "garden signs and markers"),
    ("water-feature", ("fountain", "water feature"), "a small fountain or water feature"),
    ("plant-hangers", ("macrame plant", "plant hanger"), "plant hangers"),
    ("shells-coastal", ("seashell", "coastal accent"), "shells and coastal accents"),
    ("stones-pebbles", ("river rock", "pebble"), "stones and pebbles as decor"),
    ("wood-slices", ("wood slice", "log slice"), "wood slices"),
    ("fabric-remnants", ("fabric",), "fabric remnants"),
    ("thrifted-art", ("thrifted art", "oil painting", "canvas art"), "thrifted art"),
    ("art-prints", ("art print", "framed print", "printable"), "art prints"),
    ("mirror-collage", ("mirror gallery", "mirror collage"), "a mirror gallery"),
    ("plate-wall", ("plate wall", "wall plates"), "a plate wall"),
    ("shelf-brackets", ("bracket",), "shelf brackets"),
    ("hardware-update", ("drawer pull", "knobs", "hardware"), "updating hardware"),
    ("lamp-makeover", ("lamp makeover", "lamp base"), "a lamp makeover"),
    ("candlesticks", ("candlestick", "taper"), "candlesticks and tapers"),
    ("votives", ("votive", "tea light", "tealight"), "votives and tea lights"),
    ("decor-bowls", ("decorative bowl", "wooden bowl"), "decorative bowls"),
    ("decor-boxes", ("decorative box", "trinket", "keepsake"), "decorative boxes"),
    ("vintage-books", ("book cover", "vintage books"), "vintage books as decor"),
    ("pottery", ("pottery", "ceramic"), "pottery and ceramics"),
    ("glass-bottles", ("glassware", "glass bottle"), "glass bottles and glassware"),
    ("brass-copper", ("brass", "copper"), "brass and copper accents"),
    ("flatware-decor", ("silverware", "flatware"), "silver and flatware as decor"),
    ("table-linens", ("tablecloth", "napkin"), "linens and napkins"),
    ("wall-baskets", ("wall basket",), "wall baskets"),
    ("rope-jute", ("rope", "jute", "twine"), "rope and jute accents"),
    ("reclaimed-wood", ("wood plank", "pallet", "reclaimed"), "reclaimed wood and pallets"),
    ("chalkboard", ("chalkboard", "blackboard"), "a chalkboard"),
    ("magazine-rack", ("magazine",), "a magazine rack"),
    ("record-display", ("record player", "vinyl record"), "records and a record display"),
    ("globe-map", ("globe", "world map", "vintage map"), "a globe or map display"),
    ("ladder-shelf", ("ladder shelf", "ladder"), "a ladder shelf"),
    ("wire-baskets", ("wire basket",), "wire baskets"),
    ("interior-door", ("door hanging", "door decor", "interior door"), "an interior door"),
    ("window-treatments", ("window film", "window treatment"), "window treatments"),
    ("sheer-curtains", ("sheer",), "sheer curtains"),
    ("shades-blinds", ("roman shade", "blinds"), "shades and blinds"),
    ("floor-cushions", ("floor cushion",), "floor cushions"),
    ("casual-seating", ("bean bag",), "casual seating"),
    ("storage-bench", ("storage bench", "storage ottoman"), "a storage bench"),
    ("hearth", ("hearth",), "a hearth"),
    ("faux-fireplace", ("faux fireplace", "electric fireplace"), "a faux fireplace"),
    ("shoe-bench", ("shoe bench",), "a shoe bench"),
    ("party-backdrop", ("backdrop", "party decor"), "a party backdrop"),
    ("gift-wrap-station", ("gift wrap", "wrapping station"), "a gift wrapping station"),
    ("floral-centerpiece", ("floral centerpiece",), "a floral centerpiece"),
    ("place-settings", ("place card", "place setting"), "place settings"),
    ("buffet-station", ("buffet station", "hosting"), "a buffet or hosting setup"),
    ("movie-corner", ("movie night", "media room"), "a movie night corner"),
    ("game-corner", ("game night", "board game"), "a game night corner"),
    ("music-corner", ("music corner", "guitar"), "a music corner"),
    ("yoga-corner", ("yoga", "meditation"), "a yoga or meditation corner"),
    ("pet-corner", ("pet corner", "pet bed", "dog bed", "cat shelf"), "a pet corner"),
    ("plant-shelf", ("plant shelf",), "a plant shelf"),
    ("sunroom", ("sunroom", "sun room"), "a sunroom"),
    ("galley-kitchen", ("tiny kitchen", "galley kitchen"), "a tiny galley kitchen"),
    ("studio-apartment", ("studio apartment",), "a studio apartment"),
    ("first-apartment", ("first apartment",), "a first apartment"),
    ("rental-space", ("rental",), "a rental space"),
    ("narrow-room", ("narrow",), "a narrow room"),
    ("basement", ("low ceiling", "basement"), "a basement or low-ceiling room"),
    ("low-light-room", ("dark room", "low light", "north-facing"), "a dark or low-light room"),
    ("open-concept", ("open plan", "open-concept", "open concept"), "an open-concept space"),
    ("awkward-corner", ("corner nook", "awkward corner"), "an awkward corner"),
    ("wall-niche", ("niche",), "a wall niche"),
    ("ceiling-decor", ("ceiling",), "ceiling decor and lighting"),
]


# Months in which a weather-bound subject makes sense (US seasons; (10, 3) means October through March).
# Subjects not listed here are usable all year.
SUBJECT_SEASON = {
    "porch-planters": (3, 10), "outdoor-rug": (4, 9), "garden-pots": (3, 9), "patio-table": (4, 9),
    "outdoor-seating": (4, 9), "window-boxes": (3, 8), "garden-path": (4, 9), 
    "porch-swing": (4, 9), "balcony": (4, 9), "patio-shade": (5, 9), "garden-bench": (4, 10),
    "raised-beds": (3, 7), "herb-planter": (3, 8), "hanging-baskets": (4, 8), "curb-appeal": (3, 10),
    "porch-chairs": (4, 10), "solar-lighting": (3, 10), "outdoor-dining": (4, 9), "tall-planters": (3, 9),
    "trellis": (3, 8), "hammock": (5, 9), "garden-signs": (3, 8), "water-feature": (4, 9),
    "fireplace-screen": (10, 3), "hearth": (10, 3), "faux-fireplace": (10, 3),
    "blanket-basket": (9, 4), "blanket-ladder": (9, 4), "quilt-display": (9, 4),
}


def subject_in_season(subject_id, month):
    window = SUBJECT_SEASON.get(subject_id)
    if not window:
        return True
    lo, hi = window
    return lo <= month <= hi if lo <= hi else (month >= lo or month <= hi)



_GENERIC_SUBJECT_WORDS = {"above", "around", "setup", "accents", "corner", "fresh", "stems", "decor", "small",
                          "space", "area", "storage", "display", "styling", "styled", "setting", "spots", "bedroom"}


LIST_FORMATS = {"listicle", "quick fixes under an hour", "renter-friendly ideas", "small space solutions", "seasonal checklist",
                "diy gift ideas", "thrift shopping list", "splurge or save", "decor rules", "mistakes and fixes"}


def subject_followed(draft):
    """
    Did the article really stay on the assigned subject? The subject's words are counted in the body.
    - List posts ("7 quick fixes", "5 ideas"...) must be about the subject in every item, so the body has to name
      it about once per 150 words (at least 4 times).
    - Other posts pass when the body names it at least 3 times, or when the title names it and the body at least twice.
    Drafts without an assigned subject always pass.
    """
    sid = draft.get("_subject")
    entry = next((e for e in SUBJECT_POOL if e[0] == sid), None)
    if not entry:
        return True
    _, kws, phrase = entry
    title = (draft.get("title") or "").lower()
    body = re.sub(r"<[^>]+>", " ", draft.get("html") or "").lower()
    words = [w for w in re.findall(r"[a-z]+", phrase.lower()) if len(w) >= 5 and w not in _GENERIC_SUBJECT_WORDS]
    count = max(sum(body.count(k) for k in kws),
                sum(len(re.findall(rf"\b{re.escape(w)}(?:s|es)?\b", body)) for w in words))
    if draft.get("_format") in LIST_FORMATS:
        return count >= max(4, round(len(body.split()) / 150))
    title_has = any(k in title for k in kws) or any(re.search(rf"\b{re.escape(w)}(?:s|es)?\b", title) for w in words)
    return count >= 3 or (title_has and count >= 2)


def pick_subject(history, today=None):
    """
    Returns the (id, keywords, phrase) of the subject used least so far among those that fit the current
    month, preferring ones not touched in the last 30 posts. A subject counts as used when a post saved it, or an old title contains one of its
    keywords. Ties are broken at random.
    """
    def hit(h, sid, kws):
        title = (h.get("title") or "").lower()
        return h.get("subject") == sid or any(k in title for k in kws)

    month = (today or _utcnow()).month
    scored = []
    for sid, kws, phrase in SUBJECT_POOL:
        if not subject_in_season(sid, month):
            continue   # e.g. no hammock in December, no fireplace screen in July
        used = sum(1 for h in history if hit(h, sid, kws))
        recent = any(hit(h, sid, kws) for h in history[-30:])
        scored.append((used, recent, (sid, kws, phrase)))
    best = min(u for u, _, _ in scored)
    candidates = [x for u, r, x in scored if u == best and not r] or [x for u, r, x in scored if u == best]
    return random.choice(candidates)



# An optional "angle" (who it is for / what constraint it works within) mixed into ~60% of posts, never the
# same one twice within 8 posts. No prices here, so it can't clash with the title and cost checks.
ANGLES = [
    "for renters, with nothing permanent", "for a first apartment", "for a very small space",
    "for people who have never used tools", "for homes with kids and pets", "that takes under one hour",
    "using finds from a single thrift-store trip", "with a dark, moody palette", "with a light and airy palette",
    "using natural materials like wood, jute and linen", "with a vintage touch", "for hosting guests",
    "for a room with little natural light", "for a long, narrow room", "with no sewing", "with no painting",
    "with no drilling", "using things you may already own", "for a very tight budget", "for a cozy, layered look",
    "for a clean, minimal look", "for a gift-worthy result", "for a quick weekend refresh",
    "for people who hate clutter", "for a rental kitchen or bathroom", "for an open-concept space",
    "for a cottage or farmhouse feel", "for a modern, simple look", "for a family-friendly home",
    "for an evening glow with warm lighting",
]

def pick_post_plan(history, today=None):
    """
    Decides this post's format, seasonal theme and optional style lens.
    Avoids repeating the last 10 formats, the last 8 themes and the last 8 angles.
    """
    today = today or datetime.now(timezone.utc)

    n_avoid = min(10, max(len(POST_FORMATS) - 3, 1))   # with 20 formats the last 10 are skipped
    recent_formats = {h.get("format") for h in history[-n_avoid:]}
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
    angle = None
    if random.random() < 0.6:
        recent_angles = {h.get("angle") for h in history[-8:]}
        angle = random.choice([a for a in ANGLES if a not in recent_angles] or ANGLES)
    angle_line = (
        f"ANGLE (optional flavor): write it {angle}. Use it only where it fits the subject naturally; "
        f"do not force it into the title." if angle else ""
    )
    return {"fmt": fmt, "theme": theme, "season_line": season_line, "style_line": style_line,
            "angle": angle, "angle_line": angle_line}


# Models that answered 429 (rate limit / quota) are skipped for the rest of the
# run, so retries after a rejected draft don't re-knock on doors that are shut.
_DEAD_MODELS = set()


def generate_draft(history, niche, expand_from=None):
    # Duplicate-topic avoidance window: at ~2 posts/day, checking only the
    # last 50 titles covers ~25 days — past that, older topics could start
    # silently repeating since Gemini never sees them again. 300 covers
    # ~5 months before the same widening concern reappears; titles are
    # short so this stays cheap even at that size.
    recent_titles = [h["title"] for h in history[-300:]]

    # Only entries that have a URL (i.e. posts we've actually published since
    # URL-tracking was added) are usable as internal-link candidates.
    linkable = [h for h in history if h.get("url") and not post_out_of_season(h)][-20:]   # no out-of-season holiday posts
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
    subject = None if expand_from else pick_subject(history)   # (id, keywords, phrase)
    if subject:
        subject_block = (
            "SUBJECT FOR THIS POST (fixed by the system so it cannot repeat an older post): " + subject[2] + ".\n"
            "This overrides the season, style and angle lines: if they do not fit the subject, follow the subject and use\n"
            "them only for small touches. The TITLE must name this subject (use its main words, for example \""
            + subject[1][0] + "\"), and the WHOLE article must be about it, not about the room in general.\n"
            + ("This is a list post: EVERY item must be a different idea about this subject itself (ways to style, make, fix or use it), "
               "never unrelated ideas for the rest of the room. Name the subject in most items.\n" if fmt["name"] in LIST_FORMATS else "")
            + "\n"
        )
        subject_clause = " about that subject"
    else:
        subject_block, subject_clause = "", ""
    needs_line = (
        "   Right after the hook, add <h2>What You'll Need</h2> with a short <ul> of the\n"
        "   materials and tools, so readers can scan it before the steps.\n"
        if fmt.get("needs_list") else ""
    )
    if fmt["use_title_styles"]:
        title_line = (
            f"TITLE STYLE for THIS post (the site's titles had become too similar to each\n"
            f"other, so follow this one): {title_style}. Do NOT start the title with\n"
            f'"How to Turn". NEVER put a price or any dollar amount in the title; say budget, cheap or thrifted in words. Write the '
            f'title in normal sentence case; never begin it with a label in capital letters followed by a colon. '
            f'Never copy an example title word for word.'
        )
    else:
        title_line = (
            f"TITLE for THIS post: {fmt['title_hint']}. Do NOT start the title with\n"
            f'"How to Turn". NEVER put a price or any dollar amount in the title; say budget, cheap or thrifted in words. '
            f'Never copy an example title word for word.'
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
- Never quote a specific real product. When comparing to an expensive look, DESCRIBE the pricey version in
  general terms ("a high-end retailer's piece"), never a named item.

HONESTY RULES (important):
- Do NOT claim experiences that didn't happen: no "I tried", "I spent", "in my
  home", "my husband", "when I made this". Speak to the reader ("you") or in
  general terms ("most people find...", "a common mistake is...").
- NEVER write a price, a dollar amount, a cost range, a total or a budget figure anywhere in the post: not in
  the title, the text, the lists, the FAQ, the pin text or the video script. Prices change all the time and the
  numbers are where mistakes happen. Use words instead: budget-friendly, cheap, thrifted, inexpensive.

CONSISTENCY RULES (readers notice contradictions, so check these before you answer):
- Every number you state (length, size, quantity, time) must be identical everywhere in the post: intro,
  steps, lists, closing and FAQ.
- Material lengths must add up: the stock you list (for example one 48-inch dowel) must be long
  enough for every piece you tell the reader to cut from it. Multiply count x length and check it
  against the stock length before you write it; list the right stock size instead.
- The materials list, the steps and the FAQ must agree on tools: never say that no power tools
  are needed if any step uses a drill or a saw.
- The FAQ answers must agree with the article: the same products and the same methods (if the article seals
  wood with mineral oil, the FAQ must not recommend a different sealer).
- Every piece the steps use (sides, legs, shelves, rails, cleats) must appear in the materials list with its size,
  and the cut lengths must add up to something that can really be built.

SAFETY: never suggest mixing cleaning chemicals (for example bleach with vinegar or ammonia). If a step uses
oil-soaked rags (linseed or tung oil), spray paint, solvents or heat, add one short safety line (ventilate the
room; lay oily rags flat to dry, or soak them in water, before throwing them away). Never claim that a paint
or finish is food-safe.
Anything with flames or real heat (fire pits, fireplaces, space heaters, candles near fabric): only suggest
fire-safe materials (fire brick, lava rock, metal rings made for fire) and never river rock, pea gravel, wet stones,
standard concrete blocks, landscape fabric or plastic inside the fire zone; add one short line about clearance from
the house and local burn rules.

{subject_block}Topics already covered (do NOT repeat these or anything too similar to them):
{json.dumps(recent_titles, ensure_ascii=False)}

Pick ONE fresh, specific, practical angle on {niche}{subject_clause} that is not in that list.
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
{plan["angle_line"]}

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
  marketplace"), real tools, brand-agnostic techniques.
- A light, opinionated aside is fine ("honestly, skip this step if you're short on time") as long as it doesn't claim a personal experience.
- NEVER use these overused AI-sounding words/phrases, in any form: {banned_list}.
- No generic filler sentences that could apply to any home-decor post. Every
  paragraph must teach something specific or move the project forward.

LENGTH: 400-550 words in total (never more than 600). Readers skim, so a short, simple article beats a long
one. Do not pad to hit a word count; every sentence should earn its place.

KEEP IT SIMPLE: plain everyday words, short sentences, one idea per paragraph, and only information you are
sure is correct. If you are not sure about a detail, leave it out.

PARAGRAPHS: keep every paragraph to 2-4 short sentences (about 60 words at most). Never write one long
block of text; put lists of tips into <ul> or <ol> items instead.

NO REPEATING TO REACH THE LENGTH: never restate something you already said. Do not end with a
recap, a "step-by-step process" list or a "quick steps" summary that repeats the sections above,
and do not say the same tip twice in different words. If the article is short of 400 words,
add NEW, specific detail instead: exact measurements and quantities, what to buy and what to
skip, a common mistake and how to fix it, a cheaper swap, a variation, or how to care for the
finished piece.

STRUCTURE (as HTML, using ONLY these tags: p, h2, h3, ul, ol, li, strong, blockquote, a; no tables):

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
3. Do NOT use tables and do NOT write any prices. Short paragraphs and simple lists are enough.
4. INTERNAL LINKS: here are {len(linkable)} of our own previously published
   posts (title + real URL): {linkable_json}
   If (and only if) 1-3 of them are genuinely relevant to THIS article's
   topic, link to them naturally in-line inside a sentence using
   <a href="the exact URL">relevant anchor text</a> — never a bare "read
   more" list, never a fabricated URL, never forced if nothing fits.
5. IMAGE PLACEHOLDERS: insert exactly TWO image placeholders, each on its own line: [[IMG_1]] after the
   intro and [[IMG_2]] after one of the middle sections (never as the very first thing). No more than two;
   provide a section_images entry for each.

6. QUICK FACTS: also provide time_estimate (e.g. "1 hour", "A weekend") and difficulty ("Easy",
   "Moderate", or "Advanced"), used for a quick-take summary box at the top of the post. Both MUST be plain
   strings. If this post is NOT a single project (a list, guide or plan), time_estimate is how long it would
   take to do everything and difficulty is how hard the whole thing is. Do not give any cost.

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
     transformation for a project, or a concrete mistake or result for a
     list/guide post — a specific, concrete curiosity gap (what it actually
     is, not "you won't believe this"). Never use a price or any dollar
     amount. Specific and unexpected beats generic every time: "This
     thrift-store lazy Susan looks like a stone counter riser" beats "I
     made an amazing upgrade for cheap."
  2. 2 short, concrete tip/step sentences pulled from the real project — the
     single most surprising or useful specific detail (the trick, the
     material swap, the exact technique), not a generic summary. In ONE of
     these, naturally mention how long it takes (e.g. "It only takes about an hour."). Never mention a price.
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
    # trying several different ones is far more likely to land a working one than retrying a single
    # model repeatedly. The two "-lite" models at the end have a much higher daily quota than
    # the regular flash models, so they're kept as the last resort.
    # gemini-3.8-flash / 3.7-flash are deliberately NOT in the article chain any more: on the free tier
    # they are almost always busy (503) or out of quota (429) and only cost time. To bring a model
    # back, put it in this list.
    if subject:
        prompt = subject_block + prompt   # the subject is also the very first thing the model reads
    if expand_from:
        prompt = build_expansion_prompt(expand_from)   # same chain, but the task is "make this draft longer"

    # Only the two lite models are used: gemini-3.5-flash-lite, then gemini-3.1-flash-lite (the bigger Flash
    # models no longer work on the free tier). Each is tried once; if both fail, the whole run is retried
    # once after 15 minutes (see the bottom of this file).
    top_models = []
    lite_models = []
    for m in [FALLBACK_TEXT_MODEL_4, FALLBACK_TEXT_MODEL_5]:
        if m and m not in top_models and m not in lite_models:
            lite_models.append(m)
    # Every model is tried exactly once per article request: no second pass and no waiting in
    # between (saves quota and time). The 15-minute retry of the whole run still exists below.
    models_to_try = top_models + lite_models

    last_error = None
    num_cycles = 1  # one pass through the whole model list — with 2 models to try, each
    # contributing its own separate quota, there's no need to loop back and re-try the exact
    # same 2 models again; that would just burn quota with attempts already known to fail.
    cycle_wait = 0

    for cycle in range(1, num_cycles + 1):
        is_last_cycle = cycle == num_cycles

        for model_index, model in enumerate(models_to_try):
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
                        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
                            parsed = parsed[0]   # a one-element list wrapping the article object
                        if not isinstance(parsed, dict):
                            raise json.JSONDecodeError("expected a JSON object", text, 0)   # handled like invalid JSON below
                        if expand_from:
                            parsed["_format"] = expand_from.get("_format")
                            parsed["_theme"] = expand_from.get("_theme")
                            parsed["_subject"] = expand_from.get("_subject")
                            parsed["_angle"] = expand_from.get("_angle")
                        else:
                            parsed["_format"] = fmt["name"]
                            parsed["_theme"] = plan["theme"]
                            parsed["_subject"] = subject[0] if subject else None
                            parsed["_angle"] = plan["angle"]
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
        ][:2]   # at most two section photos (plus the hero)

    if draft.get("category") not in CATEGORIES:
        warn("category", "'General Decor'")
        draft["category"] = "General Decor"

    if not isinstance(draft.get("hashtag_tags"), list):
        warn("hashtag_tags", "an empty tag list")
        draft["hashtag_tags"] = []
    else:
        draft["hashtag_tags"] = [str(t) for t in draft["hashtag_tags"] if t]

    for field, fallback in [
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

VISION_MIN_GAP = 6.0           # seconds between photo-check requests (~10 per minute) so a burst cannot trip the free per-minute limit
PHOTO_MIN_SCORE = 8              # Gemini rates the best candidate 0-10; below this it is rejected as not relevant
VISION_CHECKS_PER_RUN = 36      # cap on Gemini photo-check calls per run (a video run makes ~15-20)
_vision_state = {"used": 0, "disabled": False}


# Photos are only dropped when their description suggests a visible FACE. Plain "woman", "man",
# "person" or "people" are allowed (hands, back views...); Gemini's photo check rejects any face it sees.
_PEOPLE_WORDS = {
    "girl", "boy", "child", "children", "kid", "kids", "baby", "couple", "family", "portrait",
    "smiling", "model", "mother", "father", "bride", "groom", "friends", "toddler", "selfie",
    "face", "faces", "santa",
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


def _quota_info(res):
    """Reads a Gemini 429: which limit was hit (its quota id), whether it is per-minute or per-day, and the wait Google suggests."""
    info = {"id": "", "per_minute": False, "per_day": False, "wait": None}
    try:
        details = res.json().get("error", {}).get("details", []) or []
    except Exception:
        return info
    for d in details:
        if not isinstance(d, dict):
            continue
        for v in d.get("violations", []) or []:
            qid = f"{v.get('quotaId', '')} {v.get('quotaMetric', '')}"
            info["id"] = info["id"] or (v.get("quotaId") or v.get("quotaMetric") or "")
            low = qid.lower()
            if "perminute" in low or "per_minute" in low:
                info["per_minute"] = True
            if "perday" in low or "per_day" in low:
                info["per_day"] = True
        rd = d.get("retryDelay")
        if rd:
            m = re.match(r"\s*([\d.]+)s", str(rd))
            if m:
                info["wait"] = float(m.group(1))
    return info


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
            f"or decor photo (not mostly text or graphics). The photo must be clearly relevant to the keywords, so if none of the candidates shows that subject, answer 0. "
            f"People in the photo are fine. REJECT only a photo where a person's FACE is clearly visible; a blurred or out-of-focus face, hands, arms, a back view or a body without a visible face are fine. "
            f"Also reject any photo with clearly readable words or lettering, and any photo "
            f"showing Christmas or Halloween decor unless the keywords ask for that holiday. "
            f"Reject a photo of a different room or a different object than the subject, even if it looks nice. "
            f"If none of them genuinely fit, answer 0. "
            f"Also rate how well your chosen photo shows the subject from 0 to 10: 8-10 = the subject is clearly the focus "
            f"of the photo, 6-7 = visible but not the focus (a general room view), 5 or less = a different object or room, "
            f"or only loosely related. Reply with ONLY JSON like {{\"best\": 2, \"score\": 8}}, or {{\"best\": 0, \"score\": 0}}."
        )}]
        for i, p in enumerate(photos, start=1):
            thumb = robust_request("GET", p["src"].get("medium") or p["src"]["small"], timeout=30)
            if not thumb.ok:
                return None
            parts.append({"text": f"Photo {i}:"})
            parts.append({"inline_data": {"mime_type": "image/jpeg",
                                          "data": base64.b64encode(thumb.content).decode()}})
        _vision_state["used"] += 1

        models = [m for m in (os.environ.get("GEMINI_VISION_MODEL"), FALLBACK_TEXT_MODEL_4, FALLBACK_TEXT_MODEL_5) if m]
        models = list(dict.fromkeys(models))
        dead = _vision_state.setdefault("dead", set())   # models out of quota (429) for this run
        for attempt_round in range(2):
            # round 2 (after a pause) only retries the first two models, to bound the time a bad spell can cost
            for model in (models if attempt_round == 0 else models[:2]):
                if model in dead:
                    continue
                res = None
                for retry in (0, 1):
                    last = _vision_state.get("last")
                    if last is not None and VISION_MIN_GAP > 0:
                        gap = VISION_MIN_GAP - (time.monotonic() - last)
                        if gap > 0:
                            time.sleep(gap)   # keep under the free per-minute request limit
                    _vision_state["last"] = time.monotonic()
                    try:
                        res = requests.post(
                            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                            params={"key": GEMINI_API_KEY},
                            json={"contents": [{"parts": parts}]},
                            timeout=45,
                        )
                    except Exception as e:
                        print(f"Photo check: {model} timed out or failed ({str(e)[:80]}).")
                        res = None
                        break
                    if res.status_code == 429 and retry == 0:
                        qi = _quota_info(res)
                        if qi["per_minute"] and not qi["per_day"]:
                            wait = min(max(qi["wait"] or 20, 5), 65)
                            print(f"Photo check: {model} hit a per-minute limit ({qi['id']}), waiting {wait:.0f}s and trying again...")
                            time.sleep(wait)
                            continue
                    break
                if res is None:
                    continue
                if res.status_code == 429:
                    qi = _quota_info(res)
                    print(f"Photo check: quota hit on {model} ({qi['id'] or 'limit not named by Google'}"
                          f"{', per day' if qi['per_day'] else ', per minute' if qi['per_minute'] else ''}).")
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
                ms = re.search(r'"score"\s*:\s*(\d+(?:\.\d+)?)', text)
                if idx and ms:
                    print(f"Photo check: relevance score {ms.group(1)}/10 for '{query}'.")
                if idx and ms and float(ms.group(1)) < PHOTO_MIN_SCORE:
                    print(f"Photo check: the best candidate scored {ms.group(1)}/10 for '{query}' — not relevant enough, rejected.")
                    idx = 0
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

    # No photos with visible faces: drop any whose description suggests one
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


def _json_values(text):
    """
    Yields every complete JSON value found in `text`, in order, ignoring code fences and any
    prose before, between or after them (models sometimes add a note or a second object).
    """
    text = (text or "").replace("```json", "").replace("```", "")
    dec = json.JSONDecoder()
    pos = 0
    while pos < len(text):
        starts = [i for i in (text.find("{", pos), text.find("[", pos)) if i != -1]
        if not starts:
            return
        start = min(starts)
        try:
            value, end = dec.raw_decode(text[start:])
        except json.JSONDecodeError:
            pos = start + 1
            continue
        yield value
        pos = start + max(end, 1)


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
            items = None
            for value in _json_values(text):
                if isinstance(value, dict) and isinstance(value.get("photos"), list):
                    items = value["photos"]
                    break
                if isinstance(value, list) and any(isinstance(v, dict) and "slot" in v for v in value):
                    items = value   # a bare list of slot entries
                    break
            if items is None:
                print(f"Photo planning: {model} gave no usable JSON, trying the next model.")
                continue
            plan = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
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


COST_UNIT_BY_FORMAT = {
    "listicle": "per idea", "mistakes and fixes": "per fix", "thrift buying guide": "", "myth-busting": "per method",
    "renter-friendly ideas": "per idea", "quick fixes under an hour": "per fix", "diy gift ideas": "per gift",
    "this vs that": "depending on the option", "seasonal checklist": "per task", "color palette guide": "per item",
    "small space solutions": "per solution", "care and cleaning guide": "per item", "thrift shopping list": "per item",
    "splurge or save": "per item", "decor rules": "per rule",
}


SUM_FORMATS = {"project tutorial", "room refresh plan", "project walkthrough with pitfalls", "styling guide"}
_TOTAL_SENTENCE_RE = re.compile(
    r"\b(all in|in total|altogether|overall|total|came (?:out )?to|comes? (?:out )?to|add(?:s|ed)? up|came in at|"
    r"will cost|would cost|cost(?:s)? (?:between|about|around|roughly|just|only|under|a total)|"
    r"runs? (?:about|around)|ran (?:about|around))\b", re.IGNORECASE)


def _money(v):
    return f"${v:.0f}" if abs(v - round(v)) < 1e-9 else f"${v:.2f}"


def _price_rows(rows):
    """[(label, lo, hi)] for every data row that shows a dollar amount; a Total row is ignored."""
    out = []
    for row in rows[1:]:
        if not row or "total" in row[0].lower():
            continue
        amounts = [float(x) for cell in row[1:] for x in re.findall(r"\$\s?(\d+(?:\.\d+)?)", cell)]
        if amounts:
            out.append((row[0], min(amounts), max(amounts)))
    return out


def _set_total_row(html_text, total_text):
    """Overwrites the price cell of the table's own Total row so it agrees with the rows above it."""
    m = re.search(r"<table.*?</table>", html_text, re.DOTALL | re.IGNORECASE)
    if not m:
        return html_text
    table = m.group(0)

    def fix_row(tr_match):
        tr = tr_match.group(0)
        cells = re.findall(r"<t[dh][^>]*>.*?</t[dh]>", tr, re.DOTALL | re.IGNORECASE)
        if not cells or "total" not in re.sub(r"<[^>]+>", " ", cells[0]).lower():
            return tr

        def fix_cell(c):
            inner = c.group(2)
            if "$" not in re.sub(r"<[^>]+>", " ", inner):
                return c.group(0)
            new_inner = f"<strong>{total_text}</strong>" if "<strong>" in inner.lower() else total_text
            return f"{c.group(1)}{new_inner}{c.group(3)}"

        first_end = tr.find(cells[0]) + len(cells[0])
        return tr[:first_end] + re.sub(r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", fix_cell, tr[first_end:], flags=re.DOTALL | re.IGNORECASE)

    new_table = re.sub(r"<tr.*?</tr>", fix_row, table, flags=re.DOTALL | re.IGNORECASE)
    return html_text.replace(table, new_table, 1)


def _rewrite_closing_cost(html_text, new_sentence):
    """
    After the table, any sentence that states a total (the model's own arithmetic is what went wrong, again and
    again) is replaced by `new_sentence`, which is built from the table. If the model wrote none, ours is added
    right after the "Prices are estimates" line. Text before the table is left alone.
    """
    idx = html_text.rfind("</table>")
    if idx == -1:
        return html_text
    head, tail = html_text[:idx + len("</table>")], html_text[idx + len("</table>"):]
    state = {"done": False}

    def fix_paragraph(m):
        open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)
        if "<em>" in inner.lower() and "estimate" in inner.lower():
            return m.group(0)
        sentences = re.split(r"(?<=[.!?])\s+", inner.strip())
        kept = [x for x in sentences if not (_TOTAL_SENTENCE_RE.search(x) and "$" in x)]
        if len(kept) == len(sentences):
            return m.group(0)
        if not state["done"]:
            state["done"] = True
            kept.insert(0, new_sentence)
        return f"{open_tag}{' '.join(kept)}{close_tag}" if kept else ""

    tail = re.sub(r"(<p\b[^>]*>)(.*?)(</p>)", fix_paragraph, tail, flags=re.DOTALL)
    if not state["done"]:
        m = re.match(r"\s*<p>\s*<em>[^<]*estimate[^<]*</em>\s*</p>", tail, re.IGNORECASE)
        pos = m.end() if m else 0
        tail = tail[:pos] + f"<p>{new_sentence}</p>" + tail[pos:]
    return head + tail


_ITEM_AMT_RE = re.compile(r"\$\s?\d+(?:\.\d+)?(?:\s*(?:-|–|—|to)\s*\$?\s?\d+(?:\.\d+)?)?")
_INTRO_RANGE_RE = re.compile(r"\$\s?\d+(?:\.\d+)?\s*(?:-|–|—|to|and)\s*\$?\s?\d+(?:\.\d+)?")
_COST_WORDS_RE = re.compile(r"\b(each|per|run|runs|range|ranges|cost|costs|under|between|spend|only|just)\b", re.I)
_COMPARE_WORDS_RE = re.compile(r"retail|store-bought|store bought|boutique|designer|sells?\b|worth|original|compared|versus|instead of|normally|high-end", re.I)


def _first_heading_pos(html_text):
    positions = [i for i in (html_text.find("<h2"), html_text.find("<h3"), html_text.find("<table")) if i != -1]
    return min(positions) if positions else -1


def _fix_intro_range(html_text, lo, hi):
    """The intro often promises a price range ("run from $8 to $22"); make it the table's real range."""
    cut = _first_heading_pos(html_text)
    if cut <= 0:
        return html_text
    head, rest = html_text[:cut], html_text[cut:]
    new_range = f"{_money(lo)} to {_money(hi)}"

    def fix_sentence(sent):
        if _INTRO_RANGE_RE.search(sent) and _COST_WORDS_RE.search(sent) and not _COMPARE_WORDS_RE.search(sent):
            return _INTRO_RANGE_RE.sub(new_range, sent, count=1)
        return sent

    def fix_paragraph(m):
        return m.group(1) + " ".join(fix_sentence(x) for x in re.split(r"(?<=[.!?])\s+", m.group(2))) + m.group(3)

    return re.sub(r"(<p\b[^>]*>)(.*?)(</p>)", fix_paragraph, head, flags=re.DOTALL) + rest


def _sync_item_costs(html_text, rows):
    """
    Under each item the model writes its own "Cost: About $15-$22" line, which then disagrees with the table. When
    the number of such lines equals the number of priced table rows, each line gets its row's amount.
    """
    cells = []
    for row in rows[1:]:
        if not row or "total" in row[0].lower():
            continue
        cell = next((c for c in row[1:] if "$" in c), None)
        if cell:
            m = _ITEM_AMT_RE.search(cell)
            if m:
                cells.append(m.group(0))
    start = html_text.find("<h2")
    end = html_text.find("<table")
    if start == -1 or end == -1 or end <= start or not cells:
        return html_text
    body = html_text[start:end]
    paragraphs = [m for m in re.finditer(r"<p\b[^>]*>.*?</p>", body, re.DOTALL)
                  if re.search(r"\bcost\b", m.group(0), re.I) and "$" in m.group(0) and "<em>" not in m.group(0).lower()]
    if len(paragraphs) != len(cells):
        return html_text
    out, last = [], 0
    for m, cell in zip(paragraphs, cells):
        out.append(body[last:m.start()])
        out.append(_ITEM_AMT_RE.sub(cell, m.group(0), count=1))
        last = m.end()
    out.append(body[last:])
    return html_text[:start] + "".join(out) + html_text[end:]


def split_long_paragraphs(html_text, max_words=60):
    """Breaks any <p> of more than ~75 words into shorter paragraphs at sentence ends (words unchanged)."""
    abbrev = ("e.g.", "i.e.", "vs.", "approx.", "etc.", "oz.", "ft.", "no.")

    def balanced(chunk):
        return all(len(re.findall(rf"<{t}[\s>]", chunk)) == len(re.findall(rf"</{t}>", chunk))
                   for t in ("a", "strong", "em", "b", "i"))

    def fix(m):
        open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)
        if len(re.sub(r"<[^>]+>", " ", inner).split()) <= max_words + 15:
            return m.group(0)
        sentences = []
        for piece in re.split(r"(?<=[.!?])\s+", inner.strip()):
            if sentences and sentences[-1].lower().endswith(abbrev):   # "e.g." is not a sentence end
                sentences[-1] += " " + piece
            else:
                sentences.append(piece)
        chunks, cur, cur_words = [], [], 0
        for sent in sentences:
            w = len(re.sub(r"<[^>]+>", " ", sent).split())
            if cur and cur_words + w > max_words:
                chunks.append(" ".join(cur))
                cur, cur_words = [], 0
            cur.append(sent)
            cur_words += w
        if cur:
            chunks.append(" ".join(cur))
        if len(chunks) < 2 or not all(balanced(c) for c in chunks):
            return m.group(0)   # nothing to split, or a link/bold tag would be cut in half
        return "".join(f"{open_tag}{c}{close_tag}" for c in chunks)

    return re.sub(r"(<p\b[^>]*>)(.*?)(</p>)", fix, html_text or "", flags=re.DOTALL)


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

        unit = COST_UNIT_BY_FORMAT.get(fmt)
        if unit is not None:
            amounts = [v for row in rows[1:] for cell in row for v in amount(cell)]
            if len(amounts) >= 2:
                lo, hi = min(amounts), max(amounts)
                suffix = f" {unit}" if unit else ""
                draft["total_cost"] = (f"{fmt_amt(lo)}-{fmt_amt(hi)}{suffix}" if lo != hi else f"{fmt_amt(lo)}{suffix}")
                if unit.startswith("per "):   # the closing "total" sentence is rebuilt from the table too
                    noun = unit[4:]
                    draft["html"] = _sync_item_costs(draft["html"], rows)
                    draft["html"] = _fix_intro_range(draft["html"], lo, hi)
                    draft["html"] = _rewrite_closing_cost(
                        draft["html"],
                        (f"Each {noun} costs between {_money(lo)} and {_money(hi)}, so you can pick the ones that fit your budget."
                         if lo != hi else f"Each {noun} costs about {_money(lo)}."))
            if fmt in ("listicle", "quick fixes under an hour", "diy gift ideas", "seasonal checklist"):
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
        elif fmt in SUM_FORMATS:
            priced = _price_rows(rows)
            if len(priced) >= 2:
                lo, hi = sum(r[1] for r in priced), sum(r[2] for r in priced)
                total_text = _money(lo) if abs(lo - hi) < 1e-9 else f"{_money(lo)}-{_money(hi)}"
                draft["total_cost"] = total_text
                draft["html"] = _set_total_row(draft["html"], total_text)
                draft["html"] = _rewrite_closing_cost(
                    draft["html"],
                    (f"All in, everything in the table comes to about {_money(lo)}." if abs(lo - hi) < 1e-9
                     else f"All in, everything in the table comes to between {_money(lo)} and {_money(hi)}."))
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


_TITLE_SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs", "via", "with"}


def title_case_if_needed(title):
    """Titles written in sentence case ("A $25 entryway shoe bench that looks like...") get the site's Title Case."""
    words = (title or "").split()
    long_words = [w for w in words[1:] if re.search(r"[A-Za-z]", w) and w.lower().strip("(\"'") not in _TITLE_SMALL_WORDS]
    if not long_words:
        return title
    lower = [w for w in long_words if re.search(r"[A-Za-z]", w).group(0).islower()]
    if len(lower) / len(long_words) < 0.5:
        return title   # already Title Case (or an unusual style we leave alone)
    out = []
    for i, w in enumerate(words):
        m = re.search(r"[A-Za-z]", w)
        if m and (i == 0 or i == len(words) - 1 or w.lower().strip("(\"',:.!?") not in _TITLE_SMALL_WORDS):
            j = m.start()
            w = w[:j] + w[j].upper() + w[j + 1:]
        out.append(w)
    return " ".join(out)


PRICE_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
_PRICE_PHRASE_RE = re.compile(
    r"(?:\s*,)?(?:\s+(?:for|at|about|around|roughly|approximately|only|just|under|less than|over|up to|costs?|costing|worth|between|from))*"
    r"\s*\$\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:-|–|—|to|and)\s*\$?\s?\d[\d,]*(?:\.\d+)?)?"
    r"(?:\s+(?:each|per\s+\w+|total|in total|apiece|or less|or more))*", re.IGNORECASE)


def _strip_price_phrases(text):
    """Cuts price phrases out of a short piece of text ("(one roll, about $12)" -> "(one roll)")."""
    t = _PRICE_PHRASE_RE.sub("", text or "")
    t = re.sub(r"\s*\(\s*[,;]?\s*\)", "", t)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,;")
    if re.fullmatch(r"(?i)(about|around|roughly|approximately|only|just|under|less than|over|for|at|total|costs?|between|from)[.!?]?", t):
        return ""   # nothing but a leftover connecting word
    return t


def _drop_price_sentences(text):
    """Removes every sentence that contains a price (used for running text, where cutting out just the
    number would leave a broken sentence)."""
    sentences = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return " ".join(x for x in sentences if not PRICE_RE.search(x))


def strip_prices(draft):
    """
    Prices are not allowed anywhere (they change all the time, and every total was a chance for a mistake). The
    prompt says so; this is the safety net: tables are removed, sentences and list items with a price are cleaned,
    and the pin / FAQ / video texts lose theirs. A price in the TITLE cannot be cleaned and is rejected by the gate.
    """
    html_text = re.sub(r"<table\b.*?</table>", "", draft.get("html", "") or "", flags=re.DOTALL | re.IGNORECASE)

    def fix_p(m):
        kept = _drop_price_sentences(m.group(2))
        return f"{m.group(1)}{kept}{m.group(3)}" if kept else ""

    def fix_short(m):
        cleaned = _strip_price_phrases(m.group(2))
        return f"{m.group(1)}{cleaned}{m.group(3)}" if cleaned else ""   # an item that was only a price disappears

    html_text = re.sub(r"(<p\b[^>]*>)(.*?)(</p>)", fix_p, html_text, flags=re.DOTALL)
    html_text = re.sub(r"(<(?:li|h2|h3|blockquote)\b[^>]*>)(.*?)(</(?:li|h2|h3|blockquote)>)", fix_short, html_text, flags=re.DOTALL)
    draft["html"] = html_text
    for field in ("pin_description", "pin_hook"):
        if isinstance(draft.get(field), str):
            draft[field] = _strip_price_phrases(draft[field])
    if not draft.get("pin_hook"):
        draft["pin_hook"] = draft.get("title", "")
    if isinstance(draft.get("reel_script"), str):
        script = _drop_price_sentences(draft["reel_script"])
        draft["reel_script"] = script or f"{draft.get('pin_hook') or draft.get('title', '')}. Here's how to get the look on a budget."
    for item in draft.get("faq") or []:
        if isinstance(item, dict):
            item["question"] = _strip_price_phrases(str(item.get("question", "")))
            answer = _drop_price_sentences(str(item.get("answer", "")))
            item["answer"] = answer or "It depends on the materials you choose, so check what you already have first."
    draft["total_cost"] = ""


def finalize_draft_text(draft):
    """Everything that must be true of the text no matter what the model wrote: totals from the table, short paragraphs."""
    draft["title"] = title_case_if_needed(draft.get("title", ""))
    strip_prices(draft)
    draft["html"] = split_long_paragraphs(draft.get("html", ""))


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


def _report_unsaved_secret(secret_name, secret_value, reason):
    """
    The repo (and so its Actions logs) is public, so a new secret value must NEVER be printed
    there. When it could not be saved automatically, it is put in the run's single final email
    (to NOTIFY_EMAIL) instead.
    """
    print(f"{reason} The new {secret_name} value is NOT printed here because the logs are public.")
    if GMAIL_ADDRESS and GMAIL_APP_PASSWORD and NOTIFY_EMAIL:
        queue_email_note(
            f"ACTION NEEDED: update GitHub secret {secret_name}\n{reason}\n\n"
            f"Set the repository secret {secret_name} to this new value:\n\n{secret_value}"
        )
        print(f"The new {secret_name} value will be included in the final email of this run.")
    else:
        print(f"Email is not configured, so the new {secret_name} value could not be delivered; "
              f"it will have to be re-created manually.")


def update_github_secret(secret_name, secret_value):
    """
    Updates a GitHub Actions repository secret via the API, so tokens that
    rotate (like Pinterest's refresh_token) never need manual copy-pasting.
    Requires GH_SECRETS_PAT (a fine-grained PAT scoped to this repo with
    "Secrets: read and write"). If that's not set, this just prints instead —
    it never raises, so a missing PAT never breaks the actual publish run.
    """
    if not GH_SECRETS_PAT:
        _report_unsaved_secret(secret_name, secret_value, "GH_SECRETS_PAT is not set, so the secret was not auto-updated.")
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
        _report_unsaved_secret(secret_name, secret_value, f"Could not auto-update the secret: {e}")


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


def _post_pin(access_token, payload):
    """Creates the pin. Pinterest sometimes answers 400 / code 2787 ("something went wrong on our end"): try once more."""
    def send():
        return robust_request(
            "POST", "https://api.pinterest.com/v5/pins",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=payload, timeout=60,
        )
    res = send()
    if not res.ok and res.status_code == 400 and "2787" in res.text:
        print("Pinterest said 'something went wrong on our end' (2787) - trying once more in 20s...")
        time.sleep(20)
        res = send()
    return res


def create_pinterest_pin(access_token, board_id, title, description, link, image_url):
    res = _post_pin(access_token, {
        "board_id": board_id,
        "title": title[:100],
        "description": description[:500],
        "link": link,
        "media_source": {
            "source_type": "image_url",
            "url": image_url,
        },
    })
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

    pin_res = _post_pin(access_token, {
        "board_id": board_id,
        "title": title[:100],
        "description": description[:500],
        "link": link,
        "media_source": {
            "source_type": "video_id",
            "cover_image_url": cover_image_url,
            "media_id": media_id,
        },
    })
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
            queue_email_note(
                "⚠️ Post(s) unpublished? These auto-published posts are now Drafts on Blogger (it may have "
                "unpublished them for a guidelines issue). Check the Blogger Posts page for a notice, "
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
            *([{"type": "text", "text": f"💰 Cost: {total_cost}", "subtype": "unordered-list-item"}] if total_cost else []),
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


def count_words(html_text):
    return len(re.sub(r"<[^>]+>", " ", html_text or "").split())


def build_expansion_prompt(draft, target_words=480):
    """The task for a draft that is only slightly too short: make the SAME article longer with NEW detail."""
    fields = {k: v for k, v in draft.items() if not str(k).startswith("_")}
    words = count_words(draft.get("html", ""))
    entry = next((e for e in SUBJECT_POOL if e[0] == draft.get("_subject")), None)
    subject_rule = (f"- The subject of this article is {entry[2]}: keep the new detail about it and name it in most of the "
                    f"paragraphs you add.\n" if entry else "")
    return (
        f"You are editing a draft blog article that is too short: its html has about {words} words. Bring it to "
        f"about {target_words} words (at least 420, never more than {target_words + 70}).\n\n"
        "Return the SAME article as one JSON object with EXACTLY the same fields, the same title and category, the same "
        "faq, pin fields, image_prompt and section_images, but with a longer \"html\".\n\n"
        "Rules:\n"
        "- Keep every existing section and number unchanged, and keep every [[IMG_n]] placeholder exactly where it is.\n"
        f"- Add about {max(target_words - words, 120)} NEW words inside at least 3 of the existing sections: exact "
        "measurements and quantities, what to buy and what to skip, a common mistake and how to fix it, a cheaper swap, "
        "a variation, or how to care for the finished piece. Put the new detail into SHORT paragraphs (2-4 sentences, "
        "about 60 words at most) or list items, never into one long block.\n"
        "- NEVER repeat or restate anything already in the article: no recap, no extra \"step-by-step\" list that repeats "
        "the sections above, no same tip twice in different words.\n"
        + subject_rule +
        "- Keep the html structure (h2/h3/p/ul/ol; no tables). Never write a price or any dollar amount. No brand or store names. No claims of personal experience "
        "(never \"I tried\", \"in my home\"); speak to the reader.\n"
        "- Output ONLY the JSON object, with every html attribute value in single quotes.\n\n"
        "DRAFT:\n" + json.dumps(fields, ensure_ascii=False)
    )


def _expandable(problems):
    """True when a longer version could fix every problem: too short, possibly with a thin mention of the subject."""
    return (any(p.startswith("too short") for p in problems)
            and all(p.startswith(("too short", "ignored the assigned subject")) for p in problems))


def expand_short_draft(draft, niche, history, problems):
    """
    A draft whose only problems are being too short (but not hopelessly: 250+ words) and, maybe, naming its
    subject too rarely gets up to three requests to write a longer version of the same article (the second pass starts from the first
    pass's result if that grew but is still short), instead of being thrown away and a new topic
    requested. Returns (draft, duplicate_of, problems): the expanded version once it passes every
    check, otherwise the original untouched.
    """
    current = draft
    for pass_no in (1, 2, 3):
        words = count_words(current.get("html", ""))
        if words < 200:
            break
        print(f"Draft '{draft['title']}' is only {words} words - asking for a longer version of the same article "
              f"(new detail, no repeats), pass {pass_no}/3...")
        try:
            expanded = normalize_draft(generate_draft(history, niche, expand_from=current))
            finalize_draft_text(expanded)
        except Exception as e:
            print(f"Expansion skipped ({e}).")
            break
        new_problems = find_quality_problems(expanded)
        duplicate_of = find_duplicate_title(expanded["title"], history)
        new_words = count_words(expanded.get("html", ""))
        if duplicate_of is None and not new_problems:
            print(f"Expanded from {count_words(draft.get('html', ''))} to {new_words} words - accepted.")
            return expanded, None, []
        print(f"Expansion pass {pass_no} did not fix it ({new_words} words; "
              f"{'; '.join(new_problems) or 'title became a duplicate'}).")
        if (duplicate_of is None and new_problems and _expandable(new_problems) and new_words > words):
            current = expanded   # it grew but is still short: one more pass from this version
            continue
        break
    return draft, None, problems


# Set to True the moment main() is about to call Blogger's publish API. Any failure BEFORE that
# point means nothing went live anywhere, so the whole run can safely be retried once.
_run_state = {"publish_started": False, "retried": False, "emailed": False}

# Extra things to tell the owner (a rotated secret that could not be saved, unpublished posts...). They are
# NOT emailed on their own: the run sends exactly ONE email, at the very end, and these ride along in it.
_EMAIL_NOTES = []


def queue_email_note(text):
    _EMAIL_NOTES.append(text)


def send_final_email(subject, body):
    """The single email of a run: success or final failure, plus any queued notes."""
    if _EMAIL_NOTES:
        body = body + "\n\n---\n" + "\n\n".join(_EMAIL_NOTES)
    _run_state["emailed"] = True
    send_phone_notification(subject, body)


def _retry_pending():
    """True when the run that just failed will be retried, so it must not send an email yet."""
    if _run_state["publish_started"] or _run_state["retried"]:
        return False
    return int(os.environ.get("PREPUBLISH_RETRY_MINUTES", "15")) > 0


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
            finalize_draft_text(draft)
            duplicate_of = find_duplicate_title(draft["title"], history)
            quality_problems = find_quality_problems(draft)
            if duplicate_of is None and quality_problems and _expandable(quality_problems):
                draft, duplicate_of, quality_problems = expand_short_draft(draft, niche, history, quality_problems)
            if duplicate_of is None and not quality_problems:
                if attempt >= MAX_TOPIC_ATTEMPTS or draft_has_hero_photo(draft, history):
                    break   # (the last attempt skips the photo gate: a post goes out rather than none)
                print(f"Topic '{draft['title']}' rejected (attempt {attempt}/{MAX_TOPIC_ATTEMPTS}): no suitable "
                      f"stock photo exists for it — asking for a more photographable topic.")
                prompt_history = prompt_history + [{"title": draft["title"], "subject": draft.get("_subject")}]
                continue
            if duplicate_of is not None:
                print(f"Topic '{draft['title']}' is too similar to existing post "
                      f"'{duplicate_of}' (attempt {attempt}/{MAX_TOPIC_ATTEMPTS}) — asking for a different one.")
                prompt_history = prompt_history + [{"title": draft["title"], "subject": draft.get("_subject")}]
            else:
                print(f"Draft '{draft['title']}' rejected (attempt {attempt}/{MAX_TOPIC_ATTEMPTS}): "
                      f"{'; '.join(quality_problems)} — asking again.")
                if any(q.startswith("ignored the assigned subject") for q in quality_problems):
                    # count that subject as used so the next attempt is given a different one
                    prompt_history = prompt_history + [{"title": draft["title"], "subject": draft.get("_subject")}]
        else:
            raise RuntimeError(
                f"Could not get an acceptable draft after {MAX_TOPIC_ATTEMPTS} attempts "
                f"(last: '{draft['title']}' — {duplicate_of and 'duplicate topic' or '; '.join(quality_problems)}). "
                f"Nothing was published this run."
            )
        finalize_draft_text(draft)
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
        total_cost_raw = draft.get("total_cost", "") or ""
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
            f'<strong>Quick Take:</strong> {("Total cost: " + total_cost + " &bull; ") if total_cost else ""}'
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
        related = pick_related_posts(history, category)   # same category first, then newest; no out-of-season holiday posts
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

        article_info = (f"{count_words(draft.get('html', ''))} words | model {draft.get('_model')} | "
                        f"format {draft.get('_format')} | subject {draft.get('_subject')} | angle {draft.get('_angle') or 'none'}")
        print(f"Final article: {article_info}")
        print("Publishing to Blogger...")
        access_token = get_access_token()
        _run_state["publish_started"] = True   # from here on a failure is NOT retried (could duplicate a post)
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
        if _retry_pending():
            print("This failure will be retried, so no email is sent for it (one email is sent at the very end).")
        else:
            send_final_email(
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
        "subject": draft.get("_subject"),
        "angle": draft.get("_angle"),
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
        total_cost=draft.get("total_cost", ""),
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

    send_final_email(
        f"{tick(True)} DecorVibe posted: {draft['title'][:60]}",
        f"{draft['title']}\n{post_url}\n\n"
        f"Blogger: {tick(True)}\n"
        f"Pinterest: {tick(pinterest_ok)}\n"
        f"Tumblr: {tick(tumblr_ok)}\n\n"
        f"Article: {article_info}",
    )

    # Clean up what R2 only held for this run (Pinterest fetched its own copy of the
    # video cover). hero_url stays: Blogger embeds that exact URL permanently.
    print("Cleaning up temporary R2 files...")
    delete_from_r2(pin_cover_filepath)


def _run_with_retry():
    try:
        main()
    except Exception as e:
        # Retry once, but ONLY if the failure happened before Blogger's publish call: then nothing
        # was published anywhere, so a retry can't create a duplicate post. Typical cause: Gemini
        # "high demand" (503) spells, which usually pass within minutes. A failure after the
        # publish call is never retried.
        if not _retry_pending():
            raise
        wait_minutes = int(os.environ.get("PREPUBLISH_RETRY_MINUTES", "15"))
        print(f"No post was published ({e}). Waiting {wait_minutes} minutes, then trying once more...")
        time.sleep(wait_minutes * 60)
        # Models that already hit their quota (429) or are unavailable stay dead during the retry:
        # _DEAD_MODELS and _vision_state["dead"] are deliberately NOT cleared here.
        _vision_state.update(used=0, disabled=False, fails=0)
        _run_state["publish_started"] = False
        _run_state["retried"] = True
        main()


if __name__ == "__main__":
    try:
        _run_with_retry()
    except Exception as e:
        if not _run_state["emailed"]:   # e.g. a crash after publishing: still exactly one email
            send_final_email("❌ DecorVibe run FAILED", f"The run did not finish cleanly.\n\nError: {e}")
        raise
