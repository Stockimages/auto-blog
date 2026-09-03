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
import base64
import subprocess
import textwrap
import random
import time
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

# Auto-set by GitHub Actions as "owner/repo". Falls back for local testing.
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "your-username/your-repo")

# Model name — Google updates these periodically. If a run starts failing
# with a 404 "model not found" error, check the current name in Google AI
# Studio and update below (or set GEMINI_TEXT_MODEL as an env var/config value).
TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.6-flash")

HISTORY_FILE = "topics_history.json"
CONFIG_FILE = "config.json"
DEFAULT_NICHE = "budget-friendly home decor"

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

    prompt = f"""You are a real person who runs a {niche} blog and personally writes every
post. You've done these projects yourself, in your own home, on a real budget.
Posts are shared to Pinterest automatically the moment they're published, so
the opening line has to earn a click — then the article has to actually
deliver, like a friend explaining exactly how they did something.

Topics already covered (do NOT repeat these or anything too similar to them):
{json.dumps(recent_titles, ensure_ascii=False)}

Pick ONE fresh, specific, practical angle on {niche} that is not in that list.

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

Also write:
- "pin_hook": a punchy, benefit- or curiosity-driven phrase, 5-8 words max,
  written like Pinterest pin text (e.g. "10 Thrift Flips That Look Expensive"),
  NOT a full sentence, no ending punctuation.
- "image_prompt": 3-5 simple search keywords (not a sentence) for the vertical
  HERO photo (this is the one shown on Pinterest) — e.g. "thrifted glass vase
  living room". No brand names, no people's faces, no text.
- "section_images": a list matching your [[IMG_n]] placeholders, each with a
  "token" (e.g. "IMG_1") and a "query" (3-5 keyword search terms for a real,
  horizontal photo matching that section of the article — no people's faces,
  no text).

Return ONLY valid JSON. No markdown fences, no commentary before or after.
{{
  "title": "a specific, honest, clickable title",
  "pin_hook": "...",
  "labels": ["label1", "label2", "label3"],
  "html": "full article body as HTML, following every rule above",
  "image_prompt": "...",
  "section_images": [
    {{"token": "IMG_1", "query": "..."}},
    {{"token": "IMG_2", "query": "..."}}
  ]
}}"""

    max_attempts = 4
    wait_seconds = [15, 30, 60]  # delay before attempts 2, 3, 4

    for attempt in range(1, max_attempts + 1):
        is_last_attempt = attempt == max_attempts

        try:
            res = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{TEXT_MODEL}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=150,
            )
        except requests.exceptions.RequestException as e:
            # Network-level failure (timeout, connection reset, DNS hiccup, etc.)
            # — no HTTP response at all, so this can't be checked via status_code.
            if is_last_attempt:
                raise RuntimeError(f"Gemini request failed after {max_attempts} attempts (network error): {e}")
            delay = wait_seconds[attempt - 1]
            print(f"Gemini request failed ({e}), retrying in {delay}s "
                  f"(attempt {attempt}/{max_attempts})...")
            time.sleep(delay)
            continue

        if res.ok:
            text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = text.replace("```json", "").replace("```", "").strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                if is_last_attempt:
                    raise RuntimeError(f"Gemini returned invalid JSON after {max_attempts} attempts: {e}")
                delay = wait_seconds[attempt - 1]
                print(f"Gemini returned invalid JSON ({e}), retrying in {delay}s "
                      f"(attempt {attempt}/{max_attempts})...")
                time.sleep(delay)
                continue

        # Retry only on transient errors (overloaded / rate-limited / server hiccup).
        # Fail immediately on anything else (e.g. bad API key, bad request).
        transient = res.status_code in (429, 500, 502, 503, 504)

        if not transient or is_last_attempt:
            raise RuntimeError(f"Gemini text generation failed ({res.status_code}): {res.text}")

        delay = wait_seconds[attempt - 1]
        print(f"Gemini text generation failed ({res.status_code}), retrying in {delay}s "
              f"(attempt {attempt}/{max_attempts})...")
        time.sleep(delay)


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


def finalize_pin_image(raw_image_bytes, hook_text, max_width=1200, quality=78):
    img_with_text = add_pin_text(raw_image_bytes, hook_text)
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


def publish_post(access_token, title, html, labels):
    res = robust_request(
        "POST", f"https://www.googleapis.com/blogger/v3/blogs/{BLOGGER_BLOG_ID}/posts/",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"title": title, "content": html, "labels": labels},
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


def post_to_facebook_page(message, link):
    """
    Posts a link to the Facebook Page's feed. Never raises — if this fails
    or isn't configured, the post is still published everywhere else fine.
    Returns True/False so the caller can record status for the dashboard.
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


STATUS_FILE = "status.json"


def save_status(blogger_ok, blogger_url, facebook_ok, pinterest_ok):
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
        print("Topic chosen:", draft["title"])

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
                f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{section_filepath}"
            )

        print("Committing images to the repo...")
        git_commit_and_push(committed_paths, f"Auto post images: {draft['title']}")
        time.sleep(8)

        hero_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{hero_filepath}"

        # Swap [[IMG_n]] placeholders for real <img> tags.
        body_html = draft["html"]
        for token, url in section_urls.items():
            img_tag = f'<img src="{url}" alt="" style="max-width:100%;height:auto;" />'
            body_html = re.sub(rf"\[\[{re.escape(token)}\]\]", img_tag, body_html)
        # Remove any leftover placeholders Gemini added without a matching section_images entry.
        body_html = re.sub(r"\[\[IMG_\d+\]\]", "", body_html)

        full_html = (
            f'<img src="{hero_url}" alt="" style="max-width:100%;height:auto;" />\n{body_html}'
        )

        print("Publishing to Blogger...")
        access_token = get_access_token()
        result = publish_post(access_token, draft["title"], full_html, draft.get("labels", []))
        post_url = result.get("url")
        print("Published:", post_url)
    except Exception as e:
        # Blogger/generation itself failed — nothing got posted anywhere this
        # run. Still record it so the dashboard shows a red cross for today
        # instead of silently keeping yesterday's green tick.
        print(f"Run failed before publishing: {e}")
        try:
            save_status(blogger_ok=False, blogger_url=None, facebook_ok=False, pinterest_ok=False)
            git_commit_and_push([STATUS_FILE], "Auto post: run failed before publishing")
        except Exception as status_err:
            print(f"Could not save failure status: {status_err}")
        raise

    print("Notifying Google Indexing API...")
    submit_url_for_indexing(post_url)

    print("Posting to Facebook Page...")
    facebook_ok = post_to_facebook_page(pin_hook, post_url)

    # History (with URL, for future internal linking) is saved and committed
    # AFTER publishing, now that we actually know the post's URL.
    history.append({
        "title": draft["title"],
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
        pin_result = create_pinterest_pin(
            pinterest_token,
            board_id=PINTEREST_BOARD_ID,
            title=pin_hook,
            description=draft["title"],
            link=post_url,
            image_url=hero_url,
        )
        print("Pinned:", pin_result.get("id"))
        pinterest_ok = True
    except Exception as e:
        print(f"Pinterest post failed (blog post is still published fine): {e}")

    save_status(
        blogger_ok=True, blogger_url=post_url,
        facebook_ok=facebook_ok, pinterest_ok=pinterest_ok,
    )
    print("Committing history + status...")
    git_commit_and_push([HISTORY_FILE, STATUS_FILE], f"Auto post history: {draft['title']}")


if __name__ == "__main__":
    main()
