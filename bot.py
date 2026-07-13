"""
bot.py — Solana Whale Wallet Tracker Bot (نسخة محسّنة v2)
========================================================================
هذا ملف واحد كامل، فيه كل الكود ديال البوت + المفاتيح.
غير: pip install -r requirements.txt ثم python bot.py

========================================================================
📌 شنو تبدل فهاد النسخة (Phase 1 — الأساس: سرعة + استقرار):
------------------------------------------------------------------------
1. Logging احترافي: 3 ملفات منفصلة
   - logs/bot.log       -> كل شي (معلومات عامة)
   - logs/errors.log    -> غير الأخطاء (باش تكتشف المشاكل بسرعة)
   - logs/trades.log    -> سجل كل صفقة تسجلات (سطر واحد لكل صفقة، سهل القراءة)
2. Cache ذكي لبيانات DexScreener (TTL = 30 ثانية):
   - إذا عدة محافظ شراو نفس التوكن فنفس الدقيقة، ما نعاودوش الطلب لـ DexScreener
   - كيقلل الطلبات بزاف = أسرع + أقل احتمال Rate Limit
3. Retry + Exponential Backoff تلقائي:
   - إذا Helius أو DexScreener رجعو خطأ مؤقت، البوت يعاود المحاولة (3 مرات)
     بدل ما يستسلم مباشرة على صفقة يمكن تكون مهمة
4. فحص المحافظ بالتوازي (Async حقيقي عبر asyncio.gather + Semaphore):
   - بدل ما يفحص محفظة وحدة ب وحدة (بطيء)، كيفحص عدة محافظ فنفس الوقت
   - Semaphore كيحدد عدد الطلبات المتزامنة باش ما نضربوش Rate Limit ديال APIs المجانية
5. حماية شاملة من الأعطال (try/except على مستوى كل محفظة):
   - غلطة فمحفظة وحدة ما توقفش فحص باقي المحافظ
6. عدد المشترين خلال 5 / 15 / 60 دقيقة (بدل نافذة وحدة فقط)
7. عمر التوكن (Token Age) من DexScreener (pairCreatedAt) — مجاني، بلا API إضافي
8. رسائل التنبيه محسّنة: HTML احترافي + وقت العملية + عمر التوكن + عدد المشترين لكل نافذة
9. كتم أنواع تنبيهات معينة (Whale / Smart Money / Multi Wallet / New Position) من الإعدادات
10. كل الميزات القديمة محفوظة 100% (الأوامر، الأزرار، قاعدة البيانات، المفاتيح)

⚠️ ملاحظة أمنية: البوت توكن و API key مكتوبين هنا مباشرة (Plaintext).
هذا مقصود بناءً على طلبك، لكن نفكرك: خاصك تحافظ على هاد الملف بعيد عن أي مكان عام
(GitHub public، مثلاً)، حيت أي واحد يشوفهم يقدر يتحكم فالبوت والـ API key ديالك.
========================================================================
"""

import os
import re
import csv
import json
import time
import uuid
import random
import shutil
import asyncio
import logging
import logging.handlers
from datetime import datetime
from functools import wraps

import sqlite3
import aiohttp
from aiohttp import web

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters,
)

# ============================================================
# 📝 نظام Logging احترافي (3 ملفات منفصلة)
# ============================================================

LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
formatter = logging.Formatter(LOG_FORMAT)

# --- Logger عام (كل شي: console + bot.log) ---
logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

general_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOGS_DIR, "bot.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
general_file_handler.setFormatter(formatter)
logger.addHandler(general_file_handler)

# --- Logger خاص بالأخطاء فقط (errors.log) ---
error_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOGS_DIR, "errors.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
error_file_handler.setFormatter(formatter)
error_file_handler.setLevel(logging.ERROR)
logger.addHandler(error_file_handler)

# --- Logger خاص بالصفقات (trades.log) — سطر واحد منظم لكل صفقة ---
trades_logger = logging.getLogger("trades")
trades_logger.setLevel(logging.INFO)
trades_logger.propagate = False  # ما نكرروش نفس السطر فـ bot.log
trades_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOGS_DIR, "trades.log"), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
trades_file_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
trades_logger.addHandler(trades_file_handler)


def log_trade(wallet_name, wallet_address, action, symbol, mint, amount, usd_value, signature):
    """كيسجل كل صفقة فملف trades.log بصيغة منظمة (سهل تفتحها بـ Excel أو تحللها بعدين)"""
    trades_logger.info(
        f"{action} | wallet={wallet_name} ({wallet_address[:6]}...) | "
        f"token={symbol} | mint={mint} | amount={amount} | usd={usd_value:.2f} | sig={signature}"
    )


# ============================================================
# 🔑 الإعدادات والمفاتيح ديالك (الجزء الوحيد لي تبدل فيه إذا بدلتي البوت)
# ============================================================

BOT_TOKEN = "8845375695:AAERECGAvRvSnY6EjeInZdE9vsmb1Q6y7-E"
ADMIN_CHAT_ID = "6009339320"
HELIUS_API_KEY = "2a6c359f-9561-4167-9dea-32a2f055f8b8"

DATABASE_NAME = "wallets.db"
POLLING_INTERVAL_SECONDS = 30  # 🔧 كان 12 — بطّأناه لتقليص استهلاك Helius Credits (كل فحص محفظة = طلب)

DEFAULT_NOTIFY_BUY = True
DEFAULT_NOTIFY_SELL = True
DEFAULT_MIN_USD_ALERT = 0
DEFAULT_WHALE_USD_THRESHOLD = 10000
DEFAULT_SMART_MONEY_MIN_WALLETS = 2
DEFAULT_MULTI_WALLET_MIN = 3
DEFAULT_SMART_MONEY_WINDOW_MINUTES = 10

# --- إعدادات جديدة (كتم تنبيهات معينة) ---
DEFAULT_MUTE_WHALE = False
DEFAULT_MUTE_SMART_MONEY = False
DEFAULT_MUTE_MULTI_WALLET = False
DEFAULT_MUTE_NEW_POSITION = False

# --- إعدادات الأداء الجديدة ---
DEXSCREENER_CACHE_TTL_SECONDS = 30       # كل شحال نخزنو بيانات التوكن قبل ما نطلبوها مرة أخرى
MAX_CONCURRENT_WALLET_CHECKS = 4           # 🔧 تقليل من 8 لـ 4 (كان كيضرب Rate Limit ديال Helius)
STAGGER_INTERVAL_SECONDS = 0.5              # 🆕 فاصل زمني بين انطلاق كل طلب محفظة (توزيع الحمل)
API_RETRY_ATTEMPTS = 3                     # عدد محاولات إعادة الطلب عند فشل مؤقت
API_RETRY_BASE_DELAY = 1.5                 # ثواني (كيتضاعف: 1.5, 3, 6 ...)

# --- 🆕 فلترة الضجة (Alert Fatigue) — عتبة أدنى للـ Score قبل ما نبعتو تنبيه كامل ---
DEFAULT_MIN_TOKEN_SCORE = 0   # 0 = بلا فلترة (كيما كان قبل)
# 🆕 Rug Check (3 نداءات RPC) غير يخدم للصفقات لي قيمتها فوق هاد الرقم —
# توفير كبير لكريدي Helius (صفقة بـ$2 ماخصهاش تستهلك نفس الفحص ديال صفقة بـ$5000)
RUG_CHECK_MIN_USD_VALUE = 20
DEFAULT_DIGEST_ENABLED = True
DIGEST_INTERVAL_SECONDS = 1800  # كل 30 دقيقة كنبعتو ملخص للصفقات "الضعيفة" لي تفلترات

# --- 🆕 مصادر بيانات مجانية إضافية (Fallback + أدوات استخبارات مجانية) ---
JUPITER_PRICE_URL = "https://price.jup.ag/v4/price"  # مجاني، بديل إذا DexScreener ما عندوش التوكن

HELIUS_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
HELIUS_WEBHOOKS_URL = "https://api.helius.xyz/v0/webhooks"

# --- 🆕 إعدادات Helius Webhooks (بديل أسرع للـ Polling، اختياري) ---
# الـ Polling كيبقى خدام دايماً كـ "شبكة أمان" حتى لو فعلتي الـ Webhook، حيت
# قاعدة البيانات كتمنع التكرار (UNIQUE signature) — بلا خطر ديال إشعارات مكررة.
WEBHOOK_LISTEN_HOST = "0.0.0.0"
# 🔧 Render (وخدمات Cloud مشابهة) كيعطيو رقم المنفذ عبر متغير بيئة PORT —
# خاصنا نستعملوه، وإلا الخدمة كتفشل بـ "No open ports detected"
WEBHOOK_LISTEN_PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_PATH = "/helius-webhook"
TELEGRAM_WEBHOOK_PATH = "/telegram-webhook"

# --- 🆕 Watchdog (فحص صحة البوت) ---
WATCHDOG_CHECK_INTERVAL_SECONDS = 180
WATCHDOG_STALL_THRESHOLD_SECONDS = POLLING_INTERVAL_SECONDS * 10
AUTO_RESTART_DELAY_SECONDS = 10


# ============================================================
# ⚡ Cache بسيط فالذاكرة لبيانات DexScreener (بلا أي خدمة خارجية مدفوعة)
# ============================================================

class SimpleTTLCache:
    """
    كاش بسيط فالذاكرة (RAM) مع مدة صلاحية (TTL).
    الهدف: إذا عدة محافظ شراو نفس التوكن فنفس الدقائق، ما نديروش نفس طلب
    DexScreener عدة مرات — كيوفر وقت وكيقلل احتمال Rate Limit.
    """

    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._store = {}  # key -> (value, expires_at)

    def get(self, key):
        entry = self._store.get(key)
        if not entry:
            return None
        value, expires_at = entry
        if time.time() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key, value):
        self._store[key] = (value, time.time() + self.ttl_seconds)

    def clear_expired(self):
        """تنظيف دوري للمداخل المنتهية باش الذاكرة ما تكبرش بلا داعي"""
        now = time.time()
        expired_keys = [k for k, (_, exp) in self._store.items() if now > exp]
        for k in expired_keys:
            del self._store[k]


dexscreener_cache = SimpleTTLCache(DEXSCREENER_CACHE_TTL_SECONDS)


# ============================================================
# 🧠 Adaptive Throttle — حماية ذكية من استنزاف كريدي Helius
# ============================================================
# 🆕 إذا البوت واجه Rate Limit (429) بشكل متكرر خلال وقت قصير، معناها
# الكريدي قريب يخلص أو الحمل زايد. بدل ما يكمل يحاول (وكل محاولة فاشلة
# غالباً كتستهلك كريدي زادة)، البوت كيبطئ روحو تلقائياً لمدة، ويعلم
# الأدمن مرة وحدة، ومنبعد كيرجع للسرعة العادية وحدو بلا تدخل يدوي.

_adaptive_state = {"consecutive_rate_limits": 0, "throttled_until": 0, "alert_sent": False}
RATE_LIMIT_THRESHOLD = 3          # عدد الحوادث المتتالية قبل ما نبطّئو
THROTTLE_COOLDOWN_SECONDS = 900    # 15 دقيقة راحة قبل ما نعاودو نحاولو بالسرعة العادية


def _mark_rate_limit_incident():
    _adaptive_state["consecutive_rate_limits"] += 1
    if _adaptive_state["consecutive_rate_limits"] >= RATE_LIMIT_THRESHOLD:
        _adaptive_state["throttled_until"] = time.time() + THROTTLE_COOLDOWN_SECONDS


def _mark_rate_limit_recovered():
    _adaptive_state["consecutive_rate_limits"] = 0
    _adaptive_state["alert_sent"] = False


def is_currently_throttled() -> bool:
    return time.time() < _adaptive_state["throttled_until"]


# ============================================================
# 🔁 Retry + Exponential Backoff (لطلبات الشبكة)
# ============================================================

def with_retry(attempts: int = API_RETRY_ATTEMPTS, base_delay: float = API_RETRY_BASE_DELAY):
    """
    Decorator كيعاود محاولة الدالة async عدة مرات إذا وقع خطأ شبكة مؤقت،
    مع تأخير متزايد (Exponential Backoff) باش ما نضغطوش على الـ API فحالة مشكل مستمر.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(1, attempts + 1):
                try:
                    result = await func(*args, **kwargs)
                    _mark_rate_limit_recovered()  # 🆕 نجحت = البوت "بخير"، نصفرو العداد
                    return result
                except (aiohttp.ClientError, asyncio.TimeoutError, RateLimitedError) as e:
                    last_error = e
                    if isinstance(e, RateLimitedError):
                        _mark_rate_limit_incident()  # 🆕 نسجلو الحادثة (Adaptive Throttle)
                    if attempt < attempts:
                        delay = base_delay * (2 ** (attempt - 1))
                        logger.warning(
                            f"⚠️ محاولة {attempt}/{attempts} فشلت فـ {func.__name__}: {e} "
                            f"— نعاود بعد {delay:.1f}s"
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"❌ فشلت كل المحاولات ({attempts}) فـ {func.__name__}: {e}")
            return None
        return wrapper
    return decorator


# ============================================================
# 🗄️ قاعدة البيانات (database.py سابقاً) — نفس البنية القديمة 100%
# ============================================================
# ملاحظة: البنية (الجداول) ما تبدلاتش، باش نضمنو التوافق الكامل مع أي
# قاعدة بيانات wallets.db كاينة عندك من قبل. زدنا غير دوال جديدة (queries)
# فوق نفس الجداول القديمة، بلا ما نمسو حتى جدول موجود.

def get_connection():
    conn = sqlite3.connect(DATABASE_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # يحسن التعامل مع القراءة/الكتابة المتزامنة
    return conn


def init_db():
    """ينشئ جميع الجداول إذا ماكانتش موجودة + يعبي الإعدادات الافتراضية"""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            last_signature TEXT,
            date_added TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id TEXT PRIMARY KEY,
            name TEXT,
            date_added TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_address TEXT NOT NULL,
            wallet_name TEXT NOT NULL,
            mint TEXT NOT NULL,
            token_symbol TEXT,
            action TEXT NOT NULL,
            amount REAL,
            usd_value REAL,
            signature TEXT UNIQUE NOT NULL,
            timestamp INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts_sent (
            mint TEXT,
            alert_type TEXT,
            sent_at INTEGER,
            PRIMARY KEY (mint, alert_type)
        )
    """)

    # 🆕 جدول جديد: تسجيل كل حدث تنبيه (Whale/Smart Money/Multi Wallet) لكل توكن
    # هذا كيخدم أساس نظام التقييم (Token Score) — عدد المرات لي التوكن ضرب فيها
    # هاد الأنواع ديال التنبيهات، مؤشر قوي على "الاهتمام" بالتوكن.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT NOT NULL,
            event_type TEXT NOT NULL,
            ts INTEGER NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_signal_events_mint ON signal_events(mint, event_type)")

    # 🆕 جدول Win-Rate الحقيقي: كنسجلو السعر لحظة كل صفقة BUY، ومنبعد كنقارنو
    # مع السعر بعد 1 ساعة و24 ساعة، باش نحسبو Win-Rate حقيقي (مبني على نتيجة
    # فعلية)، ماشي على نشاط/عدد الصفقات فقط.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_outcomes (
            transaction_id INTEGER PRIMARY KEY,
            wallet_address TEXT NOT NULL,
            mint TEXT NOT NULL,
            ts INTEGER NOT NULL,
            price_at_buy REAL NOT NULL,
            outcome_1h_pct REAL,
            outcome_24h_pct REAL,
            evaluated_1h INTEGER DEFAULT 0,
            evaluated_24h INTEGER DEFAULT 0
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_wallet ON trade_outcomes(wallet_address)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_pending_1h ON trade_outcomes(evaluated_1h, ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_pending_24h ON trade_outcomes(evaluated_24h, ts)")

    # 🆕 ATH (All-Time High) — أعلى Market Cap/سعر وصلهم التوكن منذ ما بدا
    # البوت يتابعه (ماشي ATH الحقيقي التاريخي الكامل، لكن مفيد جداً باش
    # تشوف "شحال طاح من القمة" منذ ما دخل البوت فالراديو ديالك)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS token_ath (
            mint TEXT PRIMARY KEY,
            ath_mcap REAL NOT NULL,
            ath_price REAL NOT NULL,
            ath_ts INTEGER NOT NULL,
            first_seen_ts INTEGER NOT NULL
        )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_tx_wallet_mint ON transactions(wallet_address, mint, action)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tx_mint_action_time ON transactions(mint, action, timestamp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tx_timestamp ON transactions(timestamp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tx_wallet_name ON transactions(wallet_name)")

    # 🆕 جدول Digest: الصفقات "الضعيفة" (Score تحت العتبة) كتتخزن هنا بدل ما
    # تبعث تنبيه فوري، ومنبعد كيتبعتو كملخص مجمّع كل 30 دقيقة — كيقلل الضجة.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS digest_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_name TEXT,
            symbol TEXT,
            mint TEXT,
            score INTEGER,
            score_label TEXT,
            usd_value REAL,
            ts INTEGER
        )
    """)

    defaults = {
        "notify_buy": "1" if DEFAULT_NOTIFY_BUY else "0",
        "notify_sell": "1" if DEFAULT_NOTIFY_SELL else "0",
        "min_usd_alert": str(DEFAULT_MIN_USD_ALERT),
        "whale_usd_threshold": str(DEFAULT_WHALE_USD_THRESHOLD),
        "smart_money_min_wallets": str(DEFAULT_SMART_MONEY_MIN_WALLETS),
        "multi_wallet_min": str(DEFAULT_MULTI_WALLET_MIN),
        "smart_money_window_minutes": str(DEFAULT_SMART_MONEY_WINDOW_MINUTES),
        "mute_whale": "1" if DEFAULT_MUTE_WHALE else "0",
        "mute_smart_money": "1" if DEFAULT_MUTE_SMART_MONEY else "0",
        "mute_multi_wallet": "1" if DEFAULT_MUTE_MULTI_WALLET else "0",
        "mute_new_position": "1" if DEFAULT_MUTE_NEW_POSITION else "0",
        # 🆕 إعدادات Webhook (Phase 3)
        "polling_enabled": "1",   # Polling خدام بالدفولت (شبكة أمان)
        "webhook_enabled": "0",   # Webhook متوقف حتى تفعله بـ /setwebhook
        "webhook_id": "",
        "webhook_url": "",
        # 🆕 فلترة الضجة
        "min_token_score": str(DEFAULT_MIN_TOKEN_SCORE),
        "digest_enabled": "1" if DEFAULT_DIGEST_ENABLED else "0",
    }
    for key, value in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    # 🆕 توليد Secret عشوائي لمرة وحدة (باش رابط الـ Webhook ما يكونش قابل للتخمين)
    cur.execute("SELECT value FROM settings WHERE key = 'webhook_secret'")
    if not cur.fetchone():
        import uuid
        cur.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("webhook_secret", uuid.uuid4().hex))

    # 🆕 Secret خاص بـ Telegram Webhook (مختلف عن webhook_secret ديال Helius)
    cur.execute("SELECT value FROM settings WHERE key = 'telegram_webhook_secret'")
    if not cur.fetchone():
        import uuid
        cur.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("telegram_webhook_secret", uuid.uuid4().hex))

    conn.commit()
    conn.close()


# ---------------- Wallets ----------------

def add_wallet(address: str, name: str) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO wallets (address, name) VALUES (?, ?)", (address, name))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_wallet(address: str) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM wallets WHERE address = ?", (address,))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def rename_wallet(address: str, new_name: str) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE wallets SET name = ? WHERE address = ?", (new_name, address))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def get_all_wallets():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM wallets ORDER BY date_added DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_wallet_by_address(address: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM wallets WHERE address = ?", (address,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def update_last_signature(address: str, signature: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE wallets SET last_signature = ? WHERE address = ?", (signature, address))
    conn.commit()
    conn.close()


# ---------------- Settings ----------------

def get_setting(key: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else None


def get_all_settings():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM settings")
    rows = cur.fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def set_setting(key: str, value):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value))
    )
    conn.commit()
    conn.close()


# ---------------- Subscribers (Viewers) ----------------

def add_subscriber(chat_id: str, name: str = "") -> bool:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO subscribers (chat_id, name) VALUES (?, ?)", (chat_id, name))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_subscriber(chat_id: str) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def get_all_subscribers():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM subscribers")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------- Transactions ----------------

def add_transaction(wallet_address, wallet_name, mint, token_symbol, action, amount, usd_value, signature, timestamp):
    """🔧 كترجع transaction_id (int) إذا نجحت، أو None إذا كانت مسجلة من قبل (تكرار)"""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO transactions
            (wallet_address, wallet_name, mint, token_symbol, action, amount, usd_value, signature, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (wallet_address, wallet_name, mint, token_symbol, action, amount, usd_value, signature, timestamp))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def wallet_has_bought_before(wallet_address: str, mint: str) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) as c FROM transactions
        WHERE wallet_address = ? AND mint = ? AND action = 'BUY'
    """, (wallet_address, mint))
    row = cur.fetchone()
    conn.close()
    return row["c"] > 0


def count_distinct_wallets_bought_recently(mint: str, window_minutes: int) -> int:
    since = int(time.time()) - (window_minutes * 60)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(DISTINCT wallet_address) as c FROM transactions
        WHERE mint = ? AND action = 'BUY' AND timestamp >= ?
    """, (mint, since))
    row = cur.fetchone()
    conn.close()
    return row["c"]


def get_wallets_that_bought_recently(mint: str, window_minutes: int):
    since = int(time.time()) - (window_minutes * 60)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT wallet_name FROM transactions
        WHERE mint = ? AND action = 'BUY' AND timestamp >= ?
    """, (mint, since))
    rows = cur.fetchall()
    conn.close()
    return [r["wallet_name"] for r in rows]


def get_buyers_count_multi_window(mint: str) -> dict:
    """🆕 عدد المشترين خلال 5 / 15 / 60 دقيقة الأخيرة"""
    now = int(time.time())
    conn = get_connection()
    cur = conn.cursor()
    result = {}
    for label, minutes in (("5m", 5), ("15m", 15), ("60m", 60)):
        since = now - (minutes * 60)
        cur.execute("""
            SELECT COUNT(DISTINCT wallet_address) as c FROM transactions
            WHERE mint = ? AND action = 'BUY' AND timestamp >= ?
        """, (mint, since))
        result[label] = cur.fetchone()["c"]
    conn.close()
    return result


def was_alert_sent(mint: str, alert_type: str) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM alerts_sent WHERE mint = ? AND alert_type = ?", (mint, alert_type))
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_alert_sent(mint: str, alert_type: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO alerts_sent (mint, alert_type, sent_at) VALUES (?, ?, ?)",
        (mint, alert_type, int(time.time()))
    )
    conn.commit()
    conn.close()


def get_stats():
    conn = get_connection()
    cur = conn.cursor()

    now = int(time.time())
    today_start = now - (now % 86400)
    week_start = now - (7 * 86400)

    cur.execute("SELECT COUNT(*) as c FROM transactions WHERE timestamp >= ?", (today_start,))
    today_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) as c FROM transactions WHERE timestamp >= ?", (week_start,))
    week_count = cur.fetchone()["c"]

    cur.execute("""
        SELECT wallet_name, COUNT(*) as c FROM transactions
        GROUP BY wallet_name ORDER BY c DESC LIMIT 1
    """)
    top_wallet_row = cur.fetchone()
    top_wallet = top_wallet_row["wallet_name"] if top_wallet_row else "—"

    cur.execute("""
        SELECT COALESCE(token_symbol, mint) as token, COUNT(*) as c FROM transactions
        WHERE action = 'BUY' GROUP BY mint ORDER BY c DESC LIMIT 5
    """)
    top_bought = [(r["token"], r["c"]) for r in cur.fetchall()]

    conn.close()
    return {
        "today_count": today_count,
        "week_count": week_count,
        "top_wallet": top_wallet,
        "top_bought": top_bought,
    }


def get_wallet_stats(address: str) -> dict:
    """🆕 عدد Buy/Sell، متوسط قيمة الصفقة، إجمالي المشتريات/المبيعات لمحفظة وحدة"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            SUM(CASE WHEN action='BUY' THEN 1 ELSE 0 END) as buy_count,
            SUM(CASE WHEN action='SELL' THEN 1 ELSE 0 END) as sell_count,
            SUM(CASE WHEN action='BUY' THEN usd_value ELSE 0 END) as total_buy_usd,
            SUM(CASE WHEN action='SELL' THEN usd_value ELSE 0 END) as total_sell_usd,
            AVG(usd_value) as avg_trade_usd,
            COUNT(*) as total_trades
        FROM transactions WHERE wallet_address = ?
    """, (address,))
    row = cur.fetchone()
    conn.close()
    return {
        "buy_count": row["buy_count"] or 0,
        "sell_count": row["sell_count"] or 0,
        "total_buy_usd": row["total_buy_usd"] or 0.0,
        "total_sell_usd": row["total_sell_usd"] or 0.0,
        "avg_trade_usd": row["avg_trade_usd"] or 0.0,
        "total_trades": row["total_trades"] or 0,
    }


def get_top_active_wallets(since_timestamp: int, limit: int = 10):
    """🆕 أكثر المحافظ نشاطاً منذ وقت معين (اليوم / الأسبوع / الشهر)"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT wallet_name, wallet_address, COUNT(*) as c
        FROM transactions
        WHERE timestamp >= ?
        GROUP BY wallet_address
        ORDER BY c DESC LIMIT ?
    """, (since_timestamp, limit))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_wallet_signal_counts():
    """🆕 أكثر المحافظ تحقيقاً للإشارات (Buy)"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT wallet_name, COUNT(*) as c FROM transactions
        WHERE action = 'BUY' GROUP BY wallet_name ORDER BY c DESC LIMIT 10
    """)
    rows = cur.fetchall()
    conn.close()
    return [(r["wallet_name"], r["c"]) for r in rows]


def search_transactions(query: str, limit: int = 15):
    conn = get_connection()
    cur = conn.cursor()
    like_query = f"%{query}%"
    cur.execute("""
        SELECT * FROM transactions
        WHERE mint LIKE ? OR token_symbol LIKE ?
        ORDER BY timestamp DESC LIMIT ?
    """, (like_query, like_query, limit))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_watchlist(limit: int = 10):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(token_symbol, mint) as token, mint, COUNT(*) as buy_count
        FROM transactions
        WHERE action = 'BUY'
        GROUP BY mint
        ORDER BY buy_count DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_transactions():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM transactions ORDER BY timestamp DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------- 🆕 Win-Rate الحقيقي (trade_outcomes) ----------------
# الفكرة: كل صفقة BUY كنسجلو السعر لحظتها، ومنبعد بـ 1h/24h كنقارنو السعر
# الجديد. هذا الفرق بين "محفظة نشيطة" و"محفظة رابحة فعلاً" — رقم مبني على
# نتيجة حقيقية، ماشي على عدد الصفقات فقط.

def record_trade_outcome_entry(transaction_id: int, wallet_address: str, mint: str, ts: int, price_at_buy: float):
    """كيسجل نقطة انطلاق لتتبع النتيجة (Outcome) — غير لصفقات BUY لي عندها سعر معروف"""
    if not price_at_buy or price_at_buy <= 0:
        return
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO trade_outcomes (transaction_id, wallet_address, mint, ts, price_at_buy)
            VALUES (?, ?, ?, ?, ?)
        """, (transaction_id, wallet_address, mint, ts, price_at_buy))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


def get_pending_outcome_evaluations(window: str, max_age_seconds: int, limit: int = 200):
    """
    كيجيب الصفقات لي وصل الوقت باش نقيموها (مثلاً عمرها +1 ساعة) ومازال
    ماتقيماتش لهاد النافذة. window: '1h' أو '24h'.
    """
    evaluated_col = f"evaluated_{window}"
    now = int(time.time())
    cutoff = now - max_age_seconds
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT * FROM trade_outcomes
        WHERE {evaluated_col} = 0 AND ts <= ?
        ORDER BY ts ASC LIMIT ?
    """, (cutoff, limit))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_trade_outcome(transaction_id: int, window: str, pct_change: float):
    outcome_col = f"outcome_{window}_pct"
    evaluated_col = f"evaluated_{window}"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"""
        UPDATE trade_outcomes SET {outcome_col} = ?, {evaluated_col} = 1
        WHERE transaction_id = ?
    """, (pct_change, transaction_id))
    conn.commit()
    conn.close()


def mark_outcome_evaluated_unknown(transaction_id: int, window: str):
    """إذا ما قدرناش نجيب السعر الجديد (توكن اختفى من DexScreener مثلاً)، نعلموها مقيمة بلا رقم"""
    evaluated_col = f"evaluated_{window}"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"UPDATE trade_outcomes SET {evaluated_col} = 1 WHERE transaction_id = ?", (transaction_id,))
    conn.commit()
    conn.close()


def get_wallet_win_rate(wallet_address: str, window: str = "24h", min_sample: int = 1) -> dict:
    """
    🆕 Win-Rate حقيقي لمحفظة: نسبة الصفقات الرابحة + متوسط الربح/الخسارة،
    مبنية غير على صفقات تقيمو فعلياً (evaluated=1) لهاد النافذة.
    """
    outcome_col = f"outcome_{window}_pct"
    evaluated_col = f"evaluated_{window}"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT {outcome_col} as pct FROM trade_outcomes
        WHERE wallet_address = ? AND {evaluated_col} = 1 AND {outcome_col} IS NOT NULL
    """, (wallet_address,))
    rows = [r["pct"] for r in cur.fetchall()]
    conn.close()

    sample_size = len(rows)
    if sample_size < min_sample:
        return {"win_rate_pct": None, "avg_return_pct": None, "sample_size": sample_size}

    wins = sum(1 for pct in rows if pct > 0)
    win_rate = round((wins / sample_size) * 100, 1)
    avg_return = round(sum(rows) / sample_size, 1)
    return {"win_rate_pct": win_rate, "avg_return_pct": avg_return, "sample_size": sample_size}


def get_top_performers(window: str = "24h", min_sample: int = 5, limit: int = 10):
    """
    🆕 Leaderboard حقيقي: أفضل المحافظ حسب Win-Rate فعلي (ماشي عدد الصفقات).
    كنفلترو بـ min_sample باش ما نعطيوش ثقة زايدة لمحفظة عندها صفقة/صفقتين بالصدفة.
    """
    outcome_col = f"outcome_{window}_pct"
    evaluated_col = f"evaluated_{window}"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT wallet_address, {outcome_col} as pct FROM trade_outcomes
        WHERE {evaluated_col} = 1 AND {outcome_col} IS NOT NULL
    """)
    rows = cur.fetchall()
    conn.close()

    by_wallet = {}
    for r in rows:
        by_wallet.setdefault(r["wallet_address"], []).append(r["pct"])

    results = []
    wallets_index = {w["address"]: w["name"] for w in get_all_wallets()}
    for address, pct_list in by_wallet.items():
        if len(pct_list) < min_sample:
            continue
        wins = sum(1 for pct in pct_list if pct > 0)
        win_rate = round((wins / len(pct_list)) * 100, 1)
        avg_return = round(sum(pct_list) / len(pct_list), 1)
        results.append({
            "wallet_name": wallets_index.get(address, address[:6]),
            "wallet_address": address,
            "win_rate_pct": win_rate,
            "avg_return_pct": avg_return,
            "sample_size": len(pct_list),
        })

    results.sort(key=lambda x: x["win_rate_pct"], reverse=True)
    return results[:limit]


# ---------------- 🆕 ATH Tracking (منذ ما بدا البوت يتابع التوكن) ----------------

def update_token_ath(mint: str, mcap: float, price: float) -> dict:
    """
    كيسجل/يحدث أعلى Market Cap وصلها التوكن. كيرجع dict فيه ath_mcap الحالي
    (سواء تبدل دابا أو لا) باش نقدرو نحسبو "شحال طاح من القمة".
    """
    if not mcap or mcap <= 0:
        return None
    now = int(time.time())
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM token_ath WHERE mint = ?", (mint,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO token_ath (mint, ath_mcap, ath_price, ath_ts, first_seen_ts) VALUES (?, ?, ?, ?, ?)",
            (mint, mcap, price, now, now)
        )
        conn.commit()
        conn.close()
        return {"ath_mcap": mcap, "ath_price": price, "is_new_ath": True}

    result = {"ath_mcap": row["ath_mcap"], "ath_price": row["ath_price"], "is_new_ath": False}
    if mcap > row["ath_mcap"]:
        cur.execute(
            "UPDATE token_ath SET ath_mcap = ?, ath_price = ?, ath_ts = ? WHERE mint = ?",
            (mcap, price, now, mint)
        )
        conn.commit()
        result = {"ath_mcap": mcap, "ath_price": price, "is_new_ath": True}
    conn.close()
    return result


# ---------------- 🆕 Digest Queue (فلترة الضجة) ----------------

def add_to_digest_queue(wallet_name: str, symbol: str, mint: str, score: int, score_label: str, usd_value: float):
    """كيسجل صفقة 'ضعيفة' (Score تحت العتبة) باش تتبعث فملخص مجمّع بدل تنبيه فوري"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO digest_queue (wallet_name, symbol, mint, score, score_label, usd_value, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (wallet_name, symbol, mint, score, score_label, usd_value, int(time.time())))
    conn.commit()
    conn.close()


def get_and_clear_digest_queue():
    """كيجيب كل الصفقات المتجمعة فالـ Digest، ويفرغ الجدول (باش الدورة الجاية تبدا من الصفر)"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM digest_queue ORDER BY ts ASC")
    rows = [dict(r) for r in cur.fetchall()]
    cur.execute("DELETE FROM digest_queue")
    conn.commit()
    conn.close()
    return rows



    """كيسجل حدث تنبيه (whale / smart_money / multi_wallet) لهاد التوكن"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO signal_events (mint, event_type, ts) VALUES (?, ?, ?)",
        (mint, event_type, int(time.time()))
    )
    conn.commit()
    conn.close()


def get_signal_event_counts(mint: str) -> dict:
    """عدد كل نوع حدث (whale/smart_money/multi_wallet) بالنسبة لهاد التوكن"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT event_type, COUNT(*) as c FROM signal_events
        WHERE mint = ? GROUP BY event_type
    """, (mint,))
    rows = cur.fetchall()
    conn.close()
    counts = {"whale": 0, "smart_money": 0, "multi_wallet": 0}
    for r in rows:
        counts[r["event_type"]] = r["c"]
    return counts


def count_distinct_buyers_ever(mint: str) -> int:
    """🆕 إجمالي عدد المحافظ المختلفة (من بين المتابَعة) لي سبق وشرات هاد التوكن (Popularity)"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(DISTINCT wallet_address) as c FROM transactions
        WHERE mint = ? AND action = 'BUY'
    """, (mint,))
    row = cur.fetchone()
    conn.close()
    return row["c"]


def get_wallet_activity_data(address: str) -> dict:
    """🆕 بيانات خام لحساب Wallet Activity Score: عدد الصفقات، عدد التوكنات المختلفة، آخر نشاط"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) as total_trades,
               COUNT(DISTINCT mint) as distinct_mints,
               MAX(timestamp) as last_trade_ts
        FROM transactions WHERE wallet_address = ?
    """, (address,))
    row = cur.fetchone()
    conn.close()
    return {
        "total_trades": row["total_trades"] or 0,
        "distinct_mints": row["distinct_mints"] or 0,
        "last_trade_ts": row["last_trade_ts"] or 0,
    }

# ============================================================
# 📈 بيانات السوق + تصدير + نسخ احتياطي (utils.py سابقاً)
# ============================================================

@with_retry()
async def _fetch_dexscreener_raw(session: aiohttp.ClientSession, mint: str):
    """طلب خام لـ DexScreener (محمي بـ Retry/Backoff تلقائي)"""
    url = DEXSCREENER_URL.format(mint=mint)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


@with_retry(attempts=2, base_delay=1.0)
async def get_jupiter_price(session: aiohttp.ClientSession, mint: str):
    """
    🆕 مصدر بيانات مجاني ثاني (Jupiter Price API) — كنستعملوه لهدفين:
    1. Fallback: إذا DexScreener فشل أو ماعندوش التوكن
    2. Cross-Check: نقارنو السعر مع DexScreener باش نتأكدو أن المعلومة صحيحة
       وموثوقة (إذا الفرق بينهم كبير، معناها شي مصدر فيه خطأ/تأخر)
    """
    try:
        params = {"ids": mint}
        async with session.get(JUPITER_PRICE_URL, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            price_info = (data.get("data") or {}).get(mint)
            if not price_info:
                return None
            return float(price_info.get("price") or 0) or None
    except Exception:
        return None


async def get_token_market_data(session: aiohttp.ClientSession, mint: str):
    """
    كيجيب معلومات السوق ديال توكن من DexScreener (مجاني، بلا API key):
    السعر، Market Cap، Liquidity، حجم التداول، الرمز، وعمر التوكن.

    🆕 محسّن: كيستعمل Cache (30 ثانية) باش ما يعاودش نفس الطلب لنفس التوكن
    فوقت قصير، و Retry تلقائي عند فشل الشبكة.
    """
    cached = dexscreener_cache.get(mint)
    if cached is not None:
        return cached

    data = await _fetch_dexscreener_raw(session, mint)
    if not data:
        return None

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    # ناخدو الـ Pair اللي عندو أكبر Liquidity (أدق تمثيل للسعر الحقيقي)
    best_pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))

    # 🆕 عمر التوكن (Token Age) — من pairCreatedAt (ميلي ثانية)، مجاني بلا API إضافي
    token_age_str = "غير معروف"
    created_at_ms = best_pair.get("pairCreatedAt")
    if created_at_ms:
        age_seconds = time.time() - (created_at_ms / 1000)
        if age_seconds < 3600:
            token_age_str = f"{int(age_seconds // 60)} دقيقة"
        elif age_seconds < 86400:
            token_age_str = f"{int(age_seconds // 3600)} ساعة"
        else:
            token_age_str = f"{int(age_seconds // 86400)} يوم"

    # 🆕 روابط اجتماعية (Twitter/Telegram/Website) — مجانية عند DexScreener،
    # ماشي كل التوكنات عندها (خصوصاً التوكنات الجديدة بزاف أو المشبوهة)
    info_block = best_pair.get("info") or {}
    socials = info_block.get("socials") or []
    websites = info_block.get("websites") or []
    twitter_url = next((s.get("url") for s in socials if s.get("type") == "twitter"), None)
    telegram_url = next((s.get("url") for s in socials if s.get("type") == "telegram"), None)
    website_url = websites[0].get("url") if websites else None

    result = {
        "symbol": best_pair.get("baseToken", {}).get("symbol", "?"),
        "price_usd": float(best_pair.get("priceUsd") or 0),
        "liquidity_usd": float(best_pair.get("liquidity", {}).get("usd") or 0),
        "market_cap": float(best_pair.get("fdv") or 0),
        "volume_24h": float(best_pair.get("volume", {}).get("h24") or 0),
        "dexscreener_url": best_pair.get("url", f"https://dexscreener.com/solana/{mint}"),
        "token_age": token_age_str,
        "price_change_1h": float(best_pair.get("priceChange", {}).get("h1") or 0),
        "price_change_24h": float(best_pair.get("priceChange", {}).get("h24") or 0),
        # 🆕 صورة/شعار العملة (إذا كانت متوفرة عند DexScreener — ماشي كل التوكنات عندها)
        "image_url": info_block.get("imageUrl"),
        # 🆕 من وين التوكن (Raydium, PumpFun, Meteora...) — باش نزيدو روابط منصات
        # حقيقية بس (مثلاً Pump.fun غير إذا التوكن فعلاً منها، بلا رابط ميت)
        "dex_id": (best_pair.get("dexId") or "").lower(),
        # 🆕 حضور اجتماعي — مؤشر جودة/جدية إضافي (توكن بلا Twitter/Telegram = علامة استفهام)
        "twitter_url": twitter_url,
        "telegram_url": telegram_url,
        "website_url": website_url,
        # 🆕 نسبة الشراء/البيع (Buy/Sell Pressure) — من نفس استجابة DexScreener
        # لي عندنا ديجا، بلا أي طلب إضافي. مؤشر قوي بزاف فثقافة الميم كوينز.
        "buys_h1": int((best_pair.get("txns", {}).get("h1") or {}).get("buys") or 0),
        "sells_h1": int((best_pair.get("txns", {}).get("h1") or {}).get("sells") or 0),
    }
    dexscreener_cache.set(mint, result)
    return result


def export_wallets_csv(wallets: list, filepath: str):
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "address", "date_added"])
        for w in wallets:
            writer.writerow([w["name"], w["address"], w["date_added"]])
    return filepath


def export_transactions_json(transactions: list, filepath: str):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(transactions, f, ensure_ascii=False, indent=2)
    return filepath


def create_backup(filepath: str):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"backup_{timestamp}.db"
    shutil.copy(DATABASE_NAME, backup_path)
    return backup_path


# ============================================================
# 🛡️ Rug-Pull Risk Check (فحص أمان العقد) — بلا أي API مدفوع
# ============================================================
# فكرة: نفس المنطق لي كتستعملو أدوات بحال RugCheck.xyz أو GMGN، لكن مبني
# غير على Solana RPC العمومي (نفس الـ endpoint ديال Helius لي عندك ديجا،
# بلا تكلفة إضافية). كنفحصو:
# 1. Mint Authority: واش المطور مازال يقدر "يطبع" توكنات جداد بلا حدود؟
# 2. Freeze Authority: واش يقدر يجمد أي محفظة (بما فيها ديالك)؟
# 3. Holder Concentration: واش Top 10 حاملين عندهم نسبة كبيرة (خطر تلاعب)؟

HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
rug_check_cache = SimpleTTLCache(ttl_seconds=21600)  # 🔧 6 ساعات بدل 10 دقائق — Mint/Freeze Authority ما كيتبدلوش أصلاً بعد الإطلاق، فبلا داعي نعاودو النداء (3 RPC calls) كل شوية لنفس التوكن


@with_retry()
async def _solana_rpc_call(session: aiohttp.ClientSession, method: str, params: list):
    """نداء عام لأي دالة Solana JSON-RPC (getAccountInfo, getTokenLargestAccounts, getTokenSupply...)"""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(HELIUS_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        return data.get("result")


async def get_token_authorities(session: aiohttp.ClientSession, mint: str) -> dict:
    """
    كيجيب Mint Authority و Freeze Authority مباشرة من الـ Mint Account
    (jsonParsed كيعطيهم جاهزين بلا ما نحتاج نفسر Bytes يدوياً).

    🔧 محسّنة: نفس النداء كيعطي زادة الـ Supply (parsed.info.supply/decimals)
    — يعني ماعادش خاصنا نداء getTokenSupply منفصل (توفير 33% من النداءات
    ديال Rug Check: من 3 لـ 2).
    """
    result = await _solana_rpc_call(
        session, "getAccountInfo", [mint, {"encoding": "jsonParsed"}]
    )
    if not result or not result.get("value"):
        return {"mint_authority": None, "freeze_authority": None, "found": False, "total_supply_ui": None}

    try:
        parsed_info = result["value"]["data"]["parsed"]["info"]
        raw_supply = parsed_info.get("supply")
        decimals = parsed_info.get("decimals", 0)
        total_supply_ui = (float(raw_supply) / (10 ** decimals)) if raw_supply is not None else None
        return {
            "mint_authority": parsed_info.get("mintAuthority"),
            "freeze_authority": parsed_info.get("freezeAuthority"),
            "found": True,
            "total_supply_ui": total_supply_ui,
        }
    except (KeyError, TypeError, ValueError):
        return {"mint_authority": None, "freeze_authority": None, "found": False, "total_supply_ui": None}


async def get_holder_concentration(session: aiohttp.ClientSession, mint: str, total_supply_ui: float) -> float:
    """
    نسبة (%) اللي كيملكوها أكبر 10 حاملين من إجمالي الـ Supply.
    نسبة عالية = تركيز خطير (شخص وحد أو مجموعة صغيرة يقدرو يهبطو السعر بصفقة وحدة).

    🔧 محسّنة: total_supply_ui كتجي جاهزة من get_token_authorities (بلا نداء
    getTokenSupply منفصل) — توفير كريدي Helius.
    """
    if not total_supply_ui or total_supply_ui == 0:
        return None

    largest_result = await _solana_rpc_call(session, "getTokenLargestAccounts", [mint])
    if not largest_result:
        return None

    try:
        top_accounts = largest_result.get("value", [])[:10]
        top_10_sum = sum(float(acc.get("uiAmount") or 0) for acc in top_accounts)
        return round((top_10_sum / total_supply_ui) * 100, 1)
    except (KeyError, TypeError, ZeroDivisionError):
        return None


async def get_rug_check_data(session: aiohttp.ClientSession, mint: str) -> dict:
    """
    🆕 الدالة الرئيسية: كتجمع Mint/Freeze Authority + تركيز الحاملين، مع Cache
    (6 ساعات) باش ما نبقاوش نطلبو نفس التوكن كل شوية.

    🔧 محسّنة: 2 نداءات RPC بدل 3 (الـ Supply كتجي من نفس نداء Authorities).
    """
    cached = rug_check_cache.get(mint)
    if cached is not None:
        return cached

    authorities = await get_token_authorities(session, mint)
    holder_concentration = await get_holder_concentration(session, mint, authorities.get("total_supply_ui"))

    result = {
        "mint_authority_active": bool(authorities.get("mint_authority")),
        "freeze_authority_active": bool(authorities.get("freeze_authority")),
        "authorities_found": authorities.get("found", False),
        "top10_holder_pct": holder_concentration,  # None إذا فشل الجلب
    }
    rug_check_cache.set(mint, result)
    return result


def calculate_rug_risk(rug_data: dict) -> tuple:
    """
    كيحول بيانات الفحص لـ (risk_score من 100 — كلما زاد كلما خطر أكبر، risk_label، warnings list).
    منطق بسيط وواضح، بلا "AI" — غير قواعد صريحة كل واحدة عندها وزنها.
    """
    risk_score = 0
    warnings = []

    if not rug_data.get("authorities_found"):
        # ما قدرناش نتحقق — كنعتبروها معلومة ناقصة، ماشي "آمنة تلقائياً"
        warnings.append("⚠️ ما قدرناش نتحقق من Authorities (بيانات غير متوفرة)")
        risk_score += 15

    if rug_data.get("mint_authority_active"):
        warnings.append("🔴 Mint Authority مازال نشيط — المطور يقدر يطبع توكنات جداد بلا حدود")
        risk_score += 40

    if rug_data.get("freeze_authority_active"):
        warnings.append("🔴 Freeze Authority مازال نشيط — المطور يقدر يجمد أي محفظة")
        risk_score += 30

    top10 = rug_data.get("top10_holder_pct")
    if top10 is not None:
        if top10 >= 70:
            warnings.append(f"🔴 تركيز خطير: Top 10 حاملين عندهم {top10}% من الـ Supply")
            risk_score += 30
        elif top10 >= 40:
            warnings.append(f"🟡 تركيز متوسط: Top 10 حاملين عندهم {top10}% من الـ Supply")
            risk_score += 15

    risk_score = min(risk_score, 100)

    if risk_score >= 60:
        label = "🔴 خطر عالي"
    elif risk_score >= 30:
        label = "🟡 خطر متوسط"
    else:
        label = "🟢 خطر منخفض"

    return risk_score, label, warnings


# ============================================================
# 🧠 نظام التقييم الذكي (scoring.py سابقاً) — Phase 3
# ============================================================
# نظام Score كامل من 100، مبني فقط على بيانات مجانية موجودة عندنا حالياً
# (بلا أي API مدفوع): Liquidity, Market Cap, Volume, عدد المحافظ المتابَعة
# اللي شرات التوكن (Popularity)، وعدد أحداث Whale/Smart Money/Multi Wallet.
#
# الأوزان (Weights) — قابلة للتعديل بسهولة هنا فمكان واحد:
SCORE_WEIGHT_LIQUIDITY = 25      # كل ما زادت السيولة، كل ما قل احتمال الـ Rug
SCORE_WEIGHT_VOLUME = 20         # حجم التداول = اهتمام حقيقي فالسوق
SCORE_WEIGHT_POPULARITY = 20     # عدد المحافظ المتابَعة لي شراتو (كلما زاد كلما قوي)
SCORE_WEIGHT_SMART_MONEY = 15    # أحداث Smart Money (وزن كبير، مؤشر قوي)
SCORE_WEIGHT_WHALE = 10          # أحداث Whale
SCORE_WEIGHT_MULTI_WALLET = 10   # أحداث Multi Wallet


def calculate_token_score(market_data: dict, distinct_buyers_ever: int, signal_counts: dict) -> tuple:
    """
    🆕 كيحسب Token Score من 100 ويرجع (score, label).
    label من بين: "Low Interest", "Medium Interest", "High Interest", "Extreme Interest"
    """
    liquidity = market_data["liquidity_usd"] if market_data else 0
    volume = market_data["volume_24h"] if market_data else 0

    liquidity_pts = min(SCORE_WEIGHT_LIQUIDITY, (liquidity / 50000) * SCORE_WEIGHT_LIQUIDITY)
    volume_pts = min(SCORE_WEIGHT_VOLUME, (volume / 100000) * SCORE_WEIGHT_VOLUME)
    popularity_pts = min(SCORE_WEIGHT_POPULARITY, (distinct_buyers_ever / 5) * SCORE_WEIGHT_POPULARITY)
    smart_money_pts = min(SCORE_WEIGHT_SMART_MONEY, signal_counts.get("smart_money", 0) * (SCORE_WEIGHT_SMART_MONEY / 2))
    whale_pts = min(SCORE_WEIGHT_WHALE, signal_counts.get("whale", 0) * (SCORE_WEIGHT_WHALE / 2))
    multi_wallet_pts = min(SCORE_WEIGHT_MULTI_WALLET, signal_counts.get("multi_wallet", 0) * (SCORE_WEIGHT_MULTI_WALLET / 2))

    total = liquidity_pts + volume_pts + popularity_pts + smart_money_pts + whale_pts + multi_wallet_pts
    score = round(min(100, total))

    if score >= 76:
        label = "Extreme Interest"
    elif score >= 55:
        label = "High Interest"
    elif score >= 30:
        label = "Medium Interest"
    else:
        label = "Low Interest"

    return score, label


def score_label_emoji(label: str) -> str:
    return {
        "Extreme Interest": "🟣🔥",
        "High Interest": "🟢🔥",
        "Medium Interest": "🟡",
        "Low Interest": "⚪️",
    }.get(label, "⚪️")


# ---- Wallet Activity Score ----
WALLET_SCORE_WEIGHT_VOLUME = 40     # كثرة الصفقات = نشاط عالي
WALLET_SCORE_WEIGHT_DIVERSITY = 30  # عدد توكنات مختلفة تعامل بيهم
WALLET_SCORE_WEIGHT_RECENCY = 30    # آخر نشاط قريب = محفظة "حية" دابا


def calculate_wallet_activity_score(activity_data: dict) -> tuple:
    """
    🆕 كيحسب Wallet Activity Score من 100 ويرجع (score, label)
    label من بين: "Inactive", "Low Activity", "Active", "Very Active"
    """
    total_trades = activity_data["total_trades"]
    distinct_mints = activity_data["distinct_mints"]
    last_trade_ts = activity_data["last_trade_ts"]

    volume_pts = min(WALLET_SCORE_WEIGHT_VOLUME, (total_trades / 20) * WALLET_SCORE_WEIGHT_VOLUME)
    diversity_pts = min(WALLET_SCORE_WEIGHT_DIVERSITY, (distinct_mints / 6) * WALLET_SCORE_WEIGHT_DIVERSITY)

    if last_trade_ts:
        days_since = (time.time() - last_trade_ts) / 86400
        if days_since <= 1:
            recency_pts = WALLET_SCORE_WEIGHT_RECENCY
        elif days_since <= 7:
            recency_pts = WALLET_SCORE_WEIGHT_RECENCY * 0.66
        elif days_since <= 30:
            recency_pts = WALLET_SCORE_WEIGHT_RECENCY * 0.33
        else:
            recency_pts = 0
    else:
        recency_pts = 0

    total = volume_pts + diversity_pts + recency_pts
    score = round(min(100, total))

    if score >= 70:
        label = "Very Active"
    elif score >= 40:
        label = "Active"
    elif score >= 15:
        label = "Low Activity"
    else:
        label = "Inactive"

    return score, label

# ============================================================
# 🗣️ محرك السرد الذكي (Smart Narrative Engine) — بلا AI حقيقي، بلا API مدفوع
# ============================================================
# الفكرة: بدل ما نبعتو غير أرقام (سعر/كمية)، البوت كيصيغ جملة "ذكية" مبنية
# على منطق شرطي (IF/ELSE) + بنك جمل متنوع (باش ما يبانش نفس الجملة مكررة) +
# مقارنة مع باقي التوكنات ديال اليوم + كشف تسارع/تباطؤ + سطر "تفسير آلي"
# كيدمج كل الإشارات المتوفرة (Whale/Smart Money/Liquidity) فجملة واحدة.
# كلشي هنا مبني فوق بيانات عندنا ديجا فقاعدة البيانات — بلا أي مصدر جديد.

# ---- بنك الجمل (Phrase Pools) — كل تصنيف عندو عدة جمل، كيتختار وحدة عشوائياً ----
PHRASE_POOLS = {
    "explosive_up": [
        "🚀🔥 انفجار كبير! {token} طار {change:+.0f}% خلال {window}",
        "🚀🔥 {token} فجّر السعر: {change:+.0f}% فآخر {window} فقط",
        "🔥 حركة نارية على {token} — {change:+.0f}% خلال {window}",
    ],
    "strong_up": [
        "🚀 {token} كيطلع بقوة ({change:+.0f}%) خلال {window}",
        "📈 {token} فحركة صاعدة قوية: {change:+.0f}% فآخر {window}",
        "🚀 زخم واضح على {token} — {change:+.0f}% خلال {window}",
    ],
    "mild_up": [
        "📈 {token} كيطلع بشكل مزيان ({change:+.0f}%) خلال {window}",
        "🟢 {token} فحركة إيجابية هادئة ({change:+.0f}%)",
    ],
    "strong_down": [
        "📉💀 {token} طاح بزاف ({change:+.0f}%) خلال {window} — احذر",
        "🔻 هبوط قوي على {token}: {change:+.0f}% خلال {window}",
    ],
    "mild_down": [
        "📉 {token} كيهبط ({change:+.0f}%) خلال {window}",
        "🔻 حركة سلبية خفيفة على {token} ({change:+.0f}%)",
    ],
    "flat": [
        "➖ {token} مستقر تقريباً ({change:+.1f}%)",
        "⏸️ ماكاين حركة تذكر فـ {token} ({change:+.1f}%)",
    ],
}


def pick_phrase(category: str, **kwargs) -> str:
    """كيختار جملة عشوائية من البنك حسب التصنيف، ويعبي فيها القيم"""
    templates = PHRASE_POOLS.get(category, ["{token}: {change:+.1f}%"])
    template = random.choice(templates)
    return template.format(**kwargs)


def classify_change(change_pct: float) -> str:
    if change_pct >= 200:
        return "explosive_up"
    elif change_pct >= 50:
        return "strong_up"
    elif change_pct >= 10:
        return "mild_up"
    elif change_pct <= -50:
        return "strong_down"
    elif change_pct <= -10:
        return "mild_down"
    else:
        return "flat"


def get_token_buy_rank_today(mint: str) -> tuple:
    """
    🆕 مقارنة (Comparative Context): رتبة هاد التوكن اليوم من حيث عدد صفقات
    الشراء، مقارنة ببقية التوكنات لي تعامل معاهم البوت نفس اليوم.
    كيرجع (rank, total_tokens_today) — مثلاً (1, 12) يعني "الأول من أصل 12".
    """
    now = int(time.time())
    today_start = now - (now % 86400)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT mint, COUNT(*) as c FROM transactions
        WHERE action = 'BUY' AND timestamp >= ?
        GROUP BY mint ORDER BY c DESC
    """, (today_start,))
    rows = cur.fetchall()
    conn.close()
    mints_ordered = [r["mint"] for r in rows]
    total = len(mints_ordered)
    if mint in mints_ordered:
        return mints_ordered.index(mint) + 1, total
    return None, total


def build_acceleration_note(buyers_windows: dict) -> str:
    """
    🆕 كشف تسارع/تباطؤ: كيقارن معدل الشراء فآخر 5 دقائق مع معدل آخر 15/60
    دقيقة، باش يشوف واش الاهتمام كيسرع (مؤشر قوي) ولا كيبرد.
    """
    b5, b15, b60 = buyers_windows.get("5m", 0), buyers_windows.get("15m", 0), buyers_windows.get("60m", 0)

    rate_5m = b5 / 5
    rate_15m = b15 / 15 if b15 else 0
    rate_60m = b60 / 60 if b60 else 0

    if b5 == 0:
        return ""

    if rate_15m > 0 and rate_5m >= rate_15m * 1.8:
        return "⚡ الوتيرة كتسارع بوضوح مقارنة بآخر ربع ساعة"
    elif rate_60m > 0 and rate_5m >= rate_60m * 2.5:
        return "⚡ اهتمام متسارع مقارنة بمتوسط الساعة الأخيرة"
    elif b15 > 0 and b5 == 0:
        return "🐢 الوتيرة بدات كتبرد مقارنة بآخر ربع ساعة"
    return ""


def build_reasoning_line(signal_counts: dict, market_data: dict, buyers_windows: dict) -> str:
    """
    🆕 سطر "تفسير آلي" (Auto-Reasoning): كيدمج كل الإشارات المتوفرة (Whale,
    Smart Money, Liquidity, عدد المشترين) فجملة وحدة كتفسر "علاش" التوكن
    كيتحرك، بحال تحليل بشري مختصر — بلا AI، غير دمج شروط.
    """
    reasons = []
    if signal_counts.get("whale", 0) > 0:
        reasons.append(f"{signal_counts['whale']} صفقة Whale")
    if signal_counts.get("smart_money", 0) > 0:
        reasons.append("اتفاق Smart Money")
    if buyers_windows.get("5m", 0) >= 3:
        reasons.append(f"{buyers_windows['5m']} مشترين فآخر 5 دقايق")

    liquidity = market_data["liquidity_usd"] if market_data else 0
    if liquidity < 5000:
        reasons.append("⚠️ Liquidity ضعيفة (حذر)")

    if not reasons:
        return ""
    return "🧩 السبب المحتمل: " + " + ".join(reasons)


def build_smart_narrative(symbol: str, mint: str, change_pct: float, window: str,
                           buyers_windows: dict, signal_counts: dict, market_data: dict) -> str:
    """
    🆕 الدالة الرئيسية: كتجمع كل مكونات السرد الذكي فقطعة نص واحدة جاهزة
    للإدراج فرسالة التنبيه أو الملخص اليومي:
    1. جملة أساسية (من بنك الجمل المتنوع)
    2. مقارنة مع باقي التوكنات ديال اليوم
    3. ملاحظة تسارع/تباطؤ
    4. سطر تفسير آلي (دمج الإشارات)
    """
    category = classify_change(change_pct)
    base_line = pick_phrase(category, token=symbol, change=change_pct, window=window)

    lines = [base_line]

    rank, total = get_token_buy_rank_today(mint)
    if rank == 1 and total > 1:
        lines.append(f"🏅 أقوى أداء اليوم من حيث نشاط الشراء (من أصل {total} توكن)")
    elif rank and rank <= 3 and total > 3:
        lines.append(f"🏅 من بين أفضل 3 توكنات اليوم من حيث النشاط (#{rank} من {total})")

    accel_note = build_acceleration_note(buyers_windows)
    if accel_note:
        lines.append(accel_note)

    reasoning = build_reasoning_line(signal_counts, market_data, buyers_windows)
    if reasoning:
        lines.append(reasoning)

    return "\n".join(lines)

# ============================================================
# 🔍 المراقبة والتنبيهات (monitor.py سابقاً)
# ============================================================
# 🆕 التغيير الأهم فهاد النسخة: فحص المحافظ بالتوازي (async) بدل واحد بواحد،
# مع Semaphore باش نحترمو حدود الـ API المجاني، و try/except شامل على مستوى
# كل محفظة باش غلطة وحدة ما توقفش الفحص ديال الباقي.

wallet_check_semaphore = asyncio.Semaphore(MAX_CONCURRENT_WALLET_CHECKS)


class RateLimitedError(Exception):
    """🆕 استثناء خاص بـ 429 (Rate Limit) — كيخلي with_retry يعاود المحاولة بدل ما نستسلمو بصمت"""
    pass


@with_retry(attempts=4, base_delay=2.0)
async def fetch_transactions(session: aiohttp.ClientSession, address: str):
    url = HELIUS_URL.format(address=address)
    params = {"api-key": HELIUS_API_KEY, "limit": 10}
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status == 429:
            # 🔧 إصلاح مهم: قبل كان الكود كيرجع [] بصمت عند 429 (Rate Limit)
            # — يعني البوت "يعمى" على المحفظة هاد الدورة كاملة بلا ما يعاود
            # المحاولة. دابا كنرفعو استثناء خاص باش with_retry يعاود المحاولة
            # مع Backoff (ويحترم Retry-After إذا Helius بعتاه).
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                await asyncio.sleep(min(float(retry_after), 10))
            raise RateLimitedError(f"429 Rate Limited للمحفظة {address}")
        if resp.status != 200:
            logger.warning(f"Helius API رجع status {resp.status} للمحفظة {address}")
            return []
        return await resp.json()



def parse_transaction(tx: dict, wallet_address: str):
    """كيحدد نوع العملية (Buy/Sell) والتوكن المعني والكمية"""
    signature = tx.get("signature")
    token_transfers = tx.get("tokenTransfers") or []

    if not token_transfers:
        return None

    for transfer in token_transfers:
        from_addr = transfer.get("fromUserAccount")
        to_addr = transfer.get("toUserAccount")
        mint = transfer.get("mint")
        amount = transfer.get("tokenAmount")

        if to_addr == wallet_address:
            action = "BUY"
        elif from_addr == wallet_address:
            action = "SELL"
        else:
            continue

        return {
            "signature": signature,
            "action": action,
            "mint": mint,
            "amount": amount,
            "timestamp": tx.get("timestamp") or int(time.time()),
        }

    return None


async def get_target_chat_ids(admin_chat_id: str) -> list:
    """الأدمن + جميع المشاهدين"""
    chat_ids = [admin_chat_id]
    for sub in get_all_subscribers():
        chat_ids.append(sub["chat_id"])
    return chat_ids


async def broadcast(bot, admin_chat_id: str, text: str):
    for chat_id in await get_target_chat_ids(admin_chat_id):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"خطأ فإرسال رسالة لـ {chat_id}: {e}")


async def broadcast_with_image(bot, admin_chat_id: str, text: str, image_url: str = None):
    """
    🆕 كتبعت الرسالة مع صورة العملة (إذا متوفرة) كـ Caption. تيليكرام كيحدد
    الـ Caption بـ 1024 حرف كحد أقصى — إذا النص طويل، كنبعتو الصورة وحدها
    وبعدها النص كامل فرسالة نصية عادية (بلا ما نفقدو أي معلومة).
    """
    if not image_url:
        await broadcast(bot, admin_chat_id, text)
        return

    for chat_id in await get_target_chat_ids(admin_chat_id):
        try:
            if len(text) <= 1024:
                await bot.send_photo(
                    chat_id=chat_id, photo=image_url, caption=text, parse_mode="HTML"
                )
            else:
                await bot.send_photo(chat_id=chat_id, photo=image_url)
                await bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True
                )
        except Exception as e:
            # 🆕 إذا فشلت الصورة لأي سبب (رابط معطل مثلاً)، على الأقل نبعتو النص
            logger.error(f"❌ خطأ فإرسال صورة العملة لـ {chat_id}: {e} — نبعتو النص بلا صورة")
            try:
                await bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True
                )
            except Exception as e2:
                logger.error(f"❌ فشل حتى إرسال النص لـ {chat_id}: {e2}")


def build_trade_message(wallet_name, wallet_address, parsed, market_data, buyers_windows: dict,
                         score: int = None, score_label: str = None, signal_counts: dict = None,
                         rug_data: dict = None, ath_data: dict = None) -> tuple:
    """
    🆕 رسالة احترافية كاملة: Token Score، Rug Check، Smart Narrative، ATH
    Tracking (شحال طاح/طلع من القمة)، Buy/Sell Pressure، حضور اجتماعي
    (Twitter/Telegram/Website)، Graduation Status (Pump.fun)، وروابط موسّعة.
    """
    action = parsed["action"]
    mint = parsed["mint"]
    amount = parsed["amount"]

    short_address = f"{wallet_address[:6]}...{wallet_address[-4:]}"
    symbol = market_data["symbol"] if market_data else "?"
    price = market_data["price_usd"] if market_data else 0
    liquidity = market_data["liquidity_usd"] if market_data else 0
    mcap = market_data["market_cap"] if market_data else 0
    volume_24h = market_data["volume_24h"] if market_data else 0
    token_age = market_data["token_age"] if market_data else "غير معروف"
    usd_value = (amount or 0) * price

    dexscreener = market_data["dexscreener_url"] if market_data else f"https://dexscreener.com/solana/{mint}"
    solscan = f"https://solscan.io/token/{mint}"
    photon = f"https://photon-sol.tinyastro.io/en/lp/{mint}"
    gmgn = f"https://gmgn.ai/sol/token/{mint}"
    # 🆕 منصات إضافية مرتبطة بميم كوينز Solana
    birdeye = f"https://birdeye.so/token/{mint}?chain=solana"
    jupiter = f"https://jup.ag/swap/SOL-{mint}"
    axiom = f"https://axiom.trade/t/{mint}"
    # 🆕 Pump.fun غير إذا التوكن فعلاً طالع من Pump.fun/PumpSwap (بلا رابط ميت لتوكنات Raydium/Orca عادية)
    dex_id = (market_data.get("dex_id") or "") if market_data else ""
    pumpfun_line = f"<a href='https://pump.fun/{mint}'>Pump.fun</a> · " if "pump" in dex_id else ""

    # 🆕 Graduation Status — معلومة أساسية فثقافة ميم كوينز Solana: واش
    # التوكن مازال فـ Bonding Curve (خطر عالي، سيولة صناعية) ولا "تخرج"
    # (Graduated) لـ AMM حقيقي (Raydium/PumpSwap) بسيولة حقيقية
    graduation_line = ""
    if dex_id == "pumpfun":
        graduation_line = "🌱 <b>الحالة:</b> مازال فـ Bonding Curve (Pump.fun) — قبل التخرج\n"
    elif dex_id == "pumpswap":
        graduation_line = "🎓 <b>الحالة:</b> تخرج من Pump.fun (Graduated) → PumpSwap\n"

    # 🆕 ATH Tracking — شحال طاح/طلع من أعلى قمة وصلها منذ ما بدا البوت يتابعها
    ath_line = ""
    if ath_data:
        if ath_data.get("is_new_ath"):
            ath_line = "🚀 <b>قمة سعرية جديدة (ATH)</b> منذ ما بدا التتبع!\n"
        elif ath_data.get("ath_mcap"):
            drawdown = ((mcap - ath_data["ath_mcap"]) / ath_data["ath_mcap"]) * 100 if ath_data["ath_mcap"] > 0 else 0
            ath_line = f"📊 <b>من القمة (ATH):</b> {drawdown:+.1f}% (أعلى MC: ${ath_data['ath_mcap']:,.0f})\n"

    # 🆕 Buy/Sell Pressure (آخر ساعة) — من نفس بيانات DexScreener، بلا نداء إضافي
    pressure_line = ""
    if market_data:
        buys_h1 = market_data.get("buys_h1", 0)
        sells_h1 = market_data.get("sells_h1", 0)
        total_h1 = buys_h1 + sells_h1
        if total_h1 > 0:
            buy_pct = (buys_h1 / total_h1) * 100
            pressure_line = f"⚖️ <b>ضغط الشراء (1h):</b> {buys_h1} شراء / {sells_h1} بيع ({buy_pct:.0f}% شراء)\n"

    # 🆕 حضور اجتماعي — Twitter/Telegram/Website (من نفس بيانات DexScreener)
    social_links = []
    if market_data:
        if market_data.get("twitter_url"):
            social_links.append(f"<a href='{market_data['twitter_url']}'>🐦 Twitter</a>")
        if market_data.get("telegram_url"):
            social_links.append(f"<a href='{market_data['telegram_url']}'>📱 Telegram</a>")
        if market_data.get("website_url"):
            social_links.append(f"<a href='{market_data['website_url']}'>🌍 Website</a>")
    social_line = f"🔗 <b>حضور اجتماعي:</b> {' · '.join(social_links)}\n" if social_links else "⚠️ <b>بلا حضور اجتماعي</b> (بلا Twitter/Telegram/Website)\n"

    tx_time = datetime.fromtimestamp(parsed["timestamp"]).strftime("%H:%M:%S")
    divider = "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️"

    score_line = ""
    if score is not None:
        score_line = f"🧠 <b>Token Score:</b> {score}/100 {score_label_emoji(score_label)} ({score_label})\n"

    # 🆕 سطر تحذير الأمان (Rug Check) — كيبان غير إذا الخطر متوسط/عالي
    rug_line = ""
    if rug_data is not None:
        risk_score, risk_label, warnings = calculate_rug_risk(rug_data)
        if risk_score >= 30:
            top_warning = warnings[0] if warnings else ""
            rug_line = f"🛡️ <b>فحص الأمان:</b> {risk_label} ({risk_score}/100)\n   ⚠️ {top_warning}\n"
        else:
            rug_line = f"🛡️ <b>فحص الأمان:</b> {risk_label} ({risk_score}/100) ✅\n"

    # 🆕 السرد الذكي (Smart Narrative)
    narrative_block = ""
    if action == "BUY" and market_data and signal_counts is not None:
        change_pct = market_data.get("price_change_1h") or market_data.get("price_change_24h") or 0
        window_label = "ساعة" if market_data.get("price_change_1h") else "24 ساعة"
        narrative = build_smart_narrative(
            symbol, mint, change_pct, window_label, buyers_windows, signal_counts, market_data
        )
        if narrative:
            narrative_block = f"💬 <i>{narrative}</i>\n{divider}\n"

    # 🆕 سطر "ملخص سريع" (Quick Glance) — قرار فثانية وحدة بلا ما تحتاج تقرا
    # كامل الرسالة: Score + حالة الأمان + اتجاه الضغط، كلشي فسطر واحد
    quick_risk_emoji = "✅"
    if rug_data is not None:
        _rs, _rl, _ = calculate_rug_risk(rug_data)
        quick_risk_emoji = "🔴" if _rs >= 60 else ("🟡" if _rs >= 30 else "✅")
    quick_score_part = f"{score}/100 {score_label_emoji(score_label)}" if score is not None else "—"
    quick_glance = f"⚡ <b>{quick_score_part} | أمان {quick_risk_emoji}</b>\n"

    text = (
        f"🟢 <b>BUY</b> — 🪙 <b>{symbol}</b>\n"
        f"🕒 {tx_time}\n"
        f"{quick_glance}"
        f"{divider}\n"
        f"{score_line}"
        f"{rug_line}"
        f"{graduation_line}"
        f"{ath_line}"
        f"{pressure_line}"
        f"{social_line}"
        f"{divider}\n"
        f"{narrative_block}"
        f"💼 <b>المحفظة:</b> {wallet_name}\n"
        f"📍 <code>{short_address}</code>\n"
        f"🪙 <b>Contract:</b> <code>{mint}</code>\n"
        f"{divider}\n"
        f"🔢 <b>الكمية:</b> {amount}\n"
        f"💵 <b>القيمة:</b> ${usd_value:,.2f}\n"
        f"💲 <b>السعر:</b> ${price:.8f}\n"
        f"🧢 <b>Market Cap:</b> ${mcap:,.0f}\n"
        f"💧 <b>Liquidity:</b> ${liquidity:,.0f}\n"
        f"📈 <b>Volume 24h:</b> ${volume_24h:,.0f}\n"
        f"⏳ <b>عمر التوكن:</b> {token_age}\n"
        f"👥 <b>مشترين (5د/15د/60د):</b> {buyers_windows.get('5m', 0)} / {buyers_windows.get('15m', 0)} / {buyers_windows.get('60m', 0)}\n"
        f"{divider}\n"
        f"🔗 <a href='{dexscreener}'>DexScreener</a> · "
        f"<a href='{birdeye}'>Birdeye</a> · "
        f"<a href='{axiom}'>Axiom</a>\n"
        f"🔄 <a href='{jupiter}'>Jupiter (Swap)</a> · "
        f"{pumpfun_line}"
        f"<a href='{photon}'>Photon</a> · "
        f"<a href='{gmgn}'>GMGN</a> · "
        f"<a href='{solscan}'>Solscan</a>\n"
        f"{divider}\n"
        f"🦇 <i>Batdex Pro</i>"
    )
    return text, usd_value, symbol


def build_runtime_settings() -> dict:
    """🆕 يبني dict الإعدادات جاهز للاستعمال (Polling و Webhook كيستعملو نفس الدالة)"""
    raw_settings = get_all_settings()
    return {
        "notify_buy": raw_settings.get("notify_buy", "1") == "1",
        "notify_sell": raw_settings.get("notify_sell", "1") == "1",
        "min_usd_alert": float(raw_settings.get("min_usd_alert", 0)),
        "whale_threshold": float(raw_settings.get("whale_usd_threshold", 10000)),
        "smart_money_min": int(raw_settings.get("smart_money_min_wallets", 2)),
        "multi_wallet_min": int(raw_settings.get("multi_wallet_min", 3)),
        "window_minutes": int(raw_settings.get("smart_money_window_minutes", 10)),
        "mute_whale": raw_settings.get("mute_whale", "0") == "1",
        "mute_smart_money": raw_settings.get("mute_smart_money", "0") == "1",
        "mute_multi_wallet": raw_settings.get("mute_multi_wallet", "0") == "1",
        "mute_new_position": raw_settings.get("mute_new_position", "0") == "1",
        "min_token_score": int(raw_settings.get("min_token_score", 0)),
        "digest_enabled": raw_settings.get("digest_enabled", "1") == "1",
    }


async def handle_parsed_transaction(session, bot, admin_chat_id, name, address, parsed, settings):
    """
    🆕 معالجة صفقة واحدة مفسَّرة (تسجيل فالـ DB + كل أنواع التنبيهات + Token Score
    + Hot Token Detection). هاد الدالة مشتركة بين:
    - الـ Polling العادي (process_wallet أسفله)
    - الـ Webhook (helius_webhook_handler) — بلا أي تكرار للمنطق.
    محمية بالكامل بـ try/except: خطأ هنا ما يوقفش معالجة باقي الصفقات.
    """
    try:
        mint = parsed["mint"]
        action = parsed["action"]

        # 🔧 حسب طلبك: البوت كيتابع الشراء (BUY) فقط — أي عملية بيع كتترمى
        # مباشرة، بلا أي تنبيه وبلا تسجيل.
        if action == "SELL":
            return

        is_first_buy_for_wallet = not wallet_has_bought_before(address, mint)

        market_data = await get_token_market_data(session, mint)
        buyers_windows = get_buyers_count_multi_window(mint)
        symbol = market_data["symbol"] if market_data else "?"
        usd_value = (parsed["amount"] or 0) * (market_data["price_usd"] if market_data else 0)

        transaction_id = add_transaction(
            wallet_address=address, wallet_name=name, mint=mint, token_symbol=symbol,
            action=action, amount=parsed["amount"], usd_value=usd_value,
            signature=parsed["signature"], timestamp=parsed["timestamp"],
        )
        if not transaction_id:
            return  # سجلناها من قبل (مثلاً وصلات من Polling و Webhook فنفس الوقت) — ما نكرروش الإشعار

        # 🆕 Win-Rate: نسجلو نقطة الانطلاق (سعر لحظة الشراء) باش نقارنو بعد 1h/24h
        if market_data:
            record_trade_outcome_entry(
                transaction_id, address, mint, parsed["timestamp"], market_data["price_usd"]
            )

        # 🔧 إصلاح مهم: كل خطوة إثراء (Rug Check/Score/ATH) محمية بـ try خاص
        # بيها. قبل، إذا خطوة وحدة طاحت (مثلاً Rug Check تبطأ)، التنبيه كامل
        # كان يضيع بصمت — والصفقة ديجا مسجلة (signature فريد)، يعني ماغاديش
        # يتعاود المحاولة أبداً. دابا: فشل خطوة وحدة ما يأثرش على الباقي،
        # والتنبيه الأساسي ديما يتبعت حتى لو بمعلومات أقل.

        # 🆕 Rug Check (غير BUY يوصل لهنا دابا أصلاً) — وغير للصفقات لي قيمتها
        # فوق RUG_CHECK_MIN_USD_VALUE (توفير كريدي Helius من الصفقات الصغيرة)
        rug_data = None
        if usd_value >= RUG_CHECK_MIN_USD_VALUE:
            try:
                rug_data = await get_rug_check_data(session, mint)
            except Exception as e:
                logger.error(f"⚠️ فشل Rug Check لـ {mint} (كنكملو بلا فحص أمان): {e}")

        # 🆕 Token Score (بعد ما سجلنا الصفقة، باش الـ Popularity يكون محدّث)
        score, score_label = None, None
        try:
            distinct_buyers_ever = count_distinct_buyers_ever(mint)
            signal_counts = get_signal_event_counts(mint)
            score, score_label = calculate_token_score(market_data, distinct_buyers_ever, signal_counts)
        except Exception as e:
            signal_counts = {}
            logger.error(f"⚠️ فشل حساب Token Score لـ {mint} (كنكملو بلا Score): {e}")

        # 🆕 ATH Tracking — كنسجلو/نحدثو أعلى Market Cap وصلها التوكن منذ ما بدا البوت يتابعها
        ath_data = None
        try:
            if market_data:
                ath_data = update_token_ath(mint, market_data["market_cap"], market_data["price_usd"])
        except Exception as e:
            logger.error(f"⚠️ فشل ATH Tracking لـ {mint} (كنكملو بلاه): {e}")

        # 🔧 حتى بناء الرسالة محمي: إذا طاح لأي سبب غريب، نبعتو رسالة بسيطة
        # بدل ما نفقدو التنبيه بالكامل
        try:
            message, _, _ = build_trade_message(
                name, address, parsed, market_data, buyers_windows, score, score_label, signal_counts, rug_data, ath_data
            )
        except Exception as e:
            logger.error(f"⚠️ فشل بناء الرسالة الكاملة لـ {mint}، كنبعتو نسخة مبسطة: {e}")
            message = (
                f"🟢 <b>BUY</b> — {symbol}\n"
                f"💼 {name}\n"
                f"💵 القيمة: ${usd_value:,.2f}\n"
                f"<code>{mint}</code>\n"
                f"🔗 https://dexscreener.com/solana/{mint}"
            )

        log_trade(name, address, action, symbol, mint, parsed["amount"], usd_value, parsed["signature"])

        # 🆕 فلترة الضجة (Alert Fatigue): إذا الـ Score تحت العتبة المحددة، ما
        # نبعتوش تنبيه فوري كامل — نخزنوها فالـ Digest (ملخص كل 30 دقيقة) بدل
        # ما نغرقو المستخدم بتنبيهات على توكنات ضعيفة. عتبة 0 (الافتراضية) = بلا فلترة.
        is_significant = score is None or score >= settings["min_token_score"]

        if settings["notify_buy"] and usd_value >= settings["min_usd_alert"]:
            if is_significant:
                # 🆕 كتبعث مع صورة العملة (إذا متوفرة) — الرسالة كتبان مع لوغو التوكن مباشرة
                image_url = market_data.get("image_url") if market_data else None
                await broadcast_with_image(bot, admin_chat_id, message, image_url)
            elif settings["digest_enabled"]:
                add_to_digest_queue(name, symbol, mint, score, score_label, usd_value)

        # 🐋 Whale Alert
        if usd_value >= settings["whale_threshold"] and not settings["mute_whale"]:
            whale_msg = (
                f"🐋 <b>Whale Buy</b>\n"
                f"▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                f"💰 <b>قيمة الصفقة:</b> ${usd_value:,.2f}\n"
                f"💼 <b>المحفظة:</b> {name}\n"
                f"🪙 <b>العملة:</b> {symbol}\n"
                f"<code>{mint}</code>\n"
                f"▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                f"🔗 <a href='https://dexscreener.com/solana/{mint}'>DexScreener</a> · "
                f"<a href='https://birdeye.so/token/{mint}?chain=solana'>Birdeye</a>"
            )
            await broadcast(bot, admin_chat_id, whale_msg)
            log_signal_event(mint, "whale")

        # 🆕 New Position Alert
        if is_first_buy_for_wallet and not settings["mute_new_position"]:
            new_token_msg = (
                f"🆕 <b>New Position</b>\n"
                f"▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                f"💼 <b>{name}</b> شرات <b>{symbol}</b> لأول مرة\n"
                f"<code>{mint}</code>"
            )
            await broadcast(bot, admin_chat_id, new_token_msg)

        # 🔥 Smart Money / 🚀 Multi Wallet Detection
        if action == "BUY":
            distinct_buyers = count_distinct_wallets_bought_recently(mint, settings["window_minutes"])

            if distinct_buyers == settings["smart_money_min"] and not was_alert_sent(mint, "smart_money") and not settings["mute_smart_money"]:
                buyers = get_wallets_that_bought_recently(mint, settings["window_minutes"])
                sm_msg = (
                    f"🔥 <b>Smart Money Alert</b>\n"
                    f"▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                    f"🪙 <b>{symbol}</b> — <code>{mint}</code>\n"
                    f"👥 عدد المحافظ لي شراوها فآخر {settings['window_minutes']} دقائق: <b>{distinct_buyers}</b>\n"
                    f"💼 المحافظ: {', '.join(buyers)}\n"
                    f"▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                    f"🔗 <a href='https://dexscreener.com/solana/{mint}'>DexScreener</a> · "
                    f"<a href='https://gmgn.ai/sol/token/{mint}'>GMGN</a>"
                )
                await broadcast(bot, admin_chat_id, sm_msg)
                mark_alert_sent(mint, "smart_money")
                log_signal_event(mint, "smart_money")

            if distinct_buyers == settings["multi_wallet_min"] and not was_alert_sent(mint, "multi_wallet") and not settings["mute_multi_wallet"]:
                buyers = get_wallets_that_bought_recently(mint, settings["window_minutes"])
                mw_msg = (
                    f"🚀 <b>Multiple Wallet Entry</b>\n"
                    f"▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                    f"🪙 <b>{symbol}</b> — <code>{mint}</code>\n"
                    f"⚡ <b>{distinct_buyers}</b> محافظ شراو نفس العملة فآخر {settings['window_minutes']} دقائق!\n"
                    f"💼 المحافظ: {', '.join(buyers)}\n"
                    f"▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                    f"🔗 <a href='https://dexscreener.com/solana/{mint}'>DexScreener</a> · "
                    f"<a href='https://gmgn.ai/sol/token/{mint}'>GMGN</a>"
                )
                await broadcast(bot, admin_chat_id, mw_msg)
                mark_alert_sent(mint, "multi_wallet")
                log_signal_event(mint, "multi_wallet")

        # 🔥🔥 Hot Token Detection — كيتبعث مرة وحدة فقط لكل توكن أول ما يوصل لـ High/Extreme
        if score_label in ("High Interest", "Extreme Interest") and not was_alert_sent(mint, "hot_token"):
            hot_msg = (
                f"🔥🔥 <b>Hot Token Detected</b>\n"
                f"▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                f"🪙 <b>{symbol}</b> — <code>{mint}</code>\n"
                f"🧠 <b>Token Score:</b> {score}/100 {score_label_emoji(score_label)} ({score_label})\n"
                f"👥 إجمالي المحافظ المتابَعة لي شراتو: {distinct_buyers_ever}\n"
                f"▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️\n"
                f"استعمل /analyze {mint} لتحليل كامل"
            )
            await broadcast(bot, admin_chat_id, hot_msg)
            mark_alert_sent(mint, "hot_token")

    except Exception as e:
        logger.error(f"❌ خطأ فمعالجة صفقة للمحفظة {name} ({address}): {e}")


async def process_wallet(session, bot, admin_chat_id, wallet, settings, stagger_delay: float = 0):
    """
    🆕 معالجة محفظة واحدة عبر Polling (فحص + تنبيهات) — معزولة تماماً بـ
    try/except باش غلطة فمحفظة وحدة ما تأثرش على الباقي. كتخدم بالتوازي
    مع محافظ أخرى، لكن مع stagger_delay (فاصل زمني) قبل ما تبدا، باش
    الطلبات ما توصلش كلها لـ Helius فنفس المللي-ثانية (سبب Rate Limit 429).
    """
    if stagger_delay:
        await asyncio.sleep(stagger_delay)

    address = wallet["address"]
    name = wallet["name"]
    last_seen_signature = wallet["last_signature"]

    try:
        async with wallet_check_semaphore:
            transactions = await fetch_transactions(session, address)
    except Exception as e:
        logger.error(f"❌ خطأ فجلب معاملات {name} ({address}): {e}")
        return

    if not transactions:
        return

    # 🔧 إصلاح Bug مهم: محفظة جديدة (last_seen_signature = None) ماعندهاش
    # نقطة انطلاق محفوظة، فكان الكود قبل يعتبر آخر 10 صفقات "قديمة" كأنها
    # "جداد" ويبعتهم كلهم دفعة وحدة كتنبيهات (هذا لي كان كيبين "باع باع بزاف"
    # وأرقام مقلوبة). الحل: أول فحص لمحفظة جديدة، نسجلو آخر signature بصمت
    # بلا ما نبعتو تنبيهات على التاريخ، وغير الصفقات الجداد فعلاً (من دابا
    # لقدام) هوما لي غايتبعتو.
    if last_seen_signature is None:
        newest_signature = transactions[0].get("signature")
        update_last_signature(address, newest_signature)
        logger.info(f"🌱 محفظة جديدة \"{name}\" — تسجلات نقطة الانطلاق بصمت (بلا تنبيهات على التاريخ)")
        return

    new_transactions = []
    for tx in transactions:
        sig = tx.get("signature")
        if sig == last_seen_signature:
            break
        new_transactions.append(tx)

    if not new_transactions:
        return

    for tx in reversed(new_transactions):
        parsed = parse_transaction(tx, address)
        if not parsed:
            continue
        await handle_parsed_transaction(session, bot, admin_chat_id, name, address, parsed, settings)

    newest_signature = transactions[0].get("signature")
    update_last_signature(address, newest_signature)


async def check_all_wallets(bot, admin_chat_id: str):
    """
    🆕 النسخة المصلحة: كتفحص كل المحافظ بالتوازي المحدود، لكن مع تفريق زمني
    خفيف (Stagger) بين انطلاق كل طلب — قبل، كل المحافظ (حتى لو 12+) كانو
    كيطلقو الطلب فنفس المللي-ثانية بالضبط، وهذا كان كيضرب Rate Limit ديال
    Helius (429) لكل المحافظ دفعة وحدة، كل 12 ثانية، بلا ما يعاود يحاول.
    """
    wallets_list = get_all_wallets()
    if not wallets_list:
        return

    settings = build_runtime_settings()  # 🔧 إصلاح: نفس الدالة المشتركة مع Webhook (كان مكرر يدوياً قبل)

    dexscreener_cache.clear_expired()  # تنظيف دوري خفيف للكاش

    async with aiohttp.ClientSession() as session:
        tasks = [
            process_wallet(session, bot, admin_chat_id, wallet, settings, stagger_delay=i * STAGGER_INTERVAL_SECONDS)
            for i, wallet in enumerate(wallets_list)
        ]
        # return_exceptions=True: حتى لو وقع خطأ غير متوقع فمهمة وحدة، الباقي كيكمل
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for wallet, result in zip(wallets_list, results):
            if isinstance(result, Exception):
                logger.error(f"❌ خطأ عام فمعالجة محفظة {wallet['name']}: {result}")

# ============================================================
# 🌐 Helius Webhooks (بديل أسرع للـ Polling — اختياري 100%)
# ============================================================
# فكرة: بدل ما نسولفو Helius كل 12 ثانية "واش كاين شي جديد؟"، Helius هو لي
# كيبعتلنا مباشرة (Push) أول ما تصير transaction فمحفظة متابَعة. هذا real-time
# فعلي، وبلا أي تكلفة إضافية (Helius كيعطي هاد الخدمة مجاناً فالـ Free Plan).
#
# شرط وحيد: خاصك URL عمومي (https://...) وصل ليه Helius، مثلاً عبر:
#   - دومين ديالك ديجا عندك سيرفر عليه (الأسهل)
#   - أو ngrok/Cloudflare Tunnel للتجربة (مجانيين): ngrok http 8080
#
# الـ Polling كيبقى خدام فنفس الوقت دايماً كـ "شبكة أمان" — إذا الـ Webhook
# تأخر أو طاح لأي سبب، الـ Polling غادي يلقط نفس الصفقة بعد شوية. قاعدة
# البيانات (UNIQUE signature) كتمنع أي تكرار فالإشعارات.

async def helius_create_or_update_webhook(session: aiohttp.ClientSession, addresses: list, webhook_url: str):
    """
    كيخلق Webhook جديد فـ Helius (أول مرة) أو يحدث الموجود (كي تزيد/تحذف محفظة).
    كيرجع webhook_id إذا نجح، أو None إذا فشل.
    """
    payload = {
        "webhookURL": webhook_url,
        "transactionTypes": ["Any"],
        "accountAddresses": addresses,
        "webhookType": "enhanced",
    }
    existing_id = get_setting("webhook_id")
    try:
        if existing_id:
            url = f"{HELIUS_WEBHOOKS_URL}/{existing_id}?api-key={HELIUS_API_KEY}"
            async with session.put(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return existing_id
                logger.error(f"❌ فشل تحديث Helius Webhook: {resp.status} - {await resp.text()}")
                return None
        else:
            url = f"{HELIUS_WEBHOOKS_URL}?api-key={HELIUS_API_KEY}"
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    return data.get("webhookID")
                logger.error(f"❌ فشل إنشاء Helius Webhook: {resp.status} - {await resp.text()}")
                return None
    except Exception as e:
        logger.error(f"❌ خطأ فالاتصال بـ Helius Webhooks API: {e}")
        return None


async def helius_delete_webhook(session: aiohttp.ClientSession, webhook_id: str) -> bool:
    try:
        url = f"{HELIUS_WEBHOOKS_URL}/{webhook_id}?api-key={HELIUS_API_KEY}"
        async with session.delete(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        logger.error(f"❌ خطأ فحذف Helius Webhook: {e}")
        return False


async def sync_webhook_addresses():
    """
    🆕 كيتصل كي تزيد أو تحذف محفظة: إذا الـ Webhook مفعّل، كيحدّث لائحة
    العناوين عند Helius باش تبقى متزامنة مع قاعدة البيانات ديالك.
    محمي بـ try/except: إذا فشل (بلا انترنت مثلاً)، الـ Polling يبقى خدام عادي.
    """
    if get_setting("webhook_enabled") != "1":
        return
    webhook_url = get_setting("webhook_url")
    if not webhook_url:
        return
    addresses = [w["address"] for w in get_all_wallets()]
    try:
        async with aiohttp.ClientSession() as session:
            webhook_id = await helius_create_or_update_webhook(session, addresses, webhook_url)
            if webhook_id:
                set_setting("webhook_id", webhook_id)
    except Exception as e:
        logger.error(f"❌ خطأ فمزامنة عناوين الـ Webhook: {e}")


async def helius_webhook_handler(request: web.Request):
    """
    🆕 هنا كيوصل الـ Push من Helius مباشرة أول ما تصير transaction.
    كيتحقق من الـ secret (باش حتى واحد آخر ميقدرش يبعت بيانات مزورة للبوت)،
    كيفسر كل transaction، ويعالجها بنفس المنطق ديال الـ Polling (Token Score،
    Whale/Smart Money/Multi Wallet/New Position، كلشي).
    """
    secret = request.query.get("secret", "")
    expected_secret = get_setting("webhook_secret") or ""
    if not expected_secret or secret != expected_secret:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    if isinstance(payload, dict):
        payload = [payload]

    bot = request.app["bot"]
    settings = build_runtime_settings()
    wallets_by_address = {w["address"]: w["name"] for w in get_all_wallets()}

    try:
        async with aiohttp.ClientSession() as session:
            for tx in payload:
                token_transfers = tx.get("tokenTransfers") or []
                involved_addresses = set()
                for t in token_transfers:
                    if t.get("fromUserAccount"):
                        involved_addresses.add(t["fromUserAccount"])
                    if t.get("toUserAccount"):
                        involved_addresses.add(t["toUserAccount"])

                for address in involved_addresses & wallets_by_address.keys():
                    parsed = parse_transaction(tx, address)
                    if parsed:
                        await handle_parsed_transaction(
                            session, bot, ADMIN_CHAT_ID,
                            wallets_by_address[address], address, parsed, settings
                        )
    except Exception as e:
        logger.error(f"❌ خطأ فمعالجة Webhook payload: {e}")

    return web.json_response({"ok": True})


async def health_check_handler(request: web.Request):
    """🔧 Route بسيط باش خدمات Cloud (Render وغيرها) يتأكدو أن البوت حي وخدام"""
    return web.json_response({"status": "ok", "service": "whale-tracker-bot"})


async def telegram_webhook_handler(request: web.Request):
    """
    🆕 هنا Telegram كيبعت الرسائل مباشرة (Push) بدل ما البوت يسولو (Polling).
    هذا كيحل نهائياً مشكل "Conflict: terminated by other getUpdates request"
    لي كان كيصرا فـ Render، حيت ماكاينش عندنا حتى نسخة كتدير Polling —
    Render كيوجه الترافيك غير للنسخة الحية، بلا أي تسابق بين نسختين.
    """
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    expected_secret = get_setting("telegram_webhook_secret") or ""
    if not expected_secret or secret_header != expected_secret:
        return web.Response(status=401)

    try:
        data = await request.json()
        ptb_app = request.app["ptb_app"]
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.update_queue.put(update)
    except Exception as e:
        logger.error(f"❌ خطأ فمعالجة Telegram Webhook: {e}")

    return web.Response()


def build_webhook_web_app(ptb_app) -> web.Application:
    app = web.Application()
    app["bot"] = ptb_app.bot
    app["ptb_app"] = ptb_app
    app.router.add_get("/", health_check_handler)
    app.router.add_post(WEBHOOK_PATH, helius_webhook_handler)
    app.router.add_post(TELEGRAM_WEBHOOK_PATH, telegram_webhook_handler)
    return app


# ============================================================
# 🩺 Watchdog — فحص صحة البوت (Health Check)
# ============================================================
# 🆕 كيتبع آخر مرة نجحت فيها المراقبة الدورية. إذا فاتت مدة طويلة بلا نجاح
# (مثلاً 10 دورات)، كيبعت تنبيه للأدمن — علامة على مشكل (Helius واقع،
# انترنت مقطوع، إلخ) قبل ما تكتشفها بنفسك بعد فوات الوقت.

_watchdog_state = {"last_success_ts": time.time(), "alert_sent": False}


def mark_monitor_success():
    _watchdog_state["last_success_ts"] = time.time()
    _watchdog_state["alert_sent"] = False


async def watchdog_job(context):
    """جوب دوري كيتحقق واش المراقبة مازالت خدامة، ويعلم الأدمن إذا توقفت"""
    elapsed = time.time() - _watchdog_state["last_success_ts"]
    if elapsed > WATCHDOG_STALL_THRESHOLD_SECONDS and not _watchdog_state["alert_sent"]:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"⚠️ <b>تنبيه صحة البوت</b>\n\n"
                    f"المراقبة الدورية ماكملتش بنجاح من {int(elapsed // 60)} دقيقة.\n"
                    f"تأكد من: اتصال الانترنت، حالة Helius API، أو راجع logs/errors.log"
                ),
                parse_mode="HTML",
            )
            _watchdog_state["alert_sent"] = True
        except Exception as e:
            logger.error(f"❌ حتى تنبيه الـ Watchdog فشل يتبعت: {e}")


# ============================================================
# 💾 نسخة احتياطية تلقائية (حماية من فقدان البيانات)
# ============================================================
# 🆕 خدمات بحال Render كتمسح الملفات المحلية (بما فيها wallets.db) فبعض
# الحالات (Redeploy بلا Persistent Disk). هاد الجوب كيبعت نسخة احتياطية
# كاملة للأدمن تلقائياً كل 24 ساعة — حتى لو طاحت البيانات، عندك ديما آخر
# نسخة فـ Telegram (بلا أي تكلفة، بلا أي إعداد إضافي).

AUTO_BACKUP_INTERVAL_SECONDS = 86400  # كل 24 ساعة


async def auto_backup_job(context):
    """كيبعت نسخة احتياطية من قاعدة البيانات للأدمن تلقائياً، مرة فاليوم"""
    try:
        backup_path = create_backup(None)
        wallets_count = len(get_all_wallets())
        await context.bot.send_document(
            chat_id=ADMIN_CHAT_ID,
            document=open(backup_path, "rb"),
            filename=os.path.basename(backup_path),
            caption=(
                f"💾 <b>نسخة احتياطية يومية تلقائية</b>\n"
                f"📊 {wallets_count} محفظة متابَعة\n"
                f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            ),
            parse_mode="HTML",
        )
        os.remove(backup_path)  # ننظفو الملف المحلي بعد ما نبعتوه (ماخصناش ننسخو bezzaf)
    except Exception as e:
        logger.error(f"❌ فشلت النسخة الاحتياطية التلقائية: {e}")


# ============================================================
# 🤖 أزرار وأوامر تيليكرام (wallets.py سابقاً)
# ============================================================

# ---------------- حالات المحادثة ----------------
(
    ADD_ADDRESS, ADD_NAME,
    REMOVE_ADDRESS,
    RENAME_ADDRESS, RENAME_NAME,
    SET_MINUSD, SET_WHALE,
) = range(1, 8)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📌 Add Wallet", "📋 Wallet List"],
        ["❌ Remove Wallet", "✏️ Rename Wallet"],
        ["⚙ Settings", "📊 Statistics"],
        ["🔬 Analyze", "🛡️ Rug Check"],
        ["🧠 Token Score", "🔍 Search"],
        ["📈 Wallet Stats", "⚡ Wallet Activity"],
        ["🏆 Top Wallets", "🥇 Top Performers"],
        ["👀 Watchlist", "📤 Export"],
        ["💾 Backup", "📅 Daily"],
        ["🗓 Weekly", "📆 Monthly"],
        ["❓ Help"],
    ],
    resize_keyboard=True
)


def is_admin(update: Update, admin_chat_id: str) -> bool:
    return str(update.effective_chat.id) == str(admin_chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update, ADMIN_CHAT_ID):
        # 🆕 الأدمن كيشوف القائمة الكاملة (كيما كان دايماً)
        await update.message.reply_text(
            "أهلاً 👋\n"
            "هذا بوت تتبع محافظ Solana (Whale Wallet Tracker).\n"
            "اختر من الأزرار تحت، أو اكتب /help لائحة كاملة ديال الأوامر:",
            reply_markup=MAIN_KEYBOARD
        )
        return

    # 🆕 أي شخص آخر (مشاهد محتمل) كيشوف رسالة ترحيب مختصرة + زر يعطيه الـ ID
    # ديالو جاهز للنسخ، باش يبعتو للأدمن ويتزاد بـ /addviewer بلا أي تعقيد
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🆔 عرض المعرف ديالي (ID)", callback_data="show_my_id")]
    ])
    await update.message.reply_text(
        "🦇 <b>أهلاً بيك فـ Batdex</b>\n\n"
        "بوت كيراقب محافظ Solana مختارة على مدار الساعة، وكيبعت تنبيه فوري "
        "كل ما وحدة منهم تشري توكن جديد — مع تحليل ذكي (Token Score)، "
        "فحص أمان تلقائي، وتحذيرات Whale/Smart Money.\n\n"
        "📩 باش توصلك التنبيهات، خاصك تبعت المعرف (ID) ديالك للشخص لي عندو "
        "البوت، وهو غايزيدك.\n\n"
        "دوس الزر تحت باش يبان ليك المعرف ديالك جاهز للنسخ 👇",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🆕 لائحة كاملة ومنظمة ديال كل الأوامر — الأدمن كيشوف كلشي، المشاهد كيشوف نسخة مختصرة"""
    if not is_admin(update, ADMIN_CHAT_ID):
        await update.message.reply_text(
            "🦇 <b>Batdex — أوامر متاحة ليك:</b>\n\n"
            "/start — إعادة الترحيب + عرض المعرف ديالك\n\n"
            "التنبيهات كتوصلك تلقائياً، ماعندكش داعي دير شي حاجة أخرى 👍",
            parse_mode="HTML",
        )
        return

    text = (
        "🦇 <b>Batdex — دليل الأوامر الكامل</b>\n\n"
        "<b>📌 إدارة المحافظ</b>\n"
        "📌 Add Wallet — زيد محفظة\n"
        "❌ Remove Wallet — حذف محفظة\n"
        "✏️ Rename Wallet — بدل الاسم\n"
        "📋 Wallet List — عرض القائمة\n\n"
        "<b>⚙️ الإعدادات</b>\n"
        "⚙ Settings — تفعيل/كتم أنواع التنبيهات\n"
        "/setminusd [مبلغ] — الحد الأدنى للتنبيه\n"
        "/setwhale [مبلغ] — حد Whale Alert\n"
        "/setminscore [رقم 0-100] — الحد الأدنى لـ Token Score (فلترة الضجة)\n\n"
        "<b>📊 إحصائيات وتقارير</b>\n"
        "📊 Statistics — إحصائيات عامة\n"
        "/walletstats [عنوان] — إحصائيات + Win-Rate لمحفظة\n"
        "/walletactivity [عنوان] — Activity Score لمحفظة\n"
        "🏆 Top Wallets — أكثر المحافظ إشارات\n"
        "/topperformers [1h|24h] — أفضل المحافظ حسب الربح الحقيقي\n"
        "📅 Daily / 🗓 Weekly / /monthly — ملخصات دورية\n"
        "👀 Watchlist — أكثر العملات شراءً\n"
        "/search [عنوان/رمز] — بحث فالتاريخ\n\n"
        "<b>🧠 تحليل التوكنات</b>\n"
        "/tokenscore [عنوان] — Token Score من 100\n"
        "/rugcheck [عنوان] — فحص أمان العقد\n"
        "/analyze [عنوان] — تحليل كامل شامل (Score + Rug + Momentum + صورة)\n\n"
        "<b>👥 المشاهدين (Admin فقط)</b>\n"
        "/addviewer [id] [اسم] — زيد مشاهد\n"
        "/removeviewer [id] — حذف مشاهد\n"
        "/viewers — عرض اللائحة\n\n"
        "<b>💾 البيانات</b>\n"
        "📤 Export — تصدير CSV/JSON\n"
        "💾 Backup — نسخة احتياطية\n\n"
        "<b>🌐 Webhook (اختياري، للسرعة القصوى)</b>\n"
        "/setwebhook [URL] — تفعيل\n"
        "/webhookstatus — الحالة\n"
        "/disablewebhook — إيقاف"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def show_my_id_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🆕 كيعرض للشخص الـ Chat ID ديالو + أمر جاهز (Copy-Paste) باش الأدمن يزيدو بلا مجهود"""
    query = update.callback_query
    await query.answer()
    user = query.from_user
    chat_id = update.effective_chat.id
    display_name = user.first_name or user.username or "مستخدم"
    ready_command = f"/addviewer {chat_id} {display_name}"

    await query.message.reply_text(
        f"🆔 <b>المعرف ديالك</b>\n\n"
        f"👤 الاسم: {display_name}\n"
        f"🔢 ID: <code>{chat_id}</code>\n\n"
        f"📋 انسخ هاد السطر وبعتو للشخص لي عندو تحكم فالبوت:\n"
        f"<code>{ready_command}</code>",
        parse_mode="HTML",
    )


# ============ إضافة محفظة ============

async def add_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ابعت عنوان المحفظة (Solana Address):\n\nأو /cancel باش تلغي.")
    return ADD_ADDRESS


# 🆕 فحص أدق لعنوان Solana (base58، بلا 0/O/I/l) بدل غير طول السترينغ
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


async def receive_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()
    if not SOLANA_ADDRESS_RE.match(address):
        await update.message.reply_text(
            "⚠️ العنوان كيبان ماشي صحيح (خاصو يكون Base58، بلا 0/O/I/l). حاول مرة أخرى أو /cancel"
        )
        return ADD_ADDRESS
    if get_wallet_by_address(address):
        await update.message.reply_text("⚠️ هاد المحفظة موجودة من قبل. ابعت عنوان آخر أو /cancel")
        return ADD_ADDRESS
    context.user_data["pending_address"] = address
    await update.message.reply_text("عطيها اسم (مثلاً: Whale 1):")
    return ADD_NAME


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    address = context.user_data.get("pending_address")
    success = add_wallet(address, name)
    if success:
        await update.message.reply_text(f"✅ تمت إضافة \"{name}\" بنجاح.", reply_markup=MAIN_KEYBOARD)
        asyncio.create_task(sync_webhook_addresses())  # 🆕 تحديث Webhook إذا مفعّل (بلا ما يبطئ الرد)
    else:
        await update.message.reply_text("⚠️ وقع مشكل، حاول مرة أخرى.", reply_markup=MAIN_KEYBOARD)
    context.user_data.clear()
    return ConversationHandler.END


# ============ حذف محفظة ============

async def remove_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets_list = get_all_wallets()
    if not wallets_list:
        await update.message.reply_text("ماكاينش محافظ باش تحذفها.")
        return ConversationHandler.END
    text = "ابعت عنوان المحفظة اللي بغيتي تحذفها:\n\n"
    for w in wallets_list:
        text += f"• {w['name']}: `{w['address']}`\n"
    await update.message.reply_text(text, parse_mode="Markdown")
    return REMOVE_ADDRESS


async def receive_remove_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()
    success = remove_wallet(address)
    if success:
        await update.message.reply_text("✅ تم الحذف.", reply_markup=MAIN_KEYBOARD)
        asyncio.create_task(sync_webhook_addresses())  # 🆕 تحديث Webhook إذا مفعّل
    else:
        await update.message.reply_text("⚠️ ماكايناش هاد العنوان.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ============ تعديل اسم محفظة ============

async def rename_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets_list = get_all_wallets()
    if not wallets_list:
        await update.message.reply_text("ماكاينش محافظ.")
        return ConversationHandler.END
    text = "ابعت عنوان المحفظة اللي بغيتي تبدل اسمها:\n\n"
    for w in wallets_list:
        text += f"• {w['name']}: `{w['address']}`\n"
    await update.message.reply_text(text, parse_mode="Markdown")
    return RENAME_ADDRESS


async def receive_rename_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()
    if not get_wallet_by_address(address):
        await update.message.reply_text("⚠️ ماكايناش هاد العنوان. حاول مرة أخرى أو /cancel")
        return RENAME_ADDRESS
    context.user_data["rename_address"] = address
    await update.message.reply_text("عطيها الاسم الجديد:")
    return RENAME_NAME


async def receive_rename_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    address = context.user_data.get("rename_address")
    rename_wallet(address, new_name)
    await update.message.reply_text(f"✅ تبدل الاسم لـ \"{new_name}\".", reply_markup=MAIN_KEYBOARD)
    context.user_data.clear()
    return ConversationHandler.END


# ============ عرض المحافظ ============

async def list_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets_list = get_all_wallets()
    if not wallets_list:
        await update.message.reply_text("ماكاينش محافظ مضافة حالياً.")
        return
    text = "📋 <b>قائمة المحافظ:</b>\n\n"
    for w in wallets_list:
        text += f"• <b>{w['name']}</b>\n  <code>{w['address']}</code>\n\n"
    await update.message.reply_text(text, parse_mode="HTML")


# ============ الإعدادات ============

def build_settings_text_and_keyboard():
    s = get_all_settings()
    buy_status = "✅ مفعّل" if s.get("notify_buy") == "1" else "❌ متوقف"
    mute_whale = "🔇 مكتوم" if s.get("mute_whale") == "1" else "🔊 مفعّل"
    mute_sm = "🔇 مكتوم" if s.get("mute_smart_money") == "1" else "🔊 مفعّل"
    mute_mw = "🔇 مكتوم" if s.get("mute_multi_wallet") == "1" else "🔊 مفعّل"
    mute_np = "🔇 مكتوم" if s.get("mute_new_position") == "1" else "🔊 مفعّل"

    text = (
        f"⚙ <b>الإعدادات الحالية</b>\n\n"
        f"🟢 إشعارات Buy: {buy_status}\n"
        f"ℹ️ البوت كيتابع الشراء (BUY) فقط — البيع بلا أي تنبيه\n"
        f"💵 الحد الأدنى للإشعار: ${s.get('min_usd_alert')}\n"
        f"🐋 حد Whale Alert: ${s.get('whale_usd_threshold')}\n"
        f"🎯 الحد الأدنى لـ Token Score: {s.get('min_token_score', '0')}/100\n"
        f"🔥 عدد محافظ Smart Money: {s.get('smart_money_min_wallets')}\n"
        f"🚀 عدد محافظ Multi Wallet: {s.get('multi_wallet_min')}\n"
        f"⏱ النافذة الزمنية: {s.get('smart_money_window_minutes')} دقائق\n\n"
        f"<b>كتم أنواع تنبيهات:</b>\n"
        f"🐋 Whale: {mute_whale}\n"
        f"🔥 Smart Money: {mute_sm}\n"
        f"🚀 Multi Wallet: {mute_mw}\n"
        f"🆕 New Position: {mute_np}\n\n"
        f"استعمل الأزرار تحت باش تبدل، أو هاد الأوامر:\n"
        f"/setminusd [مبلغ] — بدل الحد الأدنى\n"
        f"/setwhale [مبلغ] — بدل حد Whale\n"
        f"/setminscore [رقم] — بدل الحد الأدنى لـ Token Score"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Buy: {buy_status}", callback_data="toggle_buy")],
        [InlineKeyboardButton(f"🐋 Whale: {mute_whale}", callback_data="toggle_mute_whale")],
        [InlineKeyboardButton(f"🔥 Smart Money: {mute_sm}", callback_data="toggle_mute_sm")],
        [InlineKeyboardButton(f"🚀 Multi Wallet: {mute_mw}", callback_data="toggle_mute_mw")],
        [InlineKeyboardButton(f"🆕 New Position: {mute_np}", callback_data="toggle_mute_np")],
    ])
    return text, keyboard


async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = build_settings_text_and_keyboard()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    toggle_map = {
        "toggle_buy": "notify_buy",
        "toggle_mute_whale": "mute_whale",
        "toggle_mute_sm": "mute_smart_money",
        "toggle_mute_mw": "mute_multi_wallet",
        "toggle_mute_np": "mute_new_position",
    }
    key = toggle_map.get(query.data)
    if key:
        current = get_setting(key)
        set_setting(key, "0" if current == "1" else "1")

    text, keyboard = build_settings_text_and_keyboard()
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def set_min_usd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استعمل: /setminusd 100")
        return
    try:
        value = float(context.args[0])
        set_setting("min_usd_alert", value)
        await update.message.reply_text(f"✅ الحد الأدنى للإشعار دابا ${value}")
    except ValueError:
        await update.message.reply_text("⚠️ دخل رقم صحيح، مثلاً: /setminusd 100")


async def set_whale_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استعمل: /setwhale 10000")
        return
    try:
        value = float(context.args[0])
        set_setting("whale_usd_threshold", value)
        await update.message.reply_text(f"✅ حد Whale Alert دابا ${value}")
    except ValueError:
        await update.message.reply_text("⚠️ دخل رقم صحيح، مثلاً: /setwhale 10000")


async def set_min_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    استعمل: /setminscore [رقم من 0 إلى 100]
    أي BUY عندو Token Score تحت هاد الرقم ما يبعتش تنبيه فوري كامل — كيتخزن
    ويتبعث كملخص مجمّع كل 30 دقيقة (Digest) بدل ما يغرق التنبيهات الفورية.
    0 = بلا فلترة (كل شي كيبعث فوري، كيما كان قبل).
    """
    if not context.args:
        current = get_setting("min_token_score") or "0"
        await update.message.reply_text(
            f"العتبة الحالية: {current}/100\n\n"
            f"استعمل: /setminscore [رقم من 0 إلى 100]\n"
            f"مثال: /setminscore 40 — أي توكن Score ديالو تحت 40 يتفلتر لملخص مجمّع"
        )
        return
    try:
        value = int(context.args[0])
        if not (0 <= value <= 100):
            raise ValueError
        set_setting("min_token_score", value)
        await update.message.reply_text(
            f"✅ العتبة دابا {value}/100 — أي BUY تحتها غايتفلتر لملخص مجمّع كل 30 دقيقة"
        )
    except ValueError:
        await update.message.reply_text("⚠️ دخل رقم صحيح بين 0 و100، مثلاً: /setminscore 40")


# ============ الإحصائيات ============

async def statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    text = (
        f"📊 <b>الإحصائيات</b>\n\n"
        f"📅 عمليات اليوم: {stats['today_count']}\n"
        f"🗓 عمليات هاد الأسبوع: {stats['week_count']}\n"
        f"🏆 أكثر محفظة نشاطاً: {stats['top_wallet']}\n\n"
        f"<b>أكثر العملات شراءً:</b>\n"
    )
    for token, count in stats["top_bought"]:
        text += f"  • {token}: {count}\n"

    await update.message.reply_text(text, parse_mode="HTML")


# ============ 🆕 ملخصات دورية (يومي / أسبوعي / شهري) ============

async def _send_period_summary(update: Update, label: str, since_ts: int):
    top_wallets = get_top_active_wallets(since_ts, limit=5)
    text = f"{label}\n\n<b>أكثر المحافظ نشاطاً:</b>\n"
    if not top_wallets:
        text += "  ماكاينش نشاط بعد.\n"
    for w in top_wallets:
        text += f"  • {w['wallet_name']}: {w['c']} عملية\n"
    await update.message.reply_text(text, parse_mode="HTML")


async def daily_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    since = now - (now % 86400)
    await _send_period_summary(update, "📅 <b>الملخص اليومي</b>", since)


async def weekly_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    since = int(time.time()) - (7 * 86400)
    await _send_period_summary(update, "🗓 <b>الملخص الأسبوعي</b>", since)


async def monthly_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    since = int(time.time()) - (30 * 86400)
    await _send_period_summary(update, "📆 <b>الملخص الشهري</b>", since)


# ============ 🆕 أكثر المحافظ نشاطاً (زر عام) ============

async def top_wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    signal_counts = get_wallet_signal_counts()
    if not signal_counts:
        await update.message.reply_text("ماكاينش بيانات كافية بعد.")
        return
    text = "🏆 <b>أكثر المحافظ تحقيقاً للإشارات (Buy):</b>\n\n"
    for name, count in signal_counts:
        text += f"  • {name}: {count} إشارة\n"
    await update.message.reply_text(text, parse_mode="HTML")


# ============ المشاهدين (Viewers) — أوامر خاصة بالأدمن ============

async def add_viewer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update, ADMIN_CHAT_ID):
        return
    if not context.args:
        await update.message.reply_text("استعمل: /addviewer [chat_id]")
        return
    chat_id = context.args[0]
    name = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    success = add_subscriber(chat_id, name)
    if success:
        await update.message.reply_text(f"✅ تمت إضافة المشاهد {chat_id}")
    else:
        await update.message.reply_text("⚠️ هاد الـ Chat ID موجود من قبل.")


async def remove_viewer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update, ADMIN_CHAT_ID):
        return
    if not context.args:
        await update.message.reply_text("استعمل: /removeviewer [chat_id]")
        return
    chat_id = context.args[0]
    success = remove_subscriber(chat_id)
    if success:
        await update.message.reply_text("✅ تم حذف المشاهد.")
    else:
        await update.message.reply_text("⚠️ ماكايناش هاد الـ Chat ID.")


async def list_viewers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update, ADMIN_CHAT_ID):
        return
    subs = get_all_subscribers()
    if not subs:
        await update.message.reply_text("ماكاينش مشاهدين حالياً.")
        return
    text = "👀 <b>المشاهدين:</b>\n\n"
    for s in subs:
        text += f"• {s['name'] or '—'}: <code>{s['chat_id']}</code>\n"
    await update.message.reply_text(text, parse_mode="HTML")


# ============ البحث ============

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استعمل: /search [Contract Address أو Symbol]")
        return
    query = " ".join(context.args)
    results = search_transactions(query)
    if not results:
        await update.message.reply_text("ماكاينش نتائج.")
        return
    text = f"🔍 <b>نتائج البحث عن \"{query}\":</b>\n\n"
    for r in results:
        text += (
            f"• {r['action']} — {r['wallet_name']} — {r['token_symbol']}\n"
            f"  ${r['usd_value']:,.2f}\n"
        )
    await update.message.reply_text(text, parse_mode="HTML")


# ============ 🆕 إحصائيات محفظة معينة ============

def build_wallet_stats_text(address: str):
    """🆕 دالة مشتركة: كتبني نص إحصائيات المحفظة — تُستعمل من /walletstats والزر التفاعلي"""
    wallet = get_wallet_by_address(address)
    if not wallet:
        return None
    stats = get_wallet_stats(address)
    win_1h = get_wallet_win_rate(address, window="1h")
    win_24h = get_wallet_win_rate(address, window="24h")

    def format_winrate(w):
        if w["win_rate_pct"] is None:
            return f"— (مازال ما كافيش بيانات مقيّمة)"
        return f"{w['win_rate_pct']}% (متوسط: {w['avg_return_pct']:+.1f}%، على {w['sample_size']} صفقة)"

    return (
        f"📊 <b>إحصائيات محفظة {wallet['name']}</b>\n\n"
        f"🟢 عدد صفقات Buy: {stats['buy_count']}\n"
        f"💵 إجمالي المشتريات: ${stats['total_buy_usd']:,.2f}\n"
        f"📈 متوسط قيمة الصفقة: ${stats['avg_trade_usd']:,.2f}\n\n"
        f"🏆 <b>Win-Rate الحقيقي (مبني على نتيجة فعلية):</b>\n"
        f"⏱ بعد 1 ساعة: {format_winrate(win_1h)}\n"
        f"📅 بعد 24 ساعة: {format_winrate(win_24h)}"
    )


async def wallet_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استعمل: /walletstats [عنوان المحفظة]")
        return
    text = build_wallet_stats_text(context.args[0])
    if text is None:
        await update.message.reply_text("⚠️ ماكايناش هاد المحفظة.")
        return
    await update.message.reply_text(text, parse_mode="HTML")


# ============ 🆕 Top Performers (Win-Rate Leaderboard) ============

def build_top_performers_text(window: str = "24h") -> str:
    """🆕 دالة مشتركة: كتبني نص Top Performers — تُستعمل من /topperformers وزر 1h/24h"""
    performers = get_top_performers(window=window, min_sample=5, limit=10)
    if not performers:
        return (
            "ماكاينش بيانات كافية بعد (خاص صفقات مقيّمة على الأقل 5 لكل محفظة). "
            "استناى شوية باش يتجمع بيانات كافية."
        )
    text = f"🏆 <b>Top Performers — Win-Rate ({window})</b>\n\n"
    for i, p in enumerate(performers, 1):
        text += (
            f"{i}. <b>{p['wallet_name']}</b>: {p['win_rate_pct']}% "
            f"(متوسط: {p['avg_return_pct']:+.1f}%، {p['sample_size']} صفقة)\n"
        )
    return text


async def top_performers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استعمل: /topperformers [1h|24h] — أو دوس الزر 🥇 Top Performers فالقائمة"""
    window = context.args[0] if context.args and context.args[0] in ("1h", "24h") else "24h"
    await update.message.reply_text(build_top_performers_text(window), parse_mode="HTML")


# ============ 🆕 Rug Check (فحص أمان العقد) ============

async def rug_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استعمل: /rugcheck [Contract Address]")
        return
    mint = context.args[0]
    await update.message.reply_text("⏳ كنفحصو الأمان ديال العقد...")

    async with aiohttp.ClientSession() as session:
        rug_data = await get_rug_check_data(session, mint)
    risk_score, risk_label, warnings = calculate_rug_risk(rug_data)

    text = f"🛡️ <b>Rug Check</b>\n\n🪙 <code>{mint}</code>\n\n📊 الخطر: {risk_label} ({risk_score}/100)\n\n"
    if warnings:
        text += "<b>التفاصيل:</b>\n" + "\n".join(f"• {w}" for w in warnings)
    else:
        text += "✅ ماكاينش مؤشرات خطر واضحة (Mint/Freeze Authority متشالين، وتركيز الحاملين معقول)."

    top10 = rug_data.get("top10_holder_pct")
    if top10 is not None:
        text += f"\n\n👥 Top 10 حاملين: {top10}% من الـ Supply"

    await update.message.reply_text(text, parse_mode="HTML")


# ============ 🆕 /analyze — تحليل كوين احترافي شامل (كلشي فرسالة وحدة) ============

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    استعمل: /analyze [Contract Address]
    تقرير احترافي كامل: صورة العملة + Token Score + Rug Check + Momentum
    + عدد المشترين + روابط المنصات — كل التحليل المتوفر عند البوت فرسالة وحدة.
    """
    if not context.args:
        await update.message.reply_text("استعمل: /analyze [Contract Address]")
        return
    mint = context.args[0]
    await update.message.reply_text("⏳ كنجمعو التحليل الكامل ديال هاد العملة...")

    async with aiohttp.ClientSession() as session:
        market_data = await get_token_market_data(session, mint)
        rug_data = await get_rug_check_data(session, mint)

    if not market_data:
        await update.message.reply_text(
            "⚠️ ما لقيتش بيانات سوق لهاد التوكن فـ DexScreener (يمكن جديد بزاف أو بلا Liquidity)."
        )
        return

    buyers_windows = get_buyers_count_multi_window(mint)
    distinct_buyers_ever = count_distinct_buyers_ever(mint)
    signal_counts = get_signal_event_counts(mint)
    score, score_label = calculate_token_score(market_data, distinct_buyers_ever, signal_counts)
    risk_score, risk_label, warnings = calculate_rug_risk(rug_data)
    ath_data = update_token_ath(mint, market_data["market_cap"], market_data["price_usd"])

    # 🆕 Cross-Check: نقارنو السعر مع مصدر ثاني مجاني (Jupiter) باش نتأكدو
    # أن المعلومة موثوقة (ماشي غالطة/متأخرة من مصدر وحيد)
    async with aiohttp.ClientSession() as session2:
        jupiter_price = await get_jupiter_price(session2, mint)
    price_verified_line = ""
    if jupiter_price and market_data["price_usd"] > 0:
        diff_pct = abs(jupiter_price - market_data["price_usd"]) / market_data["price_usd"] * 100
        if diff_pct <= 5:
            price_verified_line = "✅ <b>السعر موثّق</b> (مطابق بين DexScreener و Jupiter)\n"
        else:
            price_verified_line = f"⚠️ <b>فرق فالسعر بين المصادر</b> ({diff_pct:.1f}%) — تأكد قبل ما تعتمد عليه\n"

    # 🆕 Graduation Status
    graduation_line = ""
    dex_id_check = market_data.get("dex_id") or ""
    if dex_id_check == "pumpfun":
        graduation_line = "🌱 <b>الحالة:</b> مازال فـ Bonding Curve (Pump.fun) — قبل التخرج\n"
    elif dex_id_check == "pumpswap":
        graduation_line = "🎓 <b>الحالة:</b> تخرج من Pump.fun (Graduated) → PumpSwap\n"

    # 🆕 ATH Tracking
    if ath_data.get("is_new_ath"):
        ath_line = "🚀 <b>قمة سعرية جديدة (ATH)</b> منذ ما بدا التتبع!\n"
    else:
        ath_mcap = ath_data.get("ath_mcap") or 0
        drawdown = ((market_data["market_cap"] - ath_mcap) / ath_mcap * 100) if ath_mcap > 0 else 0
        ath_line = f"📊 <b>من القمة (ATH):</b> {drawdown:+.1f}% (أعلى MC: ${ath_mcap:,.0f})\n"

    # 🆕 Buy/Sell Pressure
    buys_h1 = market_data.get("buys_h1", 0)
    sells_h1 = market_data.get("sells_h1", 0)
    total_h1 = buys_h1 + sells_h1
    pressure_line = ""
    if total_h1 > 0:
        buy_pct = (buys_h1 / total_h1) * 100
        pressure_line = f"⚖️ <b>ضغط الشراء (1h):</b> {buys_h1} شراء / {sells_h1} بيع ({buy_pct:.0f}% شراء)\n"

    # 🆕 حضور اجتماعي
    social_links = []
    if market_data.get("twitter_url"):
        social_links.append(f"<a href='{market_data['twitter_url']}'>🐦 Twitter</a>")
    if market_data.get("telegram_url"):
        social_links.append(f"<a href='{market_data['telegram_url']}'>📱 Telegram</a>")
    if market_data.get("website_url"):
        social_links.append(f"<a href='{market_data['website_url']}'>🌍 Website</a>")
    social_line = f"🔗 <b>حضور اجتماعي:</b> {' · '.join(social_links)}\n" if social_links else "⚠️ <b>بلا حضور اجتماعي</b> (بلا Twitter/Telegram/Website)\n"

    change_pct = market_data.get("price_change_1h") or market_data.get("price_change_24h") or 0
    window_label = "ساعة" if market_data.get("price_change_1h") else "24 ساعة"
    narrative = build_smart_narrative(
        market_data["symbol"], mint, change_pct, window_label, buyers_windows, signal_counts, market_data
    )

    divider = "▫️▫️▫️▫️▫️▫️▫️▫️▫️▫️"
    dexscreener = market_data["dexscreener_url"]
    birdeye = f"https://birdeye.so/token/{mint}?chain=solana"
    jupiter = f"https://jup.ag/swap/SOL-{mint}"
    axiom = f"https://axiom.trade/t/{mint}"
    dex_id = market_data.get("dex_id") or ""
    pumpfun_line = f"<a href='https://pump.fun/{mint}'>Pump.fun</a> · " if "pump" in dex_id else ""
    photon = f"https://photon-sol.tinyastro.io/en/lp/{mint}"
    gmgn = f"https://gmgn.ai/sol/token/{mint}"
    solscan = f"https://solscan.io/token/{mint}"

    text = (
        f"📊 <b>تحليل كامل — {market_data['symbol']}</b>\n"
        f"{divider}\n"
        f"🧠 <b>Token Score:</b> {score}/100 {score_label_emoji(score_label)} ({score_label})\n"
        f"🛡️ <b>فحص الأمان:</b> {risk_label} ({risk_score}/100)\n"
        f"{price_verified_line}"
        f"{graduation_line}"
        f"{ath_line}"
        f"{pressure_line}"
        f"{social_line}"
        f"{divider}\n"
        f"💬 <i>{narrative}</i>\n"
        f"{divider}\n"
        f"💲 <b>السعر:</b> ${market_data['price_usd']:.8f}\n"
        f"🧢 <b>Market Cap:</b> ${market_data['market_cap']:,.0f}\n"
        f"💧 <b>Liquidity:</b> ${market_data['liquidity_usd']:,.0f}\n"
        f"📈 <b>Volume 24h:</b> ${market_data['volume_24h']:,.0f}\n"
        f"⏳ <b>عمر التوكن:</b> {market_data['token_age']}\n"
        f"{divider}\n"
        f"👥 <b>مشترين (5د/15د/60د):</b> {buyers_windows.get('5m', 0)} / {buyers_windows.get('15m', 0)} / {buyers_windows.get('60m', 0)}\n"
        f"👥 <b>إجمالي المحافظ المتابَعة لي شراتو:</b> {distinct_buyers_ever}\n"
        f"🐋 <b>أحداث Whale:</b> {signal_counts.get('whale', 0)} | "
        f"🔥 <b>Smart Money:</b> {signal_counts.get('smart_money', 0)} | "
        f"🚀 <b>Multi Wallet:</b> {signal_counts.get('multi_wallet', 0)}\n"
    )

    if warnings:
        text += f"{divider}\n⚠️ <b>تحذيرات الأمان:</b>\n" + "\n".join(f"• {w}" for w in warnings) + "\n"

    top10 = rug_data.get("top10_holder_pct")
    if top10 is not None:
        text += f"👥 Top 10 حاملين: {top10}% من الـ Supply\n"

    text += (
        f"{divider}\n"
        f"🔗 <a href='{dexscreener}'>DexScreener</a> · "
        f"<a href='{birdeye}'>Birdeye</a> · "
        f"<a href='{axiom}'>Axiom</a>\n"
        f"🔄 <a href='{jupiter}'>Jupiter (Swap)</a> · "
        f"{pumpfun_line}"
        f"<a href='{photon}'>Photon</a> · "
        f"<a href='{gmgn}'>GMGN</a> · "
        f"<a href='{solscan}'>Solscan</a>"
    )

    image_url = market_data.get("image_url")
    try:
        if image_url and len(text) <= 1024:
            await update.message.reply_photo(photo=image_url, caption=text, parse_mode="HTML")
        else:
            if image_url:
                await update.message.reply_photo(photo=image_url)
            await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"❌ خطأ فبعث صورة التحليل: {e}")
        await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


# ============ Watchlist ============

async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = get_watchlist()
    if not items:
        await update.message.reply_text("ماكاينش بيانات كافية بعد.")
        return
    text = "👀 <b>Watchlist — أكثر العملات شراءً:</b>\n\n"
    for item in items:
        text += f"• {item['token']} — {item['buy_count']} عملية شراء\n"
    await update.message.reply_text(text, parse_mode="HTML")


# ============ التصدير ============

async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets_list = get_all_wallets()
    transactions_list = get_all_transactions()

    csv_path = "export_wallets.csv"
    json_path = "export_transactions.json"
    export_wallets_csv(wallets_list, csv_path)
    export_transactions_json(transactions_list, json_path)

    await update.message.reply_document(document=open(csv_path, "rb"), filename="wallets.csv")
    await update.message.reply_document(document=open(json_path, "rb"), filename="transactions.json")


# ============ النسخ الاحتياطي ============

async def backup_database(update: Update, context: ContextTypes.DEFAULT_TYPE):
    backup_path = create_backup(None)
    await update.message.reply_document(
        document=open(backup_path, "rb"),
        filename=os.path.basename(backup_path),
        caption="💾 نسخة احتياطية من قاعدة البيانات"
    )


# ============================================================
# 🆕 نظام الأزرار الذكي — بلا ما تحتاج تكتب أي أمر يدوياً
# ============================================================
# فكرة: الأزرار لي محتاجة "معلومة" (عنوان توكن، عنوان محفظة) كتسولك عليها
# بزر أو بسؤال بسيط، بدل ما تكتب الأمر كامل بالأقواس يدوياً.

# ---- 1) أزرار كتسول على Contract Address أو نص بحث (تنتظر رسالتك الجاية) ----

async def prompt_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_command"] = "analyze"
    await update.message.reply_text("🔬 ابعت الـ Contract Address ديال العملة لي بغيتي تحللها:")


async def prompt_rugcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_command"] = "rugcheck"
    await update.message.reply_text("🛡️ ابعت الـ Contract Address ديال العملة لفحص الأمان ديالها:")


async def prompt_tokenscore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_command"] = "tokenscore"
    await update.message.reply_text("🧠 ابعت الـ Contract Address ديال العملة لحساب الـ Score ديالها:")


async def prompt_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_command"] = "search"
    await update.message.reply_text("🔍 ابعت العنوان (Contract) أو رمز العملة لي بغيتي تبحث عليه:")


async def handle_awaiting_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    🆕 كيتفعل غير إذا كان كاين "سؤال معلق" (awaiting_command) من زر سابق —
    خلاف ذلك كيتجاهل الرسالة تماماً (باش ما يردش بشكل غريب على أي نص عشوائي).
    """
    awaiting = context.user_data.pop("awaiting_command", None)
    if not awaiting:
        return  # ماكاين حتى سؤال معلق — نتجاهلو الرسالة

    text = update.message.text.strip()
    if awaiting == "search":
        context.args = text.split()
    else:
        context.args = [text]

    handler_map = {
        "analyze": analyze_command,
        "rugcheck": rug_check_command,
        "tokenscore": token_score_command,
        "search": search_command,
    }
    func = handler_map.get(awaiting)
    if func:
        await func(update, context)


# ---- 2) أزرار Wallet Stats / Wallet Activity — بلا كتابة، غير اختيار من لائحة ----

async def show_wallet_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, action_prefix: str, title: str):
    wallets_list = get_all_wallets()
    if not wallets_list:
        await update.message.reply_text("ماكاينش محافظ مضافة حالياً. زيد وحدة أول بـ 📌 Add Wallet.")
        return
    buttons = [
        [InlineKeyboardButton(w["name"], callback_data=f"{action_prefix}:{w['address']}")]
        for w in wallets_list
    ]
    await update.message.reply_text(title, reply_markup=InlineKeyboardMarkup(buttons))


async def prompt_wallet_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_wallet_picker(update, context, "wstat", "📈 اختار المحفظة لعرض الإحصائيات:")


async def prompt_wallet_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_wallet_picker(update, context, "wact", "⚡ اختار المحفظة لعرض الـ Activity Score:")


async def wallet_picker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prefix, address = query.data.split(":", 1)
    if prefix == "wstat":
        text = build_wallet_stats_text(address)
    else:
        text = build_wallet_activity_text(address)
    if text is None:
        text = "⚠️ هاد المحفظة تحذفات."
    await query.message.reply_text(text, parse_mode="HTML")


# ---- 3) زر Top Performers — اختيار 1h/24h بلا كتابة ----

async def prompt_top_performers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ آخر ساعة", callback_data="topperf:1h")],
        [InlineKeyboardButton("📅 آخر 24 ساعة", callback_data="topperf:24h")],
    ])
    await update.message.reply_text("🥇 اختار المدة:", reply_markup=keyboard)


async def top_performers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, window = query.data.split(":", 1)
    text = build_top_performers_text(window)
    await query.message.reply_text(text, parse_mode="HTML")


# ============ إلغاء ============

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("تم الإلغاء.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ============ 🆕 Token Score ============

async def token_score_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استعمل: /tokenscore [Contract Address]")
        return
    mint = context.args[0]
    async with aiohttp.ClientSession() as session:
        market_data = await get_token_market_data(session, mint)
    distinct_buyers_ever = count_distinct_buyers_ever(mint)
    signal_counts = get_signal_event_counts(mint)
    score, label = calculate_token_score(market_data, distinct_buyers_ever, signal_counts)

    if not market_data:
        await update.message.reply_text(
            "⚠️ ما لقيتش بيانات سوق لهاد التوكن فـ DexScreener (يمكن جديد بزاف أو بلا Liquidity)."
        )
        return

    text = (
        f"🧠 <b>Token Score</b>\n\n"
        f"🪙 {market_data['symbol']} — <code>{mint}</code>\n"
        f"📊 Score: <b>{score}/100</b> {score_label_emoji(label)} ({label})\n\n"
        f"💧 Liquidity: ${market_data['liquidity_usd']:,.0f}\n"
        f"🧢 Market Cap: ${market_data['market_cap']:,.0f}\n"
        f"📈 Volume 24h: ${market_data['volume_24h']:,.0f}\n"
        f"⏳ عمر التوكن: {market_data['token_age']}\n\n"
        f"👥 عدد المحافظ المتابَعة لي شراتو: {distinct_buyers_ever}\n"
        f"🐋 أحداث Whale: {signal_counts.get('whale', 0)}\n"
        f"🔥 أحداث Smart Money: {signal_counts.get('smart_money', 0)}\n"
        f"🚀 أحداث Multi Wallet: {signal_counts.get('multi_wallet', 0)}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ============ 🆕 Wallet Activity Score ============

def build_wallet_activity_text(address: str):
    """🆕 دالة مشتركة: كتبني نص Wallet Activity — تُستعمل من /walletactivity وزر الاختيار"""
    wallet = get_wallet_by_address(address)
    if not wallet:
        return None
    activity_data = get_wallet_activity_data(address)
    score, label = calculate_wallet_activity_score(activity_data)
    last_seen = (
        datetime.fromtimestamp(activity_data["last_trade_ts"]).strftime("%Y-%m-%d %H:%M")
        if activity_data["last_trade_ts"] else "—"
    )
    return (
        f"⚡ <b>Wallet Activity Score — {wallet['name']}</b>\n\n"
        f"📊 Score: <b>{score}/100</b> ({label})\n"
        f"🔢 إجمالي الصفقات: {activity_data['total_trades']}\n"
        f"🎯 عدد التوكنات المختلفة: {activity_data['distinct_mints']}\n"
        f"🕒 آخر نشاط: {last_seen}"
    )


async def wallet_activity_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استعمل: /walletactivity [عنوان المحفظة]")
        return
    text = build_wallet_activity_text(context.args[0])
    if text is None:
        await update.message.reply_text("⚠️ ماكايناش هاد المحفظة.")
        return
    await update.message.reply_text(text, parse_mode="HTML")


# ============ 🆕 إدارة Helius Webhook (Admin فقط) ============

async def set_webhook_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    استعمل: /setwebhook https://your-public-domain.com
    خاص يكون عندك URL عمومي وصل ليه Helius (دومين ديالك، أو ngrok للتجربة).
    """
    if not is_admin(update, ADMIN_CHAT_ID):
        return
    if not context.args:
        await update.message.reply_text(
            "استعمل: /setwebhook https://your-public-domain.com\n\n"
            "⚠️ خاص يكون URL عمومي (HTTPS) وصل ليه Helius. "
            "للتجربة محلياً، يمكن تستعمل ngrok: ngrok http 8080"
        )
        return

    base_url = context.args[0].rstrip("/")
    secret = get_setting("webhook_secret") or uuid.uuid4().hex
    full_webhook_url = f"{base_url}{WEBHOOK_PATH}?secret={secret}"
    addresses = [w["address"] for w in get_all_wallets()]

    await update.message.reply_text("⏳ كنسجلو الـ Webhook عند Helius...")

    async with aiohttp.ClientSession() as session:
        webhook_id = await helius_create_or_update_webhook(session, addresses, full_webhook_url)

    if webhook_id:
        set_setting("webhook_id", webhook_id)
        set_setting("webhook_url", base_url)
        set_setting("webhook_enabled", "1")
        set_setting("webhook_secret", secret)
        await update.message.reply_text(
            f"✅ الـ Webhook تفعّل بنجاح!\n\n"
            f"دابا Helius غادي يبعتلك الصفقات مباشرة (real-time)، والـ Polling "
            f"غادي يبقى خدام معاه كـ شبكة أمان.\n\n"
            f"⚠️ تأكد أن السيرفر ديالك خدام على المنفذ {WEBHOOK_LISTEN_PORT} "
            f"وأن {base_url}{WEBHOOK_PATH} وصل ليه من برا."
        )
    else:
        await update.message.reply_text(
            "❌ فشل تسجيل الـ Webhook عند Helius. تحقق من logs/errors.log للتفاصيل، "
            "أو تأكد من الـ API Key وصحة الـ URL."
        )


async def disable_webhook_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update, ADMIN_CHAT_ID):
        return
    webhook_id = get_setting("webhook_id")
    if webhook_id:
        async with aiohttp.ClientSession() as session:
            await helius_delete_webhook(session, webhook_id)
    set_setting("webhook_enabled", "0")
    await update.message.reply_text("✅ تم إيقاف الـ Webhook. البوت رجع يعتمد على Polling فقط.")


async def webhook_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update, ADMIN_CHAT_ID):
        return
    enabled = get_setting("webhook_enabled") == "1"
    webhook_url = get_setting("webhook_url") or "—"
    webhook_id = get_setting("webhook_id") or "—"
    text = (
        f"🌐 <b>حالة Webhook</b>\n\n"
        f"الحالة: {'✅ مفعّل' if enabled else '❌ متوقف'}\n"
        f"URL: {webhook_url}\n"
        f"Webhook ID: <code>{webhook_id}</code>\n\n"
        f"Polling: ✅ خدام دايماً (كل {POLLING_INTERVAL_SECONDS} ثانية) كشبكة أمان"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ============================================================
# 🚀 نقطة التشغيل (main.py سابقاً)
# ============================================================

async def monitor_job(context):
    try:
        # 🆕 Adaptive Throttle: إذا كنا فحالة "راحة" بسبب Rate Limit متكرر،
        # نتخطاو الدورة كاملة — بلا ما نستهلكو كريدي فالفراغ على حالة معروفة
        if is_currently_throttled():
            if not _adaptive_state["alert_sent"]:
                remaining_min = int((_adaptive_state["throttled_until"] - time.time()) // 60) + 1
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=(
                            f"🐢 <b>Adaptive Throttle مفعّل</b>\n\n"
                            f"حسينا بـ Rate Limit متكرر من Helius، فبطأنا المراقبة تلقائياً "
                            f"لمدة ~{remaining_min} دقيقة باش نوفرو الكريدي المتبقي.\n"
                            f"البوت غايرجع للسرعة العادية وحدو."
                        ),
                        parse_mode="HTML",
                    )
                    _adaptive_state["alert_sent"] = True
                except Exception:
                    pass
            return

        was_stalled = _watchdog_state["alert_sent"]  # 🆕 كان الـ Watchdog سبق ونبه على توقف؟
        await check_all_wallets(context.bot, ADMIN_CHAT_ID)
        mark_monitor_success()  # 🆕 كنعلمو الـ Watchdog بلي الدورة نجحت

        if was_stalled:
            # 🆕 البوت رجع يخدم بعد ما كان متوقف — نعلم الأدمن بلا ما يحتاج يتفقد بنفسو
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text="✅ <b>البوت رجع يخدم عادي</b> — المراقبة نجحت من جديد.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
    except Exception as e:
        # 🆕 حماية إضافية: حتى لو وقع خطأ عام غير متوقع، الجوب الدوري ما يتوقفش
        # (job_queue غادي يعاود يخدمها فالمرة الجاية بعد POLLING_INTERVAL_SECONDS)
        logger.error(f"❌ خطأ عام فـ monitor_job: {e}")


EVALUATE_OUTCOMES_INTERVAL_SECONDS = 300  # كل 5 دقايق كنشوفو واش كاين صفقات وصل وقتها


async def evaluate_outcomes_job(context):
    """
    🆕 جوب دوري (كل 5 دقايق): كيقارن السعر الحالي مع السعر لحظة الشراء
    لكل الصفقات لي وصل وقتها (1 ساعة أو 24 ساعة)، وكيسجل النتيجة (Win-Rate
    الحقيقي). محمي بالكامل: خطأ فتوكن وحد ما يوقفش تقييم الباقي.
    """
    try:
        async with aiohttp.ClientSession() as session:
            for window, max_age in (("1h", 3600), ("24h", 86400)):
                pending = get_pending_outcome_evaluations(window, max_age)
                for row in pending:
                    try:
                        market_data = await get_token_market_data(session, row["mint"])
                        if not market_data or not market_data.get("price_usd"):
                            mark_outcome_evaluated_unknown(row["transaction_id"], window)
                            continue
                        current_price = market_data["price_usd"]
                        price_at_buy = row["price_at_buy"]
                        pct_change = ((current_price - price_at_buy) / price_at_buy) * 100
                        update_trade_outcome(row["transaction_id"], window, pct_change)
                    except Exception as e:
                        logger.error(f"❌ خطأ فتقييم Win-Rate لصفقة {row['transaction_id']}: {e}")
                        continue
    except Exception as e:
        logger.error(f"❌ خطأ عام فـ evaluate_outcomes_job: {e}")


async def digest_job(context):
    """
    🆕 جوب دوري (كل 30 دقيقة): كيبعت ملخص مجمّع لكل الصفقات "الضعيفة" (Score
    تحت العتبة) لي تفلترات من التنبيهات الفورية — باش المعلومة توصل بلا ما
    تغرق المستخدم بضجة على توكنات ضعيفة الاهتمام.
    """
    try:
        items = get_and_clear_digest_queue()
        if not items:
            return

        lines = [f"📋 <b>ملخص الصفقات المفلترة</b> (آخر {DIGEST_INTERVAL_SECONDS // 60} دقيقة)\n"]
        for item in items[:30]:  # حد أقصى باش الرسالة ما تطولش بزاف
            lines.append(
                f"• {item['symbol']} — {item['wallet_name']} — "
                f"Score {item['score']}/100 ({item['score_label']}) — ${item['usd_value']:,.0f}"
            )
        if len(items) > 30:
            lines.append(f"\n... و{len(items) - 30} صفقة أخرى (استعمل /search باش تشوفهم كاملين)")

        await broadcast(context.bot, ADMIN_CHAT_ID, "\n".join(lines))
    except Exception as e:
        logger.error(f"❌ خطأ عام فـ digest_job: {e}")


async def on_error(update, context):
    """🆕 Error handler عام لتيليكرام: يسجل أي خطأ غير متوقع فـ errors.log بلا ما يوقف البوت"""
    logger.error(f"❌ خطأ غير متوقع: {context.error}", exc_info=context.error)


def build_application() -> Application:
    """يبني ويسجل كل الهاندلرز (بلا ما يشغل حتى حاجة بعد)"""
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(on_error)

    # ---- محادثة إضافة/حذف/تعديل المحافظ ----
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📌 Add Wallet$"), add_wallet_start),
            MessageHandler(filters.Regex("^❌ Remove Wallet$"), remove_wallet_start),
            MessageHandler(filters.Regex("^✏️ Rename Wallet$"), rename_wallet_start),
        ],
        states={
            ADD_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_address)],
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
            REMOVE_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_remove_address)],
            RENAME_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_rename_address)],
            RENAME_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_rename_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # ---- أوامر أساسية ----
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conv_handler)

    # ---- الأزرار الرئيسية ----
    app.add_handler(MessageHandler(filters.Regex("^📋 Wallet List$"), list_wallets))
    app.add_handler(MessageHandler(filters.Regex("^⚙ Settings$"), settings_menu))
    app.add_handler(MessageHandler(filters.Regex("^📊 Statistics$"), statistics))
    app.add_handler(MessageHandler(filters.Regex("^📤 Export$"), export_data))
    app.add_handler(MessageHandler(filters.Regex("^💾 Backup$"), backup_database))
    app.add_handler(MessageHandler(filters.Regex("^👀 Watchlist$"), watchlist))
    app.add_handler(MessageHandler(filters.Regex("^🏆 Top Wallets$"), top_wallets_command))
    app.add_handler(MessageHandler(filters.Regex("^📅 Daily$"), daily_summary))
    app.add_handler(MessageHandler(filters.Regex("^🗓 Weekly$"), weekly_summary))
    # ---- 🆕 أزرار جداد: تحليل، فحص أمان، بحث، إحصائيات محفظة — بلا كتابة أوامر يدوياً ----
    app.add_handler(MessageHandler(filters.Regex("^🔬 Analyze$"), prompt_analyze))
    app.add_handler(MessageHandler(filters.Regex("^🛡️ Rug Check$"), prompt_rugcheck))
    app.add_handler(MessageHandler(filters.Regex("^🧠 Token Score$"), prompt_tokenscore))
    app.add_handler(MessageHandler(filters.Regex("^🔍 Search$"), prompt_search))
    app.add_handler(MessageHandler(filters.Regex("^📈 Wallet Stats$"), prompt_wallet_stats))
    app.add_handler(MessageHandler(filters.Regex("^⚡ Wallet Activity$"), prompt_wallet_activity))
    app.add_handler(MessageHandler(filters.Regex("^🥇 Top Performers$"), prompt_top_performers))
    app.add_handler(MessageHandler(filters.Regex("^📆 Monthly$"), monthly_summary))
    app.add_handler(MessageHandler(filters.Regex("^❓ Help$"), help_command))

    # ---- أزرار الإعدادات التفاعلية (Inline) ----
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^toggle_"))
    # ---- 🆕 زر عرض المعرف (ID) للمشاهدين الجداد ----
    app.add_handler(CallbackQueryHandler(show_my_id_callback, pattern="^show_my_id$"))
    # ---- 🆕 أزرار اختيار المحفظة (Wallet Stats / Wallet Activity) ----
    app.add_handler(CallbackQueryHandler(wallet_picker_callback, pattern="^(wstat|wact):"))
    # ---- 🆕 زر اختيار المدة لـ Top Performers ----
    app.add_handler(CallbackQueryHandler(top_performers_callback, pattern="^topperf:"))

    # ---- أوامر الإعدادات النصية ----
    app.add_handler(CommandHandler("setminusd", set_min_usd))
    app.add_handler(CommandHandler("setwhale", set_whale_threshold))
    app.add_handler(CommandHandler("setminscore", set_min_score))

    # ---- أوامر المشاهدين (Admin فقط) ----
    app.add_handler(CommandHandler("addviewer", add_viewer))
    app.add_handler(CommandHandler("removeviewer", remove_viewer))
    app.add_handler(CommandHandler("viewers", list_viewers))

    # ---- البحث والإحصائيات ----
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("walletstats", wallet_stats_command))
    app.add_handler(CommandHandler("daily", daily_summary))
    app.add_handler(CommandHandler("weekly", weekly_summary))
    app.add_handler(CommandHandler("monthly", monthly_summary))

    # ---- 🆕 Token Score / Wallet Activity Score ----
    app.add_handler(CommandHandler("tokenscore", token_score_command))
    app.add_handler(CommandHandler("walletactivity", wallet_activity_command))

    # ---- 🆕 Win-Rate الحقيقي + Rug Check ----
    app.add_handler(CommandHandler("topperformers", top_performers_command))
    app.add_handler(CommandHandler("rugcheck", rug_check_command))
    app.add_handler(CommandHandler("analyze", analyze_command))

    # ---- 🆕 إدارة Helius Webhook (Admin فقط) ----
    app.add_handler(CommandHandler("setwebhook", set_webhook_command))
    app.add_handler(CommandHandler("disablewebhook", disable_webhook_command))
    app.add_handler(CommandHandler("webhookstatus", webhook_status_command))

    # ---- 🆕 Handler عام (لازم يكون آخر واحد): كيستقبل الجواب على الأسئلة
    # المعلقة (awaiting_command) من أزرار Analyze/Rug Check/Token Score/Search.
    # ماكيتفعلش إلا كي ماكاين حتى Regex/Command آخر طابق قبلو.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_awaiting_text_input))

    # ---- الخدمة الدورية (Polling على Helius) — خدامة دايماً كشبكة أمان ----
    app.job_queue.run_repeating(monitor_job, interval=POLLING_INTERVAL_SECONDS, first=10)
    # ---- 🆕 Watchdog (فحص صحة البوت) ----
    app.job_queue.run_repeating(watchdog_job, interval=WATCHDOG_CHECK_INTERVAL_SECONDS, first=60)
    # ---- 🆕 تقييم Win-Rate (مقارنة السعر بعد 1h/24h) ----
    app.job_queue.run_repeating(evaluate_outcomes_job, interval=EVALUATE_OUTCOMES_INTERVAL_SECONDS, first=120)
    # ---- 🆕 Digest (ملخص الصفقات المفلترة) ----
    app.job_queue.run_repeating(digest_job, interval=DIGEST_INTERVAL_SECONDS, first=DIGEST_INTERVAL_SECONDS)
    # ---- 🆕 نسخة احتياطية تلقائية يومية (حماية من فقدان البيانات) ----
    app.job_queue.run_repeating(auto_backup_job, interval=AUTO_BACKUP_INTERVAL_SECONDS, first=300)

    return app


async def run_bot_forever():
    """
    🆕 يشغل البوت. إذا كاين رابط عمومي (RENDER_EXTERNAL_URL، كيعطيه Render
    تلقائياً)، البوت كيستعمل Telegram Webhook Mode — Telegram هو لي كيبعت
    الرسائل مباشرة، بلا Polling بالمرة. هذا كيحل نهائياً مشكل:
    "Conflict: terminated by other getUpdates request" لي كان كيصرا كي
    Render كيشغل نسخة جديدة قبل ما يقتل القديمة (Zero-Downtime Deploy) —
    بما ماكاينش حتى نسخة كتدير Polling، ماكاين والو يتخانق.

    إذا ماكاينش رابط عمومي (تجربة محلية مثلاً)، كيرجع تلقائياً لـ Polling.
    """
    init_db()

    if not BOT_TOKEN or not ADMIN_CHAT_ID or not HELIUS_API_KEY:
        logger.warning("⚠️ تأكد أنك عبيتي BOT_TOKEN و ADMIN_CHAT_ID و HELIUS_API_KEY قبل التشغيل")

    app = build_application()

    # 🔧 نشغلو سيرفر خفيف دايماً (بلا شرط) — خاص خدمات بحال Render لي كتحتاج
    # الخدمة تفتح Port، وإلا كتعتبرها "فشلات". نفس السيرفر كيستقبل Telegram
    # Webhook + Helius Webhook (إذا مفعّل) + Health Check.
    web_app = build_webhook_web_app(app)
    web_runner = web.AppRunner(web_app)
    await web_runner.setup()
    site = web.TCPSite(web_runner, WEBHOOK_LISTEN_HOST, WEBHOOK_LISTEN_PORT)
    await site.start()
    logger.info(f"🌐 Health/Webhook Server خدام على المنفذ {WEBHOOK_LISTEN_PORT}")

    # 🆕 Render كيعطي هاد المتغير تلقائياً لكل Web Service — بلا ما تحتاج تزيدو بنفسك
    public_url = os.environ.get("RENDER_EXTERNAL_URL")
    use_webhook_mode = bool(public_url)

    async with app:
        await app.start()

        if use_webhook_mode:
            telegram_secret = get_setting("telegram_webhook_secret")
            await app.bot.set_webhook(
                url=f"{public_url}{TELEGRAM_WEBHOOK_PATH}",
                secret_token=telegram_secret,
                drop_pending_updates=True,
            )
            logger.info(f"🚀 البوت بدا الخدمة بوضع Webhook (Telegram) — {public_url}{TELEGRAM_WEBHOOK_PATH}")
        else:
            # 🔧 Fallback للتجربة المحلية (بلا رابط عمومي): نتأكدو ماكاين حتى
            # Webhook قديم مسجل عند Telegram قبل ما نبداو Polling
            await app.bot.delete_webhook(drop_pending_updates=True)
            await app.updater.start_polling()
            logger.info("🚀 البوت بدا الخدمة بوضع Polling (تجربة محلية، بلا رابط عمومي)...")

        # 🆕 إشعار مباشر للأدمن — تعرف من التلفون بلا ما تحتاج تفتح Render/Logs
        try:
            mode_label = "Webhook" if use_webhook_mode else "Polling"
            await app.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"✅ <b>البوت بدا الخدمة</b> ({mode_label})\n\nكلشي خدام وكيراقب المحافظ دابا 🦇",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"❌ فشل إرسال إشعار بدء التشغيل للأدمن: {e}")

        try:
            # كنبقاو خدامين حتى توصل إشارة إيقاف (Ctrl+C / SIGTERM)
            await asyncio.Event().wait()
        finally:
            if not use_webhook_mode:
                await app.updater.stop()
            await app.stop()
            if web_runner:
                await web_runner.cleanup()


def main():
    """
    🆕 نقطة الدخول مع Auto-Restart: إذا طاح البوت كامل لأي سبب غير متوقع
    (مثلاً انقطاع انترنت طويل، أو خطأ ماكاناش متوقع)، كيعاود يشغل نفسه
    من بعد تأخير قصير، بدل ما يبقى واقف بلا رجعة.
    """
    while True:
        try:
            asyncio.run(run_bot_forever())
            break  # خروج عادي (Ctrl+C مثلاً) — ما نعاودوش التشغيل
        except KeyboardInterrupt:
            logger.info("🛑 البوت توقف يدوياً (Ctrl+C).")
            break
        except Exception as e:
            logger.error(f"💥 البوت طاح بخطأ غير متوقع: {e}")
            logger.error(f"🔄 نعاود التشغيل بعد {AUTO_RESTART_DELAY_SECONDS} ثواني...")
            time.sleep(AUTO_RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    main()
