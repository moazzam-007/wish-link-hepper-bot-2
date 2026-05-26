import os
import random
import re
import time
import requests
import hmac
import hashlib
import base64
import json as _json
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import logging
from flask import Flask, request, jsonify
from functools import wraps
import asyncio
import threading

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
# 🔧 Environment Variables
# ============================================================
TOKEN                  = os.getenv("BOT_TOKEN")
WEBHOOK_URL            = os.getenv("WEBHOOK_URL")

WEBHOOK_SECRET         = os.getenv("WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    raise ValueError("WEBHOOK_SECRET env var is missing! Required for security.")

API_SECRET_KEY         = os.getenv("API_SECRET_KEY")
if not API_SECRET_KEY:
    raise ValueError("API_SECRET_KEY env var is missing! Required to secure private APIs.")
WISHLINK_ID            = os.getenv("WISHLINK_ID")
if not WISHLINK_ID:
    raise ValueError("WISHLINK_ID env var is missing! Required for wishlink API.")
WISHLINK_CREATOR       = os.getenv("WISHLINK_CREATOR", "budget.looks")
FIREBASE_API_KEY       = os.getenv("FIREBASE_API_KEY")
WISHLINK_REFRESH_TOKEN = os.getenv("WISHLINK_REFRESH_TOKEN")
WISHLINK_BZ_AUTH_KEY   = os.getenv("WISHLINK_BZ_AUTH_KEY")

# ── Facebook Page Credentials (for FB Graph API posting + Wishlink FB DM) ──
FB_PAGE_ACCESS_TOKEN   = os.getenv("FB_PAGE_ACCESS_TOKEN", "")
FB_PAGE_ID             = os.getenv("FB_PAGE_ID", "")

WISHLINK_CREATOR_URL = WISHLINK_CREATOR

TITLES = [
    "🔥 Loot Deal Alert!", "💥 Hot Deal Incoming!", "⚡ Limited Time Offer!",
    "🎯 Grab Fast!", "🚨 Flash Sale!", "💎 Special Deal Just For You!",
    "🛒 Shop Now!", "📢 Price Drop!", "🎉 Mega Offer!", "🤑 Crazy Discount!"
]

telegram_app = None
event_loop = None

# ============================================================
# 🔑 Firebase Token Cache + Auto Refresh
# ============================================================
_token_cache = {
    "id_token": None,
    "expires_at": 0
}
_token_lock = threading.Lock()

def get_fresh_wishlink_token():
    global _token_cache
    current_time = time.time()

    with _token_lock:
        if _token_cache["id_token"] and _token_cache["expires_at"] > current_time + 300:
            logger.info("✅ Cached token valid hai — reuse kar raha hoon")
            return _token_cache["id_token"]

    logger.info("🔄 Firebase token refresh kar raha hoon...")

    if not FIREBASE_API_KEY or not WISHLINK_REFRESH_TOKEN:
        logger.warning("⚠️ Firebase credentials missing — BZ auth key try karunga")
        return WISHLINK_BZ_AUTH_KEY

    try:
        resp = requests.post(
            f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}",
            json={
                "grant_type": "refresh_token",
                "refresh_token": WISHLINK_REFRESH_TOKEN
            },
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()

        new_token  = data.get("id_token")
        expires_in = int(data.get("expires_in", 3600))

        with _token_lock:
            _token_cache["id_token"]   = new_token
            _token_cache["expires_at"] = current_time + expires_in

        # Remove the token logging for security reasons
        logger.info(f"✅ Token refresh successful! Token is valid for {expires_in}s")
        return new_token

    except Exception as e:
        logger.error(f"❌ Firebase token refresh failed: {e}")
        if WISHLINK_BZ_AUTH_KEY:
            logger.info("🔄 BZ auth key fallback use kar raha hoon")
            return WISHLINK_BZ_AUTH_KEY
        return None


def get_creator_headers(token=None):
    if not token:
        token = get_fresh_wishlink_token()
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "Origin": "https://creator.wishlink.com",
        "Referer": "https://creator.wishlink.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }


# ============================================================
# 💰 Affiliate Link Conversion
# ============================================================
def convert_to_affiliate_link(product_url):
    try:
        token = get_fresh_wishlink_token()
        if not token:
            logger.warning("⚠️ Token nahi mila — raw URL return karunga")
            return product_url

        resp = requests.post(
            "https://api.wishlink.com/api/c/convertSingleProductLink",
            headers=get_creator_headers(token),
            json={"link": product_url, "creator": WISHLINK_CREATOR},
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()

        affiliate_link = (
            data.get("wishlink") or
            data.get("data", {}).get("wishlink") or
            data.get("url")
        )

        if affiliate_link:
            logger.info(f"✅ Affiliate link: {affiliate_link}")
            return affiliate_link
        else:
            logger.warning(f"⚠️ Affiliate link nahi mila: {data}")
            return product_url

    except Exception as e:
        logger.error(f"❌ Affiliate conversion failed: {e}")
        return product_url


# ============================================================
# 🔗 Wishlink Product Helpers
# ============================================================
def get_final_url_from_redirect(start_url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(start_url, timeout=15, headers=headers, allow_redirects=True)
        return response.url
    except Exception as e:
        logger.error(f"Redirect error: {e}")
        return None


# ============================================================
# 🛍️ Lehlah Extraction Helpers (No Auth Required)
# app.lehlah.club/pc/{id}  → Collection
# app.lehlah.club/post/{id} → Post
# ============================================================
LEHLAH_COLLECTION_API = "https://app.lehlah.club/api/collection/details"
LEHLAH_POST_API       = "https://app.lehlah.club/api/post/details"
LEHLAH_REDIRECT_API   = (
    "https://web.lehlah.club/api/redirection/"
    "generate-redirect-url-in-app-redirection"
)
LEHLAH_REQ_HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://app.lehlah.club/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def parse_lehlah_url(url):
    """Lehlah link type aur ID detect karo."""
    m = re.search(r'app\.lehlah\.club/pc/(\d+)', url)
    if m:
        return "collection", m.group(1)
    m = re.search(r'app\.lehlah\.club/post/(\d+)', url)
    if m:
        return "post", m.group(1)
    return None, None


def get_lehlah_collection_products(collection_id):
    """Collection ID se original product URLs nikalo (1 API call)."""
    try:
        resp = requests.post(
            LEHLAH_COLLECTION_API,
            json={"collection_id": str(collection_id)},
            headers=LEHLAH_REQ_HEADERS,
            timeout=15
        )
        resp.raise_for_status()
        products = resp.json().get("products", [])
        urls = [p["url"] for p in products if p.get("url")]
        logger.info(f"[Lehlah] Collection {collection_id}: {len(urls)} products mila")
        return urls
    except Exception as e:
        logger.error(f"[Lehlah] Collection API error: {e}")
        return []


def get_lehlah_post_products(post_id):
    """Post ID se original product URLs nikalo (2 API calls — post details + redirect per product)."""
    try:
        resp = requests.post(
            LEHLAH_POST_API,
            json={"post_ids": [int(post_id)]},
            headers=LEHLAH_REQ_HEADERS,
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        posts = data.get("data", {}).get("data", [])
        if not posts:
            logger.warning(f"[Lehlah] Post {post_id}: koi data nahi mila")
            return []
        tags = posts[0].get("post", {}).get("product_tags_json", [])
    except Exception as e:
        logger.error(f"[Lehlah] Post API error: {e}")
        return []

    # Parallel short_code resolution (3-4x faster than sequential)
    short_codes = [tag.get("short_code", "") for tag in tags if tag.get("short_code", "")]

    def resolve_short_code(sc):
        try:
            r2 = requests.post(
                LEHLAH_REDIRECT_API,
                json={
                    "short_code": sc,
                    "referrer": "https://app.lehlah.club/",
                    "is_in_app": False,
                    "is_telegram": False,
                    "is_youtube": False,
                    "is_instagram": False,
                    "is_ios": False,
                    "is_android": False
                },
                headers=LEHLAH_REQ_HEADERS,
                timeout=10
            )
            redirect_url = r2.json().get("redirect_url", "")
            if redirect_url and redirect_url.startswith("http"):
                logger.info(f"[Lehlah] short_code={sc} \u2192 {redirect_url[:80]}")
                return redirect_url
            logger.warning(f"[Lehlah] short_code={sc} \u2192 no valid URL")
            return None
        except Exception as e:
            logger.warning(f"[Lehlah] short_code={sc} resolve failed: {e}")
            return None

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(resolve_short_code, short_codes))

    urls = [u for u in results if u]
    logger.info(f"[Lehlah] Post {post_id}: {len(urls)} products mila")
    return urls


def get_product_links_from_lehlah_url(lehlah_url):
    """Main Lehlah function — koi bhi Lehlah link do, original product URLs milenge."""
    link_type, link_id = parse_lehlah_url(lehlah_url)
    if link_type == "collection":
        return get_lehlah_collection_products(link_id)
    elif link_type == "post":
        return get_lehlah_post_products(link_id)
    logger.warning(f"[Lehlah] Unknown URL format: {lehlah_url}")
    return []

# ============================================================
# 🛍️ Faym Extraction Helpers (No Playwright — Pure requests)
# faym.co/post/{id} → Clean Flipkart/Amazon/Myntra/Meesho URLs
# JWT secret reverse-engineered from faym.co/static/js/main.js
# ============================================================
FAYM_SECRET   = "83062d44f574b6007bdbb4fb725dc944d753c1b331ed63232828ab6d16474b6ae81524c1902bc05e78d8af777aa8256e908b85c917bb570bb3e7536bc78b23d0"
FAYM_CYPHER   = "05aea047511d9073bb7e2fbda489bdca3c0731c429859b4da48fed240d17959922ab2388ad518ef62b2402c27f43214802e496282610e88e37e7b479a497cc47"
FAYM_API_BASE = "https://backend.faym.co"
FAYM_HEADERS  = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Origin":          "https://faym.co",
    "Referer":         "https://faym.co/",
    "Accept-Language": "en-US,en;q=0.9",
}


def _faym_b64url(data: bytes) -> str:
    """Base64 URL encode (no padding) — replicates JS yC() function."""
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def generate_faym_token() -> str:
    """
    Fresh Faym JWT generate karo — replicates AC() from faym.co JS.
    Token 10 seconds valid (server-side check).
    """
    now_ms  = int(time.time() * 1000)
    exp_ms  = now_ms + 30000  # 30 seconds — Render cold start ke liye safe
    fmt     = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f"{ms % 1000:03d}Z"
    payload = {"cypherCode": FAYM_CYPHER, "expirationTime": fmt(exp_ms), "generationTime": fmt(now_ms)}
    header  = {"alg": "HS256", "typ": "JWT"}
    hdr_b64 = _faym_b64url(_json.dumps(header,  separators=(',', ':')).encode())
    pay_b64 = _faym_b64url(_json.dumps(payload, separators=(',', ':')).encode())
    message = f"{hdr_b64}.{pay_b64}".encode()
    sig     = hmac.new(FAYM_SECRET.encode(), message, hashlib.sha256).digest()
    return f"{hdr_b64}.{pay_b64}.{_faym_b64url(sig)}"


def get_faym_content_id(faym_url: str):
    """faym.co/post/{uuid} se content ID nikalo."""
    m = re.search(r'/post/([a-zA-Z0-9-]+)', faym_url.split('?')[0])
    return m.group(1) if m else None


def clean_faym_product_url(url: str) -> str:
    """
    Faym ke affiliate tracking params hataao → clean original product URL.
    dl.flipkart.com/dl/... → https://www.flipkart.com/...?pid=PID
    Amazon / Myntra / Meesho → query params strip
    """
    if not url:
        return url
    # Flipkart deep link → standard URL
    if 'dl.flipkart.com/dl/' in url:
        clean  = url.replace('http://dl.flipkart.com/dl/',  'https://www.flipkart.com/', 1)
        clean  = clean.replace('https://dl.flipkart.com/dl/', 'https://www.flipkart.com/', 1)
        parsed = urlparse(clean)
        params = parse_qs(parsed.query)
        pid    = params.get('pid', [''])[0]
        new_q  = f"pid={pid}" if pid else ""
        return parsed._replace(query=new_q).geturl()
    # Regular Flipkart URL — sirf pid rakhna
    if 'flipkart.com' in url:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        pid    = params.get('pid', [''])[0]
        new_q  = f"pid={pid}" if pid else ""
        return parsed._replace(query=new_q).geturl()
    # Amazon — /dp/ASIN ya /gp/product/ASIN tak
    if 'amazon.in' in url or 'amazon.com' in url:
        m = re.search(r'(https?://[^/]*amazon\.[a-z]+/(?:[^/]+/)?(?:dp|gp/product)/[A-Z0-9]+)', url)
        return m.group(1) if m else url.split('?')[0]
    # Myntra / Meesho — query strip
    if 'myntra.com' in url or 'meesho.com' in url:
        return url.split('?')[0]
    return url


def get_faym_products(faym_url: str) -> list:
    """
    Main Faym function — faym.co/post/{id} → clean Flipkart/Amazon product URLs.
    JWT generate karo → API call → Faym affiliate params hataao → clean URLs return.
    """
    content_id = get_faym_content_id(faym_url)
    if not content_id:
        logger.warning(f"[Faym] Invalid URL format: {faym_url}")
        return []

    try:
        token   = generate_faym_token()
        headers = dict(FAYM_HEADERS)
        headers['authorization'] = f'Bearer {token}'
        api_url = (
            f"{FAYM_API_BASE}/api/wall/content"
            f"?contentId={content_id}&offset=0&limit=8&isFirstPost=true&isShort=false&origin=wall"
        )
        r    = requests.get(api_url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"[Faym] API error: {e}")
        return []

    posts = data.get('data', {}).get('posts', [])
    if not posts:
        logger.warning(f"[Faym] No posts in response for {content_id}")
        return []

    # Exact post prefer karo, fallback to first
    matched = [p for p in posts if p.get('id') == content_id]
    target  = matched[0] if matched else posts[0]

    clean_urls = []
    for prod in target.get('products', []):
        meta    = prod.get('metaData', {})
        resp    = meta.get('response', {})
        raw_url = resp.get('affiliate_link') or meta.get('url') or ''
        if raw_url:
            clean = clean_faym_product_url(raw_url)
            if clean:
                clean_urls.append(clean)
                logger.info(f"[Faym] ✅ {clean[:80]}")

    logger.info(f"[Faym] {content_id}: {len(clean_urls)} clean URLs extracted")
    return clean_urls


def get_product_links_from_wishlink_url(wishlink_url):
    # ── Faym URL? ────────────────────────────────────────────
    if "faym.co" in wishlink_url:
        return get_faym_products(wishlink_url)
    # ── Lehlah URL? Internally handle karo ──────────────────
    if "app.lehlah.club" in wishlink_url:
        return get_product_links_from_lehlah_url(wishlink_url)
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://www.wishlink.com",
        "referer": "https://www.wishlink.com/",
        "user-agent": "Mozilla/5.0",
        "wishlinkid": WISHLINK_ID,
    }

    m = re.search(r'wishlink\.com/([^/?]+)/(post|reels|collection)/(\d+)', wishlink_url)
    if not m:
        logger.error(f"[WL] URL format nahi pehchana: {wishlink_url}")
        return []

    username  = m.group(1)
    url_type  = m.group(2)
    post_id   = m.group(3)

    if url_type == 'reels':
        post_type = 'REELS'
    elif url_type == 'collection':
        post_type = 'COLLECTION'
    else:
        post_type = 'POST'

    logger.info(f"[WL] Fetching: username={username}, type={post_type}, id={post_id}")

    url = (
        f"https://api.wishlink.com/api/store/getPostOrCollectionProducts"
        f"?page=1&limit=50&postType={post_type}"
        f"&postOrCollectionId={post_id}"
        f"&username={username}&sourceApp=STOREFRONT"
    )

    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        products = data.get("data", {}).get("products", [])
        if products:
            links = [p["purchaseUrl"] for p in products if "purchaseUrl" in p]
            logger.info(f"[WL] ✅ {len(links)} products mila from {url_type}/{post_id}")
            return links
        else:
            logger.warning(f"[WL] 0 products in response for {username}/{url_type}/{post_id}")
    except Exception as e:
        logger.error(f"[WL] ❌ API error: {e}")

    return []


# ============================================================
# 🗂️ Wishlink Collection Creator
# ============================================================
def create_wishlink_collection(product_urls, collection_name=None):
    if not product_urls:
        logger.error("❌ Product URLs nahi diye")
        return None

    if not collection_name:
        collection_name = f"Budget Looks - {time.strftime('%d %b %Y')}"

    token = get_fresh_wishlink_token()
    if not token:
        logger.error("❌ Auth token unavailable")
        return None

    headers = get_creator_headers(token)

    # ── Step 1: Collection banao ─────────
    try:
        logger.info(f"📁 Creating collection: {collection_name}")

        form_data = {
            "title": (None, collection_name),
            "image": (None, ""),
            "thumbnail_type": (None, "manual"),
            "creator": (None, WISHLINK_CREATOR),
        }
        form_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}

        create_resp = requests.post(
            "https://api.wishlink.com/api/c/createEditShopCollection",
            headers=form_headers,
            files=form_data,
            timeout=20
        )
        create_resp.raise_for_status()
        create_data = create_resp.json()

        collection_id = (
            create_data.get("collection") or
            create_data.get("data", {}).get("id") or
            create_data.get("data", {}).get("postCollectionId") or
            create_data.get("id") or
            create_data.get("postCollectionId")
        )

        if not collection_id:
            logger.error(f"❌ Collection ID nahi mila: {create_data}")
            return None

        logger.info(f"✅ Collection created! ID: {collection_id}")

    except Exception as e:
        logger.error(f"❌ Collection creation failed: {e}")
        return None

    # ── Step 2: Har product add karo aur TASK ID save karo ────
    added_count = 0
    task_url_pairs = []

    for i, prod_url in enumerate(product_urls):
        try:
            logger.info(f"➕ Adding product {i+1}/{len(product_urls)}: {prod_url[:60]}")
            scrape_resp = requests.post(
                "https://api.wishlink.com/api/c/autoScrapeProduct",
                headers=headers,
                json={"url": prod_url, "creator": WISHLINK_CREATOR},
                timeout=20
            )
            scrape_data = scrape_resp.json()

            task_id = scrape_data.get("data", {}).get("task_id")
            if task_id:
                task_url_pairs.append({
                    "task_id": task_id,
                    "url": prod_url
                })
                added_count += 1
                logger.info(f"Product add: 200 | Task ID: {task_id}")

            time.sleep(1.5)
        except Exception as e:
            logger.error(f"❌ Product add failed ({prod_url[:40]}): {e}")
            continue

    logger.info(f"✅ {added_count}/{len(product_urls)} products queued")

    # ── Step 3: Async tasks complete hone ka wait ─────────────
    wait_time = added_count * 3
    logger.info(f"⏳ Waiting {wait_time}s for scraping tasks to complete...")
    time.sleep(wait_time)

    # ── Step 4: Finalize ──────────────────────────────────────
    try:
        logger.info("🔒 Finalizing collection...")
        fin_payload = {
            "collectionId": str(collection_id),
            "postType": "collection",
            "creator": WISHLINK_CREATOR,
            "task_url_pairs": task_url_pairs
        }
        fin_resp = requests.post(
            "https://api.wishlink.com/api/c/finalizeProducts",
            headers=headers,
            json=fin_payload,
            timeout=30
        )
        logger.info(f"✅ Finalize: {fin_resp.status_code} | {fin_resp.text[:100]}")
    except Exception as e:
        logger.error(f"⚠️ Finalize warning (non-fatal): {e}")

    # ── Step 5: Publish Collection ────────────────────────────
    try:
        logger.info("📢 Publishing collection to Live...")
        pub_payload = {
            "is_alive": True,
            "is_hidden": False,
            "collectionId": str(collection_id),
            "type": "collection",
            "action_type": "publish",
            "creator": WISHLINK_CREATOR,
            "cross_post_platforms": None
        }
        pub_resp = requests.post(
            "https://api.wishlink.com/api/c/updatePostOrCollectionStatus",
            headers=headers,
            json=pub_payload,
            timeout=20
        )
        logger.info(f"✅ Publish: {pub_resp.status_code} | {pub_resp.text[:100]}")
    except Exception as e:
        logger.error(f"⚠️ Publish failed: {e}")

    # ── Step 6: Collection link banao ─────────────────────────
    collection_link = f"https://wishlink.com/{WISHLINK_CREATOR_URL}/collection/{collection_id}"
    logger.info(f"✅ Collection ready & LIVE: {collection_link}")
    return collection_link, collection_id, added_count


# ============================================================
# 📸 IG Post Data Fetcher via getInstaPostsList
# ✅ NEW: IG URL se shortcode nikalo, Wishlink API se post data fetch karo
# ============================================================
def get_ig_post_data_from_wishlink(ig_url):
    """
    IG URL se shortcode nikalo, getInstaPostsList call karo,
    permalink match karo aur post ka poora data return karo.
    Posts newest-first aate hain — nayi post page 1 pe hi milegi.
    """
    m = re.search(r'instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)', ig_url)
    if not m:
        logger.error(f"[IG-FETCH] Shortcode nahi nikla: {ig_url}")
        return None

    shortcode = m.group(1)
    logger.info(f"[IG-FETCH] Shortcode: {shortcode}")

    token = get_fresh_wishlink_token()
    if not token:
        logger.error("[IG-FETCH] Token nahi mila")
        return None

    headers = get_creator_headers(token)
    cursor = ""

    for page in range(5):  # max 5 pages try karo
        try:
            resp = requests.get(
                "https://api.wishlink.com/api/c/getInstaPostsList",
                params={
                    "nextPageCursor": cursor,
                    "include_stories": "false",
                    "creator": WISHLINK_CREATOR
                },
                headers=headers,
                timeout=15
            )
            resp.raise_for_status()
            data = resp.json()

            posts = data.get("data", {}).get("posts", [])
            for post in posts:
                permalink = post.get("permalink", "")
                if shortcode in permalink:
                    logger.info(f"[IG-FETCH] ✅ Post mila! id={post.get('id')} type={post.get('media_type')}")
                    return {
                        "ig_media_id":      post.get("id", ""),
                        "ig_media_type":    post.get("media_type", "IMAGE"),
                        "ig_media_url":     post.get("media_url", ""),
                        "ig_thumbnail_url": post.get("thumbnail_url", "") or post.get("media_url", ""),
                        "ig_timestamp":     post.get("timestamp", ""),
                        "ig_children":      post.get("children", {})
                    }

            # next page check
            if data.get("data", {}).get("next_page_exists"):
                cursor = data.get("data", {}).get("next_page_cursor", "")
                logger.info(f"[IG-FETCH] Page {page+1} done, next page try kar raha hoon...")
            else:
                logger.info(f"[IG-FETCH] No more pages after page {page+1}")
                break

        except Exception as e:
            logger.error(f"[IG-FETCH] API error page {page+1}: {e}")
            break

    logger.warning(f"[IG-FETCH] Post nahi mila shortcode={shortcode} — empty data return karunga")
    return None


# ============================================================
# 📸 Wishlink Instagram Post Linker
# ✅ FIXED: ig_media_id (numeric), ig_media_url, ig_thumbnail_url
#    ab properly accept + pass ho rahe hain — thumbnail + Auto-DM fix
# ============================================================
def create_ig_wishlink_post(
    ig_post_url,
    product_urls,
    title=None,
    ig_media_id='',        # ✅ numeric Graph API ID (e.g. 17927333469092003)
    ig_media_type='IMAGE', # ✅ REELS / IMAGE / CAROUSEL_ALBUM
    ig_media_url='',       # ✅ actual CDN media URL
    ig_thumbnail_url='',   # ✅ thumbnail URL
    ig_timestamp='',       # ✅ post timestamp ISO string
    ig_children=None       # ✅ carousel children (optional)
):
    if not ig_post_url:
        logger.error("[IG-WL] ig_post_url required")
        return None

    if product_urls is None:
        product_urls = []
    logger.info(f"[IG-WL] product_urls count: {len(product_urls)} (0 = Auto-DM only mode)")

    if ig_children is None:
        ig_children = {}

    if not title:
        title = f"Budget Look - {time.strftime('%d %b %Y')}"

    token = get_fresh_wishlink_token()
    if not token:
        logger.error("[IG-WL] Auth token unavailable")
        return None

    headers = get_creator_headers(token)

    # ── media_type normalize karo ───────────────────────────
    media_type_map = {
        'video': 'REELS',
        'reel': 'REELS',
        'reels': 'REELS',
        'image': 'IMAGE',
        'carousel': 'CAROUSEL_ALBUM',
        'carousel_album': 'CAROUSEL_ALBUM',
    }
    ig_media_type_normalized = media_type_map.get(
        ig_media_type.lower(), ig_media_type.upper()
    )

    # ── Fallback: URL se media type detect karo ─────────────
    if not ig_media_type or ig_media_type.upper() == 'IMAGE':
        if '/reel/' in ig_post_url:
            ig_media_type_normalized = 'REELS'

    logger.info(
        f"[IG-WL] ig_post_url={ig_post_url} | "
        f"ig_media_id={ig_media_id} | "
        f"ig_media_type={ig_media_type_normalized} | "
        f"ig_media_url={ig_media_url[:60] if ig_media_url else 'EMPTY'}"
    )

    # ── Step 1: createEditShopPost ──────────────────────────
    try:
        logger.info(f"[IG-WL] Step 1: createEditShopPost | url={ig_post_url}")

        step1_payload = {
            "link": ig_post_url,
            "title": title,
            "post_channel": "instagram",
            "creator": WISHLINK_CREATOR,
            "is_placeholder": False,
            "tags": [],
            "post_data": {
                "post_url": ig_post_url,
                "media_type": ig_media_type_normalized,
                "media_url": ig_media_url,
                "thumbnail_url": ig_thumbnail_url or ig_media_url,
                "post_added_on_social_media": ig_timestamp,
                "post_social_media_id": ig_media_id,
                "children": ig_children
            }
        }

        resp = requests.post(
            "https://api.wishlink.com/api/c/createEditShopPost",
            headers=headers,
            json=step1_payload,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()

        post_id = data.get("post")
        if not post_id:
            logger.error(f"[IG-WL] Step 1 failed — post_id nahi mila: {data}")
            return None

        logger.info(f"[IG-WL] Step 1 done! post_id={post_id}")

    except Exception as e:
        logger.error(f"[IG-WL] Step 1 exception: {e}")
        return None

    # ── Step 2: autoScrapeProduct (har product ke liye) ────
    task_url_pairs = []
    added_count = 0

    if product_urls:
        for i, prod_url in enumerate(product_urls):
            try:
                logger.info(f"[IG-WL] Step 2: Scraping product {i+1}/{len(product_urls)}: {prod_url[:60]}")

                scrape_resp = requests.post(
                    "https://api.wishlink.com/api/c/autoScrapeProduct",
                    headers=headers,
                    json={"url": prod_url, "creator": WISHLINK_CREATOR},
                    timeout=20
                )
                scrape_data = scrape_resp.json()

                task_id = scrape_data.get("data", {}).get("task_id")
                if task_id:
                    task_url_pairs.append({"task_id": task_id, "url": prod_url})
                    added_count += 1
                    logger.info(f"[IG-WL] Product {i+1} queued | task_id={task_id}")
                else:
                    logger.warning(f"[IG-WL] Product {i+1} — task_id missing: {scrape_data}")

                time.sleep(1.5)

            except Exception as e:
                logger.error(f"[IG-WL] Product {i+1} scrape failed: {e}")
                continue

        logger.info(f"[IG-WL] Step 2 done: {added_count}/{len(product_urls)} products queued")
    else:
        logger.info("[IG-WL] Step 2 skipped — Auto-DM only mode (no products)")

    # ── Step 3: Wait + finalizeProducts ────────────────────
    if task_url_pairs:
        wait_time = max(added_count * 4, 10)
        logger.info(f"[IG-WL] Step 3: Waiting {wait_time}s for background scraping...")
        time.sleep(wait_time)

        try:
            fin_payload = {
                "postId": str(post_id),
                "postType": "post",
                "creator": WISHLINK_CREATOR,
                "task_url_pairs": task_url_pairs
            }
            fin_resp = requests.post(
                "https://api.wishlink.com/api/c/finalizeProducts",
                headers=headers,
                json=fin_payload,
                timeout=30
            )
            logger.info(f"[IG-WL] Step 3 finalize: {fin_resp.status_code} | {fin_resp.text[:150]}")

        except Exception as e:
            logger.warning(f"[IG-WL] Step 3 warning (non-fatal): {e}")
    else:
        logger.info("[IG-WL] Step 3 skipped — no task_url_pairs to finalize")

    # ── Step 4: updatePostOrCollectionStatus (Publish) ─────
    if product_urls:
        logger.info("[IG-WL] Step 4: Waiting 10s before publishing...")
        time.sleep(10)

        try:
            pub_payload = {
                "is_alive": True,
                "is_hidden": False,
                "postId": str(post_id),
                "type": "post",
                "action_type": "publish",
                "cross_post_platforms": ["facebook"],
                "follow_gate_enabled": False,
                "creator": WISHLINK_CREATOR
            }
            pub_resp = requests.post(
                "https://api.wishlink.com/api/c/updatePostOrCollectionStatus",
                headers=headers,
                json=pub_payload,
                timeout=20
            )
            logger.info(f"[IG-WL] Step 4 publish: {pub_resp.status_code} | {pub_resp.text[:150]}")
            pub_data = pub_resp.json()
            if not pub_data.get("success", False):
                logger.error(f"[IG-WL] Step 4 publish failed: {pub_data}")
                return None
        except Exception as e:
            logger.error(f"[IG-WL] Step 4 publish exception: {e}")
            return None
    else:
        logger.info("[IG-WL] Step 4 skipped (0 products) — Will be published during custom message setup")

    # ── Return result ───────────────────────────────────────
    wishlink_post_url = f"https://wishlink.com/{WISHLINK_CREATOR_URL}/post/{post_id}"
    logger.info(f"[IG-WL] ✅ All done! Wishlink post LIVE: {wishlink_post_url}")
    return wishlink_post_url, post_id


# ============================================================
# 📘 Wishlink Facebook Post Linker
# FB post ke liye Wishlink DM automation activate karo
# Bilkul create_ig_wishlink_post jaisa — sirf FB specific values
# ============================================================
def create_fb_wishlink_post(
    fb_post_url,
    product_urls,
    title=None,
    fb_post_id='',          # Facebook post numeric ID (e.g. 104552146865_1221075383)
    fb_media_type='FB_REEL', # FB_REEL ya FB_POST
    fb_media_url='',         # actual CDN video/image URL
    fb_thumbnail_url='',     # thumbnail URL
    fb_timestamp='',         # ISO timestamp
):
    if not fb_post_url:
        logger.error("[FB-WL] fb_post_url required")
        return None

    if product_urls is None:
        product_urls = []
    logger.info(f"[FB-WL] product_urls count: {len(product_urls)} (0 = Auto-DM only mode)")

    if not title:
        title = f"Budget Look - {time.strftime('%d %b %Y')}"

    token = get_fresh_wishlink_token()
    if not token:
        logger.error("[FB-WL] Auth token unavailable")
        return None

    headers = get_creator_headers(token)

    # ── media_type normalize (FB specific) ──────────────────
    # Wishlink FB ke liye: FB_REEL ya FB_POST use karta hai
    media_type_map = {
        'video':   'FB_REEL',
        'reel':    'FB_REEL',
        'fb_reel': 'FB_REEL',
        'image':   'FB_POST',
        'photo':   'FB_POST',
        'fb_post': 'FB_POST',
    }
    fb_media_type_normalized = media_type_map.get(
        fb_media_type.lower(), fb_media_type.upper()
    )

    # Fallback: URL se detect karo
    if '/reel/' in fb_post_url and fb_media_type_normalized == 'FB_POST':
        fb_media_type_normalized = 'FB_REEL'

    logger.info(
        f"[FB-WL] fb_post_url={fb_post_url} | "
        f"fb_post_id={fb_post_id} | "
        f"fb_media_type={fb_media_type_normalized} | "
        f"fb_media_url={fb_media_url[:60] if fb_media_url else 'EMPTY'}"
    )

    # ── Step 1: createEditShopPost (Facebook channel) ───────
    try:
        logger.info(f"[FB-WL] Step 1: createEditShopPost | url={fb_post_url}")

        step1_payload = {
            "link":         fb_post_url,
            "title":        title,
            "post_channel": "facebook",       # ← yahi Instagram se alag hai
            "creator":      WISHLINK_CREATOR,
            "is_placeholder": False,
            "tags": [],
            "post_data": {
                "post_url":                  fb_post_url,
                "media_type":                fb_media_type_normalized,  # FB_REEL / FB_POST
                "media_url":                 fb_media_url,
                "thumbnail_url":             fb_thumbnail_url or fb_media_url,
                "post_added_on_social_media": fb_timestamp,
                "post_social_media_id":      fb_post_id,
                "children": {}
            }
        }

        resp = requests.post(
            "https://api.wishlink.com/api/c/createEditShopPost",
            headers=headers,
            json=step1_payload,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()

        post_id = data.get("post")
        if not post_id:
            logger.error(f"[FB-WL] Step 1 failed — post_id nahi mila: {data}")
            return None

        logger.info(f"[FB-WL] Step 1 done! post_id={post_id}")

    except Exception as e:
        logger.error(f"[FB-WL] Step 1 exception: {e}")
        return None

    # ── Step 2: autoScrapeProduct (har product ke liye) ─────
    task_url_pairs = []
    added_count = 0

    if product_urls:
        for i, prod_url in enumerate(product_urls):
            try:
                logger.info(f"[FB-WL] Step 2: Scraping product {i+1}/{len(product_urls)}: {prod_url[:60]}")

                scrape_resp = requests.post(
                    "https://api.wishlink.com/api/c/autoScrapeProduct",
                    headers=headers,
                    json={"url": prod_url, "creator": WISHLINK_CREATOR},
                    timeout=20
                )
                scrape_data = scrape_resp.json()

                task_id = scrape_data.get("data", {}).get("task_id")
                if task_id:
                    task_url_pairs.append({"task_id": task_id, "url": prod_url})
                    added_count += 1
                    logger.info(f"[FB-WL] Product {i+1} queued | task_id={task_id}")
                else:
                    logger.warning(f"[FB-WL] Product {i+1} — task_id missing: {scrape_data}")

                time.sleep(1.5)

            except Exception as e:
                logger.error(f"[FB-WL] Product {i+1} scrape failed: {e}")
                continue

        logger.info(f"[FB-WL] Step 2 done: {added_count}/{len(product_urls)} products queued")
    else:
        logger.info("[FB-WL] Step 2 skipped — Auto-DM only mode (no products)")

    # ── Step 3: Wait + finalizeProducts ─────────────────────
    if task_url_pairs:
        wait_time = max(added_count * 4, 10)
        logger.info(f"[FB-WL] Step 3: Waiting {wait_time}s for background scraping...")
        time.sleep(wait_time)

        try:
            fin_payload = {
                "postId":        str(post_id),
                "postType":      "post",
                "creator":       WISHLINK_CREATOR,
                "task_url_pairs": task_url_pairs
            }
            fin_resp = requests.post(
                "https://api.wishlink.com/api/c/finalizeProducts",
                headers=headers,
                json=fin_payload,
                timeout=30
            )
            logger.info(f"[FB-WL] Step 3 finalize: {fin_resp.status_code} | {fin_resp.text[:150]}")

        except Exception as e:
            logger.warning(f"[FB-WL] Step 3 warning (non-fatal): {e}")
    else:
        logger.info("[FB-WL] Step 3 skipped — no task_url_pairs to finalize")

    # ── Step 4: updatePostOrCollectionStatus (Publish) ──────
    if product_urls:
        logger.info("[FB-WL] Step 4: Waiting 10s before publishing...")
        time.sleep(10)

        try:
            pub_payload = {
                "is_alive":            True,
                "is_hidden":           False,
                "postId":              str(post_id),
                "type":                "post",
                "action_type":         "publish",
                "cross_post_platforms": ["facebook"],
                "follow_gate_enabled": False,
                "creator":             WISHLINK_CREATOR
            }
            pub_resp = requests.post(
                "https://api.wishlink.com/api/c/updatePostOrCollectionStatus",
                headers=headers,
                json=pub_payload,
                timeout=20
            )
            logger.info(f"[FB-WL] Step 4 publish: {pub_resp.status_code} | {pub_resp.text[:150]}")
            pub_data = pub_resp.json()
            if not pub_data.get("success", False):
                logger.error(f"[FB-WL] Step 4 publish failed: {pub_data}")
                return None
        except Exception as e:
            logger.error(f"[FB-WL] Step 4 publish exception: {e}")
            return None
    else:
        logger.info("[FB-WL] Step 4 skipped (0 products) — Will be published during custom message setup")

    # ── Return result ────────────────────────────────────────
    wishlink_post_url = f"https://wishlink.com/{WISHLINK_CREATOR_URL}/post/{post_id}"
    logger.info(f"[FB-WL] ✅ All done! Wishlink FB post LIVE: {wishlink_post_url}")
    return wishlink_post_url, post_id


def set_custom_dm_message(post_id, custom_message):
    """
    Sets a custom DM template message for a Wishlink post ID.
    Calls POST https://api.wishlink.com/api/c/addShopProducts
    """
    logger.info(f"[SET-MSG] Setting custom message on Post ID {post_id}...")
    token = get_fresh_wishlink_token()
    if not token:
        logger.error("[SET-MSG] Fresh token generation failed")
        return None

    headers = get_creator_headers(token)
    payload = {
        "postId": str(post_id),
        "productLinks": [],
        "type": "post",
        "customizationType": "TEXT",
        "customEngageValues": [
            {
                "message": custom_message
            }
        ],
        "creator": WISHLINK_CREATOR
    }

    try:
        resp = requests.post(
            "https://api.wishlink.com/api/c/addShopProducts",
            headers=headers,
            json=payload,
            timeout=20
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"[SET-MSG] Custom message API response: {data}")
        if data.get("success", False) or resp.status_code == 200:
            # Now publish / activate the post on Wishlink
            logger.info(f"[SET-MSG] Custom message set successfully. Activating DM automation/publish for Post ID {post_id}...")
            pub_payload = {
                "is_alive": True,
                "is_hidden": False,
                "postId": str(post_id),
                "type": "post",
                "action_type": "publish",
                "cross_post_platforms": ["facebook"],
                "follow_gate_enabled": False,
                "creator": WISHLINK_CREATOR
            }
            try:
                pub_resp = requests.post(
                    "https://api.wishlink.com/api/c/updatePostOrCollectionStatus",
                    headers=headers,
                    json=pub_payload,
                    timeout=20
                )
                pub_data = pub_resp.json()
                logger.info(f"[SET-MSG] Activation/Publish response: {pub_data}")
                if pub_data.get("success", False):
                    logger.info(f"[SET-MSG] Successfully published/activated Post ID {post_id}")
                    return True
                else:
                    logger.error(f"[SET-MSG] Activation/Publish failed: {pub_data}")
                    return None
            except Exception as e:
                logger.error(f"[SET-MSG] Activation/Publish exception: {e}")
                return None
        return None
    except Exception as e:
        logger.error(f"[SET-MSG] Failed to set custom message: {e}")
        return None


# ============================================================
# 📱 Telegram Bot Handlers
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Start command from user: {update.effective_user.id}")
    await update.message.reply_text(
        "👋 Budget Looks Bot mein swagat hai!\n\n"
        "Neeche diye commands use karo:\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 /extraction\n"
        "Wishlink ya Lehlah URL do → original product links milenge\n"
        "(Wishlink.com ✅ | app.lehlah.club ✅)\n\n"
        "📦 /create_collection\n"
        "Wishlink ya Lehlah URL do → affiliate collection link milega\n\n"
        "🔗 /single_affiliate\n"
        "Koi bhi ek product URL do → ek affiliate Wishlink milega\n\n"
        "🗂️ /collection_from_links\n"
        "Apni khud ki 2-20 product links do → affiliate collection ban jayega\n\n"
        "📲 /dm_automation\n"
        "Instagram URL + product links bhejo → Wishlink Auto-DM activate ho jayega\n\n"
        "✅ /done\n"
        "Jab saare links bhej chuke hon processing start karne ke liye isko type karein.\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 Supported URLs:\n"
        "• wishlink.com/username/post/ID\n"
        "• wishlink.com/username/collection/ID\n"
        "• app.lehlah.club/pc/ID  (Lehlah Collection)\n"
        "• app.lehlah.club/post/ID  (Lehlah Post)\n"
        "• faym.co/post/ID  (Faym Post) ✨\n\n"
        "Kaise use karein:\n"
        "1. Pehle command type karo\n"
        "2. Phir apni link(s) bhejo (ek ya alag-alag messages me)\n"
        "3. Aakhir me /done bhejo (kuch commands ke liye) ✅"
    )


async def cmd_extraction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['state'] = 'extraction'
    await update.message.reply_text(
        "🔍 Extraction Mode\n\n"
        "Ab ek Wishlink URL bhejo — main usme se saare product links nikaal dunga.\n\n"
        "Example:\n"
        "https://www.wishlink.com/username/post/123456\n"
        "ya\n"
        "https://www.wishlink.com/share/xxxxx"
    )


async def cmd_create_collection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['state'] = 'create_collection'
    await update.message.reply_text(
        "📦 Collection Creation Mode\n\n"
        "Ek Wishlink post/reel/collection URL bhejo.\n"
        "Main usme se products extract karke affiliate collection bana dunga.\n\n"
        "Example:\n"
        "https://www.wishlink.com/username/collection/123456\n"
        "ya koi bhi post/reel link"
    )


async def cmd_single_affiliate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['state'] = 'single_affiliate'
    await update.message.reply_text(
        "🔗 Single Affiliate Mode\n\n"
        "Koi bhi ek product URL bhejo — main use Wishlink affiliate link mein convert kar dunga.\n\n"
        "Example:\n"
        "https://www.amazon.in/dp/XXXXXX\n"
        "ya koi bhi supported product URL"
    )


async def cmd_collection_from_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['state'] = 'collection_from_links'
    context.user_data['col_product_urls'] = []
    await update.message.reply_text(
        "🗂️ Collection From Links Mode\n\n"
        "Ab apni product links bhejo (ek message me ya alag-alag messages me).\n"
        "Minimum 2, Maximum 20 links allowed hain.\n\n"
        "👉 Jab saari links bhej do, to /done type karna process start karne ke liye!"
    )


async def cmd_dm_automation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['state'] = 'dm_automation'
    context.user_data['dm_ig_url'] = None
    context.user_data['dm_product_urls'] = []
    await update.message.reply_text(
        "📲 Wishlink Auto-DM Activation Mode\n\n"
        "Ab mujhe ye links bhejo:\n"
        "1. Instagram post/reel URL\n"
        "2. Product links (Amazon, Flipkart, etc. max 10)\n\n"
        "Aap ek hi message mein sari links de sakte ho ya alag messages mein bhi bhej sakte ho.\n"
        "👉 Jab sab kuch bhej do, tab /done type karna!"
    )

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state')
    
    if state == 'dm_automation':
        ig_url = context.user_data.get('dm_ig_url')
        product_urls = context.user_data.get('dm_product_urls', [])
        await _execute_dm_automation(update, context, ig_url, product_urls)
    elif state == 'collection_from_links':
        product_urls = context.user_data.get('col_product_urls', [])
        await _execute_collection_from_links(update, context, product_urls)
    else:
        await update.message.reply_text("❌ Koi active process nahi hai jisko /done kiya ja sake. Pehle koi command /dm_automation ya /collection_from_links use karein.")



async def _handle_extraction(update, context, urls):
    if not urls:
        await update.message.reply_text("❌ Koi valid URL nahi mila. Dobara bhejo.")
        return

    context.user_data['state'] = None
    url = urls[0]

    # ── Faym URL detect karo ─────────────────────────────────
    if "faym.co" in url:
        content_id = get_faym_content_id(url)
        await update.message.reply_text(
            f"🛍️ Faym Post link detect hua! (ID: {content_id})\n"
            "⏳ Original product links extract kar raha hoon..."
        )
        loop = asyncio.get_running_loop()
        all_links = await loop.run_in_executor(None, get_faym_products, url)
    # ── Lehlah URL detect karo ───────────────────────────────
    elif "app.lehlah.club" in url:
        link_type, link_id = parse_lehlah_url(url)
        type_str = "Collection" if link_type == "collection" else "Post"
        await update.message.reply_text(
            f"🛍️ Lehlah {type_str} link detect hua! (ID: {link_id})\n"
            "⏳ Original product links extract kar raha hoon..."
        )
        loop = asyncio.get_running_loop()
        all_links = await loop.run_in_executor(None, get_product_links_from_lehlah_url, url)
    else:
        await update.message.reply_text("🔍 Extracting product links... ⏳")
        all_links = []

        if '/share/' in url:
            redirected = get_final_url_from_redirect(url)
            if redirected:
                if 'wishlink.com' in redirected:
                    all_links = get_product_links_from_wishlink_url(redirected)
                else:
                    all_links = [redirected]
        elif 'wishlink.com' in url:
            all_links = get_product_links_from_wishlink_url(url)

    if not all_links:
        await update.message.reply_text(
            "❌ Koi product links nahi mile.\n"
            "Sahi Wishlink URL check karo aur dobara try karo.\n\n"
            "Phir se try karne ke liye /extraction bhejo."
        )
        return

    chunk = f"✅ {len(all_links)} Products Mile!\n\n"
    for i, link in enumerate(all_links, 1):
        line = f"{i}. {link}\n\n"
        if len(chunk) + len(line) > 3800:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += line

    if chunk:
        await update.message.reply_text(chunk)

    await update.message.reply_text(
        f"🎯 Total: {len(all_links)} links extracted!\n\n"
        "Agle kaam ke liye:\n/extraction | /create_collection | /single_affiliate"
    )


async def _handle_create_collection(update, context, urls):
    if not urls:
        await update.message.reply_text("❌ Koi valid URL nahi mila. Dobara bhejo.")
        return

    context.user_data['state'] = None
    url = urls[0]

    # ── Faym / Lehlah / Wishlink detect ─────────────────────
    if "faym.co" in url:
        content_id = get_faym_content_id(url)
        await update.message.reply_text(
            f"🛍️ Faym Post (ID: {content_id}) se products extract kar raha hoon...\n"
            "⏳ Phir Wishlink collection banaunga — 2-5 min lagenge!"
        )
    elif "app.lehlah.club" in url:
        link_type, link_id = parse_lehlah_url(url)
        type_str = "Collection" if link_type == "collection" else "Post"
        await update.message.reply_text(
            f"🛍️ Lehlah {type_str} (ID: {link_id}) se products extract kar raha hoon...\n"
            "⏳ Phir Wishlink collection banaunga — 2-5 min lagenge!"
        )
    else:
        await update.message.reply_text(
            "📦 Collection bana raha hoon...\n"
            "⏳ Thoda time lagega (2-5 min) — please wait karo!"
        )

    if '/share/' in url:
        url = get_final_url_from_redirect(url) or url

    # run_in_executor: blocking I/O call ko event loop block nahi karne denge
    loop = asyncio.get_running_loop()
    product_urls = await loop.run_in_executor(
        None, get_product_links_from_wishlink_url, url
    )

    if not product_urls:
        await update.message.reply_text(
            "❌ Products nahi mile is URL se.\n"
            "Phir se try karne ke liye /create_collection bhejo."
        )
        return

    await update.message.reply_text(f"✅ {len(product_urls)} products mile! Collection create ho raha hai...")

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, create_wishlink_collection, product_urls, None)

    if not result:
        await update.message.reply_text(
            "❌ Collection create nahi ho saka. Logs check karo.\n"
            "Phir se try karne ke liye /create_collection bhejo."
        )
        return

    collection_link, collection_id, added_count = result

    await update.message.reply_text(
        f"🎉 Collection Ready!\n\n"
        f"🔗 {collection_link}\n\n"
        f"📦 Products added: {added_count}/{len(product_urls)}\n\n"
        "Agle kaam ke liye:\n/extraction | /create_collection | /single_affiliate"
    )


async def _handle_single_affiliate(update, context, urls):
    if not urls:
        await update.message.reply_text("❌ Koi valid URL nahi mila. Dobara bhejo.")
        return

    context.user_data['state'] = None
    url = urls[0]

    await update.message.reply_text("🔗 Affiliate link bana raha hoon... ⏳")

    if 'wishlink.com' in url and '/share/' in url:
        url = get_final_url_from_redirect(url) or url

    if 'wishlink.com' in url and any(t in url for t in ['/post/', '/reels/', '/collection/']):
        product_urls = get_product_links_from_wishlink_url(url)
        if not product_urls:
            await update.message.reply_text(
                "❌ Product URL extract nahi hua.\n"
                "Seedha product link bhejo (Amazon, Myntra, etc.)"
            )
            return
        url = product_urls[0]

    loop = asyncio.get_running_loop()
    affiliate_link = await loop.run_in_executor(None, convert_to_affiliate_link, url)

    if affiliate_link and affiliate_link != url:
        await update.message.reply_text(
            f"✅ Affiliate Link Ready!\n\n"
            f"🔗 {affiliate_link}\n\n"
            "Agle kaam ke liye:\n/extraction | /create_collection | /single_affiliate | /collection_from_links"
        )
    else:
        await update.message.reply_text(
            f"⚠️ Affiliate conversion nahi hua — raw URL:\n{url}\n\n"
            "Token ya API issue ho sakta hai. Logs check karo."
        )


async def _handle_collection_from_links(update, context, text):
    lines = text.strip().splitlines()
    found_urls = []
    for line in lines:
        line = line.strip()
        if re.match(r'https?://', line):
            found_urls.append(line)

    if found_urls:
        context.user_data.setdefault('col_product_urls', []).extend(found_urls)

    total_links = len(context.user_data.get('col_product_urls', []))
    await update.message.reply_text(
        f"✅ Saved! Naye links: {len(found_urls)} | Total abhi tak: {total_links}\n"
        "Agar aur links hain toh bhejo, warna /done type karo."
    )

async def _execute_collection_from_links(update, context, product_urls):
    if len(product_urls) < 2:
        await update.message.reply_text(
            "❌ Kam se kam 2 valid product URLs chahiye.\n"
            "Process cancel ho gaya. Dobara try karne ke liye /collection_from_links bhejo."
        )
        context.user_data['state'] = None
        return

    if len(product_urls) > 20:
        await update.message.reply_text(
            f"⚠️ {len(product_urls)} links jama hui hain — sirf pehli 20 use karunga."
        )
        product_urls = product_urls[:20]

    context.user_data['state'] = None

    await update.message.reply_text(
        f"✅ {len(product_urls)} links process ho rahi hain!\n"
        "📦 Collection bana raha hoon... 3-7 min lagenge, wait karo ⏳"
    )

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, create_wishlink_collection, product_urls, None)

    if not result:
        await update.message.reply_text(
            "❌ Collection create nahi ho saka. Logs check karo.\n"
            "Phir se try karne ke liye /collection_from_links bhejo."
        )
        return

    collection_link, collection_id, added_count = result

    await update.message.reply_text(
        f"🎉 Collection Ready!\n\n"
        f"🔗 {collection_link}\n\n"
        f"📦 Products added: {added_count}/{len(product_urls)}\n\n"
        "Agle kaam ke liye:\n/extraction | /create_collection | /single_affiliate | /collection_from_links"
    )


# ============================================================
# 📲 DM Automation Handler
# ✅ UPDATED: getInstaPostsList se ig_media_id + thumbnail fetch hota hai
# ============================================================
async def _handle_dm_automation(update, context, text):
    lines = text.strip().splitlines()
    ig_url = None
    product_urls = []

    for line in lines:
        line = line.strip()
        url_match = re.search(r'https?://\S+', line)
        if not url_match:
            continue
        url = url_match.group(0).rstrip(')')
        clean = url.split('?')[0].rstrip('/')
        if 'instagram.com' in url:
            if ig_url is None:
                ig_url = clean + '/'
        else:
            product_urls.append(url)

    if ig_url:
        context.user_data['dm_ig_url'] = ig_url
    if product_urls:
        context.user_data.setdefault('dm_product_urls', []).extend(product_urls)

    curr_ig = context.user_data.get('dm_ig_url')
    curr_prods = context.user_data.get('dm_product_urls', [])

    await update.message.reply_text(
        f"✅ Saved! Abhi tak mile hain:\n"
        f"📸 IG Post: {'Mila' if curr_ig else 'Nahi Mila'}\n"
        f"📦 Products: {len(curr_prods)}\n\n"
        "Agar aur links hain toh bhejte raho, warna processing start karne ke liye /done type karo."
    )

async def _execute_dm_automation(update, context, ig_url, product_urls):
    if not ig_url:
        await update.message.reply_text(
            "❌ Instagram URL miss ho gaya.\n\n"
            "Process cancel. Dobara try karne ke liye /dm_automation bhejo."
        )
        context.user_data['state'] = None
        return

    if not product_urls or len(product_urls) == 0:
        await update.message.reply_text(
            "❌ Koi product URL nahi mila.\n\n"
            "Process cancel. Dobara try karne ke liye /dm_automation bhejo."
        )
        context.user_data['state'] = None
        return

    if len(product_urls) > 10:
        await update.message.reply_text(
            f"⚠️ {len(product_urls)} product links mile — sirf pehli 10 use karunga (Wishlink limit)."
        )
        product_urls = product_urls[:10]

    context.user_data['state'] = None

    await update.message.reply_text(
        f"✅ Sab kuch mil gaya!\n"
        f"📸 IG Post: {ig_url}\n"
        f"📦 Products: {len(product_urls)}\n\n"
        f"🔍 Post data fetch kar raha hoon Wishlink se...\n"
        f"⏳ 2-3 min lagenge — please wait karo!"
    )

    # ✅ IG post data fetch karo — media_id, thumbnail, type sab milega
    loop = asyncio.get_running_loop()
    ig_data = await loop.run_in_executor(None, get_ig_post_data_from_wishlink, ig_url)

    if ig_data:
        logger.info(f"[DM-BOT] IG data mila: {ig_data['ig_media_id']} | {ig_data['ig_media_type']}")
        await update.message.reply_text(
            f"✅ Post data fetch ho gaya!\n"
            f"🆔 Media ID: {ig_data['ig_media_id']}\n"
            f"📁 Type: {ig_data['ig_media_type']}\n\n"
            f"📲 Wishlink Auto-DM setup ho raha hai..."
        )
    else:
        logger.warning("[DM-BOT] IG data nahi mila — empty values se try karunga")
        await update.message.reply_text(
            "⚠️ Post data fetch nahi hua — thumbnail placeholder aa sakti hai.\n"
            "Auto-DM activate karne ki koshish kar raha hoon..."
        )
        ig_data = {
            "ig_media_id": "", "ig_media_type": "IMAGE",
            "ig_media_url": "", "ig_thumbnail_url": "",
            "ig_timestamp": "", "ig_children": {}
        }

    logger.info(f"[DM-BOT] Starting | ig={ig_url} | products={len(product_urls)}")

    result = await loop.run_in_executor(
        None,
        create_ig_wishlink_post,
        ig_url,
        product_urls,
        None,
        ig_data["ig_media_id"],
        ig_data["ig_media_type"],
        ig_data["ig_media_url"],
        ig_data["ig_thumbnail_url"],
        ig_data["ig_timestamp"],
        ig_data["ig_children"]
    )

    if not result:
        await update.message.reply_text(
            "❌ Wishlink Auto-DM setup fail ho gaya!\n\n"
            "Possible reasons:\n"
            "• Instagram URL sahi nahi\n"
            "• Wishlink API error\n"
            "• Token expire ho gaya\n\n"
            "Dobara try karo ya Render logs check karo."
        )
        return

    wishlink_post_url, post_id = result

    await update.message.reply_text(
        f"🎉 Wishlink Auto-DM LIVE!\n\n"
        f"📸 Instagram Post:\n{ig_url}\n\n"
        f"🛍️ Wishlink Post:\n{wishlink_post_url}\n\n"
        f"📦 Products Tagged: {len(product_urls)}\n\n"
        "Ab jab bhi koi comment karega\n"
        "→ Auto-DM mein product links jayengi! 🚀\n\n"
        "Agle kaam ke liye:\n/dm_automation | /collection_from_links"
    )


async def send_links_in_parts(update, all_links, title):
    max_links_per_message = 8
    if len(all_links) <= max_links_per_message:
        output = f"🎉 {title}\n\n"
        for i, link in enumerate(all_links, 1):
            discount = random.randint(50, 85)
            output += f"{i}. ({discount}% OFF)\n{link}\n\n"
        await update.message.reply_text(output)
    else:
        total_parts = (len(all_links) + max_links_per_message - 1) // max_links_per_message
        for part in range(total_parts):
            start_idx = part * max_links_per_message
            end_idx = min(start_idx + max_links_per_message, len(all_links))
            part_links = all_links[start_idx:end_idx]
            output = f"🎉 {title} (Part {part + 1}/{total_parts})\n\n"
            for i, link in enumerate(part_links, start_idx + 1):
                discount = random.randint(50, 85)
                output += f"{i}. ({discount}% OFF)\n{link}\n\n"
            await update.message.reply_text(output)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Message received from user: {update.effective_user.id}")
    text = update.message.text or update.message.caption
    if not text:
        return

    logger.info(f"Processing text: {text}")

    urls = []
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "url":
                url = text[entity.offset:entity.offset + entity.length]
                urls.append(url)
    if not urls:
        urls = re.findall(r'(https?://\S+)', text)

    state = context.user_data.get('state', None)

    if state == 'extraction':
        await _handle_extraction(update, context, urls)
        return
    elif state == 'create_collection':
        await _handle_create_collection(update, context, urls)
        return
    elif state == 'single_affiliate':
        await _handle_single_affiliate(update, context, urls)
        return
    elif state == 'collection_from_links':
        await _handle_collection_from_links(update, context, text)
        return
    elif state == 'dm_automation':
        await _handle_dm_automation(update, context, text)
        return

    if not urls:
        await update.message.reply_text(
            "👋 Koi URL nahi mila!\n\n"
            "Kya karna chahte ho? Ek command choose karo:\n\n"
            "🔍 /extraction — Wishlink se product links nikalo\n"
            "📦 /create_collection — Affiliate collection banao\n"
            "🔗 /single_affiliate — Ek product link convert karo\n"
            "🗂️ /collection_from_links — Apni links se collection banao\n"
            "📲 /dm_automation — Instagram Auto-DM activate karo"
        )
        return

    ig_urls_found      = [u for u in urls if 'instagram.com' in u]
    # Wishlink/Lehlah/Faym URLs ko product URL na samjha jaye — alag se handle hote hain
    product_urls_found = [
        u for u in urls
        if 'instagram.com' not in u
        and 'wishlink.com' not in u
        and 'app.lehlah.club' not in u
        and 'faym.co' not in u
    ]

    if ig_urls_found and product_urls_found:
        logger.info(f"[AUTO-DM] Smart detect: IG URL + products in one message")
        # Direct execution for smart detect without requiring /done
        ig_url = ig_urls_found[0]
        await _execute_dm_automation(update, context, ig_url, product_urls_found)
        return

    if ig_urls_found and not product_urls_found:
        await update.message.reply_text(
            "📸 Instagram URL mila!\n\n"
            "Lekin product links nahi mili.\n\n"
            "Wishlink Auto-DM ke liye ek hi message mein bhejo:\n"
            "Line 1: Instagram URL\n"
            "Line 2+: Product links (Amazon, Flipkart, etc.)\n\n"
            "Ya /dm_automation type karo."
        )
        return

    await update.message.reply_text("Processing your link… 🔄")
    all_links = []
    for url in urls:
        if "/share/" in url:
            redirected = get_final_url_from_redirect(url)
            if redirected:
                all_links.append(redirected)
        elif "faym.co" in url:
            product_links = get_faym_products(url)
            all_links.extend(product_links)
        elif "app.lehlah.club" in url:
            product_links = get_product_links_from_lehlah_url(url)
            all_links.extend(product_links)
        elif "wishlink.com" in url:
            product_links = get_product_links_from_wishlink_url(url)
            all_links.extend(product_links)

    if not all_links:
        await update.message.reply_text(
            "🤔 Koi product link nahi mila!\n\n"
            "Supported URLs:\n"
            "• wishlink.com/username/post/123456\n"
            "• app.lehlah.club/pc/ID  (Collection)\n"
            "• app.lehlah.club/post/ID  (Post)\n\n"
            "Ya koi command choose karo:\n"
            "🔍 /extraction | 📦 /create_collection\n"
            "🔗 /single_affiliate | 🗂️ /collection_from_links\n"
            "📲 /dm_automation"
        )
        return

    title = random.choice(TITLES)
    try:
        await send_links_in_parts(update, all_links, title)
        await update.message.reply_text(
            "💡 Tip: Seedha command use karo next time:\n"
            "/extraction | /create_collection | /dm_automation"
        )
    except Exception as e:
        logger.error(f"Failed to send response: {e}")
        await update.message.reply_text(f"✅ Found {len(all_links)} product links!")


def process_update_in_thread(update_dict):
    global telegram_app, event_loop
    if telegram_app and event_loop:
        try:
            update = Update.de_json(update_dict, telegram_app.bot)
            asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), event_loop)
        except Exception as e:
            logger.error(f"Error while queuing update for processing: {e}")


# ============================================================
# 🌐 Flask App
# ============================================================
app = Flask(__name__)

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not API_SECRET_KEY or key != API_SECRET_KEY:
            logger.warning("⚠️ Blocked unauthorized API access attempt")
            return jsonify({"error": "Unauthorized. Invalid or missing API_SECRET_KEY header."}), 401
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def home():
    return "🤖 Wishlink Bot is running!"

@app.route('/health')
def health():
    return "OK"

@app.route('/status')
def status():
    return "Active"


@app.route('/get-product-links', methods=['POST'])
@require_api_key
def get_product_links_api():
    try:
        data = request.get_json()
        wishlink_url = data.get('wishlink_url', '')

        if not wishlink_url:
            return jsonify({"error": "wishlink_url required"}), 400

        logger.info(f"API request for: {wishlink_url}")

        if '/share/' in wishlink_url:
            final_url = get_final_url_from_redirect(wishlink_url)
            if not final_url:
                return jsonify({"error": "Redirect failed"}), 500

            if 'wishlink.com' not in final_url:
                affiliate_link = convert_to_affiliate_link(final_url)
                return jsonify({
                    "success": True,
                    "post_id": None,
                    "post_type": "DIRECT",
                    "product_links": [final_url],
                    "first_product": final_url,
                    "affiliate_link": affiliate_link,
                    "total": 1
                })

            match = re.search(r'/(?:post|reels)/(\d+)', final_url)
            if not match:
                return jsonify({"error": f"Post ID nahi mila: {final_url}"}), 500
            post_id   = match.group(1)
            post_type = 'REELS' if '/reels/' in final_url else 'POST'
            
            product_links = get_product_links_from_wishlink_url(final_url)

        else:
            match = re.search(r'/(?:post|reels)/(\d+)', wishlink_url)
            if not match:
                return jsonify({"error": "URL format galat"}), 400
            post_id   = match.group(1)
            post_type = 'REELS' if '/reels/' in wishlink_url else 'POST'

            product_links = get_product_links_from_wishlink_url(wishlink_url)

        if not product_links:
            return jsonify({"success": False, "error": "Koi product nahi mila"}), 404

        first_product  = product_links[0]
        affiliate_link = convert_to_affiliate_link(first_product)

        return jsonify({
            "success": True,
            "post_id": post_id,
            "post_type": post_type,
            "product_links": product_links,
            "first_product": first_product,
            "affiliate_link": affiliate_link,
            "total": len(product_links)
        })

    except Exception as e:
        logger.error(f"API error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/create-collection', methods=['POST'])
@require_api_key
def create_collection_api():
    try:
        data = request.get_json()

        product_urls            = data.get('product_urls', [])
        wishlink_collection_url = data.get('wishlink_collection_url', '')
        wishlink_post_url       = data.get('wishlink_post_url', '')
        collection_name         = data.get('collection_name', '')

        if not product_urls and wishlink_collection_url:
            product_urls = get_product_links_from_wishlink_url(wishlink_collection_url)

        if not product_urls and wishlink_post_url:
            if '/share/' in wishlink_post_url:
                wishlink_post_url = get_final_url_from_redirect(wishlink_post_url) or wishlink_post_url
            product_urls = get_product_links_from_wishlink_url(wishlink_post_url)

        if not product_urls:
            return jsonify({
                "success": False,
                "error": "Koi product URL nahi mila."
            }), 400

        result = create_wishlink_collection(product_urls, collection_name)

        if not result:
            return jsonify({"success": False, "error": "Collection creation failed"}), 500

        collection_link, collection_id, added_count = result

        return jsonify({
            "success": True,
            "collection_link": collection_link,
            "collection_id": collection_id,
            "products_added": added_count,
            "total_input": len(product_urls)
        })

    except Exception as e:
        logger.error(f"create_collection API error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/create-collection-with-singles', methods=['POST'])
@require_api_key
def create_collection_with_singles_api():
    try:
        data = request.get_json()

        # ── Wishlink / Lehlah / Faym URL — sab accept karo ──
        wishlink_url    = data.get('wishlink_url', '') or data.get('lehlah_url', '') or data.get('faym_url', '')
        collection_name = data.get('collection_name', '')

        if not wishlink_url:
            return jsonify({"success": False, "error": "wishlink_url or lehlah_url or faym_url required"}), 400

        if '/share/' in wishlink_url:
            wishlink_url = get_final_url_from_redirect(wishlink_url) or wishlink_url

        logger.info(f"📦 Extracting products from: {wishlink_url}")
        product_urls = get_product_links_from_wishlink_url(wishlink_url)

        if not product_urls:
            return jsonify({"success": False, "error": "Koi product nahi mila is URL se"}), 404

        logger.info(f"✅ {len(product_urls)} products extracted")

        result = create_wishlink_collection(product_urls, collection_name)

        collection_link = ""
        collection_id = ""
        added_count = 0

        if result:
            collection_link, collection_id, added_count = result
            logger.info(f"✅ Collection ready: {collection_link}")
        else:
            logger.warning("⚠️ Collection creation failed, sirf singles return karunga")

        # ⚡ Individual affiliate link creation DISABLED — saves 5-7 min
        # Collection link hi sufficient hai for all use cases
        logger.info("✅ Skipping individual affiliate conversion — returning collection link only")

        return jsonify({
            "success": True,
            "collection_link": collection_link,
            "collection_id": str(collection_id),
            "products_added": added_count,
            "total_products": len(product_urls),
            "product_urls": product_urls,
            "individual_affiliate_links": []
        })

    except Exception as e:
        logger.error(f"create_collection_with_singles API error: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# 🛍️ ENDPOINT — Extract Lehlah Products
# n8n ya koi bhi caller Lehlah URL bheje → original product URLs milenge
# Supports: app.lehlah.club/pc/{id} (Collection) + /post/{id} (Post)
# ============================================================
@app.route('/extract-lehlah', methods=['POST'])
@require_api_key
def extract_lehlah_api():
    try:
        data = request.get_json()
        lehlah_url = data.get('lehlah_url', '').strip()

        if not lehlah_url:
            return jsonify({"success": False, "error": "lehlah_url required"}), 400

        link_type, link_id = parse_lehlah_url(lehlah_url)
        if not link_type:
            return jsonify({
                "success": False,
                "error": "Invalid Lehlah URL. Use app.lehlah.club/pc/{id} or /post/{id}"
            }), 400

        logger.info(f"[Lehlah API] Extracting {link_type} ID={link_id} from {lehlah_url}")
        product_urls = get_product_links_from_lehlah_url(lehlah_url)

        if not product_urls:
            return jsonify({"success": False, "error": "Koi product nahi mila"}), 404

        logger.info(f"[Lehlah API] ✅ {len(product_urls)} products extracted")
        return jsonify({
            "success": True,
            "link_type": link_type,
            "link_id": link_id,
            "product_urls": product_urls,
            "total": len(product_urls)
        })

    except Exception as e:
        logger.error(f"[Lehlah API] Error: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# 🛍️ ENDPOINT — Extract Faym Products
# n8n ya koi bhi caller Faym URL bheje → clean product URLs milenge
# Supports: faym.co/post/{uuid}
# ============================================================
@app.route('/extract-faym', methods=['POST'])
@require_api_key
def extract_faym_api():
    try:
        data      = request.get_json()
        faym_url  = data.get('faym_url', '').strip()

        if not faym_url:
            return jsonify({"success": False, "error": "faym_url required"}), 400

        content_id = get_faym_content_id(faym_url)
        if not content_id:
            return jsonify({
                "success": False,
                "error": "Invalid Faym URL. Use faym.co/post/{id}"
            }), 400

        logger.info(f"[Faym API] Extracting content_id={content_id} from {faym_url}")
        product_urls = get_faym_products(faym_url)

        if not product_urls:
            return jsonify({"success": False, "error": "Koi product nahi mila"}), 404

        logger.info(f"[Faym API] ✅ {len(product_urls)} products extracted")
        return jsonify({
            "success":      True,
            "content_id":   content_id,
            "product_urls": product_urls,
            "total":        len(product_urls)
        })

    except Exception as e:
        logger.error(f"[Faym API] Error: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# ✅ ENDPOINT 4 — Create Wishlink Instagram Post
# ✅ FIXED: ig_media_id, ig_media_url, ig_thumbnail_url properly handled
# ============================================================
@app.route('/create-ig-wishlink-post', methods=['POST'])
@require_api_key
def create_ig_wishlink_post_api():
    try:
        data = request.get_json()

        ig_post_url      = data.get('ig_post_url', '').strip()
        product_urls     = data.get('product_urls', [])
        title            = data.get('title', '')

        ig_media_id      = data.get('ig_media_id', '')
        ig_media_type    = data.get('ig_media_type', 'IMAGE')
        ig_media_url     = data.get('ig_media_url', '')
        ig_thumbnail_url = data.get('ig_thumbnail_url', '')
        ig_timestamp     = data.get('ig_timestamp', '')
        ig_children      = data.get('ig_children', {})

        if not ig_post_url:
            return jsonify({"success": False, "error": "ig_post_url required"}), 400

        if not isinstance(product_urls, list):
            product_urls = []

        product_urls = product_urls[:10]

        logger.info(
            f"[IG-WL] /create-ig-wishlink-post called | "
            f"url={ig_post_url} | products={len(product_urls)} | "
            f"ig_media_id={ig_media_id} | ig_media_type={ig_media_type}"
        )

        result = create_ig_wishlink_post(
            ig_post_url,
            product_urls,
            title or None,
            ig_media_id,
            ig_media_type,
            ig_media_url,
            ig_thumbnail_url,
            ig_timestamp,
            ig_children
        )

        if not result:
            logger.error("[IG-WL] create_ig_wishlink_post returned None")
            return jsonify({"success": False, "error": "Wishlink post creation failed — check server logs"}), 500

        wishlink_post_url, post_id = result
        logger.info(f"[IG-WL] Done! post_id={post_id} | wishlink_url={wishlink_post_url}")

        return jsonify({
            "success": True,
            "wishlink_post_url": wishlink_post_url,
            "post_id": str(post_id),
            "ig_post_url": ig_post_url,
            "products_count": len(product_urls)
        })

    except Exception as e:
        logger.error(f"[IG-WL] /create-ig-wishlink-post API error: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# ✅ ENDPOINT 5 — Create Wishlink Facebook Post
# n8n FB node se fb_post_url, fb_post_id, product_urls aate hain
# Wishlink FB DM automation activate karta hai
# ============================================================
@app.route('/create-fb-wishlink-post', methods=['POST'])
@require_api_key
def create_fb_wishlink_post_api():
    try:
        data = request.get_json()

        fb_post_url      = data.get('fb_post_url', '').strip()
        product_urls     = data.get('product_urls', [])
        title            = data.get('title', '')

        fb_post_id       = data.get('fb_post_id', '')
        fb_media_type    = data.get('fb_media_type', 'FB_REEL')
        fb_media_url     = data.get('fb_media_url', '')
        fb_thumbnail_url = data.get('fb_thumbnail_url', '')
        fb_timestamp     = data.get('fb_timestamp', '')

        if not fb_post_url:
            return jsonify({"success": False, "error": "fb_post_url required"}), 400

        if not isinstance(product_urls, list):
            product_urls = []

        product_urls = product_urls[:10]

        logger.info(
            f"[FB-WL] /create-fb-wishlink-post called | "
            f"url={fb_post_url} | products={len(product_urls)} | "
            f"fb_post_id={fb_post_id} | fb_media_type={fb_media_type}"
        )

        result = create_fb_wishlink_post(
            fb_post_url,
            product_urls,
            title or None,
            fb_post_id,
            fb_media_type,
            fb_media_url,
            fb_thumbnail_url,
            fb_timestamp
        )

        if not result:
            logger.error("[FB-WL] create_fb_wishlink_post returned None")
            return jsonify({"success": False, "error": "Wishlink FB post creation failed — check server logs"}), 500

        wishlink_post_url, post_id = result
        logger.info(f"[FB-WL] Done! post_id={post_id} | wishlink_url={wishlink_post_url}")

        return jsonify({
            "success":          True,
            "wishlink_post_url": wishlink_post_url,
            "post_id":          str(post_id),
            "fb_post_url":      fb_post_url,
            "products_count":   len(product_urls)
        })

    except Exception as e:
        logger.error(f"[FB-WL] /create-fb-wishlink-post API error: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# ✅ ENDPOINT 6 — Set Custom DM Message for Wishlink Post
# ============================================================
@app.route('/set-custom-dm-message', methods=['POST'])
@require_api_key
def set_custom_dm_message_api():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "Request body must be JSON"}), 400

        post_id = data.get('post_id', '')
        custom_message = data.get('custom_message', '')

        if not post_id or not custom_message:
            return jsonify({"success": False, "error": "Both 'post_id' and 'custom_message' are required"}), 400

        post_id = str(post_id).strip()
        custom_message = str(custom_message).strip()

        logger.info(f"[SET-MSG] Route called for post_id={post_id}")
        result = set_custom_dm_message(post_id, custom_message)

        if result:
            logger.info(f"[SET-MSG] Successfully set custom message for post_id={post_id}")
            return jsonify({
                "success": True,
                "message": "Custom DM message activated successfully!",
                "post_id": post_id
            })
        else:
            logger.error(f"[SET-MSG] Failed to set custom message for post_id={post_id}")
            return jsonify({"success": False, "error": "Wishlink API returned failure status"}), 500

    except Exception as e:
        logger.error(f"[SET-MSG] /set-custom-dm-message API error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route(f'/{WEBHOOK_SECRET}', methods=['POST'])
def webhook():
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token') != WEBHOOK_SECRET:
        logger.warning("⚠️ Invalid Webhook Request Received (Wrong Secret Token)")
        return jsonify({"error": "unauthorized"}), 401

    try:
        update_dict = request.get_json()
        if update_dict:
            thread = threading.Thread(target=process_update_in_thread, args=(update_dict,))
            thread.start()
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500


def run_event_loop_in_background(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

def main():
    global telegram_app, event_loop
    logger.info("Starting bot...")
    event_loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(
        target=run_event_loop_in_background,
        args=(event_loop,),
        daemon=True
    )
    loop_thread.start()

    telegram_app = ApplicationBuilder().token(TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("extraction", cmd_extraction))
    telegram_app.add_handler(CommandHandler("create_collection", cmd_create_collection))
    telegram_app.add_handler(CommandHandler("single_affiliate", cmd_single_affiliate))
    telegram_app.add_handler(CommandHandler("collection_from_links", cmd_collection_from_links))
    telegram_app.add_handler(CommandHandler("dm_automation", cmd_dm_automation))
    telegram_app.add_handler(CommandHandler("done", cmd_done))
    telegram_app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_link))

    async def setup_webhook():
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.bot.set_webhook(
            url=f"{WEBHOOK_URL}/{WEBHOOK_SECRET}",
            secret_token=WEBHOOK_SECRET
        )

    future = asyncio.run_coroutine_threadsafe(setup_webhook(), event_loop)
    future.result()
    logger.info("Webhook set successfully!")
    port = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

if __name__ == "__main__":
    main()
