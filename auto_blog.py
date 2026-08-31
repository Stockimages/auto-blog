"""
Fully automatic Blogger publisher for a budget home-decor blog.

Flow each run:
  1. Read topics_history.json so Gemini doesn't repeat itself
  2. Ask Gemini for a fresh topic + full article + an image search query
  3. Search Pexels for a matching real stock photo
  4. Overlay a bold Pinterest-style text hook, resize + compress (WebP)
  5. Commit the image + updated history to this repo (so it gets a public raw.githubusercontent.com URL)
  6. Get a fresh Blogger access token from the stored refresh token
  7. Publish the post to Blogger
  8. Get a fresh Pinterest access token from the stored refresh token
  9. Create a Pin on Pinterest pointing back to the new post

Meant to be run by the GitHub Actions workflow in .github/workflows/auto-blog.yml,
on a schedule, with no human interaction.
"""

import os
import json
import base64
import subprocess
import textwrap
import random
import time
from io import BytesIO
from datetime import datetime, timezone

import requests
from PIL import Image, ImageDraw, ImageFont

# ---- Required secrets / env vars (set these as GitHub Actions secrets) ----
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BLOGGER_BLOG_ID = os.environ["BLOGGER_BLOG_ID"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]

# Pinterest — used to auto-post a Pin right after each Blogger post goes live.
PINTEREST_APP_ID = os.environ["PINTEREST_APP_ID"]
PINTEREST_APP_SECRET = os.environ["PINTEREST_APP_SECRET"]
PINTEREST_REFRESH_TOKEN = os.environ["PINTEREST_REFRESH_TOKEN"]
PINTEREST_BOARD_ID = os.environ["PINTEREST_BOARD_ID"]

# Auto-set by GitHub Actions as "owner/repo". Falls back for local testing.
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "your-username/your-repo")

# Model name — Google updates these periodically. If a run starts failing
# with a 404 "model not found" error, check the current name in Google AI
# Studio and update below (or set GEMINI_TEXT_MODEL as an env var/config value).
TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.6-flash")

HISTORY_FILE = "topics_history.json"
CONFIG_FILE = "config.json"
DEFAULT_NICHE = "budget-friendly home decor"


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
    prompt = f"""You are the writer for a {niche} blog whose posts are shared to Pinterest
automatically the moment they're published. Every post needs to win a Pinterest click
first, then deliver once someone lands on the article.

Topics already covered (do NOT repeat these or anything too similar to them):
{json.dumps(recent_titles, ensure_ascii=False)}

Pick ONE fresh, specific, practical angle on {niche} that is not in that list.

Write a 500-800 word article: h2/h3 subheadings, short paragraphs, concrete
actionable tips (real price ranges, real product types, real techniques).
Avoid vague generic advice. The article's OPENING paragraph must work as a
standalone hook (curiosity or a specific promise) since Pinterest and search
engines often show just this first line as the preview text.

Also write:
- "pin_hook": a punchy, benefit- or curiosity-driven phrase, 5-8 words max,
  written like Pinterest pin text (e.g. "10 Thrift Flips That Look Expensive"),
  NOT a full sentence, no ending punctuation.
- "image_prompt": 3-5 simple search keywords (not a sentence) to find a matching
  real stock photo — e.g. "thrifted glass vase living room". No brand names,
  no people's faces, no text.

Return ONLY valid JSON. No markdown fences, no commentary before or after.
{{
  "title": "a specific, honest, clickable title",
  "pin_hook": "...",
  "labels": ["label1", "label2", "label3"],
  "html": "<p>hook opening paragraph</p><h2>...</h2><p>...</p> ... full article body as simple HTML",
  "image_prompt": "..."
}}"""

    max_attempts = 4
    wait_seconds = [15, 30, 60]  # delay before attempts 2, 3, 4

    for attempt in range(1, max_attempts + 1):
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{TEXT_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=120,
        )

        if res.ok:
            text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = text.replace("```json", "").replace("```", "").strip()
            return json.loads(text)

        # Retry only on transient errors (overloaded / rate-limited / server hiccup).
        # Fail immediately on anything else (e.g. bad API key, bad request).
        transient = res.status_code in (429, 500, 502, 503, 504)
        is_last_attempt = attempt == max_attempts

        if not transient or is_last_attempt:
            raise RuntimeError(f"Gemini text generation failed ({res.status_code}): {res.text}")

        delay = wait_seconds[attempt - 1]
        print(f"Gemini text generation failed ({res.status_code}), retrying in {delay}s "
              f"(attempt {attempt}/{max_attempts})...")
        time.sleep(delay)


def search_pexels_image(query):
    res = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "orientation": "portrait", "per_page": 15},
        timeout=30,
    )
    if not res.ok:
        raise RuntimeError(f"Pexels search failed ({res.status_code}): {res.text}")

    photos = res.json().get("photos", [])
    if not photos:
        res = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": "home decor", "orientation": "portrait", "per_page": 15},
            timeout=30,
        )
        res.raise_for_status()
        photos = res.json().get("photos", [])
        if not photos:
            raise RuntimeError(f"No Pexels photos found for query: {query}")

    photo = random.choice(photos)
    image_url = photo["src"]["large2x"]
    image_res = requests.get(image_url, timeout=30)
    image_res.raise_for_status()
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


def git_commit_and_push(paths, message):
    subprocess.run(["git", "config", "user.email", "auto-blog-bot@users.noreply.github.com"], check=True)
    subprocess.run(["git", "config", "user.name", "auto-blog-bot"], check=True)
    subprocess.run(["git", "add", *paths], check=True)
    result = subprocess.run(["git", "commit", "-m", message])
    if result.returncode == 0:
        subprocess.run(["git", "push"], check=True)


def get_access_token():
    """Google/Blogger access token, refreshed from the stored Google refresh token."""
    res = requests.post(
        "https://oauth2.googleapis.com/token",
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
    res = requests.post(
        f"https://www.googleapis.com/blogger/v3/blogs/{BLOGGER_BLOG_ID}/posts/",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"title": title, "content": html, "labels": labels},
        timeout=60,
    )
    if not res.ok:
        raise RuntimeError(f"Blogger publish failed ({res.status_code}): {res.text}")
    return res.json()


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

    res = requests.post(
        "https://api.pinterest.com/v5/oauth/token",
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
        print("!!! Pinterest issued a NEW refresh_token. Update the "
              "PINTEREST_REFRESH_TOKEN GitHub secret to this value:")
        print(new_refresh_token)

    return data["access_token"]


def create_pinterest_pin(access_token, board_id, title, description, link, image_url):
    res = requests.post(
        "https://api.pinterest.com/v5/pins",
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


def main():
    config = load_config()
    niche = config.get("niche", DEFAULT_NICHE)

    global TEXT_MODEL
    TEXT_MODEL = config.get("text_model", TEXT_MODEL)

    history = load_history()

    print(f"Niche: {niche}")
    print("Asking Gemini for a topic + article...")
    draft = generate_draft(history, niche)
    print("Topic chosen:", draft["title"])

    print("Finding a matching stock photo...")
    raw_image = search_pexels_image(draft["image_prompt"])
    pin_hook = draft.get("pin_hook", draft["title"])
    compressed = finalize_pin_image(raw_image, pin_hook)
    print(f"Image (with pin text) compressed to {len(compressed) / 1024:.1f} KB")

    os.makedirs("images", exist_ok=True)
    filename = f"decor-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.webp"
    filepath = os.path.join("images", filename)
    with open(filepath, "wb") as f:
        f.write(compressed)

    history.append({"title": draft["title"], "date": datetime.now(timezone.utc).isoformat()})
    save_history(history)

    print("Committing image + history to the repo...")
    git_commit_and_push([filepath, HISTORY_FILE], f"Auto post image: {draft['title']}")

    time.sleep(8)

    image_url = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/{filepath}"
    full_html = f'<img src="{image_url}" alt="" style="max-width:100%;height:auto;" />\n{draft["html"]}'

    print("Publishing to Blogger...")
    access_token = get_access_token()
    result = publish_post(access_token, draft["title"], full_html, draft.get("labels", []))
    post_url = result.get("url")
    print("Published:", post_url)

    # Pinterest is posted last and wrapped in try/except on purpose: if this
    # fails for any reason, the Blogger post has already gone live and should
    # NOT be rolled back or treated as a failed run.
    print("Posting to Pinterest...")
    try:
        pinterest_token = get_pinterest_access_token()
        pin_result = create_pinterest_pin(
            pinterest_token,
            board_id=PINTEREST_BOARD_ID,
            title=pin_hook,
            description=draft["title"],
            link=post_url,
            image_url=image_url,
        )
        print("Pinned:", pin_result.get("id"))
    except Exception as e:
        print(f"Pinterest post failed (blog post is still published fine): {e}")


if __name__ == "__main__":
    main()
