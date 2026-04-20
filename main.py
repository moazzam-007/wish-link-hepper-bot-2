import os
import random
import re
import time
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import logging
from flask import Flask, request, jsonify
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
WISHLINK_ID            = os.getenv("WISHLINK_ID", "1752163729058-1dccdb9e-a0f9-f088-a678-e14f8997f719")
WISHLINK_CREATOR       = os.getenv("WISHLINK_CREATOR", "budget.looks")
FIREBASE_API_KEY       = os.getenv("FIREBASE_API_KEY")
WISHLINK_REFRESH_TOKEN = os.getenv("WISHLINK_REFRESH_TOKEN")
WISHLINK_BZ_AUTH_KEY   = os.getenv("WISHLINK_BZ_AUTH_KEY")

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

def get_fresh_wishlink_token():
    global _token_cache
    current_time = time.time()

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

        _token_cache["id_token"]   = new_token
        _token_cache["expires_at"] = current_time + expires_in

        logger.info(f"✅ Token refresh successful! {expires_in}s valid")
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

def get_product_links_from_wishlink_url(wishlink_url):
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

    if not product_urls:
        logger.error("[IG-WL] product_urls list required")
        return None

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
    # n8n se 'video' ya 'image' aa sakta hai — Wishlink ko uppercase chahiye
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
                "media_type": ig_media_type_normalized,      # ✅ REELS / IMAGE / CAROUSEL_ALBUM
                "media_url": ig_media_url,                   # ✅ actual CDN URL
                "thumbnail_url": ig_thumbnail_url or ig_media_url,  # ✅ fallback to media_url
                "post_added_on_social_media": ig_timestamp,  # ✅ ISO timestamp
                "post_social_media_id": ig_media_id,         # ✅ numeric ID — MOST IMPORTANT
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

    # ── Step 3: Wait + finalizeProducts ────────────────────
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

    # ── Step 4: updatePostOrCollectionStatus (Publish) ─────
    logger.info("[IG-WL] Step 4: Waiting 10s before publishing...")
    time.sleep(10)

    try:
        pub_payload = {
            "is_alive": True,
            "is_hidden": False,
            "postId": str(post_id),
            "type": "post",
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
        logger.info(f"[IG-WL] Step 4 publish: {pub_resp.status_code} | {pub_resp.text[:150]}")

    except Exception as e:
        logger.warning(f"[IG-WL] Step 4 publish warning: {e}")

    # ── Return result ───────────────────────────────────────
    wishlink_post_url = f"https://wishlink.com/{WISHLINK_CREATOR_URL}/post/{post_id}"
    logger.info(f"[IG-WL] ✅ All done! Wishlink post LIVE: {wishlink_post_url}")
    return wishlink_post_url, post_id


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
        "Wishlink URL do → original product links milenge\n\n"
        "📦 /create_collection\n"
        "Wishlink post/collection URL do → ek affiliate collection link milega\n\n"
        "🔗 /single_affiliate\n"
        "Koi bhi ek product URL do → ek affiliate Wishlink milega\n\n"
        "🗂️ /collection_from_links\n"
        "Apni khud ki 2-20 product links do (ek line mein ek) → affiliate collection ban jayega\n\n"
        "📲 /dm_automation\n"
        "Ek message mein bhejo: Instagram URL + product links (ek line ek)\n"
        "→ Wishlink Auto-DM activate ho jayega (Comment → DM milega)\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Kaise use karein:\n"
        "1. Pehle command type karo\n"
        "2. Phir apni link(s) bhejo\n"
        "3. Result aayega automatically ✅"
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
    await update.message.reply_text(
        "🗂️ Collection From Links Mode\n\n"
        "Ab apni product links bhejo — ek line mein ek link.\n"
        "Minimum 2, Maximum 20 links de sakte ho.\n\n"
        "Example:\n"
        "https://www.amazon.in/dp/XXXXXX\n"
        "https://www.myntra.com/XXXXXX\n"
        "https://www.amazon.in/dp/YYYYYY\n"
        "...\n\n"
        "⏳ Collection banne mein 3-7 min lag sakte hain — wait karo!"
    )


async def cmd_dm_automation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['state'] = 'dm_automation'
    await update.message.reply_text(
        "📲 Wishlink Auto-DM Activation Mode\n\n"
        "Ek hi message mein ye bhejo:\n"
        "Line 1: Instagram post/reel URL\n"
        "Line 2+: Product links (ek line mein ek, max 10)\n\n"
        "Example (copy mat karna, asli links bhejo):\n"
        "https://www.instagram.com/p/DXWSchgjRh/\n"
        "https://amzn.in/d/abc123\n"
        "https://dl.flipkart.com/s/xyz456\n"
        "https://amzn.in/d/def789\n\n"
        "⏳ Processing mein 2-3 min lagenge."
    )


async def _handle_extraction(update, context, urls):
    if not urls:
        await update.message.reply_text("❌ Koi valid URL nahi mila. Dobara bhejo.")
        return

    context.user_data['state'] = None
    url = urls[0]

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

    await update.message.reply_text(
        "📦 Collection bana raha hoon...\n"
        "⏳ Thoda time lagega (2-5 min) — please wait karo!"
    )

    if '/share/' in url:
        url = get_final_url_from_redirect(url) or url

    product_urls = get_product_links_from_wishlink_url(url)

    if not product_urls:
        await update.message.reply_text(
            "❌ Products nahi mile is URL se.\n"
            "Phir se try karne ke liye /create_collection bhejo."
        )
        return

    await update.message.reply_text(f"✅ {len(product_urls)} products mile! Collection create ho raha hai...")

    loop = asyncio.get_event_loop()
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

    loop = asyncio.get_event_loop()
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
    product_urls = []
    for line in lines:
        line = line.strip()
        if re.match(r'https?://', line):
            product_urls.append(line)

    if len(product_urls) < 2:
        await update.message.reply_text(
            "❌ Kam se kam 2 valid product URLs chahiye.\n"
            "Ek line mein ek link bhejo.\n\n"
            "Phir se try karne ke liye /collection_from_links bhejo."
        )
        return

    if len(product_urls) > 20:
        await update.message.reply_text(
            f"⚠️ {len(product_urls)} links mile — sirf pehli 20 use karunga."
        )
        product_urls = product_urls[:20]

    context.user_data['state'] = None

    await update.message.reply_text(
        f"✅ {len(product_urls)} links mil gayi!\n"
        "📦 Collection bana raha hoon... 3-7 min lagenge, wait karo ⏳"
    )

    loop = asyncio.get_event_loop()
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


async def _handle_dm_automation(update, context, text):
    """Single message mein IG URL + product links — parse karke Auto-DM activate karo"""

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

    if not ig_url:
        await update.message.reply_text(
            "❌ Instagram URL nahi mila.\n\n"
            "Pehli line mein instagram.com/p/XXXXX/ ya instagram.com/reel/XXXXX/ dalna zaroori hai.\n"
            "Dobara /dm_automation bhejo aur sahi format mein message bhejo."
        )
        return

    if len(product_urls) == 0:
        await update.message.reply_text(
            f"❌ Koi product URL nahi mila.\n\n"
            f"Instagram URL mila: {ig_url}\n"
            "Lekin product links (Amazon, Flipkart, Meesho) nahi mili.\n\n"
            "Dobara /dm_automation bhejo aur saare links ek saath bhejo."
        )
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
        f"📲 Wishlink Auto-DM setup ho raha hai...\n"
        f"⏳ 2-3 min lagenge — please wait karo!\n\n"
        f"⚠️ Note: Telegram bot se ig_media_id nahi milta,\n"
        f"isliye thumbnail placeholder aa sakti hai.\n"
        f"Auto-DM activate hoga ✅"
    )

    logger.info(f"[DM-BOT] Starting | ig={ig_url} | products={len(product_urls)}")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        create_ig_wishlink_post,
        ig_url,
        product_urls,
        None,
        '',   # ig_media_id — bot ke paas nahi hota
        'IMAGE',
        '',
        '',
        '',
        None
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
    product_urls_found = [u for u in urls if 'instagram.com' not in u and 'wishlink.com' not in u]

    if ig_urls_found and product_urls_found:
        logger.info(f"[AUTO-DM] Smart detect: IG URL + products in one message")
        await _handle_dm_automation(update, context, text)
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
        elif "wishlink.com" in url:
            product_links = get_product_links_from_wishlink_url(url)
            all_links.extend(product_links)

    if not all_links:
        await update.message.reply_text(
            "🤔 Koi product link nahi mila!\n\n"
            "Agar Wishlink URL diya tha to sahi format check karo:\n"
            "wishlink.com/username/post/123456\n\n"
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
def create_collection_with_singles_api():
    try:
        data = request.get_json()

        wishlink_url    = data.get('wishlink_url', '')
        collection_name = data.get('collection_name', '')

        if not wishlink_url:
            return jsonify({"success": False, "error": "wishlink_url required"}), 400

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

        logger.info("⏳ Waiting 120s for API rate limit to reset before affiliate conversion...")
        time.sleep(120)

        MAX_SINGLES = 10
        singles_to_convert = product_urls[:MAX_SINGLES]
        logger.info(f"🔗 Converting {len(singles_to_convert)} products to individual links")

        BATCH_SIZE = 5
        COOLDOWN_SECONDS = 120
        GAP_SECONDS = 1

        individual_affiliate_links = []
        for i, prod_url in enumerate(singles_to_convert):
            converted = False
            for attempt in range(2):
                try:
                    aff_link = convert_to_affiliate_link(prod_url)
                    if aff_link and aff_link != prod_url:
                        individual_affiliate_links.append(aff_link)
                        logger.info(f"🔗 Affiliate {i+1}: {aff_link[:60]}")
                        converted = True
                        break
                    else:
                        logger.warning(f"⚠️ Affiliate {i+1} raw URL returned, attempt {attempt+1}")
                        if attempt == 0:
                            time.sleep(10)
                except Exception as e:
                    logger.error(f"⚠️ Affiliate {i+1} attempt {attempt+1} failed: {e}")
                    if attempt == 0:
                        time.sleep(10)

            if not converted:
                individual_affiliate_links.append(prod_url)
                logger.warning(f"⚠️ Fallback raw URL for product {i+1}")

            time.sleep(GAP_SECONDS)

            if (i + 1) % BATCH_SIZE == 0 and (i + 1) < len(product_urls):
                logger.info(f"⏳ Batch {(i+1)//BATCH_SIZE} done. Cooling down {COOLDOWN_SECONDS}s...")
                time.sleep(COOLDOWN_SECONDS)

        return jsonify({
            "success": True,
            "collection_link": collection_link,
            "collection_id": str(collection_id),
            "products_added": added_count,
            "total_products": len(product_urls),
            "product_urls": product_urls,
            "individual_affiliate_links": individual_affiliate_links
        })

    except Exception as e:
        logger.error(f"create_collection_with_singles API error: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# ✅ ENDPOINT 4 — Create Wishlink Instagram Post
# ✅ FIXED: ig_media_id, ig_media_url, ig_thumbnail_url properly handled
# ============================================================
@app.route('/create-ig-wishlink-post', methods=['POST'])
def create_ig_wishlink_post_api():
    try:
        data = request.get_json()

        ig_post_url      = data.get('ig_post_url', '').strip()
        product_urls     = data.get('product_urls', [])
        title            = data.get('title', '')

        # ✅ NEW fields — n8n se aate hain
        ig_media_id      = data.get('ig_media_id', '')       # numeric Graph API ID
        ig_media_type    = data.get('ig_media_type', 'IMAGE')
        ig_media_url     = data.get('ig_media_url', '')
        ig_thumbnail_url = data.get('ig_thumbnail_url', '')
        ig_timestamp     = data.get('ig_timestamp', '')
        ig_children      = data.get('ig_children', {})

        if not ig_post_url:
            return jsonify({"success": False, "error": "ig_post_url required"}), 400

        if not isinstance(product_urls, list) or len(product_urls) == 0:
            return jsonify({"success": False, "error": "product_urls required (non-empty list)"}), 400

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


@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
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
    telegram_app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_link))

    async def setup_webhook():
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{TOKEN}")

    future = asyncio.run_coroutine_threadsafe(setup_webhook(), event_loop)
    future.result()
    logger.info("Webhook set successfully!")
    port = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

if __name__ == "__main__":
    main()
