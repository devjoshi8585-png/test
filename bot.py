# ══════════════════════════════════════════════════════════════════════
from flask import Flask
from threading import Thread
import os, sys, io, json, random, hashlib, logging, re, asyncio
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import aiohttp
import discord
from discord.ext import commands, tasks

try:
    from PIL import Image
except Exception:
    Image = None

_flask_app = Flask("")

@_flask_app.route("/")
def _home():
    return "Bot is alive! 🔥"

def _run_flask():
    port = int(os.environ.get("PORT", 10000))
    _flask_app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=_run_flask, daemon=True)
    t.start()

# ══════════════════════════════════════════════════════════════════════
#  NSFW GATE  (operator responsibility — see disclaimer above)
#  Set on Render → Environment Variables:
#    NSFW_MODE=true          unlocks e621, yandere, hypnohub providers
#    AGE_VERIFICATION=true   your legal acknowledgment as operator
# ══════════════════════════════════════════════════════════════════════
NSFW_MODE    = str(os.getenv("NSFW_MODE",        "false")).strip().lower() in ("1","true","yes","on")
AGE_VERIFIED = str(os.getenv("AGE_VERIFICATION", "false")).strip().lower() in ("1","true","yes","on")

# ══════════════════════════════════════════════════════════════════════
#  ENVIRONMENT VARIABLES  — set all of these on Render
# ══════════════════════════════════════════════════════════════════════
TOKEN              = os.getenv("TOKEN", "")              # REQUIRED: Discord bot token
WAIFUIM_API_KEY    = os.getenv("WAIFUIM_API_KEY",  "")   # optional — waifu.im
DANBOORU_USER      = os.getenv("DANBOORU_USER",    "")   # optional — danbooru
DANBOORU_API_KEY   = os.getenv("DANBOORU_API_KEY", "")   # optional — danbooru
GELBOORU_API_KEY   = os.getenv("GELBOORU_API_KEY", "")   # optional — gelbooru
GELBOORU_USER      = os.getenv("GELBOORU_USER",    "")   # optional — gelbooru
E621_USER          = os.getenv("E621_USER",        "")   # optional — e621 (free acct)
E621_API_KEY       = os.getenv("E621_API_KEY",     "")   # optional — e621 (free acct)
WAIFU_IT_API_KEY   = os.getenv("WAIFU_IT_API_KEY", "")   # optional — waifu.it (free Discord key)

DEBUG_FETCH            = str(os.getenv("DEBUG_FETCH",       "")).strip().lower() in ("1","true","yes","on")
TRUE_RANDOM            = str(os.getenv("TRUE_RANDOM",       "")).strip().lower() in ("1","true","yes")
REQUEST_TIMEOUT        = int(os.getenv("REQUEST_TIMEOUT",   "14"))
DISCORD_MAX_UPLOAD     = int(os.getenv("DISCORD_MAX_UPLOAD", str(8 * 1024 * 1024)))
HEAD_SIZE_LIMIT        = DISCORD_MAX_UPLOAD
DATA_FILE              = os.getenv("DATA_FILE",             "data_nsfw.json")
AUTOSAVE_INTERVAL      = int(os.getenv("AUTOSAVE_INTERVAL", "30"))
FETCH_ATTEMPTS         = int(os.getenv("FETCH_ATTEMPTS",    "40"))
MAX_USED_GIFS_PER_USER = int(os.getenv("MAX_USED_GIFS_PER_USER", "1000"))

# VC_CHANNEL_ID: text channel where join/leave messages + periodic drops are sent
VC_CHANNEL_ID = int(os.getenv("VC_CHANNEL_ID", "0"))

# VC_IDS: comma-separated list of voice channel IDs the bot monitors/joins
_VC_IDS_RAW = os.getenv("VC_IDS", "")
if _VC_IDS_RAW.strip():
    VC_IDS = [int(x.strip()) for x in _VC_IDS_RAW.split(",") if x.strip().isdigit()]
else:
    VC_IDS = []

# ══════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════
logging.basicConfig(level=logging.DEBUG if DEBUG_FETCH else logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("nsfw-bot-v2")

logger.info(f"NSFW_MODE={NSFW_MODE} | AGE_VERIFIED={AGE_VERIFIED}")
if not NSFW_MODE:
    logger.warning("[NSFW-GATE] NSFW_MODE is not true — e621/yandere/hypnohub providers DISABLED.")
if not AGE_VERIFIED:
    logger.warning("[AGE-GATE] AGE_VERIFICATION not set. Set AGE_VERIFICATION=true on Render to confirm legal responsibility.")
if not VC_IDS:
    logger.warning("[VC] VC_IDS env var not set — voice channel features disabled.")
if not VC_CHANNEL_ID:
    logger.warning("[VC] VC_CHANNEL_ID env var not set — text channel messages disabled.")

# ══════════════════════════════════════════════════════════════════════
#  TAG SYSTEM
# ══════════════════════════════════════════════════════════════════════
_token_split_re = re.compile(r"[^a-z0-9]+", re.I)

def _tag_is_disallowed(tag: str) -> bool:
    return False  # no restrictions

def _normalize_text(s: str) -> str:
    return "" if not s else re.sub(r"[\s\-_]+", " ", s.lower())

def _dedupe_preserve_order(lst):
    seen, out = set(), []
    for x in lst:
        if not isinstance(x, str): continue
        nx = x.strip().lower()
        if not nx or nx in seen: continue
        seen.add(nx); out.append(nx)
    return out

def add_tag(tag: str, GIF_TAGS, data_save):
    if not tag or not isinstance(tag, str): return False
    t = tag.strip().lower()
    if len(t) < 3 or t in GIF_TAGS or _tag_is_disallowed(t): return False
    GIF_TAGS.append(t)
    data_save["gif_tags"] = _dedupe_preserve_order(data_save.get("gif_tags", []) + [t])
    try:
        with open(DATA_FILE, "w") as f: json.dump(data_save, f, indent=2)
    except Exception: pass
    logger.debug(f"Learned tag: {t}")
    return True

def extract_tags_from_meta(meta_text: str, GIF_TAGS, data_save):
    if not meta_text: return
    text = _normalize_text(meta_text)
    for tok in _token_split_re.split(text):
        tok = tok.strip()
        if not tok or tok.isdigit() or len(tok) < 3: continue
        add_tag(tok, GIF_TAGS, data_save)

# ══════════════════════════════════════════════════════════════════════
#  DATA FILE  (persists tags + sent history between restarts)
# ══════════════════════════════════════════════════════════════════════
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({"provider_weights": {}, "sent_history": {}, "gif_tags": [], "vc_state": {}}, f, indent=2)

with open(DATA_FILE, "r") as f:
    data = json.load(f)

data.setdefault("provider_weights", {})
data.setdefault("sent_history",     {})
data.setdefault("gif_tags",         [])
data.setdefault("vc_state",         {})

def save_data():
    try:
        data["gif_tags"] = GIF_TAGS
        with open(DATA_FILE, "w") as f: json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Save failed: {e}")

# ══════════════════════════════════════════════════════════════════════
#  SPICY TAGS — injected into provider queries
# ══════════════════════════════════════════════════════════════════════
SPICY_TAGS = [
    "ahegao", "creampie", "cum_inside", "gangbang", "double_penetration",
    "deepthroat", "paizuri", "titfuck", "throatfuck", "facesitting",
    "doggy_style", "missionary", "squirting", "bondage", "bdsm",
    "tentacles", "orgasm", "riding", "thighjob", "cumshot", "blowjob",
    "anal", "pussy", "hardcore", "futanari", "public", "group",
    "nude", "naked", "sex", "handjob", "footjob", "femdom",
    "harem", "milf", "big_breasts", "large_breasts", "busty",
    "ass", "buttjob", "spanking", "hypnosis", "mind_break",
    "pov", "solo_female", "multiple_boys", "cum_on_face",
    "spread_legs", "cum_on_body", "breast_grab", "nipples",
]

_seed_gif_tags = [
    "hentai", "sex", "blowjob", "anal", "creampie", "cumshot", "ahegao",
    "paizuri", "gangbang", "deepthroat", "tentacles", "futanari", "orgasm",
    "squirt", "bondage", "milf", "oppai", "pussy", "hardcore", "animated",
    "nude", "naked", "big_breasts", "femdom", "pov", "ass", "busty",
    "nipples", "spread_legs", "riding", "deepthroat",
]

persisted = _dedupe_preserve_order(data.get("gif_tags", []))
seed      = _dedupe_preserve_order(_seed_gif_tags)
combined  = seed + [t for t in persisted if t not in seed]
GIF_TAGS  = [t for t in _dedupe_preserve_order(combined) if not _tag_is_disallowed(t)]
if not GIF_TAGS:
    GIF_TAGS = ["hentai"]

# ══════════════════════════════════════════════════════════════════════
#  DOWNLOAD HELPER
# ══════════════════════════════════════════════════════════════════════
async def _download_bytes(session, url, size_limit=HEAD_SIZE_LIMIT, timeout=REQUEST_TIMEOUT):
    try:
        to = aiohttp.ClientTimeout(total=timeout)
        async with session.get(url, timeout=to, allow_redirects=True) as resp:
            if resp.status != 200:
                if DEBUG_FETCH: logger.debug(f"GET {url} → {resp.status}")
                return None, None
            ctype = resp.content_type or ""
            total, chunks = 0, []
            async for chunk in resp.content.iter_chunked(1024):
                if not chunk: break
                chunks.append(chunk)
                total += len(chunk)
                if total > size_limit:
                    if DEBUG_FETCH: logger.debug(f"Size limit exceeded for {url}")
                    return None, ctype
            return b"".join(chunks), ctype
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"GET exception {url}: {e}")
        return None, None

# ══════════════════════════════════════════════════════════════════════
#  IMAGE COMPRESSION
# ══════════════════════════════════════════════════════════════════════
async def compress_image(image_bytes, target_size=DISCORD_MAX_UPLOAD):
    if not Image: return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.format == "GIF": return image_bytes
        output = io.BytesIO()
        quality = 95
        while quality > 10:
            output.seek(0); output.truncate()
            img.save(output, format=img.format or "JPEG", quality=quality, optimize=True)
            if output.tell() <= target_size: return output.getvalue()
            quality -= 10
        return output.getvalue()
    except Exception as e:
        logger.error(f"Compression failed: {e}")
        return image_bytes

# ══════════════════════════════════════════════════════════════════════
#  GELBOORU-COMPATIBLE HELPER
#  Shared by: gelbooru, tbib, xbooru, realbooru, hypnohub, atfbooru
# ══════════════════════════════════════════════════════════════════════
async def _gelbooru_compat(session, base_url, api_key=None, user_id=None, extra_tags=None):
    try:
        tags = ["rating:explicit"]
        if random.random() < 0.85:
            tags.append(random.choice(SPICY_TAGS))
        if random.random() < 0.60:
            tags.append("animated")
        if extra_tags:
            tags.extend(extra_tags)
        params = {
            "page": "dapi", "s": "post", "q": "index",
            "json": "1", "tags": " ".join(tags), "limit": 20,
        }
        if api_key and user_id:
            params["api_key"] = api_key
            params["user_id"] = user_id
        hdrs = {"User-Agent": "NSFWBotV2/2.0 (Discord Bot)"}
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get(base_url, params=params, headers=hdrs, timeout=to) as resp:
            if resp.status != 200: return None, None, None
            try:
                payload = await resp.json(content_type=None)
            except Exception:
                return None, None, None
            posts = payload if isinstance(payload, list) else payload.get("post", [])
            if not posts: return None, None, None
            post = random.choice(posts)
            gif_url = post.get("file_url")
            if not gif_url: return None, None, None
            # filter video files
            if gif_url.lower().endswith((".webm", ".mp4", ".swf")):
                return None, None, None
            extract_tags_from_meta(post.get("tags", ""), GIF_TAGS, data)
            return gif_url, base_url, post
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"gelbooru_compat {base_url} fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 01 — rule34.xxx   weight=45  (dominant, GIF + spicy bias)
#  API: https://rule34.xxx/index.php — no auth required
# ══════════════════════════════════════════════════════════════════════
async def fetch_rule34(session, positive=None):
    try:
        tags = ["rating:explicit"]
        if random.random() < 0.90: tags.append(random.choice(SPICY_TAGS))
        if random.random() < 0.70: tags.append("animated")
        params = {
            "page": "dapi", "s": "post", "q": "index",
            "json": "1", "tags": " ".join(tags), "limit": 120,
        }
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get("https://api.rule34.xxx/index.php", params=params, timeout=to) as resp:
            if resp.status != 200: return None, None, None
            posts = await resp.json(content_type=None)
            if not posts or not isinstance(posts, list): return None, None, None
            post = random.choice(posts)
            gif_url = post.get("file_url")
            if not gif_url: return None, None, None
            if gif_url.lower().endswith((".webm", ".mp4", ".swf")): return None, None, None
            extract_tags_from_meta(post.get("tags", ""), GIF_TAGS, data)
            return gif_url, "rule34", post
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"rule34 fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 02 — gelbooru.com   weight=18
#  API: https://gelbooru.com — optional free API key
#  Get free key: https://gelbooru.com/index.php?page=account&s=register
# ══════════════════════════════════════════════════════════════════════
async def fetch_gelbooru(session, positive=None):
    url, _, post = await _gelbooru_compat(
        session, "https://gelbooru.com/index.php",
        api_key=GELBOORU_API_KEY or None,
        user_id=GELBOORU_USER or None,
    )
    return url, "gelbooru", post

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 03 — nekosapi.com   weight=15   no auth required
#  API: https://api.nekosapi.com/v4/docs
# ══════════════════════════════════════════════════════════════════════
async def fetch_nekosapi(session, positive=None):
    try:
        params = {"rating": "explicit", "limit": 5}
        if random.random() < 0.80:
            params["tags"] = random.choice(SPICY_TAGS)
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get("https://api.nekosapi.com/v4/images/random",
                               params=params, timeout=to) as resp:
            if resp.status != 200: return None, None, None
            payload = await resp.json(content_type=None)
            images = payload.get("items", [])
            if not images: return None, None, None
            img = random.choice(images)
            gif_url = img.get("url")
            if not gif_url: return None, None, None
            extract_tags_from_meta(str(img.get("tags", "")), GIF_TAGS, data)
            return gif_url, "nekosapi", img
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"nekosapi fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 04 — konachan.com   weight=12   no auth required
#  API: https://konachan.com/help/api
# ══════════════════════════════════════════════════════════════════════
async def fetch_konachan(session, positive=None):
    try:
        tags = ["rating:explicit"]
        if random.random() < 0.85: tags.append(random.choice(SPICY_TAGS).replace("_", " "))
        if random.random() < 0.60: tags.append("animated")
        params = {"tags": " ".join(tags), "limit": 20}
        hdrs = {"User-Agent": "NSFWBotV2/2.0"}
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get("https://konachan.com/post.json",
                               params=params, headers=hdrs, timeout=to) as resp:
            if resp.status != 200: return None, None, None
            posts = await resp.json(content_type=None)
            if not posts or not isinstance(posts, list): return None, None, None
            image_posts = [p for p in posts if p.get("file_url")
                           and not p.get("file_url", "").lower().endswith((".webm", ".mp4"))]
            if not image_posts: return None, None, None
            post = random.choice(image_posts)
            gif_url = post.get("file_url")
            if not gif_url: return None, None, None
            extract_tags_from_meta(post.get("tags", ""), GIF_TAGS, data)
            return gif_url, "konachan", post
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"konachan fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 05 — nekobot.xyz   weight=10   no auth required
#  API: https://nekobot.xyz/api
# ══════════════════════════════════════════════════════════════════════
async def fetch_nekobot(session, positive=None):
    try:
        category = random.choice([
            "hentai", "hentai_anal", "hass", "hboobs",
            "hthigh", "paizuri", "tentacle", "pgif",
        ])
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get(f"https://nekobot.xyz/api/image?type={category}",
                               timeout=to) as resp:
            if resp.status != 200: return None, None, None
            payload = await resp.json(content_type=None)
            if not payload.get("success"): return None, None, None
            gif_url = payload.get("message")
            if not gif_url: return None, None, None
            extract_tags_from_meta(category, GIF_TAGS, data)
            return gif_url, f"nekobot_{category}", payload
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"nekobot fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 06 — danbooru.donmai.us   weight=8   optional free key
#  API: https://danbooru.donmai.us/wiki_pages/api
#  Free key: https://danbooru.donmai.us/users/new
# ══════════════════════════════════════════════════════════════════════
async def fetch_danbooru(session, positive=None):
    try:
        import base64
        tags = ["rating:explicit"]
        if random.random() < 0.85: tags.append(random.choice(SPICY_TAGS))
        if random.random() < 0.60: tags.append("animated")
        params = {"tags": " ".join(tags), "limit": 20, "random": "true"}
        headers = {}
        if DANBOORU_USER and DANBOORU_API_KEY:
            creds = base64.b64encode(f"{DANBOORU_USER}:{DANBOORU_API_KEY}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get("https://danbooru.donmai.us/posts.json",
                               params=params, headers=headers or None, timeout=to) as resp:
            if resp.status != 200: return None, None, None
            posts = await resp.json(content_type=None)
            if not posts: return None, None, None
            post = random.choice(posts)
            gif_url = post.get("file_url") or post.get("large_file_url")
            if not gif_url: return None, None, None
            if gif_url.lower().endswith((".webm", ".mp4", ".swf")): return None, None, None
            extract_tags_from_meta(str(post.get("tag_string", "")), GIF_TAGS, data)
            return gif_url, "danbooru", post
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"danbooru fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 07 — nekos.life   weight=8   no auth required
#  API: https://nekos.life/api/v2
# ══════════════════════════════════════════════════════════════════════
async def fetch_nekos_life(session, positive=None):
    try:
        category = random.choice([
            "blowjob", "cum", "hentai", "classical",
            "ero", "spank", "lewd", "feet",
        ])
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get(f"https://nekos.life/api/v2/img/{category}",
                               timeout=to) as resp:
            if resp.status != 200: return None, None, None
            payload = await resp.json(content_type=None)
            gif_url = payload.get("url")
            if not gif_url: return None, None, None
            extract_tags_from_meta(category, GIF_TAGS, data)
            return gif_url, f"nekos_life_{category}", payload
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"nekos_life fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 08 — tbib.org (The Big Image Board)   weight=7
#  Gelbooru-compatible API — no auth required
# ══════════════════════════════════════════════════════════════════════
async def fetch_tbib(session, positive=None):
    url, _, post = await _gelbooru_compat(session, "https://tbib.org/index.php")
    return url, "tbib", post

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 09 — xbooru.com   weight=7
#  Gelbooru-compatible API — no auth required
# ══════════════════════════════════════════════════════════════════════
async def fetch_xbooru(session, positive=None):
    url, _, post = await _gelbooru_compat(session, "https://xbooru.com/index.php")
    return url, "xbooru", post

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 10 — realbooru.com   weight=6
#  Gelbooru-compatible API — no auth required
# ══════════════════════════════════════════════════════════════════════
async def fetch_realbooru(session, positive=None):
    url, _, post = await _gelbooru_compat(session, "https://realbooru.com/index.php")
    return url, "realbooru", post

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 11 — waifu.im   weight=5   optional free key
#  API: https://waifu.im/docs — key optional but increases rate limit
# ══════════════════════════════════════════════════════════════════════
async def fetch_waifu_im(session, positive=None):
    try:
        q = positive or random.choice(GIF_TAGS)
        params = {"included_tags": q, "is_nsfw": "true", "limit": 8}
        headers = {}
        if WAIFUIM_API_KEY:
            headers["Authorization"] = f"Bearer {WAIFUIM_API_KEY}"
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get("https://api.waifu.im/search",
                               params=params, headers=headers or None, timeout=to) as resp:
            if resp.status != 200: return None, None, None
            payload = await resp.json(content_type=None)
            images = payload.get("images", [])
            if not images: return None, None, None
            img = random.choice(images)
            gif_url = img.get("url")
            if not gif_url: return None, None, None
            extract_tags_from_meta(str(img.get("tags", "")), GIF_TAGS, data)
            return gif_url, "waifu_im", img
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"waifu_im fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 12 — rule34.paheal.net   weight=5   no auth required
#  XML API: https://rule34.paheal.net/api
# ══════════════════════════════════════════════════════════════════════
async def fetch_paheal(session, positive=None):
    try:
        tag = random.choice(SPICY_TAGS).replace("_", " ")
        params = {"tags": tag, "limit": 50}
        hdrs = {"User-Agent": "NSFWBotV2/2.0"}
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get("https://rule34.paheal.net/api/danbooru/find_posts/index.xml",
                               params=params, headers=hdrs, timeout=to) as resp:
            if resp.status != 200: return None, None, None
            text = await resp.text()
            root = ET.fromstring(text)
            posts = root.findall(".//post")
            if not posts: return None, None, None
            post = random.choice(posts)
            gif_url = post.get("file_url")
            if not gif_url: return None, None, None
            if gif_url.lower().endswith((".webm", ".mp4", ".swf", ".flv")):
                return None, None, None
            extract_tags_from_meta(post.get("tags", ""), GIF_TAGS, data)
            return gif_url, "paheal", {"file_url": gif_url, "tags": post.get("tags", "")}
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"paheal fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 13 — waifu.it   weight=5   optional free key
#  Get key free: join their Discord server → run /token
#  Discord: https://discord.gg/waifuit
#  Env var: WAIFU_IT_API_KEY
# ══════════════════════════════════════════════════════════════════════
async def fetch_waifu_it(session, positive=None):
    if not WAIFU_IT_API_KEY: return None, None, None
    try:
        category = random.choice([
            "creampie", "thighjob", "ero", "paizuri",
            "oppai", "anal", "blowjob", "hentai",
        ])
        hdrs = {"Authorization": WAIFU_IT_API_KEY}
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get(f"https://waifu.it/api/v4/{category}",
                               headers=hdrs, timeout=to) as resp:
            if resp.status != 200: return None, None, None
            payload = await resp.json(content_type=None)
            gif_url = payload.get("url")
            if not gif_url: return None, None, None
            extract_tags_from_meta(category, GIF_TAGS, data)
            return gif_url, f"waifu_it_{category}", payload
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"waifu_it fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 14 — nekos.moe   weight=3   no auth required
#  API: https://nekos.moe/docs
# ══════════════════════════════════════════════════════════════════════
async def fetch_nekos_moe(session, positive=None):
    try:
        hdrs = {"User-Agent": "NSFWBotV2/2.0"}
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get("https://nekos.moe/api/v1/random/image?nsfw=true&count=1",
                               headers=hdrs, timeout=to) as resp:
            if resp.status != 200: return None, None, None
            payload = await resp.json(content_type=None)
            images = payload.get("images", [])
            if not images: return None, None, None
            img = random.choice(images)
            img_id = img.get("id")
            if not img_id: return None, None, None
            gif_url = f"https://nekos.moe/image/{img_id}.jpg"
            extract_tags_from_meta(" ".join(img.get("tags", [])), GIF_TAGS, data)
            return gif_url, "nekos_moe", img
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"nekos_moe fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 15 — waifu.pics   weight=2   no auth required
#  API: https://waifu.pics/docs
# ══════════════════════════════════════════════════════════════════════
async def fetch_waifu_pics(session, positive=None):
    try:
        category = random.choice(["waifu", "neko", "trap", "blowjob"])
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get(f"https://api.waifu.pics/nsfw/{category}",
                               timeout=to) as resp:
            if resp.status != 200: return None, None, None
            payload = await resp.json(content_type=None)
            gif_url = payload.get("url") or payload.get("image")
            if not gif_url: return None, None, None
            extract_tags_from_meta(json.dumps(payload), GIF_TAGS, data)
            return gif_url, f"waifu_pics_{category}", payload
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"waifu_pics fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 16 — e621.net   weight=18   NSFW_MODE=true required
#  Free account: https://e621.net/users/new → Account → Manage API Access
#  Env vars: E621_USER, E621_API_KEY  (anonymous = lower rate limit)
# ══════════════════════════════════════════════════════════════════════
async def fetch_e621(session, positive=None):
    if not NSFW_MODE: return None, None, None
    try:
        tags = ["rating:explicit", "order:random"]
        if random.random() < 0.85: tags.append(random.choice(SPICY_TAGS))
        params = {"tags": " ".join(tags), "limit": 20}
        hdrs = {"User-Agent": "NSFWBotV2/2.0 (by discord_bot_operator on e621)"}
        auth = None
        if E621_USER and E621_API_KEY:
            auth = aiohttp.BasicAuth(E621_USER, E621_API_KEY)
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get("https://e621.net/posts.json",
                               params=params, headers=hdrs, auth=auth, timeout=to) as resp:
            if resp.status == 429:
                logger.warning("e621: 429 rate limited, backing off")
                await asyncio.sleep(2); return None, None, None
            if resp.status == 401:
                logger.warning("e621: 401 Unauthorized — check E621_USER/E621_API_KEY")
                return None, None, None
            if resp.status != 200: return None, None, None
            payload = await resp.json(content_type=None)
            posts = payload.get("posts", [])
            if not posts: return None, None, None
            image_posts = [p for p in posts
                           if p.get("file", {}).get("ext") in ("jpg","jpeg","png","gif","webp")]
            if not image_posts: return None, None, None
            post = random.choice(image_posts)
            gif_url = post.get("file", {}).get("url")
            if not gif_url: return None, None, None
            tag_str = " ".join(
                post.get("tags", {}).get("general", []) +
                post.get("tags", {}).get("character", [])
            )
            extract_tags_from_meta(tag_str, GIF_TAGS, data)
            return gif_url, "e621", post
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"e621 fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 17 — yande.re   weight=14   NSFW_MODE=true required
#  API: https://yande.re/help/api — no auth required
# ══════════════════════════════════════════════════════════════════════
async def fetch_yandere(session, positive=None):
    if not NSFW_MODE: return None, None, None
    try:
        tags = ["rating:explicit", "order:random"]
        if random.random() < 0.85: tags.append(random.choice(SPICY_TAGS).replace("_", " "))
        params = {"tags": " ".join(tags), "limit": 20}
        hdrs = {"User-Agent": "NSFWBotV2/2.0"}
        to = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with session.get("https://yande.re/post.json",
                               params=params, headers=hdrs, timeout=to) as resp:
            if resp.status == 429:
                await asyncio.sleep(2); return None, None, None
            if resp.status != 200: return None, None, None
            posts = await resp.json(content_type=None)
            if not posts or not isinstance(posts, list): return None, None, None
            image_posts = [p for p in posts if p.get("file_url")
                           and not p.get("file_url", "").lower().endswith((".webm", ".mp4"))]
            if not image_posts: return None, None, None
            post = random.choice(image_posts)
            gif_url = post.get("file_url")
            if not gif_url: return None, None, None
            extract_tags_from_meta(post.get("tags", ""), GIF_TAGS, data)
            return gif_url, "yandere", post
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"yandere fail: {e}")
        return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER 18 — hypnohub.net   weight=6   NSFW_MODE=true required
#  Gelbooru-compatible API — no auth required
# ══════════════════════════════════════════════════════════════════════
async def fetch_hypnohub(session, positive=None):
    if not NSFW_MODE: return None, None, None
    url, _, post = await _gelbooru_compat(session, "https://hypnohub.net/index.php")
    return url, "hypnohub", post

# ══════════════════════════════════════════════════════════════════════
#  PROVIDER REGISTRY
#  Base: always active (no auth needed / optional keys only)
#  NSFW_EXTRA: only when NSFW_MODE=true
# ══════════════════════════════════════════════════════════════════════
_BASE_PROVIDERS = [
    # name            function            weight
    ("rule34",        fetch_rule34,       45),   # #1 — max spice, GIF bias
    ("gelbooru",      fetch_gelbooru,     18),
    ("nekosapi",      fetch_nekosapi,     15),
    ("konachan",      fetch_konachan,     12),
    ("nekobot",       fetch_nekobot,      10),
    ("danbooru",      fetch_danbooru,      8),
    ("nekos_life",    fetch_nekos_life,    8),   # NEW
    ("tbib",          fetch_tbib,          7),   # NEW
    ("xbooru",        fetch_xbooru,        7),   # NEW
    ("realbooru",     fetch_realbooru,     6),   # NEW
    ("waifu_im",      fetch_waifu_im,      5),
    ("paheal",        fetch_paheal,        5),   # NEW (XML API)
    ("waifu_it",      fetch_waifu_it,      4),   # NEW (key optional)
    ("nekos_moe",     fetch_nekos_moe,     3),
    ("waifu_pics",    fetch_waifu_pics,    2),
]

_NSFW_EXTRA_PROVIDERS = [
    ("e621",      fetch_e621,     18),   # rich tag system, high quality
    ("yandere",   fetch_yandere,  14),   # high-quality anime explicit
    ("hypnohub",  fetch_hypnohub,  6),   # NEW gelbooru-based
]

PROVIDERS = _BASE_PROVIDERS + (_NSFW_EXTRA_PROVIDERS if NSFW_MODE else [])
logger.info(f"Active providers ({len(PROVIDERS)}): {[n for n,_,_ in PROVIDERS]}")

# ══════════════════════════════════════════════════════════════════════
#  FETCH LOGIC
# ══════════════════════════════════════════════════════════════════════
def _hash_url(url):
    return hashlib.md5(url.encode()).hexdigest()

def _choose_provider():
    if TRUE_RANDOM: return random.choice(PROVIDERS)
    weights = [w for _, _, w in PROVIDERS]
    return random.choices(PROVIDERS, weights=weights, k=1)[0]

async def _fetch_one(session, used_hashes=None, force_tag=None):
    if used_hashes is None: used_hashes = set()
    tag = force_tag or random.choice(GIF_TAGS)
    name, fetch_func, _ = _choose_provider()
    try:
        url, source, meta = await fetch_func(session, tag)
        if url:
            h = _hash_url(url)
            if h not in used_hashes:
                return url, source, meta, h
    except Exception as e:
        if DEBUG_FETCH: logger.debug(f"{name} fail: {e}")
    return None, None, None, None

async def fetch_gif(session, user_id=None, force_tag=None):
    uid = str(user_id) if user_id else "global"
    history = data["sent_history"].setdefault(uid, [])
    used = set(history)
    for attempt in range(FETCH_ATTEMPTS):
        url, source, meta, url_hash = await _fetch_one(session, used, force_tag=force_tag)
        if url:
            history.append(url_hash)
            if len(history) > MAX_USED_GIFS_PER_USER: history.pop(0)
            data["sent_history"][uid] = history
            logger.info(f"Attempt {attempt+1}: fetched from {source}")
            return url, source, meta
    logger.warning(f"Failed to fetch after {FETCH_ATTEMPTS} attempts")
    return None, None, None

# ══════════════════════════════════════════════════════════════════════
#  GREETINGS  — join messages
# ══════════════════════════════════════════════════════════════════════
JOIN_GREETINGS = [
    "💋 {display_name} slips in like a slow caress — the room just warmed up.",
    "🔥 {display_name} arrived, tracing heat across the air; someone hold the temperature.",
    "✨ {display_name} joins — all eyes and soft smiles. Dare to stir trouble?",
    "😈 {display_name} steps through the door with a dangerous smile and a hungry look.",
    "👀 {display_name} appeared — sudden quiet, then the world leans in.",
    "🖤 {display_name} joined, breath shallow, pulse audible — tempting, isn't it?",
    "🌙 {display_name} glides in as if they own the moment — claim it or be claimed.",
    "🕯️ {display_name} arrives wrapped in dusk and whispering promises.",
    "🍷 {display_name} joined — like a warm pour, smooth and slow.",
    "🥀 {display_name} walked in with a smile that asked for trouble.",
    "🕶️ {display_name} stepped in cool, but the air around them is anything but.",
    "💎 {display_name} joined — rare, polished, and distractingly beautiful.",
    "👑 {display_name} arrived; treat them like royalty or lose the crown.",
    "🌫️ {display_name} drifted in; the air tastes sweeter already.",
    "🪞 {display_name} joined — catch their reflection if you dare.",
    "⚡ {display_name} joined and the electricity in the room changed lanes.",
    "🧠 {display_name} arrived with a mind on fire — play smart, play dangerous.",
    "💋 {display_name} slipped in with a grin; the night just leaned forward.",
    "🩸 {display_name} joined — bold, a little wicked, entirely noticed.",
    "🐍 {display_name} slithered in, sly and sure. Watch your step.",
    "🌒 {display_name} arrived quietly — but the silence hums with intent.",
    "🧿 {display_name} joined; the air says stay, the body says closer.",
    "🎭 {display_name} entered with a mischievous tilt — masks are optional.",
    "🪶 {display_name} stepped in with featherlight steps and heavy intent.",
    "🩶 {display_name} joined, calm on the outside, simmering on the inside.",
    "👁️ {display_name} arrived; one look and the night got complicated.",
    "🕸️ {display_name} stepped into the web — enjoy getting tangled.",
    "🌘 {display_name} joined — shadow-soft and dangerously inviting.",
    "🧊 {display_name} arrived cool, but their presence melts the room.",
    "⚖️ {display_name} walked in — the balance shifted toward desire.",
    "🪄 {display_name} joined and something magical tightened in the chest.",
    "🌺 {display_name} arrived like a slow bloom — intoxicating.",
    "🫦 {display_name} joined — lips curved, promise implied.",
    "🎶 {display_name} arrived on a private rhythm; follow if you want to sway.",
    "🌪️ {display_name} joined — whirlwinds look calm until they hit.",
    "🖤 {display_name} slipped in, hush and hunger wrapped together.",
    "💼 {display_name} entered composed — look closer, there's mischief under the suit.",
    "💫 {display_name} joined and the room took a breathless pause.",
    "🩸 {display_name} enters — the room tightens like it knows what's coming.",
    "🖤 {display_name} joined. Lock your thoughts, not your doors.",
    "🌑 {display_name} stepped in — eyes linger longer than they should.",
    "😈 {display_name} arrived with intent. Pretend you don't feel it.",
    "🕷️ {display_name} entered — something just wrapped around your focus.",
    "🔥 {display_name} joined. Heat climbs. Control slips.",
    "👁️ {display_name} is here — watched before watching back.",
    "🖤 {display_name} arrived. Breathe slow. This one doesn't rush.",
    "🌒 {display_name} slipped in — confidence sharp enough to cut.",
    "🩶 {display_name} joined quietly. Dangerous people don't announce themselves.",
    "😼 {display_name} joined with a look that asks permission from no one.",
    "🕯️ {display_name} arrived — slow burn, no mercy.",
    "🐍 {display_name} slid in — smooth, patient, inevitable.",
    "🧿 {display_name} joined — attention captured, consent assumed.",
    "🕶️ {display_name} arrived — unreadable, unbothered, unresisted.",
    "🎯 {display_name} joined — precise, unavoidable, magnetic.",
    "🔒 {display_name} arrived — doors close a little tighter.",
    "🗝️ {display_name} unlocked the room; keys aren't always literal.",
    "🧨 {display_name} entered — contained chaos with an inviting grin.",
    "🌌 {display_name} joined — vast, dark, and impossible to ignore.",
    "🖤 {display_name} slips in — the shadows made room for them.",
    "🌑 {display_name} arrived; the air tightened at their name.",
    "🩸 {display_name} walked in with an intent that hums.",
    "🔥 {display_name} entered — eyes sharpen, breaths slow.",
    "😈 {display_name} came — dangerous grace, measured steps.",
    "🕯️ {display_name} arrives, dusk trailing like a promise.",
    "👁️ {display_name} joined — every light found them first.",
    "🐍 {display_name} slipped through; patience wrapped around them.",
    "⚡ {display_name} stepped in and the dark learned to behave.",
    "🗝️ {display_name} unlocked eyes; rooms rearranged themselves.",
    "🖤 {display_name} arrived — presence heavy, attention willing.",
    "🌘 {display_name} joined; the hush leaned forward.",
    "🕶️ {display_name} walked in — unreadable, owning the pause.",
    "🔒 {display_name} arrived; the world closed in tighter.",
    "🧿 {display_name} came — watched and watching back.",
    "🩶 {display_name} stepped through — calm, collected, inevitable.",
    "🌫️ {display_name} appears like smoke — hard to push away.",
    "🐺 {display_name} joined alone; the pack noticed.",
    "🪞 {display_name} arrived — reflection trembles when they move.",
    "🎯 {display_name} entered — precise, unavoidable.",
    "🧨 {display_name} slipped in; contained trouble with a smile.",
    "🪄 {display_name} arrived — small magic, large consequence.",
    "🕷️ {display_name} stepped into the web — enjoy the pull.",
    "🌪️ {display_name} entered — calm before the pleasant storm.",
    "💎 {display_name} joined — cold, beautiful, demanding regard.",
    "🫦 {display_name} arrived; lips quiet, intentions loud.",
    "🎭 {display_name} came with a hidden grin — masks not required.",
    "🍷 {display_name} entered like a slow pour; taste lingers.",
    "🐉 {display_name} arrived — ancient hush follows new steps.",
    "🧠 {display_name} joined; thinking people are deliciously dangerous.",
    "💫 {display_name} arrives and the room holds its breath.",
    "🌺 {display_name} stepped in — beauty that commands.",
    "🕯️ {display_name} came — slow flame, sharp heat.",
    "🪶 {display_name} drifted in; soft steps, heavy intent.",
    "⚖️ {display_name} joined — balance subtly tipped.",
    "🌌 {display_name} arrived — vast, dark, magnetic.",
    "🔮 {display_name} entered; futures leaned toward them.",
    "🧊 {display_name} came cool, but the air betrayed them.",
    "🖤 {display_name} arrived — possession in every glance.",
    "🌒 {display_name} stepped in and the night acknowledged them.",
    "👑 {display_name} joined; crowns fit easily on slow smiles.",
    "🔥 {display_name} slipped in — embers trailed their footsteps.",
    "🕶️ {display_name} arrived — no one dares stare too long.",
    "🩸 {display_name} entered; the room kept a small, sharp memory.",
    "🧿 {display_name} joined — attention, harvested whole.",
    "🗝️ {display_name} stepped through; doors sighed closed behind them.",
    "🐍 {display_name} arrived — elegant, patient, inevitable.",
    "🌘 {display_name} joined; darkness welcomed a familiar shape.",
    "🎶 {display_name} entered on a low, dangerous rhythm.",
    "🪞 {display_name} came — mirrors preferred their reflection tonight.",
    "🥀 {display_name} arrives — wilted petals, potent scent.",
    "🕸️ {display_name} joined; the web tightened delightfully.",
    "💼 {display_name} stepped in — composed, with a hidden edge.",
    "🧨 {display_name} arrived — quiet fuse, loud results.",
    "🫀 {display_name} came — heartbeats answered them.",
    "🪙 {display_name} arrived — coin dropped, choices made.",
    "🐾 {display_name} joined — footsteps marking territory.",
    "🖤 {display_name} entered and the room learned to follow their lead.",
    "⚡ {display_name} arrived; sparks were not accidental.",
    "🌑 {display_name} joined — welcome to the darker side of curiosity.",
]
while len(JOIN_GREETINGS) < 120:
    JOIN_GREETINGS.append(random.choice(JOIN_GREETINGS[:]))

# ══════════════════════════════════════════════════════════════════════
#  GREETINGS  — leave messages
# ══════════════════════════════════════════════════════════════════════
LEAVE_GREETINGS = [
    "🌙 {display_name} slips away — the afterglow lingers.",
    "🖤 {display_name} left; the room exhales and remembers the warmth.",
    "🌑 {display_name} drifted out, leaving charged silence in their wake.",
    "👀 {display_name} left — eyes still searching the doorway.",
    "🕯️ {display_name} exited; the candle burned a little brighter while they were here.",
    "😈 {display_name} disappeared — mischief properly recorded.",
    "🌫️ {display_name} faded into the night; whispers followed.",
    "🧠 {display_name} walked away smiling — plotting, no doubt.",
    "🕶️ {display_name} slipped out unnoticed — or cleverly unnoticed.",
    "💎 {display_name} left — the room is slightly less dazzling.",
    "🔥 {display_name} exited — the temperature is slow to drop.",
    "🩸 {display_name} is gone; the air still carries a memory.",
    "🐍 {display_name} slithered away — patient until next time.",
    "🪞 {display_name} stepped out; reflections linger.",
    "👑 {display_name} left — brief reign, lasting impression.",
    "🌒 {display_name} faded — the shadow kept the scent.",
    "💋 {display_name} slipped away — lips still warm with goodbye.",
    "🕷️ {display_name} left the web — threads still vibrate.",
    "🧿 {display_name} exited — the room blinked and they were gone.",
    "⚡ {display_name} departed — static still crackles.",
    "🌺 {display_name} left — fragrance lingers like a second presence.",
    "🎶 {display_name} stepped out mid-beat; the rhythm misses them.",
    "🪄 {display_name} vanished — no trick, just absence.",
    "🌘 {display_name} left with the dark; the room forgot to breathe.",
    "⚖️ {display_name} exited — balance quietly unsettled.",
    "🫦 {display_name} left — the promise hangs unfinished.",
    "🌪️ {display_name} is gone; the calm feels suspicious.",
    "🗝️ {display_name} locked up and left — what did they take?",
    "🩶 {display_name} stepped out quietly — most dangerous departures are.",
    "🐺 {display_name} went back to the dark; the pack felt it.",
    "🔒 {display_name} left — something closed with them.",
    "🎯 {display_name} exited precisely — no wasted movement.",
    "🌌 {display_name} faded into the vast — see you on the other side.",
    "🐾 {display_name} left footprints; warmth on the floor.",
    "🧨 {display_name} is gone — the fuse remembers the spark.",
    "🔮 {display_name} exited; the future tilts a little.",
    "🌑 {display_name} stepped into the night — gracefully, inevitably.",
    "😈 {display_name} vanished — the mischief is still here somewhere.",
    "🕯️ {display_name} left; the flame lowered but didn't die.",
    "🖤 {display_name} gone. The hush that replaced them says everything.",
    "💫 {display_name} left; the room adjusts slowly to less light.",
    "🐉 {display_name} departed — the old presence lingers like myth.",
    "🌺 {display_name} slipped out; the air still smells of them.",
    "🩸 {display_name} left. Something small and sharp stayed.",
    "🪙 {display_name} exited — the coin has been spent.",
    "🎭 {display_name} removed their mask on the way out — imagine that.",
    "🖤 {display_name} is gone. Door's open. Heart isn't.",
    "🌒 {display_name} drifted away — half the night went with them.",
    "🧊 {display_name} left; a cool vacancy settled in.",
    "👁️ {display_name} stopped watching — the room feels less seen.",
    "🔥 {display_name} left; the embers hold the shape of their heat.",
    "🗡️ {display_name} departed — the edge lingered.",
    "🌫️ {display_name} dissolved into the air — breathe deep.",
    "⚡ {display_name} stepped out; the sparks miss them already.",
    "🎯 {display_name} gone. The precision of their absence is felt.",
    "🕸️ {display_name} left the web spinning — enjoy the vibration.",
    "🪞 {display_name} exited — the glass is duller without them.",
    "💎 {display_name} gone — the room noticed the drop in brilliance.",
    "🧿 {display_name} stepped away — but the eye still watches.",
    "🐍 {display_name} coiled away into the dark. Until next time.",
]
while len(LEAVE_GREETINGS) < 60:
    LEAVE_GREETINGS.append(random.choice(LEAVE_GREETINGS[:]))

# ══════════════════════════════════════════════════════════════════════
#  SEND GREETING EMBED  (to chat channel + DM to member)
# ══════════════════════════════════════════════════════════════════════
async def send_greeting_embed(channel, session, greeting_text, image_url, member, send_to_dm=None):
    try:
        image_bytes, content_type = await _download_bytes(session, image_url)
        if image_bytes and len(image_bytes) > DISCORD_MAX_UPLOAD:
            image_bytes = await compress_image(image_bytes)
        if not image_bytes or len(image_bytes) > DISCORD_MAX_UPLOAD:
            await channel.send(greeting_text)
            return

        lurl = image_url.lower()
        ctype = content_type or ""
        ext = ".jpg"
        if "gif" in lurl or "gif" in ctype: ext = ".gif"
        elif "png" in lurl or "png" in ctype: ext = ".png"
        elif "webp" in lurl or "webp" in ctype: ext = ".webp"

        filename = f"nsfw{ext}"

        # ── send to channel ──
        ch_file = discord.File(io.BytesIO(image_bytes), filename=filename)
        ch_embed = discord.Embed(description=greeting_text, color=discord.Color.from_rgb(220, 53, 69))
        ch_embed.set_author(
            name=member.display_name,
            icon_url=getattr(member.display_avatar, "url", None),
        )
        ch_embed.set_image(url=f"attachment://{filename}")
        ch_embed.set_footer(text="NSFW Bot v2")
        await channel.send(embed=ch_embed, file=ch_file)

        # ── DM the member ──
        if send_to_dm:
            try:
                dm_file = discord.File(io.BytesIO(image_bytes), filename=filename)
                dm_embed = discord.Embed(
                    description=greeting_text,
                    color=discord.Color.from_rgb(46, 204, 113),
                )
                dm_embed.set_author(
                    name=member.display_name,
                    icon_url=getattr(member.display_avatar, "url", None),
                )
                dm_embed.set_image(url=f"attachment://{filename}")
                dm_embed.set_footer(text="NSFW Bot v2 — private drop")
                await send_to_dm.send(embed=dm_embed, file=dm_file)
            except Exception as e:
                logger.warning(f"Could not DM {member.display_name}: {e}")

    except Exception as e:
        logger.error(f"send_greeting_embed failed: {e}")
        try:
            await channel.send(greeting_text)
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════
#  VC HELPERS
# ══════════════════════════════════════════════════════════════════════
def get_vcs_with_users(guild):
    """Return list of (VoiceChannel, [users]) for all monitored VCs that have users."""
    out = []
    for vc_id in VC_IDS:
        vc = guild.get_channel(vc_id)
        if vc and isinstance(vc, discord.VoiceChannel):
            users = [m for m in vc.members if not m.bot]
            if users: out.append((vc, users))
    return out

def all_vcs_empty(guild):
    for vc_id in VC_IDS:
        vc = guild.get_channel(vc_id)
        if vc and isinstance(vc, discord.VoiceChannel):
            if [m for m in vc.members if not m.bot]: return False
    return True

# ══════════════════════════════════════════════════════════════════════
#  UPDATE BOT VC POSITION
#
#  Logic (in priority order):
#   1. If target_channel given and has users → follow instantly
#   2. Stay in current VC if it still has non-bot users
#   3. Move to first VC_IDS-ordered VC that has users
#   4. All VCs empty → stay in last VC (24/7 presence, no disconnect)
#   5. Not connected at all → fallback: connect to first available VC
# ══════════════════════════════════════════════════════════════════════
async def update_vc_position(guild, target_channel=None):
    vc_client = guild.voice_client

    # Step 1 — follow user to target channel
    if target_channel and target_channel.id in VC_IDS:
        users = [m for m in target_channel.members if not m.bot]
        if users:
            try:
                if vc_client and vc_client.is_connected():
                    if vc_client.channel.id != target_channel.id:
                        await vc_client.move_to(target_channel)
                        logger.info(f"Bot FOLLOWED user to: {target_channel.name}")
                else:
                    await target_channel.connect()
                    logger.info(f"Bot joined target VC: {target_channel.name}")
                return target_channel
            except Exception as e:
                logger.error(f"Failed to follow to {target_channel.name}: {e}")

    # Step 2 — stay in current VC if still occupied
    if vc_client and vc_client.is_connected():
        current = vc_client.channel
        if current and current.id in VC_IDS:
            users = [m for m in current.members if not m.bot]
            if users:
                logger.debug(f"Staying in current VC: {current.name}")
                return current

    # Step 3 — move to first occupied VC (VC_IDS order)
    vcs = get_vcs_with_users(guild)
    if vcs:
        order = {vid: i for i, vid in enumerate(VC_IDS)}
        vcs.sort(key=lambda x: order.get(x[0].id, 999))
        target_vc = vcs[0][0]
        try:
            if vc_client and vc_client.is_connected():
                if vc_client.channel.id != target_vc.id:
                    await vc_client.move_to(target_vc)
                    logger.info(f"Bot moved to active VC: {target_vc.name}")
            else:
                await target_vc.connect()
                logger.info(f"Bot joined active VC: {target_vc.name}")
            return target_vc
        except Exception as e:
            logger.error(f"Failed to move to active VC: {e}")

    # Step 4 — all empty, stay in last VC (24/7 mode, no disconnect)
    if vc_client and vc_client.is_connected():
        logger.info("All VCs empty — bot staying in last VC (24/7 mode)")
        return vc_client.channel

    # Step 5 — fallback: connect to first available monitored VC
    for vc_id in VC_IDS:
        vc = guild.get_channel(vc_id)
        if vc and isinstance(vc, discord.VoiceChannel):
            try:
                await vc.connect()
                logger.info(f"Fallback: bot connected to {vc.name}")
                return vc
            except Exception: continue
    return None

# ══════════════════════════════════════════════════════════════════════
#  BOT SETUP
# ══════════════════════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.voice_states    = True
intents.message_content = True
intents.members         = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ══════════════════════════════════════════════════════════════════════
#  BACKGROUND TASKS
# ══════════════════════════════════════════════════════════════════════
@tasks.loop(seconds=AUTOSAVE_INTERVAL)
async def autosave_task():
    try: save_data()
    except Exception as e: logger.warning(f"Autosave failed: {e}")

@tasks.loop(seconds=45)
async def periodic_vc_drop():
    """Every 45 s: drop an explicit image in the text channel when 2+ users are in a VC."""
    if not VC_CHANNEL_ID: return
    for vc_id in VC_IDS:
        vc = bot.get_channel(vc_id)
        if not vc or not isinstance(vc, discord.VoiceChannel): continue
        users = [m for m in vc.members if not m.bot]
        if len(users) >= 2:
            channel = bot.get_channel(VC_CHANNEL_ID)
            if not channel: continue
            try:
                async with aiohttp.ClientSession() as session:
                    url, source, meta = await fetch_gif(session)
                    if url:
                        image_bytes, content_type = await _download_bytes(session, url)
                        if image_bytes:
                            if len(image_bytes) > DISCORD_MAX_UPLOAD:
                                image_bytes = await compress_image(image_bytes)
                            if image_bytes and len(image_bytes) <= DISCORD_MAX_UPLOAD:
                                ext = ".gif" if (
                                    "gif" in url.lower() or
                                    (content_type and "gif" in content_type)
                                ) else ".jpg"
                                await channel.send(
                                    file=discord.File(io.BytesIO(image_bytes), filename=f"nsfw{ext}")
                                )
            except Exception as e:
                logger.error(f"periodic_vc_drop error: {e}")
            break  # only one drop per tick regardless of how many VCs are active

@tasks.loop(seconds=300)
async def vc_reconnect_heartbeat():
    """Every 5 min: reconnect to VC if bot got disconnected."""
    for guild in bot.guilds:
        try:
            vc_client = guild.voice_client
            if vc_client and vc_client.is_connected(): continue
            connected = False
            # Try VC with users first
            for vc_id in VC_IDS:
                vc = guild.get_channel(vc_id)
                if vc and isinstance(vc, discord.VoiceChannel):
                    if [m for m in vc.members if not m.bot]:
                        try:
                            await vc.connect()
                            logger.info(f"Heartbeat reconnected to: {vc.name}")
                            connected = True; break
                        except Exception as e:
                            logger.error(f"Heartbeat connect fail {vc_id}: {e}")
            # Fallback to first available VC
            if not connected:
                for vc_id in VC_IDS:
                    vc = guild.get_channel(vc_id)
                    if vc and isinstance(vc, discord.VoiceChannel):
                        try:
                            await vc.connect()
                            logger.info(f"Heartbeat fallback: {vc.name}")
                            break
                        except Exception as e:
                            logger.error(f"Heartbeat fallback fail {vc_id}: {e}")
        except Exception as e:
            logger.error(f"vc_reconnect_heartbeat error: {e}")

# ══════════════════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info(f"NSFW_MODE={NSFW_MODE} | AGE_VERIFIED={AGE_VERIFIED}")
    logger.info(f"Providers ({len(PROVIDERS)}): {[n for n,_,_ in PROVIDERS]}")
    logger.info(f"VC_IDS: {VC_IDS}")
    logger.info(f"VC_CHANNEL_ID: {VC_CHANNEL_ID}")
    logger.info(f"GIF_TAGS loaded: {len(GIF_TAGS)}")
    # Start background tasks
    for task in (autosave_task, periodic_vc_drop, vc_reconnect_heartbeat):
        if not task.is_running(): task.start()
    # Initial VC join on startup
    for guild in bot.guilds:
        try:
            await update_vc_position(guild)
        except Exception as e:
            logger.warning(f"Initial VC join failed ({guild.name}): {e}")

@bot.event
async def on_voice_state_update(member, before, after):
    """Called when any user changes voice state. Handles VC follow + greetings + DMs."""
    if member.id == bot.user.id: return  # ignore bot's own state changes

    guild   = member.guild
    channel = bot.get_channel(VC_CHANNEL_ID) if VC_CHANNEL_ID else None

    was_monitored = before and before.channel and before.channel.id in VC_IDS
    now_monitored = after  and after.channel  and after.channel.id  in VC_IDS

    # ── VC position logic ──
    if was_monitored or now_monitored:
        if now_monitored and (not was_monitored or before.channel.id != after.channel.id):
            # User joined or switched to a monitored VC → follow them
            await update_vc_position(guild, target_channel=after.channel)
        else:
            # User left or switched within monitored VCs → re-evaluate position
            await update_vc_position(guild)

    if not channel: return  # no text channel configured, skip messages

    async with aiohttp.ClientSession() as session:
        # ── JOIN ──
        if now_monitored and (not was_monitored or before.channel.id != after.channel.id):
            greeting = random.choice(JOIN_GREETINGS).format(display_name=member.display_name)
            gif_url, _, _ = await fetch_gif(session, member.id)
            if gif_url:
                await send_greeting_embed(
                    channel, session, greeting, gif_url, member, send_to_dm=member
                )
            else:
                await channel.send(greeting)

        # ── LEAVE ──
        elif was_monitored and not now_monitored:
            leave_msg = random.choice(LEAVE_GREETINGS).format(display_name=member.display_name)
            gif_url, _, _ = await fetch_gif(session, member.id)
            if gif_url:
                await send_greeting_embed(
                    channel, session, leave_msg, gif_url, member, send_to_dm=member
                )
            else:
                await channel.send(leave_msg)

# ══════════════════════════════════════════════════════════════════════
#  COMMAND HELPER
# ══════════════════════════════════════════════════════════════════════
async def _send_image(ctx, force_tag=None):
    """Fetch and send one NSFW image to the command's channel."""
    async with aiohttp.ClientSession() as session:
        gif_url, source, meta = await fetch_gif(session, ctx.author.id, force_tag=force_tag)
        if gif_url:
            try:
                image_bytes, content_type = await _download_bytes(session, gif_url)
                if image_bytes:
                    if len(image_bytes) > DISCORD_MAX_UPLOAD:
                        image_bytes = await compress_image(image_bytes)
                    if image_bytes and len(image_bytes) <= DISCORD_MAX_UPLOAD:
                        ext = ".gif" if (
                            "gif" in gif_url.lower() or
                            (content_type and "gif" in content_type)
                        ) else ".jpg"
                        await ctx.send(
                            file=discord.File(io.BytesIO(image_bytes), filename=f"nsfw{ext}")
                        )
                        return True
            except Exception as e:
                logger.error(f"_send_image error: {e}")
        await ctx.send("❌ Could not fetch content right now. Try again.")
        return False

# ══════════════════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════════════════

# — General —
@bot.command(name="nsfw", aliases=["n", "pic", "image"])
async def cmd_nsfw(ctx, *, tag: str = None):
    """!nsfw [tag] — random NSFW image, optional tag"""
    await _send_image(ctx, force_tag=tag.strip() if tag else None)

@bot.command(name="hentai", aliases=["h"])
async def cmd_hentai(ctx):
    """!hentai — hentai tagged content"""
    await _send_image(ctx, force_tag="hentai")

@bot.command(name="gif")
async def cmd_gif(ctx):
    """!gif — animated GIF"""
    await _send_image(ctx, force_tag="animated")

# — Category shortcuts —
@bot.command(name="anal")
async def cmd_anal(ctx):
    await _send_image(ctx, force_tag="anal")

@bot.command(name="blowjob", aliases=["bj", "oral"])
async def cmd_blowjob(ctx):
    await _send_image(ctx, force_tag="blowjob")

@bot.command(name="creampie", aliases=["cream"])
async def cmd_creampie(ctx):
    await _send_image(ctx, force_tag="creampie")

@bot.command(name="gangbang", aliases=["gang"])
async def cmd_gangbang(ctx):
    await _send_image(ctx, force_tag="gangbang")

@bot.command(name="deepthroat", aliases=["dt", "throat"])
async def cmd_deepthroat(ctx):
    await _send_image(ctx, force_tag="deepthroat")

@bot.command(name="tentacle", aliases=["tentacles"])
async def cmd_tentacle(ctx):
    await _send_image(ctx, force_tag="tentacles")

@bot.command(name="ahegao")
async def cmd_ahegao(ctx):
    await _send_image(ctx, force_tag="ahegao")

@bot.command(name="bondage", aliases=["bdsm"])
async def cmd_bondage(ctx):
    await _send_image(ctx, force_tag="bondage")

@bot.command(name="futanari", aliases=["futa"])
async def cmd_futanari(ctx):
    await _send_image(ctx, force_tag="futanari")

@bot.command(name="paizuri", aliases=["titfuck", "tits"])
async def cmd_paizuri(ctx):
    await _send_image(ctx, force_tag="paizuri")

@bot.command(name="squirt", aliases=["squirting"])
async def cmd_squirt(ctx):
    await _send_image(ctx, force_tag="squirting")

@bot.command(name="milf")
async def cmd_milf(ctx):
    await _send_image(ctx, force_tag="milf")

@bot.command(name="cumshot", aliases=["cum", "facial"])
async def cmd_cumshot(ctx):
    await _send_image(ctx, force_tag="cumshot")

@bot.command(name="riding")
async def cmd_riding(ctx):
    await _send_image(ctx, force_tag="riding")

@bot.command(name="pov")
async def cmd_pov(ctx):
    await _send_image(ctx, force_tag="pov")

@bot.command(name="facesitting", aliases=["facesit"])
async def cmd_facesitting(ctx):
    await _send_image(ctx, force_tag="facesitting")

@bot.command(name="femdom")
async def cmd_femdom(ctx):
    await _send_image(ctx, force_tag="femdom")

@bot.command(name="ass", aliases=["butt", "booty"])
async def cmd_ass(ctx):
    await _send_image(ctx, force_tag="ass")

@bot.command(name="boobs", aliases=["boob", "oppai", "busty"])
async def cmd_boobs(ctx):
    await _send_image(ctx, force_tag="big_breasts")

@bot.command(name="nipples", aliases=["nips"])
async def cmd_nipples(ctx):
    await _send_image(ctx, force_tag="nipples")

@bot.command(name="handjob", aliases=["hj"])
async def cmd_handjob(ctx):
    await _send_image(ctx, force_tag="handjob")

@bot.command(name="footjob", aliases=["feet"])
async def cmd_footjob(ctx):
    await _send_image(ctx, force_tag="footjob")

@bot.command(name="spanking", aliases=["spank"])
async def cmd_spanking(ctx):
    await _send_image(ctx, force_tag="spanking")

@bot.command(name="missionary")
async def cmd_missionary(ctx):
    await _send_image(ctx, force_tag="missionary")

@bot.command(name="doggy", aliases=["doggystyle"])
async def cmd_doggy(ctx):
    await _send_image(ctx, force_tag="doggy_style")

@bot.command(name="dp", aliases=["doublepenetration"])
async def cmd_dp(ctx):
    await _send_image(ctx, force_tag="double_penetration")

# — Utility —
@bot.command(name="providers", aliases=["srcs"])
async def cmd_providers(ctx):
    """!providers — list all active NSFW providers"""
    names = [n for n, _, _ in PROVIDERS]
    msg = f"**Active providers ({len(names)}):**\n`{'` `'.join(names)}`"
    await ctx.send(msg)

@bot.command(name="tags", aliases=["taglist"])
async def cmd_tags(ctx):
    """!tags — show first 40 known tags"""
    sample = GIF_TAGS[:40]
    await ctx.send(f"**Known tags ({len(GIF_TAGS)} total, showing 40):**\n`{'` `'.join(sample)}`")

@bot.command(name="addtag")
async def cmd_addtag(ctx, *, tag: str):
    """!addtag <tag> — add a custom tag to the rotation"""
    added = add_tag(tag.strip().lower(), GIF_TAGS, data)
    if added:
        await ctx.send(f"✅ Tag `{tag.strip().lower()}` added to rotation!")
    else:
        await ctx.send(f"⚠️ Tag `{tag.strip().lower()}` is already in the list or too short.")

@bot.command(name="cmds", aliases=["commands", "help2", "bothelp"])
async def cmd_commands(ctx):
    """!cmds — show all bot commands"""
    lines = [
        "**━━━ NSFW Bot v2 Commands ━━━**",
        "`!nsfw [tag]` — random image, optional tag",
        "`!hentai` `!gif` — type-specific",
        "",
        "**Category shortcuts:**",
        "`!anal` `!blowjob` `!creampie` `!gangbang` `!deepthroat`",
        "`!tentacle` `!ahegao` `!bondage` `!futanari` `!paizuri`",
        "`!squirt` `!milf` `!cumshot` `!riding` `!pov` `!facesitting`",
        "`!femdom` `!ass` `!boobs` `!nipples` `!handjob` `!footjob`",
        "`!spanking` `!missionary` `!doggy` `!dp`",
        "",
        "**Utility:**",
        "`!providers` — list active providers",
        "`!tags` — show tag list",
        "`!addtag <tag>` — add custom tag",
        "`!cmds` — this menu",
    ]
    await ctx.send("\n".join(lines))

# ══════════════════════════════════════════════════════════════════════
#  LAUNCH
# ══════════════════════════════════════════════════════════════════════
if not TOKEN:
    logger.error("TOKEN env var is not set. Add it on Render → Environment Variables.")
    sys.exit(1)

keep_alive()
bot.run(TOKEN)
