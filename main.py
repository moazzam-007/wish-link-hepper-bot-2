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
TOKEN              = os.getenv("BOT_TOKEN")
WEBHOOK_URL        = os.getenv("WEBHOOK_URL")
WISHLINK_ID        = os.getenv("WISHLINK_ID", "1752163729058-1dccdb9e-a0f9-f088-a678-e14f8997f719")
WISHLINK_CREATOR   = os.getenv("WISHLINK_CREATOR", "budget.looks")
FIREBASE_API_KEY   = os.getenv("FIREBASE_API_KEY")           # AIzaSyDLL6Yr...
WISHLINK_REFRESH_TOKEN = os.getenv("WISHLINK_REFRESH_TOKEN") # AMf-vBxr4oai...

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
    "expires_at": 0   # Unix timestamp
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

    logger.info("🔄 Token refresh kar raha hoon...")

    if not FIREBASE_API_KEY or not WISHLINK_REFRESH_TOKEN:
        logger.error("❌ FIREBASE_API_KEY ya WISHLINK_REFRESH_TOKEN env variable missing!")
        return None

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

        _token_cache["id_token"]    = new_token
        _token_cache["expires_at"]  = current_time + expires_in

        logger.info(f"✅ Token refresh successful! {expires_in}s valid")
        return new_token

    except Exception as e:
        logger.error(f"❌ Token refresh failed: {e}")
        return None


# ============================================================
# 💰 Affiliate Link Conversion
# ============================================================
def convert_to_affiliate_link(product_url):
    """
    Kisi bhi raw product URL (Flipkart/Amazon etc.) ko
    budget.looks ke Wishlink affiliate link mein convert karo.
    """
    try:
        token = get_fresh_wishlink_token()
        if not token:
            logger.warning("⚠️ Token nahi mila — raw URL return karunga")
            return product_url

        headers = {
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "Origin": "https://creator.wishlink.com",
            "Referer": "https://creator.wishlink.com/"
        }

        resp = requests.post(
            "https://api.wishlink.com/api/c/convertSingleProductLink",
            headers=headers,
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
            logger.warning(f"⚠️ Affiliate link response mein nahi mila: {data}")
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
            response = requests.get(api_url, headers=headers)
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
    return "🤖 Bot is running!"

@app.route('/health')
def health():
    return "OK"

@app.route('/status')
def status():
    return "Active"


# ✅ MAIN ENDPOINT — n8n ke liye
# Product links fetch + affiliate link conversion (auto token refresh)
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

            # ✅ Directly external product URL (Flipkart/Amazon)
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

            # Wishlink post page — numeric ID nikalo
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

        # ✅ First product ko affiliate link mein convert karo (auto token refresh)
        first_product  = product_links[0]
        affiliate_link = convert_to_affiliate_link(first_product)

        return jsonify({
            "success": True,
            "post_id": post_id,
            "post_type": post_type,
            "product_links": product_links,
            "first_product": first_product,
            "affiliate_link": affiliate_link,   # ← n8n ye use kare
            "total": len(product_links)
        })

    except Exception as e:
        logger.error(f"API error: {e}")
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
    loop_thread = threading.Thread(target=run_event_loop_in_background, args=(event_loop,), daemon=True)
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
