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
WISHLINK_CREATOR       = os.getenv("WISHLINK_CREATOR", "budget-looks")
FIREBASE_API_KEY       = os.getenv("FIREBASE_API_KEY")
WISHLINK_REFRESH_TOKEN = os.getenv("WISHLINK_REFRESH_TOKEN")
WISHLINK_BZ_AUTH_KEY   = os.getenv("WISHLINK_BZ_AUTH_KEY")   # _bz_auth_key from browser storage

# URL-safe creator name (dots/hyphens ke saath)
WISHLINK_CREATOR_URL = WISHLINK_CREATOR.replace(" ", "-").replace(".", "-")

# Random titles for Telegram bot responses
TITLES = [
    "🔥 Loot Deal Alert!", "💥 Hot Deal Incoming!", "⚡ Limited Time Offer!",
    "🎯 Grab Fast!", "🚨 Flash Sale!", "💎 Special Deal Just For You!",
    "🛒 Shop Now!", "📢 Price Drop!", "🎉 Mega Offer!", "🤑 Crazy Discount!"
]

# Global variables
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
    """
    Firebase refreshToken se fresh idToken lo.
    Token memory mein cache hota hai — sirf expire hone pe refresh hoga.
    """
    global _token_cache

    current_time = time.time()

    # Cache check — 5 min buffer ke saath
    if _token_cache["id_token"] and _token_cache["expires_at"] > current_time + 300:
        logger.info("✅ Cached token valid hai — reuse kar raha hoon")
        return _token_cache["id_token"]

    logger.info("🔄 Firebase token refresh kar raha hoon...")

    if not FIREBASE_API_KEY or not WISHLINK_REFRESH_TOKEN:
        logger.warning("⚠️ Firebase credentials missing — BZ auth key try karunga")
        return WISHLINK_BZ_AUTH_KEY  # Fallback to BZ key

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
        # BZ auth key as fallback
        if WISHLINK_BZ_AUTH_KEY:
            logger.info("🔄 BZ auth key fallback use kar raha hoon")
            return WISHLINK_BZ_AUTH_KEY
        return None


def get_creator_headers(token=None):
    """Common headers for Wishlink Creator API calls."""
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
    """
    Kisi bhi raw product URL ko budget.looks ke Wishlink affiliate link mein convert karo.
    """
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

def extract_post_id_from_url(url):
    match = re.search(r"/(?:post|reels)/(\d+)", url)
    result = match.group(1) if match else None
    logger.info(f"Extract post ID from {url}: {result}")
    return result

def get_product_links_from_post(post_id):
    """Wishlink POST ya REELS se product links nikalo."""
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://www.wishlink.com",
        "referer": "https://www.wishlink.com/",
        "user-agent": "Mozilla/5.0",
        "wishlinkid": WISHLINK_ID,
    }
    api_urls = [
        f"https://api.wishlink.com/api/store/getPostOrCollectionProducts?page=1&limit=50&postType=POST&postOrCollectionId={post_id}&sourceApp=STOREFRONT",
        f"https://api.wishlink.com/api/store/getPostOrCollectionProducts?page=1&limit=50&postType=REELS&postOrCollectionId={post_id}&sourceApp=STOREFRONT"
    ]
    for api_url in api_urls:
        try:
            logger.info(f"Trying API: {api_url}")
            response = requests.get(api_url, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            products = data.get("data", {}).get("products", [])
            logger.info(f"API response: {len(products)} products found")
            if products:
                links = [p["purchaseUrl"] for p in products if "purchaseUrl" in p]
                logger.info(f"Product links: {len(links)}")
                return links
        except Exception as e:
            logger.error(f"API error: {e}")
            continue
    return []


def get_product_links_from_collection(collection_id):
    """
    Kisi bhi creator ki Wishlink COLLECTION se saare product links nikalo.
    collection_id: numeric ID (e.g. 885774)
    Multiple API endpoints try karta hai — fallback strategy.
    """
    storefront_headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://www.wishlink.com",
        "referer": "https://www.wishlink.com/",
        "user-agent": "Mozilla/5.0",
        "wishlinkid": WISHLINK_ID,
    }

    # Attempt 1: storefront — getShopProductDetails (collection ke liye)
    try:
        url = (
            f"https://api.wishlink.com/api/store/getShopProductDetails"
            f"?posttype=collection&postCollectionId={collection_id}&sourceApp=STOREFRONT"
        )
        logger.info(f"[Collection A1] Trying: {url}")
        r = requests.get(url, headers=storefront_headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        products = (
            data.get("data", {}).get("products") or
            data.get("data", {}).get("postProducts") or []
        )
        if products:
            links = [p.get("purchaseUrl") or p.get("url") for p in products if p.get("purchaseUrl") or p.get("url")]
            logger.info(f"[Collection A1] ✅ {len(links)} products mila")
            return links
    except Exception as e:
        logger.warning(f"[Collection A1] Failed: {e}")

    # Attempt 2: storefront — getPostOrCollectionProducts with COLLECTION (uppercase)
    try:
        url = (
            f"https://api.wishlink.com/api/store/getPostOrCollectionProducts"
            f"?page=1&limit=50&postType=COLLECTION"
            f"&postOrCollectionId={collection_id}&sourceApp=STOREFRONT"
        )
        logger.info(f"[Collection A2] Trying: {url}")
        r = requests.get(url, headers=storefront_headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        products = data.get("data", {}).get("products", [])
        if products:
            links = [p["purchaseUrl"] for p in products if "purchaseUrl" in p]
            logger.info(f"[Collection A2] ✅ {len(links)} products mila")
            return links
    except Exception as e:
        logger.warning(f"[Collection A2] Failed: {e}")

    # Attempt 3: Creator API (auth ke saath) — getShopPostsOnCollectionDetails
    try:
        token = get_fresh_wishlink_token()
        if token:
            url = (
                f"https://api.wishlink.com/api/c/getShopPostsOnCollectionDetails"
                f"?posttype=collection&postCollectionId={collection_id}"
                f"&creator={WISHLINK_CREATOR}"
            )
            logger.info(f"[Collection A3] Trying with auth: {url}")
            r = requests.get(url, headers=get_creator_headers(token), timeout=15)
            r.raise_for_status()
            data = r.json()
            products = (
                data.get("data", {}).get("products") or
                data.get("data", {}).get("postProducts") or []
            )
            if products:
                links = [p.get("purchaseUrl") or p.get("link") for p in products if p.get("purchaseUrl") or p.get("link")]
                logger.info(f"[Collection A3] ✅ {len(links)} products mila")
                return links
    except Exception as e:
        logger.warning(f"[Collection A3] Failed: {e}")

    logger.error(f"❌ Saare collection API attempts fail ho gaye for ID: {collection_id}")
    return []


# ============================================================
# 🗂️ Wishlink Collection Creator
# ============================================================
def create_wishlink_collection(product_urls, collection_name=None):
    """
    User ke Wishlink account mein nayi collection banao.

    product_urls: list of raw product URLs (Flipkart/Amazon etc.)
    collection_name: optional name for the collection

    Returns: collection_link (string) or None on failure
    """
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

    # ── Step 1: Collection banao ──────────────────────────────
    try:
        logger.info(f"📁 Creating collection: {collection_name}")
        create_resp = requests.post(
            "https://api.wishlink.com/api/c/createEditShopCollection",
            headers=headers,
            json={"name": collection_name, "creator": WISHLINK_CREATOR},
            timeout=20
        )
        create_resp.raise_for_status()
        create_data = create_resp.json()
        logger.info(f"Collection create response: {create_data}")

        # Collection ID extract karo
        collection_id = (
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

    # ── Step 2: Har product add karo ─────────────────────────
    added_count = 0
    for i, url in enumerate(product_urls):
        try:
            logger.info(f"➕ Adding product {i+1}/{len(product_urls)}: {url[:60]}")
            scrape_resp = requests.post(
                "https://api.wishlink.com/api/c/autoScrapeProduct",
                headers=headers,
                json={
                    "link": url,
                    "collectionId": collection_id,
                    "creator": WISHLINK_CREATOR
                },
                timeout=20
            )
            logger.info(f"Product add status: {scrape_resp.status_code}")
            added_count += 1
            time.sleep(0.8)  # Rate limit se bachne ke liye
        except Exception as e:
            logger.error(f"❌ Product add failed ({url[:40]}): {e}")
            continue

    logger.info(f"✅ {added_count}/{len(product_urls)} products added")

    # ── Step 3: Finalize ─────────────────────────────────────
    try:
        logger.info("🔒 Finalizing products...")
        requests.post(
            "https://api.wishlink.com/api/c/finalizeProducts",
            headers=headers,
            json={"collectionId": collection_id, "creator": WISHLINK_CREATOR},
            timeout=15
        )
        logger.info("✅ Products finalized")
    except Exception as e:
        logger.error(f"⚠️ Finalize warning (non-fatal): {e}")

    # ── Step 4: Collection link banao ─────────────────────────
    collection_link = f"https://wishlink.com/{WISHLINK_CREATOR_URL}/collection/{collection_id}"
    logger.info(f"✅ Collection ready: {collection_link}")
    return collection_link, collection_id, added_count


# ============================================================
# 📱 Telegram Bot Handlers
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Start command from user: {update.effective_user.id}")
    await update.message.reply_text(
        "Hey! 👋 Send me a Wishlink or Instagram post/reel link and I'll fetch the real product links for you.\n\nExample:\nhttps://www.wishlink.com/share/dupdx\nor\nhttps://wishlink.com/username/post/123456"
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
    if not urls:
        return
    await update.message.reply_text("Processing your link… 🔄")
    all_links = []
    for url in urls:
        if "/share/" in url:
            redirected = get_final_url_from_redirect(url)
            if redirected:
                all_links.append(redirected)
        elif "wishlink.com" in url:
            post_id = extract_post_id_from_url(url)
            if post_id:
                product_links = get_product_links_from_post(post_id)
                all_links.extend(product_links)
    if not all_links:
        await update.message.reply_text("❌ No product links found.")
        return
    title = random.choice(TITLES)
    try:
        await send_links_in_parts(update, all_links, title)
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


# ============================================================
# ✅ ENDPOINT 1 — Get Product Links (existing, unchanged)
# ============================================================
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

            logger.info(f"Redirected to: {final_url}")

            if 'wishlink.com' not in final_url:
                logger.info(f"Direct external URL mili: {final_url}")
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

        logger.info(f"Post ID: {post_id}, Type: {post_type}")

        product_links = get_product_links_from_post(post_id)

        if not product_links:
            return jsonify({
                "success": False,
                "error": "Koi product nahi mila",
                "post_id": post_id,
                "post_type": post_type
            }), 404

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


# ============================================================
# ✅ ENDPOINT 2 — Create Collection (NEW)
# n8n yahan POST karega, bot collection banayega
# ============================================================
@app.route('/create-collection', methods=['POST'])
def create_collection_api():
    """
    Input (JSON):
      Option A — Direct product URLs:
        { "product_urls": ["flipkart.com/...", ...], "collection_name": "optional" }

      Option B — Se creator ki wishlink collection URL:
        { "wishlink_collection_url": "https://wishlink.com/creator/collection/12345" }

      Option C — Creator ki wishlink POST URL:
        { "wishlink_post_url": "https://wishlink.com/creator/post/12345" }

    Output:
        { "success": true, "collection_link": "...", "collection_id": 865774, "products_added": 4 }
    """
    try:
        data = request.get_json()

        product_urls             = data.get('product_urls', [])
        wishlink_collection_url  = data.get('wishlink_collection_url', '')
        wishlink_post_url        = data.get('wishlink_post_url', '')
        collection_name          = data.get('collection_name', '')

        # ── Option B: Collection URL se products nikalo ───────
        if not product_urls and wishlink_collection_url:
            col_match = re.search(r'/collection/(\d+)', wishlink_collection_url)
            if col_match:
                col_id = col_match.group(1)
                logger.info(f"Fetching products from collection: {col_id}")
                product_urls = get_product_links_from_collection(col_id)
            else:
                logger.warning(f"Collection ID nahi mila URL se: {wishlink_collection_url}")

        # ── Option C: Post URL se products nikalo ────────────
        if not product_urls and wishlink_post_url:
            if '/share/' in wishlink_post_url:
                wishlink_post_url = get_final_url_from_redirect(wishlink_post_url) or wishlink_post_url
            post_id = extract_post_id_from_url(wishlink_post_url)
            if post_id:
                logger.info(f"Fetching products from post: {post_id}")
                product_urls = get_product_links_from_post(post_id)

        if not product_urls:
            return jsonify({
                "success": False,
                "error": "Koi product URL nahi mila. product_urls, wishlink_collection_url, ya wishlink_post_url dena zaroori hai."
            }), 400

        logger.info(f"Creating collection with {len(product_urls)} products")

        # ── Collection banao ──────────────────────────────────
        result = create_wishlink_collection(product_urls, collection_name)

        if not result:
            return jsonify({
                "success": False,
                "error": "Collection creation failed — logs check karo"
            }), 500

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


# ============================================================
# 🔗 Webhook
# ============================================================
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
