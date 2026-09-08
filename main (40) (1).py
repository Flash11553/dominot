import asyncio
import datetime
import random
import os
import re
import json
import io
import logging
from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    InlineQueryResultCachedSticker,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils import executor
from aiogram.utils.exceptions import RetryAfter, InvalidQueryID
from aiohttp import web
from aiogram.dispatcher.middlewares import BaseMiddleware
from pymongo import MongoClient

# Loglamaq üçün əlavə edildi
logging.basicConfig(level=logging.INFO)

# =======================================================
# 1. KONFİQURASİYA VƏ MONGODB BAĞLANTISI
# =======================================================
# Təhlükəsizlik üçün os.getenv() ilə Config Vars-dan oxumaq məsləhətdir.
# İndi isə yer tutucu olaraq saxlanılır.
TOKEN = os.getenv("BOT_TOKEN", "8836570400:AAHQBUqDX2srWhJ-qLVevzzcM41FybzacrQ") 
# 🟢 YENİ: Mini App-ın canlı ünvanı (Cloudflare Workers-ə deploy etdiyin ünvan).
# Frontend-i yenidən deploy etsən belə bu adres adətən sabit qalır (yalnız
# backend tunel ünvanı dəyişəndə frontend faylındakı API_BASE-i yenilə).
APP_URL = os.getenv("APP_URL", "https://REPLACE-WITH-YOUR-WORKERS-URL.workers.dev")
MONGO_URL = os.getenv("MONGO_URL", "mongodb+srv://flash:flash@vipbotlar.v2sjp3w.mongodb.net/?appName=Vipbotlar")

BOT_USERNAME = None # Botun username-i on_startup zamanı təyin ediləcək
BOT_ID = None # Botun ID-si on_startup zamanı təyin ediləcək (get_me() təkrarını azaltmaq üçün)

# MongoDB Bağlantısı (Botun düzgün işləməsi üçün zəruridir)
try:
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client["domino_bot_db"]
    ratings_collection = db["ratings"]
    chats_collection = db["chats"]
    users_collection = db["users"]
    history_collection = db["game_history"]
    logging.info("MongoDB bağlantısı uğurlu.")
except Exception as e:
    logging.error(f"MongoDB bağlantı xətası: {e}. Reytinq funksiyası işləməyəcək.")
    ratings_collection = None
    chats_collection = None
    users_collection = None
    history_collection = None

# /broadcast əmrini yalnız bu ID-lərdəki şəxslər işlədə bilər
SUDO_USERS = os.getenv("SUDO_USERS", "7578184117").split(",")

bot = Bot(TOKEN)
dp = Dispatcher(bot)


@dp.errors_handler()
async def global_error_handler(update: types.Update, exception: Exception):
    # 🟢 DÜZƏLİŞ: Hər hansı handlerdə tutulmamış xəta botu "dondurmasın" —
    # sadəcə loga yazılır və növbəti update-lər normal işləməyə davam edir.
    logging.exception(f"Tutulmamış xəta: {exception} | Update: {update}")
    return True

# =======================================================
# 2. MONGODB VƏ KÖMƏKÇİ FUNKSİYALAR
# =======================================================

# 🟢 BURAYA ƏLAVƏ EDİN ⬇️

games = {}
TIMEOUT_MINUTES = 5
CHECK_INTERVAL_SECONDS = 150 # 5 dəqiqədən bir yoxlama
LOBBY_TIMEOUT_MINUTES = 5

async def check_for_inactive_games():
    """Hər CHECK_INTERVAL_SECONDS-dan bir vaxtı keçmiş oyunları RAM-da yoxlayır və silir."""
    
    while True:
        # Taskın dayanmaması üçün intervala uyğun gözləyirik
        await asyncio.sleep(CHECK_INTERVAL_SECONDS) 

        # 🟢 DÜZƏLİŞ: Bu dövrədə hər hansı gözlənilməz xəta baş versə belə
        # (məs. şəbəkə problemi), background task tamamilə ölüb bir daha
        # işləməsin deyə, bütün gövdəni try/except ilə əhatə edirik.
        try:
            current_time = datetime.datetime.utcnow()
        
            # 1. Timeout Hədlərini Hesabla
            active_timeout_threshold = current_time - datetime.timedelta(minutes=TIMEOUT_MINUTES)
            lobby_timeout_threshold = current_time - datetime.timedelta(minutes=LOBBY_TIMEOUT_MINUTES)
            
            # Silinəcək oyunların siyahısı (chat_id, səbəb) formatında
            games_to_delete = [] 

            for chat_id, game in list(games.items()):
                
                game_has_started = game.get("turn") is not None
                
                # 🟢 1. LOBBY TIMEOUT YOXLANILMASI
                last_lobby_activity = game.get("last_lobby_activity") 
                
                # Şərt: Lobbidirsə VƏ oyun başlamayıbsa
                if last_lobby_activity and not game_has_started:
                    
                    # Vaxt limiti keçibsə
                    if last_lobby_activity < lobby_timeout_threshold:
                        
                        if len(game["players"]) < 2:
                            # Ssenari 1: Oyunçu çatışmazlığı
                            games_to_delete.append((chat_id, "lobby_timeout_low_players"))
                            continue
                        
                        elif len(game["players"]) >= 2:
                            # Ssenari 2: Kifayət qədər oyunçu var, amma başlatmırlar
                            games_to_delete.append((chat_id, "lobby_timeout_full"))
                            continue

                # 🔴 2. AKTİV OYUN TIMEOUT YOXLANILMASI
                if game_has_started:
                    last_activity = game.get("last_activity")
                    
                    # Əgər aktiv oyun fəaliyyətsizdirsə
                    if last_activity and last_activity < active_timeout_threshold: 
                        games_to_delete.append((chat_id, "active_timeout"))
            
            
            # 3. Silinmə prosesi və mesaj göndərilməsi
            for chat_id, reason in games_to_delete:
                game = games.get(chat_id)
                if not game: continue

                try:
                    games.pop(chat_id, None)
                    
                    if reason == "lobby_timeout_low_players":
                        message = (f"⏳ **Oyun Dayandırdı**\n"
                                   f"Qeydiyyat başlayandan **{LOBBY_TIMEOUT_MINUTES} dəqiqə** keçdi, lakin ən az 2 oyunçu qoşulmadı. Yeni oyun üçün /domino yazın ✅")
                    elif reason == "lobby_timeout_full":
                         message = (f"⏳ **Oyun Dayandırdı.**\n"
                                   f"Qeydiyyat başlayandan **{LOBBY_TIMEOUT_MINUTES} dəqiqə** keçdi. Kifayət qədər oyunçu olsa da, oyun başladılmadı. Yeni oyun üçün /domino yazın ✅")
                    else: # active_timeout
                        message = (f"⛔ **Oyun Dayandırıldı!**\n"
                                   f"Oyun son {TIMEOUT_MINUTES} dəqiqə ərzində oynanılmadığı üçün avtomatik dayandırıldı. Yeni oyun üçün /domino yazın ✅")
                    
                    logging.info(f"Oyun {reason} səbəbi ilə dayandırıldı. Chat ID: {chat_id}")

                    await bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
                    
                except Exception as e:
                    logging.error(f"Timeout bildirişi göndərilərkən xəta: {e}")

        except Exception as e:
            logging.exception(f"check_for_inactive_games dövrəsində gözlənilməz xəta: {e}")

# --- Broadcast üçün qeydiyyat (heç vaxt silinmir, digər botlardakı ilə eyni məntiq) ---

def add_served_chat(chat_id):
    if chats_collection is None:
        return
    if chats_collection.find_one({"chat_id": chat_id}):
        return
    chats_collection.insert_one({"chat_id": chat_id})


def get_served_chats() -> list:
    if chats_collection is None:
        return []
    return list(chats_collection.find({}))


def add_served_user(user_id):
    if users_collection is None:
        return
    if users_collection.find_one({"user_id": user_id}):
        return
    users_collection.insert_one({"user_id": user_id})


def get_served_users() -> list:
    if users_collection is None:
        return []
    return list(users_collection.find({}))


def _log_broadcast_error(kind, entity_id, error_message):
    filename = "errors_chats.txt" if kind == "chat" else "errors_users.txt"
    with open(filename, "a", encoding="utf-8") as file:
        file.write(f"🆔ID: {entity_id}, ❌Xəta: {error_message}\n")


class ActivityRegisterMiddleware(BaseMiddleware):
    """Hər mesajda istifadəçi/qrupu səssizcə Mongo-ya yazır (broadcast üçün).
    Digər handler-lərin işinə heç bir şəkildə mane olmur (aiogram middleware
    normal handler dispatch-dən AYRICA, paralel işləyir)."""

    async def on_process_message(self, message: types.Message, data: dict):
        try:
            if message.chat.type == "private":
                if message.from_user:
                    add_served_user(message.from_user.id)
            else:
                add_served_chat(message.chat.id)
                if message.from_user:
                    add_served_user(message.from_user.id)
        except Exception:
            pass


dp.middleware.setup(ActivityRegisterMiddleware())


# --- "Boz" (oynana bilməyən) daşlar üçün - generate_grayed_stickers.py skripti ilə yaradılır ---
GRAYED_STICKERS_FILE = "grayed_stickers.json"
DOMINO_GRAYED = {}       # stone -> boz stikerin file_id-si (göstərmək üçün)
GRAYED_UNIQUE = {}       # stone -> boz stikerin file_unique_id-si (seçim tanımaq üçün)
if os.path.exists(GRAYED_STICKERS_FILE):
    try:
        with open(GRAYED_STICKERS_FILE, "r", encoding="utf-8") as f:
            _grayed_raw = json.load(f)
        for _stone, _info in _grayed_raw.items():
            if isinstance(_info, dict):
                DOMINO_GRAYED[_stone] = _info.get("file_id")
                if _info.get("file_unique_id"):
                    GRAYED_UNIQUE[_stone] = _info["file_unique_id"]
        logging.info(f"{len(DOMINO_GRAYED)} boz (grayed) daş stikeri yükləndi.")
    except Exception as e:
        logging.error(f"grayed_stickers.json oxunarkən xəta: {e}")

def escape_md(text: str) -> str:
    """İstifadəçi adları kimi sərbəst mətnləri legacy Markdown üçün təhlükəsizləşdirir.

    🟢 DÜZƏLİŞ: Fantaziya şriftli (𝓜𝓪𝓻𝓲), tək emoji/rəqəm/simvol və s. adlar
    Markdown-un xüsusi simvollarını ('_', '*', '`', '[') ehtiva edə bilər.
    Bu simvollar kölgələnmədən mesaja salınanda Telegram "can't parse
    entities" xətası qaytarır və bütün funksiya (qeydiyyat də daxil olmaqla)
    yarımçıq kəsilirdi. Bu funksiya həmin simvolları "\\" ilə kölgələyir ki,
    istənilən ad problemsiz göndərilə bilsin.
    """
    if not text:
        return text
    return re.sub(r'([_*`\[])', r'\\\1', text)


def build_mention(user: types.User) -> str:
    """İstifadəçi üçün 'tag' (username) varsa @username, yoxdursa adını qaytarır."""
    if user.username:
        return f"@{user.username}"
    return escape_md(user.first_name)


async def bot_has_delete_permission(chat_id: int) -> bool:
    """Botun bu qrupda mesaj silmə səlahiyyəti (admin + can_delete_messages) olub-olmadığını yoxlayır."""
    try:
        member = await bot.get_chat_member(chat_id, BOT_ID)
        if member.status == "creator":
            return True
        if member.status == "administrator":
            return bool(getattr(member, "can_delete_messages", False))
        return False
    except Exception:
        return False


async def send_move_warning(chat_id: int, wrong_msg: types.Message, extra_text: str):
    """Səhv gedişdən sonra xəbərdarlığı HƏMİŞƏ QRUPA göndərir (heç vaxt şəxsi mesajla YOX).

    🟢 DÜZƏLİŞ: Səhvən atılan (boz) stiker indi SİLİNİR, xəbərdarlıq isə adi
    mesaj kimi göndərilir.
    """
    mention = build_mention(wrong_msg.from_user)
    text = f"❌ **{mention}**, Bu gediş uyğun deyil:\n{extra_text}"

    try:
        await wrong_msg.delete()
    except Exception:
        pass

    await bot.send_message(chat_id, text, parse_mode="Markdown")


def stone_fits_board(game, stone: str) -> bool:
    """Konkret bir daşın hazırkı taxta uclarına uyğun gəlib-gəlmədiyini yoxlayır."""
    if game["left"] is None:
        return True  # taxta boşdursa, istənilən daş oynana bilər
    a, b = map(int, stone.split("-"))
    return a == game["left"] or b == game["left"] or a == game["right"] or b == game["right"]


def update_winner_count(user_id, name):
    """Qalibin qələbə sayını MongoDB-də artırır."""
    if ratings_collection is None: return
    user_id_str = str(user_id) 
    ratings_collection.update_one(
        {"_id": user_id_str},
        {"$inc": {"wins": 1}, "$set": {"name": name}},
        upsert=True
    )

def load_top_ratings():
    """MongoDB-dən ən çox qələbə qazanan 25 istifadəçini yükləyir."""
    if ratings_collection is None: return []
    return list(ratings_collection.find().sort("wins", -1).limit(25))

# =======================================================
# SƏVİYYƏ VƏ RÜTBƏ SİSTEMİ (UNO botu ilə eyni məntiq)
# =======================================================
# Hər dəfə bir oyunu qazandığında (update_winner_count) xalın (qələbə sayın)
# 1 artır. Xallar (qələbə sayı) üst-üstə toplandıqca səviyyə/rütbə avtomatik
# yüksəlir - hamı 1-ci səviyyədən başlayır.

# (lazımi_qələbə_sayı, səviyyə, rütbə_adı)
LEVELS = [
    (0,   1,  "🔰 Yeni Başlayan"),
    (5,   2,  "🎮 Həvəskar"),
    (15,  3,  "🥇 Təcrübəli"),
    (30,  4,  "⚔️ Peşəkar"),
    (50,  5,  "💠 Elit"),
    (80,  6,  "🔥 Ekspert"),
    (120, 7,  "👑 Master"),
    (180, 8,  "🌟 Qrandmaster"),
    (250, 9,  "🏆 Əfsanə"),
    (350, 10, "💎 Domino İmperatoru"),
]


def compute_level(wins: int):
    """Qələbə sayına görə (səviyyə, rütbə_adı) qaytarır."""
    wins = wins or 0
    level, rank_name = LEVELS[0][1], LEVELS[0][2]
    for threshold, lvl, name in LEVELS:
        if wins >= threshold:
            level, rank_name = lvl, name
        else:
            break
    return level, rank_name


def get_user_wins(user_id) -> int:
    """MongoDB-dən istifadəçinin qələbə sayını oxuyur (yoxdursa 0)."""
    if ratings_collection is None:
        return 0
    doc = ratings_collection.find_one({"_id": str(user_id)})
    if not doc:
        return 0
    return doc.get("wins", 0) or 0


def record_game_played(user_id, name):
    """Tamamlanmış hər oyunda İŞTİRAK EDƏN bütün oyunçular üçün (qalib
    olsun, olmasın) oynanılan oyun sayını 1 artırır - /profile-dəki
    'Oyun' sayı buradan gəlir."""
    if ratings_collection is None:
        return
    ratings_collection.update_one(
        {"_id": str(user_id)},
        {"$inc": {"games": 1}, "$set": {"name": name}},
        upsert=True
    )


def save_game_history(chat_id, winner_id, winner_name, reason, players: dict, scores: dict):
    """Bitmiş hər oyunu 'game_history' kolleksiyasına yazır.

    🟢 YENİ: Bu, həm bot (qrup) tərəfindən, həm də gələcəkdə mini app
    tərəfindən OXUNACAQ ortaq tarixçə mənbəyidir — hər iki interfeys eyni
    məlumatı göstərsin deyə, oyunun bitdiyi HƏR yerdə (bloklanma və ya
    əl boşalması) bu funksiya çağırılır.
    """
    if history_collection is None:
        return
    details = []
    for uid, name in players.items():
        data = scores.get(uid, {"score": 0, "tiles": 0})
        details.append({
            "user_id": str(uid),
            "name": name,
            "tiles": data.get("tiles", 0),
            "score": data.get("score", 0),
        })
    details.sort(key=lambda d: d["score"])
    history_collection.insert_one({
        "chat_id": chat_id,
        "winner_id": str(winner_id),
        "winner_name": winner_name,
        "reason": reason,
        "players": [str(uid) for uid in players.keys()],
        "details": details,
        "created_at": datetime.datetime.utcnow(),
    })

DOMINO = {

    "0-0": "CAACAgIAAyEFAATAdUXNAAJGg2mHd5OUggKZTg6OazUIecmg-kdIAAKJfgACWEtBS8lZlTOQp_OOOgQ",

    "0-1": "CAACAgIAAyEFAAThAAHIbgACNA1phx6hZBTMpQwG3V_6kSmdl-E1sQACIncAAr05QEsRHrzlq51tZzoE",

    "0-2": "CAACAgIAAyEFAATAdUXNAAJIk2mHfG_T9b-lbRYNqmuJBvv_x77OAAKSfQACqXVBS9Q_UmbH9sHPOgQ",

    "0-3": "CAACAgIAAyEFAATakR8LAAIFxGmHbXCr7uWAHM1G7IfNBGBVHwqhAAK3cQACuw1BS4gPkE6xlpHIOgQ",

    "0-4": "CAACAgIAAyEFAATAdUXNAAJIkWmHfGvF60VVJ5v8Gk-2WpsjhHj2AAKbhAAC0iRZSTU6UFhn1r0eOgQ",

    "0-5": "CAACAgIAAyEFAATAdUXNAAJIZmmHe9YO6yG4PdoxgQgr2eCQ_cLNAAI6ewACJKhAS8ynAiexW-63OgQ",

    "0-6": "CAACAgIAAyEFAASB6ZHnAAJl_mmLogp94yvnr8WPMmrt9M95Ip1KAALbeAACuRlBS6fcAyIVVqxcOgQ",

    "1-1": "CAACAgIAAyEFAATakR8LAAIF02mHbaJawLCVyz_e2wue3VJlCSN7AAKKjQACGj9BSQe2fE9YCK8SOgQ",

    "1-2": "CAACAgIAAyEFAASuUMZCAAEEozlph7Y52P0AAS7P-dLVtgO97p0rPVkAAuJ-AAI2n0FLkYKoLFKPQzM6BA",

    "1-3": "CAACAgIAAyEFAATl-DveAANBaYjjp2A745YwGe_wNBc6UJU5xYUAAqF6AALZC0BLnGQpnLoFOZA6BA",

    "1-4": "CAACAgIAAyEFAASB6ZHnAAJl9mmLoaIJI9Onr3ajVs95YoZqHE-0AAIIcgACK-1ASxHkAfwDqBjvOgQ",

    "1-5": "CAACAgIAAyEFAASB6ZHnAAJl92mLoaQVKUm_SM7dZkjiLKv4fIo9AAKMcQACzdhBS4cn4G5Zq3xCOgQ",

    "1-6": "CAACAgIAAyEFAATkcpubAAIL-mmKXCRjmEbhGAyVQvyYPIVw6N8WAAJYbQACqRBBS9DySg1d0RHuOgQ",

    "2-2": "CAACAgIAAx0CZsyQ0AABAVwPaYiTe2fXIpRQwcp_wkxjZunz-l0AAmZ7AALGUUBLQiPTtsnW5686BA",

    "2-3": "CAACAgIAAyEFAATeziFGAAM1aYjWBqSk6u-LBb77sBHyun5GdAIAAk50AAKntkBLfIKs1GZIVCc6BA",

    "2-4": "CAACAgIAAyEFAASuUMZCAAEEvkdpikT1u0nhK4ZrpyCCatomzwz-7AACFHkAAof_QEuz4Eik6GWgbDoE",

    "2-5": "CAACAgIAAyEFAATm6xS7AAMKaYbyeTCPZh-UaeOlAR3RgNeLchAAAgx4AAKhU0BLqLZDe-AF4lY6BA",

    "2-6": "CAACAgIAAyEFAATAdUXNAAJIl2mHfHX6LLzczzMmFOnRTBObHeXBAAJuegACEAxAS068auG6tfSTOgQ",

    "3-3": "CAACAgIAAyEFAATl-DveAAMgaYjikV_gqG29PjBbs3WKn-l9sn0AAgN3AAKeFkFLOwAB4KdkN072OgQ",

    "3-4": "CAACAgIAAyEFAASuUMZCAAEEvlJpikUbOW9U_HE6CPM73jEGYHp-9gACUW8AAteuQEtPAwH_7_4uezoE",

    "3-5": "CAACAgIAAyEFAATakR8LAAIFyGmHbYjQPNet0hcPKJPtIvSLCJcIAAJpfQACaHBAS6HTPRmbrvgmOgQ",

    "3-6": "CAACAgIAAyEFAATkcpubAAIL5WmKW-DjmHoZOrPkNDXmzhH5vX-IAALtvwACLspAS1EIr2BxfA4tOgQ",

    "4-4": "CAACAgIAAyEFAASuUMZCAAEEswVpiPnRRL2oBe_vU1Unm0HtFmQVXwACa30AAsPqQEsoYNxxfqxgMjoE",

    "4-5": "CAACAgIAAyEFAASuUMZCAAEEswhpiPnfCsTxLObG0xwr2VxoO3uhmQAComkAAmq5QEu_tPtmio3psDoE",

    "4-6": "CAACAgIAAyEFAATgi0JIAAIDF2mI9J1QvK85a5wlItqvltQ54VIGAAISdAACHKhBS2A0kjx4j3FhOgQ",

    "5-5": "CAACAgIAAyEFAATm6xS7AAMMaYbyqEVYykqCuXN28I0d7hYIg1EAAuZxAAIs9kFLzIDvtqEk6206BA",

    "5-6": "CAACAgIAAyEFAATAdUXNAAJIZWmHe86PPEy0t_699loY4-2jocfjAAIZdgACJaA4SyaMcWvTkHw8OgQ",

    "6-6": "CAACAgIAAyEFAATm6xS7AAMNaYby12JgT5IPkm9ugRKbsQ4s9fQAArl0AAJAAAFBS0xagljp7surOgQ"

}
PASS_ID = "CAACAgIAAyEFAATkcpubAAIMeWmKXWnRrOASZmjRlEMxzAf_dtbZAAJQdwACovM4S5xdlHnxogtLOgQ" 
TAKE_STONE_ID = "CAACAgIAAyEFAASB6ZHnAAJmBGmLooHH34hYFRCNFO_dTETwe-fIAALFdQACrhlBS6gjrmBr8NfgOgQ" 

STICKER_TO_STONE = {v: k for k, v in DOMINO.items()}
STICKER_TO_STONE[PASS_ID] = "PASS"
STICKER_TO_STONE[TAKE_STONE_ID] = "TAKE"

DOMINO_UNIQUE = {
    "0-0": "AgADiX4AAlhLQUs", "0-1": "AgADIncAAr05QEs", "0-2": "AgADkn0AAql1QUs",
    "0-3": "AgADt3EAArsNQUs", "0-4": "AgADm4QAAtIkWUk", "0-5": "AgADOnsAAiSoQEs",
    "0-6": "AgAD23gAArkZQUs", "1-1": "AgADio0AAho_QUk", "1-2": "AgAD4n4AAjafQUs",
    "1-3": "AgADoXoAAtkLQEs", "1-4": "AgADCHIAAivtQEs", "1-5": "AgADjHEAAs3YQUs",
    "1-6": "AgADWG0AAqkQQUs", "2-2": "AgADZnsAAsZRQEs", "2-3": "AgADTnQAAqe2QEs",
    "2-4": "AgADFHkAAof_QEs", "2-5": "AgADDHgAAqFTQEs", "2-6": "AgADbnoAAhAMQEs",
    "3-3": "AgADA3cAAp4WQUs", "3-4": "AgADUW8AAteuQEs", "3-5": "AgADaX0AAmhwQEs",
    "3-6": "AgAD7b8AAi7KQEs", "4-4": "AgADa30AAsPqQEs", "4-5": "AgADomkAAmq5QEs",
    "4-6": "AgADEnQAAhyoQUs", "5-5": "AgAD5nEAAiz2QUs", "5-6": "AgADGXYAAiWgOEs",
    "6-6": "AgADuXQAAkAAAUFL",
    "PASS": "AgADUHcAAqLzOEs",
    "TAKE": "AgADxXUAAq4ZQUs"
}

# Ağ VƏ boz versiyaların hər ikisini eyni axtarış lüğətində birləşdiririk ki,
# oyunçu boz daşı seçsə belə, hansı daş olduğu düzgün tanınsın.
# DİQQƏT: bu, unique_id -> stone lüğətidir (əksinə YOX!) - çünki hər daşın
# HƏM ağ, HƏM boz versiyasının unique_id-si var; stone adı ilə açar düzəltsək
# (əvvəlki səhv kimi) ağ və boz bir-birinin üstündən yazıb məhv edirdi.
ALL_UNIQUE_LOOKUP = {}
for _stone, _uid in DOMINO_UNIQUE.items():
    ALL_UNIQUE_LOOKUP[_uid] = _stone
for _stone, _uid in GRAYED_UNIQUE.items():
    ALL_UNIQUE_LOOKUP[_uid] = _stone

# =======================================================
# CANLІ TAXTA ŞƏKLİ — S-ziqzaq düzümü, hər gedişdən sonra
# YENİ şəkil göndərilir (Seçiminizi Edin düyməsindən əvvəl)
# =======================================================

import math as _math

# ── Rənglər ───────────────────────────────────────────────────────────────────
_BG       = (30, 120, 100)    # taxta fonu (yaşıl)
_TILE_BG  = (252, 248, 235)   # sümük rəngli daş
_TILE_BD  = (55, 35, 8)       # tünd kənar
_DOT_C    = (12, 12, 12)      # nöqtə rəngi
_SEP_C    = (55, 35, 8)       # ayırıcı xətt
_HL_L     = (0,  220, 80)     # sol uc (yaşıl highlight)
_HL_R     = (30, 150, 255)    # sağ uc (mavi highlight)
_HDR_BG   = (18, 18, 42)      # başlıq + oyunçu bloku fonu
_HDR_TXT  = (255, 210, 0)     # qızılı başlıq yazısı
_TURN_BG  = (55, 55, 130)     # növbəsi olan oyunçu fonu
_NORM_BG  = (22, 22, 55)      # digər oyunçu fonu
_TURN_TXT = (255, 235, 80)    # növbə yazı rəngi
_NORM_TXT = (195, 195, 215)   # normal yazı rəngi
_WIN_BG   = (170, 125, 0)     # qalib fonu (qızılı)
_WIN_TXT  = (255, 255, 255)   # qalib yazı (ağ)

# ── Daş ölçüləri ──────────────────────────────────────────────────────────────
_TW   = 44    # üfüqi daşın hündürlüyü (piksel)
_TH   = 84    # üfüqi daşın eni (piksel)
_GAP  = 0     # daşlar arası məsafə — 0: daşlar bir-birinə tam bitişik olsun
_COLS = 9     # sıradakı max daş sayı (28 daş 4 sıraya bölünür, tam sığır)

def _font(path, size):
    try:    return ImageFont.truetype(path, size)
    except: return ImageFont.load_default()

_FB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

import unicodedata as _unicodedata
try:
    from fontTools.ttLib import TTFont as _TTFont
except Exception:
    _TTFont = None

# DejaVuSans şəkil fontu bütün simvolları çəkə bilmir: (1) adi emojilər,
# (2) "fancy/stylish" ad generatorlarının işlətdiyi xüsusi Unicode hərflər
# (məs. 𝓛𝓮𝓰𝓮𝓷𝓭), (3) tamamilə dəstəklənməyən başqa skriptlər/simvollar.
# Bunlar sətirdə qalanda PIL onları boş "tofu" qutucuqları kimi göstərir və
# nəticədə ad ya heç görünmür, ya da yanında boşluq qalır.
#
# Həll: əvvəlcə Unicode NFKC normallaşdırması aparılır — bu, "fancy" hərfləri
# (Mathematical Alphanumeric Symbols və s.) avtomatik adi hərflərə çevirir
# (𝓛𝓮𝓰𝓮𝓷𝓭 -> Legend). Sonra fontun HƏQİQİ dəstəklədiyi simvollar siyahısı
# (cmap) yoxlanılıb, qalan dəstəklənməyən simvollar (əsl emoji, tofu yaradan
# simvollar və s.) sətirdən çıxarılır — beləliklə adın dəstəklənən hissəsi
# TAM göstərilir, boş qutucuq/boşluq qalmır.
_UNSUPPORTED_GLYPHS_RE = re.compile(
    "["
    "\U0001F000-\U0001FFFF"   # emoji (əyləncə, üz, obyekt və s. blokları)
    "\U00002600-\U000027BF"   # müxtəlif simvollar və dingbatlar
    "\U00002300-\U000023FF"   # texniki simvollar (⏳ və s.)
    "\U00002B00-\U00002BFF"   # ox və şəkil simvolları
    "\U0001F1E6-\U0001F1FF"   # bayraq (regional indicator) hərfləri
    "\U0000FE00-\U0000FE0F"   # variation selector-lar
    "\U0000200D"              # zero-width joiner
    "\U00002190-\U000021FF"   # oxlar
    "]+"
)

def _load_font_cmap(path):
    """Fontun daxilində HƏQİQƏTƏN çəkilə bilən simvolların (cmap) siyahısını qaytarır."""
    if _TTFont is None:
        return None
    try:
        tt = _TTFont(path, lazy=True)
        chars = set()
        for table in tt["cmap"].tables:
            try:
                chars.update(table.cmap.keys())
            except Exception:
                continue
        return chars
    except Exception:
        return None

_FONT_CMAP = _load_font_cmap(_FR) or _load_font_cmap(_FB)

def _clean_display_text(text: str) -> str:
    """Adı/rütbəni fontun çəkə biləcəyi formaya salır:
    1) 'fancy' Unicode hərfləri adi hərflərə çevirir (NFKC),
    2) çəkilə bilməyən emoji/simvolları silir,
    3) fontun cmap-ində olmayan HƏR HANSI digər simvolu da silir
       (beləcə tofu qutucuqlar əvəzinə ad tam görünür)."""
    if not text:
        return text
    normalized = _unicodedata.normalize("NFKC", text)
    cleaned = _UNSUPPORTED_GLYPHS_RE.sub("", normalized)
    if _FONT_CMAP is not None:
        cleaned = "".join(
            ch for ch in cleaned
            if ch.isspace() or ord(ch) in _FONT_CMAP
        )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

def _dot_pts(val, x, y, w, h):
    """Bir yarım-daşdakı nöqtə koordinatları"""
    cx, cy = x+w//2, y+h//2
    lx, rx = x+7, x+w-7
    ty, by = y+7, y+h-7
    return {
        0: [],
        1: [(cx, cy)],
        2: [(lx, ty), (rx, by)],
        3: [(lx, ty), (cx, cy), (rx, by)],
        4: [(lx, ty), (rx, ty), (lx, by), (rx, by)],
        5: [(lx, ty), (rx, ty), (cx, cy), (lx, by), (rx, by)],
        6: [(lx, ty), (rx, ty), (lx, cy), (rx, cy), (lx, by), (rx, by)],
    }.get(val, [])

def _draw_dots(draw, pts, r=4):
    for px, py in pts:
        draw.ellipse([px-r, py-r, px+r, py+r], fill=_DOT_C)

def _draw_tile_h(draw, x, y, a, b, hl=None):
    """Üfüqi daş: sol yarım=a, sağ yarım=b. Ölçü: _TH x _TW"""
    W, H = _TH, _TW
    if hl == "left":
        draw.rounded_rectangle([x-3, y-3, x+W+3, y+H+3], radius=8, outline=_HL_L, width=3)
    elif hl == "right":
        draw.rounded_rectangle([x-3, y-3, x+W+3, y+H+3], radius=8, outline=_HL_R, width=3)
    draw.rounded_rectangle([x, y, x+W, y+H], radius=6, fill=_TILE_BG, outline=_TILE_BD, width=2)
    mid = x + W//2
    draw.line([(mid, y+5), (mid, y+H-5)], fill=_SEP_C, width=2)
    _draw_dots(draw, _dot_pts(a, x+2,   y+2, W//2-3, H-4))
    _draw_dots(draw, _dot_pts(b, mid+2, y+2, W//2-3, H-4))

def _draw_tile_v(draw, x, y, a, b, hl=None):
    """Şaquli daş (dönüş sütunu üçün): yuxarı yarım=a, aşağı yarım=b. Ölçü: _TW x _TH"""
    W, H = _TW, _TH
    if hl == "left":
        draw.rounded_rectangle([x-3, y-3, x+W+3, y+H+3], radius=8, outline=_HL_L, width=3)
    elif hl == "right":
        draw.rounded_rectangle([x-3, y-3, x+W+3, y+H+3], radius=8, outline=_HL_R, width=3)
    draw.rounded_rectangle([x, y, x+W, y+H], radius=6, fill=_TILE_BG, outline=_TILE_BD, width=2)
    mid = y + H//2
    draw.line([(x+5, mid), (x+W-5, mid)], fill=_SEP_C, width=2)
    _draw_dots(draw, _dot_pts(a, x+2, y+2,   W-4, H//2-3))
    _draw_dots(draw, _dot_pts(b, x+2, mid+2, W-4, H//2-3))

_ROW_LEN  = 7   # bir üfüqi sırada neçə daş olsun, sonra dönüş başlayır
_TURN_LEN = 2   # dönüş sütununda neçə şaquli daş olsun

def _layout_board(tiles, ox, oy, row_len=_ROW_LEN, turn_len=_TURN_LEN):
    """
    S-ziqzaq düzüm: üfüqi sıra (row_len ədəd) -> şaquli dönüş sütunu
    (turn_len ədəd) -> əks istiqamətdə üfüqi sıra ... QOŞA daşlar HƏMİŞƏ
    cari axının perpendikulyar istiqamətində çəkilir (üfüqi sırada şaquli,
    dönüş sütununda üfüqi), öz seqmentinin mərkəz xəttinə TAM ORTALANIR.

    KÜNC (dönüş) DÜZƏLİŞİ: hər dönüşdə yeni seqmentin başladığı kənar,
    köhnə seqmentin sonuncu daşının HƏQİQİ (hesablanmış) kənarına görə
    təyin olunur — mücərrəd mərkəz xəttinə görə YOX. Yəni yeni seqmentin
    İLK daşı elə yerləşdirilir ki, onun arxa kənarı köhnə daşın ön kənarı
    ilə tam üst-üstə düşsün (nə boşluq, nə də yanlış aşma qalsın) — bu,
    xüsusilə künc nöqtəsində QOŞA daş olduğu hallarda əvvəlki versiyada
    boşluq yaradan səhvi aradan qaldırır. Sonrakı düz seqment daşları isə
    öz mərkəz xəttlərinə görə adi qaydada ardıcıl yerləşdirilir.

    Bütün mövqelər (0,0) başlanğıcına görə hesablanıb sonra bounding-box-a
    əsasən (ox,oy)-ə sürüşdürülür ki, heç bir daş kətandan kənara çıxmasın.

    tiles: [(a, b), ...] — DÜZGÜN İSTİQAMƏTLƏNMİŞ sırayla (hər daşın b-si
           növbəti daşın a-sına toxunur).

    Qaytarır: (positions, max_x, max_y)
      positions: [(x, y, orient, is_first, is_last, reverse), ...]  orient: 'H' | 'V'
        reverse: True olduqda tile (a,b) yarımları ƏKS (b,a) çəkilməlidir
        (sola gedən sıralarda düzgün toxunma üçün).
      max_x, max_y: son tutulan sağ-alt künc (kətan ölçüsü üçün)
    """
    n = len(tiles)
    if n == 0:
        return [], ox + _TH, oy + _TW

    positions = [None] * n

    def _tile_wh(idx, seg_is_horizontal):
        a, b = tiles[idx]
        is_double = (a == b)
        if seg_is_horizontal:
            orient = "V" if is_double else "H"
        else:
            orient = "H" if is_double else "V"
        w, h = (_TW, _TH) if orient == "V" else (_TH, _TW)
        return orient, w, h

    mode_horizontal = True
    hdir = 1                 # üfüqi sıralarda cari istiqamət (+1 sağa, -1 sola)
    seg_target = row_len
    seg_count = 0

    # cur_edge — cari axında (üfüqi seqmentdə: x, şaquli seqmentdə: y)
    # NÖVBƏTİ daşın arxa kənarının haradan başlayacağı. center — perpendikulyar
    # oxdakı SABİT mərkəz xətti (sıra üçün y-mərkəz, sütun üçün x-mərkəz),
    # bir seqment boyunca dəyişməz qalır.
    _, first_w, first_h = _tile_wh(0, True)
    cur_edge = 0
    center = first_h // 2

    min_x = min_y = 0
    max_x = _TH
    max_y = _TW

    for i in range(n):
        orient, w, h = _tile_wh(i, mode_horizontal)

        if mode_horizontal:
            ty = center - h // 2
            if hdir == 1:
                tx = cur_edge
                next_edge = cur_edge + w
            else:
                tx = cur_edge - w
                next_edge = cur_edge - w
            # Sola (hdir=-1) gedən üfüqi sıralarda tərs istiqamətdə axın gedir:
            # qonşu daşla düzgün rəqəm-rəqəmə TOXUNMA üçün yarımları (a,b) əks
            # çəkmək lazımdır — yoxsa gediş "əks göstərilir" effekti yaranır.
            reverse = (orient == "H" and hdir == -1)
        else:
            tx = center - w // 2
            ty = cur_edge
            next_edge = cur_edge + h
            reverse = False

        positions[i] = (tx, ty, orient, i == 0, i == n - 1, reverse)
        min_x = min(min_x, tx); min_y = min(min_y, ty)
        max_x = max(max_x, tx + w); max_y = max(max_y, ty + h)

        last_tx, last_ty, last_w, last_h = tx, ty, w, h
        cur_edge = next_edge

        seg_count += 1
        if seg_count >= seg_target and i < n - 1:
            seg_count = 0
            if mode_horizontal:
                # Üfüqi -> Şaquli: yeni sütunun mərkəzi elə seçilir ki, onun
                # (hdir istiqamətindəki) kənarı sıranın bitdiyi HƏQİQİ kənarla
                # (cur_edge) tam üst-üstə düşsün — sütun sıranın altına
                # "keçir", nə kənara aşır, nə də boşluq buraxır.
                row_bottom = last_ty + last_h
                _, next_w, next_h = _tile_wh(i + 1, False)
                if hdir == 1:
                    center = cur_edge - next_w // 2
                else:
                    center = cur_edge + next_w // 2
                cur_edge = row_bottom
                mode_horizontal = False
                seg_target = turn_len
            else:
                # Şaquli -> Üfüqi: yeni sıra sütunun bitdiyi kənara (aşağı)
                # bərkidilir; üfüqi başlanğıc nöqtəsi isə sütunun SONUNCU
                # daşının HƏQİQİ (mərkəzə görə yox!) kənarına görə təyin
                # olunur ki, yeni istiqamətdə (new_hdir) düz, boşluqsuz
                # davam etsin.
                col_bottom = cur_edge
                new_hdir = -hdir
                _, next_w, next_h = _tile_wh(i + 1, True)
                if new_hdir == 1:
                    cur_edge = last_tx
                else:
                    cur_edge = last_tx + last_w
                center = col_bottom + next_h // 2
                hdir = new_hdir
                mode_horizontal = True
                seg_target = row_len

    # ── Bounding-box-a görə (ox, oy)-ə sürüşdür: mənfi koordinat qalmasın ──
    shift_x = ox - min_x
    shift_y = oy - min_y
    positions = [
        (px + shift_x, py + shift_y, orient, is_first, is_last, reverse)
        for (px, py, orient, is_first, is_last, reverse) in positions
    ]
    max_x += shift_x
    max_y += shift_y

    return positions, max_x, max_y

def render_board_image(game: dict, player_wins: list, winner_uid=None) -> bytes:
    """
    Oyun taxta vəziyyətini PIL ilə şəklə çevirir.
    player_wins : [(ad, wins_sayı), ...]
    winner_uid  : oyun bitəndə qalib oyunçunun Telegram ID-si (None=oyun davam edir)
    """
    board    = game.get("board_tiles", [])
    left     = game.get("left")
    right    = game.get("right")
    stock_n  = len(game.get("stock", []))
    turn_idx = int(game.get("turn", 0) or 0)
    players  = list(game["players"].items())

    f15 = _font(_FB, 15)
    f16 = _font(_FB, 16)
    f12 = _font(_FR, 12)

    STEP   = _TH + _GAP
    PAD    = 14

    HDR_H    = 38
    P_ROW_H  = 30
    player_h = 18 + len(players) * P_ROW_H + 10

    # ── Daş düzümünü əvvəlcədən hesabla (kətan ölçüsünü ona görə tənzimləyirik) ──
    if board:
        positions, max_x, max_y = _layout_board(board, PAD+4, HDR_H+PAD)
    else:
        positions, max_x, max_y = [], PAD+4+_TH, HDR_H+PAD+_TW

    board_h  = (max_y - HDR_H) + PAD
    total_w  = max(max_x + PAD, 520)
    total_h  = HDR_H + board_h + player_h + 6

    img  = Image.new("RGB", (total_w, total_h), _BG)
    draw = ImageDraw.Draw(img)

    # ── Başlıq sətri ──────────────────────────────────────────────────────────
    draw.rectangle([0, 0, total_w, HDR_H], fill=_HDR_BG)
    if left is None:
        info = "Stol boşdur  |  Bazarda: {} daş  |  İlk daşı at!".format(stock_n)
    else:
        info = "Sol uc: [{}]   |   Sağ uc: [{}]   |   Bazarda: {} daş".format(
            left, right, stock_n)
    draw.text((14, 10), info, fill=_HDR_TXT, font=f15)

    # ── Taxta kənar çərçivəsi ─────────────────────────────────────────────────
    draw.rounded_rectangle(
        [4, HDR_H+3, total_w-4, HDR_H+board_h-3],
        radius=10, outline=(10, 70, 45), width=2)

    # ── Daşlar (üfüqi sıra + şaquli dönüş sütunları) ────────────────────────────
    for i, (a, b) in enumerate(board):
        px, py, orient, is_first, is_last, reverse = positions[i]
        hl = "left" if is_first else ("right" if is_last else None)
        if reverse:
            a, b = b, a
        if orient == "V":
            _draw_tile_v(draw, px, py, a, b, hl=hl)
        else:
            _draw_tile_h(draw, px, py, a, b, hl=hl)

    # ── Oyunçu bloku ──────────────────────────────────────────────────────────
    sy = HDR_H + board_h + 4
    draw.rectangle([0, sy, total_w, total_h], fill=_HDR_BG)
    sy += 8
    draw.text((14, sy), "Oyunçular:", fill=_HDR_TXT, font=f16)
    sy += 20

    for idx, (uid, name) in enumerate(players):
        hand_n = len(game["hands"].get(uid, []))
        wins   = next((w for n, w in player_wins if n == name), 0)
        level, rank = compute_level(wins)

        is_winner = (winner_uid is not None and uid == winner_uid)
        is_turn   = (winner_uid is None and idx == turn_idx)

        if is_winner:
            bg, tc, pfx = _WIN_BG,  _WIN_TXT,  "  QALİB!  "
        elif is_turn:
            bg, tc, pfx = _TURN_BG, _TURN_TXT, ">  "
        else:
            bg, tc, pfx = _NORM_BG, _NORM_TXT, "   "

        draw.rounded_rectangle([8, sy, total_w-8, sy+26], radius=5, fill=bg)
        # Adda emoji/simvol ola bilər (font onları çəkə bilmir) — yalnız
        # dəstəklənməyən simvolları çıxarırıq, adın özü tam qalır.
        clean_name = _clean_display_text(name) or "Anonim"
        nm   = (clean_name[:15]+"..") if len(clean_name) > 17 else clean_name
        clean_rank = _clean_display_text(rank) or rank
        line = "{}{:<17} | Əl:{:>2} daş | Sv.{} {:<12}| {} qalibiyyət".format(
            pfx, nm, hand_n, level, clean_rank, wins)
        draw.text((12, sy+5), line, fill=tc, font=f12)
        sy += P_ROW_H

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()


games = {}

# board_msg_ids — köhnə kod uyğunluğu üçün saxlanır
board_msg_ids: dict = {}
async def _get_player_wins_list(game: dict) -> list:
    """Hər oyunçunun (ad, wins) cütünü qaytarır (şəkil render üçün)."""
    result = []
    for uid, name in game["players"].items():
        wins = get_user_wins(uid)
        result.append((name, wins))
    return result


async def _send_board_photo(chat_id: int, winner_uid=None):
    """Taxta şəklini göndərir. Hər dəfə YENİ foto (edit deyil).
    winner_uid verilsə qalib vurğulanmış son vəziyyəti göstərir."""
    game = games.get(chat_id)
    if not game:
        return
    pw    = await _get_player_wins_list(game)
    data  = render_board_image(game, pw, winner_uid=winner_uid)
    photo = types.InputFile(io.BytesIO(data), filename="taxta.png")
    await bot.send_photo(chat_id, photo)


async def send_turn(chat_id: int):
    """Növbəni bildirən inline düyməli mesaj göndərir."""
    game = games.get(chat_id)
    if not game:
        return
    player_ids = list(game["players"].keys())
    try:
        current_player_id = player_ids[game["turn"]]
    except (TypeError, IndexError):
        logging.error(f"Səhv növbə indeksi: {game.get('turn')}")
        await bot.send_message(chat_id, "Oyun növbəsi təyin edilməyib. Yenidən başlayın.")
        return
    uname = game["players"][current_player_id]
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Seçiminizi Edin 🕹️", switch_inline_query_current_chat=""))
    # 🟢 YENİ: Eyni oyunu Mini App-da açmaq üçün düymə — chat_id ötürülür ki,
    # app hansı oyunu göstərəcəyini bilsin.
    if APP_URL and "REPLACE-WITH" not in APP_URL:
        try:
            kb.add(InlineKeyboardButton(
                "📱 App-da aç",
                web_app=types.WebAppInfo(url=f"{APP_URL}?chat_id={chat_id}")
            ))
        except Exception as e:
            # Köhnə aiogram versiyasında WebAppInfo olmaya bilər — bu, botu
            # çökdürməsin, sadəcə bu düymə göstərilmir.
            logging.warning(f"WebApp düyməsi yaradıla bilmədi: {e}")
    await bot.send_message(
        chat_id,
        f"*{uname} adlı oyunçunun növbəsidir*",
        reply_markup=kb,
        parse_mode="Markdown",
    )


def can_player_move(game: dict, user_id) -> bool:
    """Oyunçunun əlindəki daşlardan birini stola vurmaq mümkündürmü?"""
    if game.get("left") is None:
        return True   # stol hələ boşdur, istənilən daşı atmaq olar
    left, right = game["left"], game["right"]
    for stone in game["hands"].get(user_id, []):
        try:
            a, b = map(int, stone.split("-"))
        except Exception:
            continue
        if a == left or b == left or a == right or b == right:
            return True
    return False


def check_for_blocked_game(chat_id: int) -> bool:
    """Oyun bloklanıbmı? — Heç bir oyunçunun oynaya bilər daşı yoxdur
    VƏ bazarda da taxtaya uyğun gələ biləcək HEÇ BİR daş qalmayıb.

    🟢 DÜZƏLİŞ: Əvvəllər bazarda daş qaldığı müddətcə oyun "bloklanmayıb"
    sayılırdı — hətta bazardakı daşların HEÇ BİRİ taxtaya uyğun gəlməsə belə.
    Bu, ədalətsizliyə səbəb olurdu: oyunçu boş yerə bazardan çoxlu daş alıb
    əlində daş yığırdı, sonra da bazar tam bitəndə "oyun bloklanıb" deyilib
    həmin oyunçu əlindəki çoxlu daşa görə uduzurdu. İndi bazardakı daşlar da
    yoxlanılır — əgər onlardan heç biri taxtaya uymursa, oyun artıq
    bloklanıb sayılır və bazar tam boşalana qədər gözlənilmir.
    """
    game = games.get(chat_id)
    if not game:
        return False

    # Bazarda taxtaya uyğun gələ biləcək bir daş varsa, oyun bloklanmayıb
    for stone in game.get("stock", []):
        if stone_fits_board(game, stone):
            return False

    for uid in game["players"]:
        if can_player_move(game, uid):
            return False
    return True


def calculate_scores(game: dict) -> dict:
    """Hər oyunçunun əlindəki daşların cəmini hesablayır.
    Qaytarır: {uid: {'score': int, 'tiles': int}}"""
    result = {}
    for uid, hand in game.get("hands", {}).items():
        total = 0
        for stone in hand:
            try:
                a, b = map(int, stone.split("-"))
                total += a + b
            except Exception:
                pass
        result[uid] = {"score": total, "tiles": len(hand)}
    return result


async def end_game_by_score(chat_id: int):
    """Bütün oyunçular pass etdi / bazar boşaldı — ən az xallı oyunçu qalibdir."""
    game = games.get(chat_id)
    if not game:
        return

    scores    = calculate_scores(game)
    players   = game["players"]

    # Ən az xallı oyunçu qalibdir
    winner_uid = min(scores, key=lambda uid: scores[uid]["score"])
    winner_name = players.get(winner_uid, "Anonim")

    for uid, name in players.items():
        record_game_played(uid, name)

    update_winner_count(winner_uid, winner_name)
    save_game_history(chat_id, winner_uid, winner_name, "Oyun bloklandı, heç kim gedə bilmir", players, scores)

    sorted_scores = sorted(scores.items(), key=lambda x: x[1]["score"])
    score_list = []
    for uid, data in sorted_scores:
        name   = players.get(uid, "Anonim")
        status = "🏆 **Qalib**" if uid == winner_uid else \
                 f"{data['score']} xalı qaldı ({data['tiles']} daş)"
        score_list.append(f"*{escape_md(name)}*: {status}")

    result_msg = (
        "🚫 **OYUN BLOKANDI!** Heç kim gedə bilmir.\n\n"
        f"🏆 **{escape_md(winner_name)}** ən az xalla qalib gəldi!\n\n"
        + "\n".join(score_list)
    )
    await _send_board_photo(chat_id, winner_uid=winner_uid)
    await bot.send_message(chat_id, result_msg, parse_mode="Markdown")
    board_msg_ids.pop(chat_id, None)
    games.pop(chat_id, None)


async def send_board_status(chat_id: int):
    """Taxta şəkli + növbə mesajı: YENİ şəkil, sonra inline düymə."""
    await _send_board_photo(chat_id)
    await send_turn(chat_id)


def get_join_keyboard(players_count, chat_id):
    kb = InlineKeyboardMarkup(row_width=2)
    # 🟢 DÜZƏLİŞ: "Oyuna Qoşul" düyməsi indi birbaşa botun şəxsi çatına yönləndirir
    # və orada /start join_<chat_id> avtomatik işə düşür.
    join_button = InlineKeyboardButton(
        "Oyuna Qoşul 🙋‍♂️",
        url=f"https://t.me/{BOT_USERNAME}?start=join_{chat_id}"
    )

    if players_count >= 2:
        start_button = InlineKeyboardButton("Oyunu Başlat ▶️", callback_data="start_game")
        kb.add(join_button, start_button)
    else:
        kb.add(join_button)
        
    return kb

async def update_join_message(chat_id):
    game = games.get(chat_id)
    if not game or "join_msg_id" not in game or game["join_msg_id"] is None:
        return

    players_count = len(game["players"])

    if players_count:
        lines = []
        for uid, name in game["players"].items():
            level, rank_name = compute_level(get_user_wins(uid))
            lines.append(f"• {escape_md(name)} — {rank_name} ⭐{level}")
        player_lines = "\n".join(lines)
    else:
        player_lines = "Hələ kimsə qoşulmayıb."

    message_text = (
        f"🎮 **Oyuna Qeydiyyat Başladı!**\n\n"
        f"**Qoşulanlar ({players_count} nəfər):**\n"
        f"{player_lines}\n\n"
        f"(Oyunun başlanması üçün ən az 2 nəfər lazımdır)"
    )
    
    kb = get_join_keyboard(players_count, chat_id)
    
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=game["join_msg_id"],
            text=message_text,
            reply_markup=kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.warning(f"Mesajı yeniləmək mümkün olmadı: {e}")


# =======================================================
# 3. ƏSAS BOT ƏMRLƏRİ
# =======================================================

async def handle_join_via_start(msg: types.Message, args: str):
    """Qrupdan 'Oyuna Qoşul' düyməsinə basıb botun şəxsi çatına yönləndirilən
    oyunçunu, oradan gələn /start join_<chat_id> payload-u ilə oyuna qoşur."""
    user_id = msg.from_user.id
    user_name = msg.from_user.first_name

    try:
        target_chat_id = int(args[len("join_"):])
    except (ValueError, IndexError):
        await msg.answer("❌ Yanlış qoşulma linki.")
        return

    game = games.get(target_chat_id)
    if not game:
        await msg.answer("❌ Oyun tapılmadı və ya artıq bitib.")
        return

    # Oyun artıq başlayıbsa, yeni qoşulmaya icazə vermə
    if game.get("turn") is not None:
        await msg.answer("⛔ Oyun artıq başlayıb, indi qoşula bilməzsiniz.")
        return

    # Artıq qoşulubsa
    if user_id in game["players"]:
        await msg.answer("Sən Artıq Oyuna Qoşulmusan ⛔")
        return

    # Maksimum sayı yoxla
    MAX_PLAYERS = 4
    if len(game["players"]) >= MAX_PLAYERS:
        await msg.answer(f"⚠️ Oyunçu sayı artıq maksimuma ({MAX_PLAYERS} nəfər) çatıb.")
        return

    # --- QOŞULMA MƏNTİQİ ---
    game["players"][user_id] = user_name
    game["last_lobby_activity"] = datetime.datetime.utcnow()

    await update_join_message(target_chat_id)

    # Qrupda bildiriş
    count = len(game["players"])
    try:
        await bot.send_message(
            chat_id=target_chat_id,
            text=f"✅ **{escape_md(user_name)}** oyuna qoşuldu! ({count}/{MAX_PLAYERS} nəfər)",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.warning(f"Qoşulma bildirişi qrupa göndərilə bilmədi: {e}")

    # Şəxsi çatda təsdiq mesajı — istifadəçinin adı nə olursa olsun, bu HƏMİŞƏ gedir.
    await msg.answer("Oyuna Uğurla Qoşuldun ✅")


@dp.message_handler(commands=["start"])
async def cmd_start_welcome(msg: types.Message):
    # 🟢 DÜZƏLİŞ: "Oyuna Qoşul" düyməsindən gələn /start join_<chat_id> payload-unu yoxla
    args = msg.get_args()
    if args and args.startswith("join_") and msg.chat.type == "private":
        await handle_join_via_start(msg, args)
        return

    # Qrup yoxlaması
    if msg.chat.type in ["group", "supergroup"]:
        pass 
        
    welcome_text = (
        "👋 Salam, <b>Mən Qrupda Domino Oynamaq Üçün Botam</b> 🎮\n\n"
        "📌 Domino sevərlər üçün əla xəbər:\n"
        "Artıq Telegramda dostlarınızla rahat\n"
        "Domino oyununu oynamaq üçün bot var 🤩\n\n"
        "✅ Məni qrupa əlavə edib <b>(Ancaq Mesaj Silmə Yetkisini Verməyiniz Kifayətdir, Düzgün Oyun Getməsi Üçün)</b> /domino əmrini verərək bu maraqlı oyunu birlikdə oynaya bilərsiniz 👥"
    )
    
    kb = InlineKeyboardMarkup(row_width=2)
    
    # Şəkildəki düymələrə uyğun dəyişikliklər
    # URL-ləri özünüzə uyğun dəyişməyi unutmayın!
    kb.row(
        InlineKeyboardButton(text="Diger Botlar 👑", url="https://t.me/VIPBotlar"), 
        InlineKeyboardButton(text="Dəstək Qrupu ⚙️", url="https://t.me/VIPbotlarSupport") 
    )

    kb.add(
        InlineKeyboardButton(text="Kömək ✅", callback_data="show_help")
    )

    kb.add(
        InlineKeyboardButton(text="➕ Məni Qrupa Əlavə Et ➕", url=f"https://t.me/{BOT_USERNAME}?startgroup=start")
    )
    
    # parse_mode="HTML" ilə göndərilir
    await msg.answer(welcome_text, reply_markup=kb, parse_mode="HTML")

@dp.message_handler(commands=["help"])
async def cmd_help(msg: types.Message):
    # HTML parse_mode istifadə olunacaq
    help_text = (
        "🎴 <strong>Domino Oyununu Qrupda Rahat Oynamaq Üçün Bot</strong> 🎮\n\n"
        "📌 Mən, qrupda domino oynayaraq dostlarınız ilə birlikdə vaxtınızı əyləncəli keçirmək üçün yaradılmış botam\n\n"
        "Məni qrupa əlavə edib, bir dənə (<strong>Mesajları Silmək Yetkisi</strong>) verməyiniz kifayətdir düzgün oyun getməsi üçün ✅\n\n"
        
        "<strong>Əsas Əmrlər:</strong>\n"
        "🔸 /domino: Domino Oyununu Başladır\n"
        "🔸 /rating: Ən çox qələbə qazananların siyahısını göstərir\n"
        "🔸 /profile: Şəxsi statistikanızı göstərir\n"
        "🔸 /rutbeler: Səviyyə və rütbələr haqqında məlumat verir\n"
        "🔸 /stop: Aktiv oyunu dayandırır\n\n"
        
        "<strong>Oyun Məntiqi:</strong>\n"
        "1. <code>/domino</code> ilə oyunu başladın\n"
        "2. Növbə sizdə olanda <b>Seçiminizi Edin</b> düyməsinə basıb bir daş atın\n"
        "3. Sonda kimin daşları ilk bitərsə o Qalib Olur\n"
    )
    
    kb = InlineKeyboardMarkup(row_width=2)
    
    # Şəkildəki düymələrə uyğun dəyişikliklər
    # URL-ləri özünüzə uyğun dəyişməyi unutmayın!
    kb.row(
        InlineKeyboardButton(text="Diger Botlar 👑", url="https://t.me/VIPBotlar"), 
        InlineKeyboardButton(text="Dəstək Qrupu ⚙️", url="https://t.me/VIPbotlarSupport")
    )
    
    kb.add(
        InlineKeyboardButton(text="➕ Məni Qrupa Əlavə Et ➕", url=f"https://t.me/{BOT_USERNAME}?startgroup=start")
    )
    
    # parse_mode="HTML" istifadə olunur
    await msg.answer(help_text, reply_markup=kb, parse_mode="HTML")

@dp.message_handler(commands=["domino", "yeni"]) 
async def cmd_domino_new(msg: types.Message):
    if msg.chat.type not in ["group", "supergroup"]:
        return await msg.reply("Bu əmr yalnız qruplarda işləyir.")
    
    if msg.chat.id in games:
        return await msg.reply("✅ Artıq aktiv domino oyunu var. Oyunu bitirmək üçün /stop yazın.")
        
    # 🟢 DÜZƏLİŞLƏR: "players" lüğətə çevrildi və "last_lobby_activity" əlavə edildi
    games[msg.chat.id] = {
        "players": {},
        "last_lobby_activity": datetime.datetime.utcnow(),
        "turn": None,
        "left": None,
        "right": None,
        "hands": {},
        "stock": [],
        "join_msg_id": None,
        "board_tiles": [],  # canlı taxta şəkli üçün — atılan daşlar sırayla
    }
    
    initial_kb = get_join_keyboard(0, msg.chat.id)
    
    sent_msg = await msg.reply(
        "🎮 **Oyuna Qeydiyyat Başladı!**\n\n"
        "**Qoşulanlar (0 nəfər):**\n"
        "*Hələ kimsə qoşulmayıb.*\n\n"
        "(Oyunun başlanması üçün ən az 2 nəfər lazımdır)", 
        reply_markup=initial_kb, 
        parse_mode="Markdown"
    )
    
    games[msg.chat.id][
"join_msg_id"] = sent_msg.message_id

@dp.message_handler(commands=["stop"])
async def cmd_stop_game(msg: types.Message):
    chat_id = msg.chat.id
    if chat_id not in games:
        return await msg.reply("✅ Aktiv domino oyunu yoxdur.")

    try:
        if "join_msg_id" in games[chat_id] and games[chat_id]["join_msg_id"] is not None:
            await bot.delete_message(chat_id, games[chat_id]["join_msg_id"])
    except: pass 

    games.pop(chat_id, None)
    board_msg_ids.pop(chat_id, None)
    await msg.reply("❌ Aktiv domino oyunu sonlandırıldı. Yeni oyun başlamaq üçün /domino yazın.")

def escape_markdown_symbols(text):
    """Əsasən alt xətt simvolunu escape edir ki, Telegram onu kursiv sanmasın."""
    # Sadəcə * və _ simvollarını escape etmək adətən bu xətanı həll edir.
    return re.sub(r'([*_])', r'\\\1', text) 

@dp.message_handler(commands=["rating"])
async def cmd_rating(msg: types.Message):
    top_ratings = load_top_ratings()
    
    if not top_ratings:
        return await msg.reply("Reytinq siyahısı hələ boşdur. Oyun oynayın və qalib gəlin!")

    rating_message = "👑 **Domino Reytinq Siyahısı:**\n\n" # Başlığı daha qalın edək
    
    for i, user_data in enumerate(top_ratings):
        name = user_data.get("name", "Anonim İstifadəçi")
        wins = user_data.get("wins", 0)
        level, rank_name = compute_level(wins)
        
        # ✅ Düzəliş: Adı göndərməzdən əvvəl escape edirik
        safe_name = escape_markdown_symbols(name)
        
        rating_message += f"{i+1}. {safe_name} — {rank_name} ⭐{level} — **{wins} qələbə**\n"
        
    await msg.reply(rating_message, parse_mode="Markdown")


@dp.message_handler(commands=["profile"])
async def cmd_profile(msg: types.Message):
    """Şəxsi statistika kartı (Uno botundakı /profile ilə eyni format)"""
    user = msg.from_user
    doc = ratings_collection.find_one({"_id": str(user.id)}) if ratings_collection is not None else None
    games_played = (doc.get("games", 0) or 0) if doc else 0
    wins = (doc.get("wins", 0) or 0) if doc else 0

    if games_played == 0:
        return await msg.reply(
            "Siz hələ heç bir oyun oynamamısınız. Əvvəlcə ən azından bir oyun "
            "oynayın qrupda dostlarınızla: /domino yazaraq oyun başladın!"
        )

    level, rank_name = compute_level(wins)

    profile_text = (
        f"👤 {user.first_name}\n\n"
        f"🏅 Rütbə: {rank_name}\n"
        f"⭐ Səviyyə: {level}\n\n"
        f"🎮 Oyun: {games_played}\n"
        f"🏆 Qələbə: {wins}\n"
    )
    await msg.reply(profile_text)


@dp.message_handler(commands=["rutbeler"])
async def cmd_rutbeler(msg: types.Message):
    """Səviyyə/rütbə sistemini izah edir (Uno botundakı /rutbeler ilə eyni)"""
    lines = ["🎖 **Səviyyə və Rütbələr**\n",
             "Hər dəfə bir oyunu qazandıqca xalınız (qələbə sayınız) 1 artır. "
             "Qələbə sayınız artdıqca səviyyəniz və rütbəniz avtomatik "
             "yüksəlir:\n"]

    for i, (threshold, level, rank_name) in enumerate(LEVELS):
        if i + 1 < len(LEVELS):
            next_threshold = LEVELS[i + 1][0]
            lines.append(f"⭐ Səviyyə {level} — {rank_name}: {threshold}-{next_threshold - 1} qələbə")
        else:
            lines.append(f"⭐ Səviyyə {level} — {rank_name}: {threshold}+ qələbə")

    lines.append("\nHamı 1-ci səviyyədən (🔰 Yeni Başlayan) başlayır. "
                 "Öz statistikanızı /profile, top 25 reytinqi isə /rating "
                 "əmri ilə görə bilərsiniz.")

    await msg.reply("\n".join(lines), parse_mode="Markdown")


@dp.message_handler(commands=["broadcast"])
async def broadcast_command(msg: types.Message):
    if str(msg.from_user.id) not in SUDO_USERS:
        return await msg.reply("⛔ Bu əmr yalnız adminlər üçündür.")

    if not msg.reply_to_message:
        return await msg.reply(
            "✍️ Yaymaq istədiyin mesaja (reklam, kanal postu, şəkil - nə olursa) REPLY edərək /broadcast yaz."
        )

    source_chat_id = msg.chat.id
    source_message_id = msg.reply_to_message.message_id

    status_msg = await msg.reply("⚡ Reklam prosesi başladı, gözlə...")

    sent_chats, failed_chats = 0, 0
    for chat in get_served_chats():
        chat_id = chat["chat_id"]
        try:
            await bot.forward_message(chat_id, source_chat_id, source_message_id)
            sent_chats += 1
            await asyncio.sleep(0.3)
        except RetryAfter as e:
            if e.timeout > 200:
                failed_chats += 1
                continue
            await asyncio.sleep(e.timeout)
            try:
                await bot.forward_message(chat_id, source_chat_id, source_message_id)
                sent_chats += 1
            except Exception as ex:
                _log_broadcast_error("chat", chat_id, str(ex))
                failed_chats += 1
        except Exception as e:
            _log_broadcast_error("chat", chat_id, str(e))
            failed_chats += 1

    sent_users, failed_users = 0, 0
    for u in get_served_users():
        uid = u["user_id"]
        try:
            await bot.forward_message(uid, source_chat_id, source_message_id)
            sent_users += 1
            await asyncio.sleep(0.3)
        except RetryAfter as e:
            if e.timeout > 200:
                failed_users += 1
                continue
            await asyncio.sleep(e.timeout)
            try:
                await bot.forward_message(uid, source_chat_id, source_message_id)
                sent_users += 1
            except Exception as ex:
                _log_broadcast_error("user", uid, str(ex))
                failed_users += 1
        except Exception as e:
            _log_broadcast_error("user", uid, str(e))
            failed_users += 1

    summary = (
        f"✅ Reklam prosesi bitdi!\n\n"
        f"👥 Qruplar: {sent_chats} uğurlu, {failed_chats} uğursuz\n"
        f"👤 İstifadəçilər: {sent_users} uğurlu, {failed_users} uğursuz"
    )
    try:
        await status_msg.edit_text(summary)
    except Exception:
        await msg.reply(summary)

# =======================================================
# 4. CALLBACK & QOŞULMA HANDLERLƏRİ
# =======================================================

@dp.message_handler(content_types=types.ContentType.NEW_CHAT_MEMBERS)
async def new_member_handler(msg: types.Message):
    # DÜZƏLİŞ: əvvəlki kod hər hansı yeni üzv qoşulanda (hətta adi insanlar üçün
    # də) hər dəfə bot.get_me() ilə əlavə API sorğusu edirdi. İndi on_startup-da
    # bir dəfə əldə edilmiş BOT_ID istifadə olunur.
    if BOT_ID in [user.id for user in msg.new_chat_members]:
        # Qrupu heç bir əmr gözləmədən dərhal /broadcast siyahısına (Mongo) yaz
        try:
            add_served_chat(msg.chat.id)
        except Exception:
            pass

        info_text = (
            "✨ Məni Bu Qrupa Əlavə Etdiyiniz Üçün Təşəkkürlər 😊\n\n"
            "<b>Domino Oyununu başlamaq üçün sadəcə </b><code>/domino</code><b> əmrini yazın</b> ✅"
        )
        
        # HTML formatında göndərilir
        await bot.send_message(msg.chat.id, info_text, parse_mode="HTML")

@dp.callback_query_handler(lambda c: c.data == "join_game")
async def join_game_callback(call: types.CallbackQuery):
    chat_id = call.message.chat.id
    game = games.get(chat_id)
    user_id = call.from_user.id
    user_name = call.from_user.first_name
    
    # 1. Oyunun varlığını yoxla
    if not game: 
        return await call.answer("Oyun bitib. /domino ilə yenisini başladın.", show_alert=True)
    
    # 2. Artıq qoşulubmu yoxla
    if user_id in game["players"]:
        return await call.answer("Siz artıq oyundasınız!", show_alert=False)
        
    # 3. Maksimum sayı yoxla
    MAX_PLAYERS = 4 
    if len(game["players"]) >= MAX_PLAYERS:
        return await call.answer(f"Oyunçu sayı artıq maksimuma ({MAX_PLAYERS} nəfər) çatıb.", show_alert=True)
        
    # --- QOŞULMA MƏNTİQİ ---
    
    # Oyunçunu lüğətə əlavə et (ID: Name)
    game["players"][user_id] = user_name
    
    # 🟢 LOBBY FƏALİYYƏTİNİ YENİLƏ (Timeout üçün vacibdir!)
    # Bu, son qoşulmadan 15 dəqiqə sonra lobby-nin bağlanmasını önləyir.
    game["last_lobby_activity"] = datetime.datetime.utcnow()
    
    await update_join_message(chat_id) 
    
    # Qrupda bildiriş
    count = len(game['players'])
    await bot.send_message(
        chat_id=chat_id, 
        text=f"✅ **{user_name}** oyuna qoşuldu! ({count}/{MAX_PLAYERS} nəfər)", 
        parse_mode="Markdown"
    )
    
    # Callback Query-yə cavab ver
    await call.answer(f"{user_name} oyuna qoşuldu!", show_alert=False)

@dp.callback_query_handler(lambda c: c.data == "start_game")
async def game_start(call: types.CallbackQuery):
    chat_id = call.message.chat.id
    game = games.get(chat_id)
    
    # 1. Başlanğıc Yoxlamaları
    if not game or len(game["players"]) < 2: 
        return await call.answer("Oyunçular çatışmır (min 2 nəfər).")

    # Qoşulma mesajını silməyə çalış
    try:
        await bot.delete_message(chat_id, game["join_msg_id"])
    except: pass
        
    await call.answer("Oyun Başladı!")
    
    # 2. Oyun Başlama Məntiqi (Daşların Paylanması)
    all_stones = list(DOMINO.keys())
    random.shuffle(all_stones)
    stones_per_player = 7 
    remaining_stones = all_stones[:]
    
    for p in game["players"]:
        game["hands"][p] = remaining_stones[:stones_per_player]
        remaining_stones = remaining_stones[stones_per_player:]
        
    game["stock"] = remaining_stones
    
    if game["stock"]: await bot.send_message(chat_id, f"🎴 Bazarda {len(game['stock'])} daş var")
    else: await bot.send_message(chat_id, "🎴 Bazar yoxdur")
        
    game["turn"] = 0
    game["left"] = None
    game["right"] = None
    game["board_tiles"] = []          # ilk şəkil üçün sıfırla
    board_msg_ids.pop(chat_id, None)  # əvvəlki oyunun şəkil mesaj ID-sini təmizlə

    # İlk taxta şəklini göndər (canlı görüntü başlasın)
    await _send_board_photo(chat_id)
    await send_turn(chat_id)


# =======================================================
# 5. INLINE OYUN MƏNTİQİ (KART SEÇİMİ)
# =======================================================

@dp.inline_handler()
async def inline_menu(q: types.InlineQuery):
    user_id = q.from_user.id
    chat_id = None
    is_my_turn = False
    fallback_chat_id = None  # oyunda iştirakçıdır, sadəcə növbəsi olmaya bilər

    # 1. Əvvəlcə növbənin sizdə olduğu aktiv oyunu tap
    for cid, g in games.items():
        if user_id in g["players"]:
            # 🟢 DÜZƏLİŞ 1: player_ids Dəyişənini Təyin Edin
            player_ids = list(g["players"].keys())

            # 🟢 DÜZƏLİŞ 2: Oyunun başladığını və növbənin təyin olunduğunu yoxlayın
            # VƏ KeyError/TypeError xətasının qarşısını alın (g["turn"] != None)
            if g.get("turn") is not None and g["turn"] < len(player_ids):
                # Oyunçu başlamış bir oyunda iştirak edir — hətta növbəsi
                # olmasa belə, öz daşlarını görə bilsin deyə ehtiyat variant kimi saxlayırıq.
                fallback_chat_id = cid

                # Cari növbədəki oyunçunun ID-sini alın (Düzgün istifadə)
                current_turn_player_id = player_ids[g["turn"]]

                # 🟢 DÜZƏLİŞ 3: Növbəni yoxlayın
                if current_turn_player_id == user_id:
                    chat_id = cid
                    is_my_turn = True
                    break  # Oyunu tapdıq, dövrü dayandırırıq

    # 2. Növbə sizdə deyilsə də, aktiv oyunda iştirak edirsinizsə: "Növbə sizin
    # deyil" mesajı əvəzinə, öz daşlarınızı hər zaman görə bilməlisiniz —
    # sadəcə növbəsi olan oyunçu üçün bu YOX, çünki onun üçün normal (oynaya
    # bilən) məntiq onsuz da aşağıda işləyir.
    if chat_id is None:
        if fallback_chat_id is not None:
            chat_id = fallback_chat_id
        else:
            # Heç bir aktiv oyunda deyil
            try:
                return await q.answer(
                    [],
                    is_personal=True,
                    cache_time=1,
                    switch_pm_text="Aktiv Oyununuz Yoxdur ✅",
                    switch_pm_parameter="no_game"
                )
            except Exception:
                # 🟢 DÜZƏLİŞ: Sorğu artıq köhnəlibsə (istifadəçi gec basıbsa),
                # botu çökdürmək əvəzinə sadəcə səssizcə keç.
                return

    game = games[chat_id]
    results = []
    can_move = can_player_move(game, user_id)
    stock_has_tiles = bool(game["stock"])

    # 1. Oynaya bilmirsə: DOĞRU tək seçim (bazarda daş varsa TAKE, yoxdursa
    # PASS) HƏMİŞƏ LAP BAŞDA, ağ rəngdə göstərilir - əl daşlarından ƏVVƏL.
    # O biri (yanlış) seçim HEÇ göstərilmir, çünki məntiqcə ya PASS, ya da
    # daş götürmə mümkündür - ikisi BİRDƏN YOX.
    # 🟢 DÜZƏLİŞ: TAKE/PASS seçimləri yalnız həqiqətən növbəsi olan oyunçuya göstərilir,
    # çünki növbəsi olmayan oyunçu sadəcə öz daşlarına baxır, hərəkət edə bilməz.
    if is_my_turn and not can_move:
        if stock_has_tiles:
            results.append(
                InlineQueryResultCachedSticker(
                    id="TAKE_STONE",
                    sticker_file_id=TAKE_STONE_ID,
                )
            )
        else:
            results.append(
                InlineQueryResultCachedSticker(
                    id="PASS",
                    sticker_file_id=PASS_ID,
                )
            )

    # 2. Əldəki daşlar - HAMISI stiker olaraq (ağ = oynana bilən, boz = bilməyən)
    # DİQQƏT: daşları HƏM oynana bilmə statusuna (ağ/boz) görə qruplaşdırırıq,
    # HƏM DƏ hər qrupun içində rəqəmə görə sıralayırıq. Yalnız dəyərə görə
    # sıralamaq kifayət etmir - çünki ağ və boz daşların dəyərləri bir-birinin
    # arasına düşəndə (məs. boz "1-2"dən sonra ağ "1-4" gəlirsə) boz qrupu
    # ortadan bölünüb səpələnmiş görünür. Əvvəlcə "uyğun gəlir" (ağ) statusuna
    # görə qruplaşdırmaq, hər iki qrupun HƏMİŞƏ tam bitişik qalmasını təmin edir.
    sorted_hand = sorted(
        game["hands"].get(user_id, []),
        key=lambda s: (not stone_fits_board(game, s), tuple(map(int, s.split("-")))),
    )
    for stone in sorted_hand:
        if stone in DOMINO:
            if stone_fits_board(game, stone):
                sticker_id = DOMINO[stone]
            else:
                # Boz versiya yoxdursa (skript işlədilməyibsə) adi göstərilir - heç nə pozulmur
                sticker_id = DOMINO_GRAYED.get(stone, DOMINO[stone])
            results.append(
                InlineQueryResultCachedSticker(
                    id=f"STONE_{stone}",
                    sticker_file_id=sticker_id,
                )
            )

    if not results:
        try:
            return await q.answer(
                [],
                is_personal=True,
                cache_time=1,
                switch_pm_text="Siz hərəkət edə bilməzsiniz, ya da əlinizdə daş yoxdur",
                switch_pm_parameter="no_tiles"
            )
        except Exception:
            return

    try:
        await q.answer(results, is_personal=True, cache_time=1)
    except Exception:
        # 🟢 DÜZƏLİŞ: Köhnəlmiş/etibarsız inline sorğu botu çökdürməsin.
        pass

 # process_sticker_move funksiyasının başlanğıcını bununla əvəz edin:

@dp.message_handler(content_types=['sticker'])
async def process_sticker_move(msg: types.Message):
    chat_id = msg.chat.id
    game = games.get(chat_id)
    
    if not game: return 

    user_id = msg.from_user.id
    user_name = msg.from_user.first_name 
    
    # 🟢 YENİ: Dəyişməyən Unique ID-ni götürürük (ağ VƏ boz versiyaların hər ikisini tanıyır)
    incoming_uid = msg.sticker.file_unique_id
    stone_value = None
    
    # Daşı Unique ID vasitəsilə tapırıq
    stone_value = ALL_UNIQUE_LOOKUP.get(incoming_uid)
    
    if not stone_value:
        # Tanınmayan stiker gələndə bot sadəcə xəbərdarlıq verir
        logging.warning(f"Tanınmayan stiker! UID: {incoming_uid}")
        return 
        
    # --- OYUN MƏNTİQİ ---
    
    player_ids = list(game["players"].keys()) 
    
    try:
        current_player_id = player_ids[game["turn"]] 
    except (TypeError, IndexError, KeyError):
        return await bot.send_message(chat_id, "❌ Növbə məlumatı səhvdir. Oyunu yenidən başladın.")

    if current_player_id != user_id:
        current_player_name = game["players"][current_player_id]
        await bot.send_message(chat_id, f"❌ *{escape_md(msg.from_user.first_name)}*, indi {escape_md(current_player_name)} adlı oyunçunun növbəsidir!", parse_mode="Markdown")
        try: await msg.delete()
        except: pass
        return

    # PASS Məntiqi (YENİLƏNDİ)
    if stone_value == "PASS":
        # 1. Əlində oynaya biləcəyi daş varmı?
        if can_player_move(game, user_id):
            await bot.send_message(chat_id, "⚠️ Sizin oynaya biləcəyiniz daş var. PASS edə bilməzsiniz!")
            try: await msg.delete()
            except: pass
            return

        # 2. Bazarda daş qalıbmı? (YENİ ŞƏRT)
        if len(game.get("stock", [])) > 0:
            # 🟢 DÜZƏLİŞ: Bazarda daş qalsa belə, əgər onların HEÇ BİRİ
            # taxtaya uymursa və heç bir oyunçu oynaya bilmirsə, oyun artıq
            # bloklanıb — oyunçunu boş yerə bazara göndərmək əvəzinə oyunu
            # dərhal bitiririk.
            if check_for_blocked_game(chat_id):
                return await end_game_by_score(chat_id)

            await send_move_warning(
                chat_id, msg,
                "⚠️ Gedişat üçün uyğun daşınız olmadığından, bazardan daş götürməlisiniz "
            )
            return
        
        # Əgər həm daşı yoxdursa, həm də bazar boşdursa, o zaman PASS olar:
        await bot.send_message(chat_id, f"⏩ *{escape_md(msg.from_user.first_name)}* daşı olmadığı və bazar boş olduğu üçün növbəni keçdi (PASS)", parse_mode="Markdown")
        try: await msg.delete()
        except: pass
            
        game["turn"] = (game["turn"] + 1) % len(game["players"])
        game["last_activity"] = datetime.datetime.utcnow() 
        
        if check_for_blocked_game(chat_id):
            return await end_game_by_score(chat_id)
            
        await send_turn(chat_id)
        return

    # TAKE Məntiqi
    if stone_value == "TAKE":
        if can_player_move(game, user_id):
            await bot.send_message(chat_id, "⚠️ Sizin oynaya biləcəyiniz daş var. Daş götürməyə ehtiyac yoxdur")
            try: await msg.delete()
            except: pass
            return

        # 🟢 DÜZƏLİŞ: Bazarda daş qalsa belə, o daşların HEÇ BİRİ taxtaya
        # uymursa və heç bir oyunçu oynaya bilmirsə, oyun artıq bloklanıb —
        # oyunçu boş yerə bazardan daş yığmasın, oyun dərhal bitsin.
        if check_for_blocked_game(chat_id):
            return await end_game_by_score(chat_id)

        if not game["stock"]:
            if check_for_blocked_game(chat_id):
                 return await end_game_by_score(chat_id)
            await bot.send_message(chat_id, "⚠️ Bazar boşdur. Növbəni keçməlisiniz (PASS)")
            try: await msg.delete()
            except: pass
            return
            
        new_stone = game["stock"].pop(0)
        game["hands"][user_id].append(new_stone)
        await bot.send_message(chat_id, f"📥 *{escape_md(msg.from_user.first_name)}* bazardan 1 daş götürdü. Bazarda **{len(game['stock'])}** daş qaldı", parse_mode="Markdown")
        try: await msg.delete()
        except: pass

        game["last_activity"] = datetime.datetime.utcnow() 

        # 🟢 DÜZƏLİŞ: Əgər götürülən bu SON daş idisə və bazar artıq boşdusa,
        # dərhal bloklanma yoxlanılır ki, oyunçular boş yerə bir daha
        # bazara "getməyə" çalışmasınlar — oyun elə indi dayanıb nəticə versin.
        if check_for_blocked_game(chat_id):
            return await end_game_by_score(chat_id)

        await send_turn(chat_id)
        return

    # Daşın yoxlanılması və Atılması
    if stone_value not in game["hands"].get(user_id, []):
        await bot.send_message(chat_id, f"⚠️ *{escape_md(msg.from_user.first_name)}*, bu daş əlinizdə yoxdur", parse_mode="Markdown")
        try: await msg.delete()
        except: pass
        return

    a, b = map(int, stone_value.split("-"))
    
    # --- YENİ SEÇİM MƏNTİQİ ---
    if game["left"] is not None:
        fits_left = (a == game["left"] or b == game["left"])
        fits_right = (a == game["right"] or b == game["right"])

        # Əgər daş hər iki uca da uyğundursa və uclar fərqlidirsə:
        if fits_left and fits_right and game["left"] != game["right"]:
            markup = types.InlineKeyboardMarkup(row_width=2)
            btn_left = types.InlineKeyboardButton(f"⬅️ {game['left']} tərəfə", callback_data=f"set_side:left:{stone_value}")
            btn_right = types.InlineKeyboardButton(f"{game['right']} tərəfə ➡️", callback_data=f"set_side:right:{stone_value}")
            markup.add(btn_left, btn_right)
            
            await bot.send_message(chat_id, f"🤔 **{stone_value}** daşı hər iki uca uyğundur. Hansı tərəflə birləşdirilsin?", 
                            reply_markup=markup, parse_mode="Markdown")
            return # Burada dayandırırıq, çünki düymə basılmalıdır.
    # --- SEÇİM MƏNTİQİ SONU ---

    moved = False
    attach_side = None   # "left" | "right" | "first"
    new_left, new_right = game["left"], game["right"]

    if game["left"] is None:
        new_left, new_right = a, b
        moved = True
        attach_side = "first"
    else:
        # Kodun ardıcıllığı (sol tərəfə üstünlük verir)
        if a == game["left"]: 
            new_left = b
            moved = True
            attach_side = "left"
        elif b == game["left"]: 
            new_left = a
            moved = True
            attach_side = "left"
        elif a == game["right"]: 
            new_right = b
            moved = True
            attach_side = "right"
        elif b == game["right"]: 
            new_right = a
            moved = True
            attach_side = "right"

    if not moved:
        if can_player_move(game, user_id):
            # Oyunçunun oynaya biləcəyi BAŞQA uyğun daşı var, sadəcə səhv daş seçib
            extra_text = (
                f"💡 Üstü **ağ** görünən daşlardan birini seçin\n\n"
                f"Stoldakı Son Uclar: ({game['left']} ; {game['right']})"
            )
        elif game["stock"]:
            extra_text = (
                "⚠️ Gedişat üçün uyğun daşınız olmadığından, bazardan daş götürməlisiniz "
            )
        else:
            extra_text = (
                "⚠️ Gedişat üçün uyğun daşınız olmadığından və bazarda daş qalmadığından, növbənizi PASS etməlisiniz"
            )

        await send_move_warning(chat_id, msg, extra_text)
        return

    # Gediş Uğurludur
    old_left, old_right = game["left"], game["right"]
    game["hands"][user_id].remove(stone_value)
    game["left"], game["right"] = new_left, new_right
    game["last_activity"] = datetime.datetime.utcnow()

    # Atılan daşı canlı taxta siyahısına DÜZGÜN İSTİQAMƏTLƏNMİŞ əlavə et:
    # sol uca gedirsə əvvələ, sağ uca gedirsə sona — və hər tərəfdə TOXUNAN
    # rəqəm qonşu daşa baxan tərəfdə olsun ki, şəkildə uclar üst-üstə düşsün.
    board_tiles = game.setdefault("board_tiles", [])
    if attach_side == "left":
        board_tiles.insert(0, (new_left, old_left))
    elif attach_side == "right":
        board_tiles.append((old_right, new_right))
    else:  # "first" — ilk daş, hələ qonşusu yoxdur
        board_tiles.append((a, b))

    # Oyun bitmə yoxlaması
    if len(game["hands"][user_id]) == 0:
        update_winner_count(user_id, user_name)

        for uid, name in game["players"].items():
            record_game_played(uid, name)

        scores = calculate_scores(game)
        sorted_scores = sorted(scores.items(), key=lambda item: item[1]['score'])

        save_game_history(chat_id, user_id, user_name, "Bütün daşlarını atdı", game["players"], scores)

        score_list = []
        for uid, data in sorted_scores:
            name = game['players'].get(uid, "Anonim")
            status = "🏆 **Qalib**" if uid == user_id else f"{data['score']} xalı qaldı ({data['tiles']} daş)"
            score_list.append(f"*{escape_md(name)}*: {status}")

        result_message = f"🎉 **OYUN BAŞA ÇATDI!** 🎉\n\n🏆 **{escape_md(user_name)}** qazandı!\n\n" + "\n".join(score_list)
        # Son taxta vəziyyətini qalibi vurğulayaraq göndər, sonra nəticə mesajı
        await _send_board_photo(chat_id, winner_uid=user_id)
        await bot.send_message(chat_id, result_message, parse_mode="Markdown")
        board_msg_ids.pop(chat_id, None)
        games.pop(chat_id, None)
        return

    # Oyun davam edir: növbəni növbəti oyunçuya keçir və taxtanı yenilə
    game["turn"] = (game["turn"] + 1) % len(game["players"])

    if check_for_blocked_game(chat_id):
        return await end_game_by_score(chat_id)

    await send_board_status(chat_id)
    return
# --- Təxmin edilən funksiyanın sonu ---

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('set_side'))
async def process_side_choice(callback_query: types.CallbackQuery):
    _, side, stone_value = callback_query.data.split(":")
    chat_id = callback_query.message.chat.id
    user_id = callback_query.from_user.id
    game = games.get(chat_id)

    if not game: 
        try:
            return await callback_query.answer("❌ Aktiv oyun tapılmadı.")
        except Exception:
            return
    
    player_ids = list(game["players"].keys())
    if player_ids[game["turn"]] != user_id:
        try:
            return await callback_query.answer("❌ Sizin növbəniz deyil!", show_alert=True)
        except Exception:
            # 🟢 DÜZƏLİŞ: sorğu artıq köhnəlibsə (InvalidQueryID), botu
            # çökdürmək əvəzinə sadəcə səssizcə keç.
            return

    a, b = map(int, stone_value.split("-"))
    user_name = callback_query.from_user.first_name

    # Seçilən tərəfə görə ucları yeniləyirik
    old_left, old_right = game["left"], game["right"]
    if side == "left":
        game["left"] = b if a == old_left else a
    else:
        game["right"] = b if a == old_right else a

    # Daşı əldən silirik
    if stone_value in game["hands"][user_id]:
        game["hands"][user_id].remove(stone_value)

    # Atılan daşı canlı taxta siyahısına DÜZGÜN İSTİQAMƏTLƏNMİŞ əlavə et
    # (bax: process_sticker_move-dakı eyni məntiq)
    board_tiles = game.setdefault("board_tiles", [])
    if side == "left":
        board_tiles.insert(0, (game["left"], old_left))
    else:
        board_tiles.append((old_right, game["right"]))

    game["last_activity"] = datetime.datetime.utcnow()

    # Oyun bitmə yoxlaması
    if len(game["hands"][user_id]) == 0:
        try: await callback_query.message.delete()
        except: pass

        update_winner_count(user_id, user_name)

        for uid, name in game["players"].items():
            record_game_played(uid, name)

        scores = calculate_scores(game)
        sorted_scores = sorted(scores.items(), key=lambda item: item[1]['score'])

        save_game_history(chat_id, user_id, user_name, "Bütün daşlarını atdı", game["players"], scores)

        score_list = []
        for uid, data in sorted_scores:
            name = game['players'].get(uid, "Anonim")
            status = "🏆 **Qalib**" if uid == user_id else f"{data['score']} xalı qaldı ({data['tiles']} daş)"
            score_list.append(f"*{escape_md(name)}*: {status}")

        result_message = f"🎉 **OYUN BAŞA ÇATDI!** 🎉\n\n🏆 **{escape_md(user_name)}** qazandı!\n\n" + "\n".join(score_list)
        # Son taxta vəziyyətini qalibi vurğulayaraq göndər, sonra nəticə mesajı
        await _send_board_photo(chat_id, winner_uid=user_id)
        await bot.send_message(chat_id, result_message, parse_mode="Markdown")
        board_msg_ids.pop(chat_id, None)
        games.pop(chat_id, None)
        return
    game["turn"] = (game["turn"] + 1) % len(game["players"])
    
    try: await callback_query.message.delete() # Düyməli mesajı silirik ki, qrupda yer tutmasın
    except: pass
    
    # Bloklanma yoxlaması
    if check_for_blocked_game(chat_id):
        return await end_game_by_score(chat_id)
        
    # YALNIZ BİR DƏFƏ ÇAĞIRIRIQ:
    await send_board_status(chat_id)
    try:
        await callback_query.answer(f"✅ Daş {side} tərəfə yerləşdirildi.")
    except Exception:
        # 🟢 DÜZƏLİŞ: gediş artıq tam icra olunub, sorğu köhnəlmiş olsa belə
        # (InvalidQueryID) bu, botu çökdürməməlidir.
        pass
          
@dp.callback_query_handler(lambda c: c.data in ['join_game_again', 'show_help', 'show_rating'])
async def process_ignore_or_redirect(call: types.CallbackQuery):
    if call.data == 'show_help':
        await call.answer("Kömək məlumatları göstərilir...", show_alert=False)
        await cmd_help(call.message)
    elif call.data == 'show_rating':
        await call.answer("Reytinq siyahısı göstərilir...", show_alert=False)
        await cmd_rating(call.message)
    else:
        await call.answer()


# =======================================================
# 5.5 PAYLAŞILAN API QATI (Mini App bunları çağıracaq)
# =======================================================
# 🟢 YENİ: Bu bölmə botun özündən ayrı DEYİL — eyni prosesdə, eyni
# MongoDB kolleksiyalarında işləyir ki, qrupda oynanan və app-da görünən
# profil/reytinq/tarixçə HƏMİŞƏ eyni olsun. Yalnız OXUMA (GET) endpoint-
# ləridir — canlı oyuna yazma (gediş etmə) hələ bu mərhələdə yoxdur, bu
# növbəti addımdır.

@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as ex:
            resp = ex
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _profile_payload(doc: dict) -> dict:
    wins = doc.get("wins", 0) or 0
    level, rank = compute_level(wins)
    return {
        "user_id": doc.get("_id"),
        "name": doc.get("name", "Anonim"),
        "wins": wins,
        "games": doc.get("games", 0) or 0,
        "level": level,
        "rank": rank,
    }


async def api_profile(request):
    user_id = request.match_info["user_id"]
    if ratings_collection is None:
        return web.json_response({"error": "Verilənlər bazası mövcud deyil"}, status=503)
    doc = ratings_collection.find_one({"_id": str(user_id)})
    if not doc:
        return web.json_response(_profile_payload({"_id": str(user_id)}))
    return web.json_response(_profile_payload(doc))


async def api_leaderboard(request):
    if ratings_collection is None:
        return web.json_response({"error": "Verilənlər bazası mövcud deyil"}, status=503)
    docs = load_top_ratings()
    return web.json_response([_profile_payload(d) for d in docs])


async def api_history_for_user(request):
    user_id = str(request.match_info["user_id"])
    if history_collection is None:
        return web.json_response({"error": "Verilənlər bazası mövcud deyil"}, status=503)
    cursor = history_collection.find({"players": user_id}).sort("created_at", -1).limit(30)
    out = []
    for doc in cursor:
        out.append({
            "id": str(doc["_id"]),
            "winner_name": doc.get("winner_name"),
            "reason": doc.get("reason"),
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
        })
    return web.json_response(out)


async def api_history_detail(request):
    history_id = request.match_info["history_id"]
    if history_collection is None:
        return web.json_response({"error": "Verilənlər bazası mövcud deyil"}, status=503)
    from bson import ObjectId
    try:
        doc = history_collection.find_one({"_id": ObjectId(history_id)})
    except Exception:
        doc = None
    if not doc:
        return web.json_response({"error": "Tapılmadı"}, status=404)
    return web.json_response({
        "winner_name": doc.get("winner_name"),
        "reason": doc.get("reason"),
        "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
        "details": doc.get("details", []),
    })


async def api_lobbies(request):
    """Hələ başlamamış (qeydiyyat mərhələsində olan) açıq lobbilər."""
    out = []
    for chat_id, game in list(games.items()):
        if game.get("turn") is not None:
            continue  # artıq başlayıb, lobbi sayılmır
        out.append({
            "chat_id": chat_id,
            "players": list(game.get("players", {}).values()),
            "count": len(game.get("players", {})),
        })
    return web.json_response(out)


async def api_health(request):
    return web.json_response({"status": "ok"})


import hmac
import hashlib
from urllib.parse import parse_qsl


def verify_telegram_init_data(init_data: str):
    """Telegram Mini App-dan gələn 'initData' imzasını yoxlayır və içindəki
    istifadəçini qaytarır (yoxdursa/saxtadırsa None).

    🟢 VACİB: Bu olmadan istənilən adam özünü başqası kimi göstərib başqasının
    adından gediş edə bilərdi — ona görə HƏR yazma (POST) sorğusunda mütləq
    bu yoxlamadan keçirilir.
    """
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed_hash, received_hash):
            return None
        user_json = parsed.get("user")
        if not user_json:
            return None
        user = json.loads(user_json)
        return {"id": user.get("id"), "first_name": user.get("first_name", "Anonim")}
    except Exception:
        return None


def _auth_from_request(request):
    return verify_telegram_init_data(request.headers.get("X-Telegram-Init-Data", ""))


def _game_public_state(chat_id, game, requester_id=None):
    """Botdakı 'games[chat_id]' obyektini app-ın oxuya biləcəyi JSON-a çevirir.
    Bu, HƏM qrupda, HƏM app-da EYNİ canlı oyun vəziyyətidir — ayrı-ayrı
    saxlanmır, elə həmin dict-dən oxunur."""
    player_ids = list(game["players"].keys())
    turn_uid = None
    if game.get("turn") is not None and game["turn"] < len(player_ids):
        turn_uid = player_ids[game["turn"]]

    players_out = []
    for uid in player_ids:
        players_out.append({
            "user_id": str(uid),
            "name": game["players"][uid],
            "tile_count": len(game["hands"].get(uid, [])),
        })

    state = {
        "chat_id": chat_id,
        "left": game.get("left"),
        "right": game.get("right"),
        "board_tiles": game.get("board_tiles", []),
        "players": players_out,
        "turn_user_id": str(turn_uid) if turn_uid is not None else None,
        "stock_count": len(game.get("stock", [])),
        "started": game.get("turn") is not None,
    }
    if requester_id is not None and requester_id in game.get("hands", {}):
        state["your_hand"] = game["hands"][requester_id]
        state["is_your_turn"] = (turn_uid == requester_id)
    return state


async def api_game_state(request):
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"error": "Yanlış chat_id"}, status=400)
    game = games.get(chat_id)
    if not game:
        return web.json_response({"error": "Aktiv oyun tapılmadı"}, status=404)
    auth = _auth_from_request(request)
    requester_id = auth["id"] if auth else None
    return web.json_response(_game_public_state(chat_id, game, requester_id))


async def api_lobby_join(request):
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"error": "Yanlış chat_id"}, status=400)
    auth = _auth_from_request(request)
    if not auth:
        return web.json_response({"error": "Kimlik doğrulanmadı"}, status=401)
    user_id, user_name = auth["id"], auth["first_name"]

    game = games.get(chat_id)
    if not game:
        return web.json_response({"error": "Oyun tapılmadı və ya artıq bitib"}, status=404)
    if game.get("turn") is not None:
        return web.json_response({"error": "Oyun artıq başlayıb, indi qoşula bilməzsiniz"}, status=409)
    if user_id in game["players"]:
        return web.json_response({"error": "Sən artıq oyuna qoşulmusan"}, status=409)

    MAX_PLAYERS = 4
    if len(game["players"]) >= MAX_PLAYERS:
        return web.json_response({"error": "Oyunçu sayı artıq maksimuma çatıb"}, status=409)

    game["players"][user_id] = user_name
    game["last_lobby_activity"] = datetime.datetime.utcnow()
    await update_join_message(chat_id)

    count = len(game["players"])
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"✅ **{escape_md(user_name)}** (app-dan) oyuna qoşuldu! ({count}/{MAX_PLAYERS} nəfər)",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.warning(f"App-dan qoşulma bildirişi qrupa göndərilə bilmədi: {e}")

    return web.json_response({"ok": True})


async def api_game_play(request):
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"error": "Yanlış chat_id"}, status=400)
    auth = _auth_from_request(request)
    if not auth:
        return web.json_response({"error": "Kimlik doğrulanmadı"}, status=401)
    user_id, user_name = auth["id"], auth["first_name"]

    game = games.get(chat_id)
    if not game:
        return web.json_response({"error": "Aktiv oyun tapılmadı"}, status=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    stone_value = body.get("stone")
    side_choice = body.get("side")  # "left" | "right" | None

    player_ids = list(game["players"].keys())
    try:
        current_player_id = player_ids[game["turn"]]
    except (TypeError, IndexError, KeyError):
        return web.json_response({"error": "Növbə məlumatı səhvdir"}, status=409)

    if current_player_id != user_id:
        return web.json_response({"error": "Sizin növbəniz deyil"}, status=409)

    if not stone_value or stone_value not in game["hands"].get(user_id, []):
        return web.json_response({"error": "Bu daş əlinizdə yoxdur"}, status=400)

    a, b = map(int, stone_value.split("-"))

    # --- Botdakı ilə EYNİ seçim məntiqi ---
    if game["left"] is not None:
        fits_left = (a == game["left"] or b == game["left"])
        fits_right = (a == game["right"] or b == game["right"])
        if fits_left and fits_right and game["left"] != game["right"] and not side_choice:
            return web.json_response({"needs_side_choice": True})

    moved = False
    attach_side = None
    new_left, new_right = game["left"], game["right"]

    if game["left"] is None:
        new_left, new_right = a, b
        moved = True
        attach_side = "first"
    elif side_choice == "left":
        new_left = b if a == game["left"] else a
        moved = True
        attach_side = "left"
    elif side_choice == "right":
        new_right = b if a == game["right"] else a
        moved = True
        attach_side = "right"
    else:
        if a == game["left"]:
            new_left = b; moved = True; attach_side = "left"
        elif b == game["left"]:
            new_left = a; moved = True; attach_side = "left"
        elif a == game["right"]:
            new_right = b; moved = True; attach_side = "right"
        elif b == game["right"]:
            new_right = a; moved = True; attach_side = "right"

    if not moved:
        return web.json_response({"error": "Bu daş taxtaya uymur"}, status=400)

    # --- Botdakı ilə EYNİ tətbiq addımları ---
    old_left, old_right = game["left"], game["right"]
    game["hands"][user_id].remove(stone_value)
    game["left"], game["right"] = new_left, new_right
    game["last_activity"] = datetime.datetime.utcnow()

    board_tiles = game.setdefault("board_tiles", [])
    if attach_side == "left":
        board_tiles.insert(0, (new_left, old_left))
    elif attach_side == "right":
        board_tiles.append((old_right, new_right))
    else:
        board_tiles.append((a, b))

    # Oyun bitmə yoxlaması (bot ilə EYNİ, MongoDB-yə eyni funksiyalarla yazır)
    if len(game["hands"][user_id]) == 0:
        update_winner_count(user_id, user_name)
        for uid, name in game["players"].items():
            record_game_played(uid, name)
        scores = calculate_scores(game)
        save_game_history(chat_id, user_id, user_name, "Bütün daşlarını atdı", game["players"], scores)

        sorted_scores = sorted(scores.items(), key=lambda item: item[1]['score'])
        score_list = []
        for uid, data in sorted_scores:
            name = game['players'].get(uid, "Anonim")
            status = "🏆 **Qalib**" if uid == user_id else f"{data['score']} xalı qaldı ({data['tiles']} daş)"
            score_list.append(f"*{escape_md(name)}*: {status}")
        result_message = f"🎉 **OYUN BAŞA ÇATDI!** 🎉\n\n🏆 **{escape_md(user_name)}** qazandı!\n\n" + "\n".join(score_list)

        await _send_board_photo(chat_id, winner_uid=user_id)
        await bot.send_message(chat_id, result_message, parse_mode="Markdown")
        board_msg_ids.pop(chat_id, None)
        games.pop(chat_id, None)
        return web.json_response({"ok": True, "game_over": True, "winner": user_name})

    game["turn"] = (game["turn"] + 1) % len(game["players"])

    if check_for_blocked_game(chat_id):
        await end_game_by_score(chat_id)
        return web.json_response({"ok": True, "game_over": True})

    # 🟢 SİNXRONİZASİYA: app-dan edilən gediş qrupda da (foto+mətn) görünsün
    await send_board_status(chat_id)
    return web.json_response({"ok": True, "state": _game_public_state(chat_id, games.get(chat_id), user_id)})


async def api_game_draw(request):
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"error": "Yanlış chat_id"}, status=400)
    auth = _auth_from_request(request)
    if not auth:
        return web.json_response({"error": "Kimlik doğrulanmadı"}, status=401)
    user_id, user_name = auth["id"], auth["first_name"]

    game = games.get(chat_id)
    if not game:
        return web.json_response({"error": "Aktiv oyun tapılmadı"}, status=404)

    player_ids = list(game["players"].keys())
    try:
        current_player_id = player_ids[game["turn"]]
    except (TypeError, IndexError, KeyError):
        return web.json_response({"error": "Növbə məlumatı səhvdir"}, status=409)
    if current_player_id != user_id:
        return web.json_response({"error": "Sizin növbəniz deyil"}, status=409)

    if can_player_move(game, user_id):
        return web.json_response({"error": "Oynaya biləcəyiniz daş var, bazara ehtiyac yoxdur"}, status=409)

    if check_for_blocked_game(chat_id):
        await end_game_by_score(chat_id)
        return web.json_response({"ok": True, "game_over": True})

    if not game["stock"]:
        return web.json_response({"error": "Bazar boşdur, PASS etməlisiniz"}, status=409)

    new_stone = game["stock"].pop(0)
    game["hands"][user_id].append(new_stone)
    game["last_activity"] = datetime.datetime.utcnow()

    try:
        await bot.send_message(
            chat_id,
            f"📥 *{escape_md(user_name)}* (app-dan) bazardan 1 daş götürdü. Bazarda **{len(game['stock'])}** daş qaldı",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    if check_for_blocked_game(chat_id):
        await end_game_by_score(chat_id)
        return web.json_response({"ok": True, "game_over": True})

    return web.json_response({"ok": True, "state": _game_public_state(chat_id, game, user_id)})


async def api_game_pass(request):
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        return web.json_response({"error": "Yanlış chat_id"}, status=400)
    auth = _auth_from_request(request)
    if not auth:
        return web.json_response({"error": "Kimlik doğrulanmadı"}, status=401)
    user_id, user_name = auth["id"], auth["first_name"]

    game = games.get(chat_id)
    if not game:
        return web.json_response({"error": "Aktiv oyun tapılmadı"}, status=404)

    player_ids = list(game["players"].keys())
    try:
        current_player_id = player_ids[game["turn"]]
    except (TypeError, IndexError, KeyError):
        return web.json_response({"error": "Növbə məlumatı səhvdir"}, status=409)
    if current_player_id != user_id:
        return web.json_response({"error": "Sizin növbəniz deyil"}, status=409)

    if can_player_move(game, user_id):
        return web.json_response({"error": "Oynaya biləcəyiniz daş var, PASS edə bilməzsiniz"}, status=409)
    if len(game.get("stock", [])) > 0:
        return web.json_response({"error": "Bazarda daş var, əvvəlcə oradan götürməlisiniz"}, status=409)

    try:
        await bot.send_message(
            chat_id,
            f"⏩ *{escape_md(user_name)}* (app-dan) daşı olmadığı və bazar boş olduğu üçün növbəni keçdi (PASS)",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    game["turn"] = (game["turn"] + 1) % len(game["players"])
    game["last_activity"] = datetime.datetime.utcnow()

    if check_for_blocked_game(chat_id):
        await end_game_by_score(chat_id)
        return web.json_response({"ok": True, "game_over": True})

    await send_turn(chat_id)
    return web.json_response({"ok": True, "state": _game_public_state(chat_id, game, user_id)})


async def start_api_server():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/profile/{user_id}", api_profile)
    app.router.add_get("/api/leaderboard", api_leaderboard)
    app.router.add_get("/api/history/{user_id}", api_history_for_user)
    app.router.add_get("/api/history/game/{history_id}", api_history_detail)
    app.router.add_get("/api/lobbies", api_lobbies)
    app.router.add_get("/api/game/{chat_id}", api_game_state)
    app.router.add_post("/api/lobby/{chat_id}/join", api_lobby_join)
    app.router.add_post("/api/game/{chat_id}/play", api_game_play)
    app.router.add_post("/api/game/{chat_id}/draw", api_game_draw)
    app.router.add_post("/api/game/{chat_id}/pass", api_game_pass)
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response())

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8000)
    await site.start()
    logging.info("API server started on port 8000")


# =======================================================
# 6. STARTUP BLOKU (HEROKU ÜÇÜN DÜZƏLİŞ)
# =======================================================

async def on_startup(dp):
    global BOT_USERNAME, BOT_ID
    
    await bot.delete_webhook() # Köhnə Webhook-u təmizlə
    
    # BOT_USERNAME-i və BOT_ID-ni təyin et
    me = await bot.get_me()
    BOT_USERNAME = me.username
    BOT_ID = me.id
    
    logging.info(f"Bot Polling started. Username: @{BOT_USERNAME}")

    # 🟢 ƏLAVƏ EDİLƏN XƏTT: Vaxt Aşımı Yoxlama Taskını Başlatın!
    # check_for_inactive_games funksiyasını arxa fonda işə salır.
    asyncio.create_task(check_for_inactive_games())
    # --------------------------------------------------------

    # 🟢 YENİ: Paylaşılan API-nı da eyni prosesdə, arxa fonda başlat
    # (port 8000 — VDS-dəki Cloudflare Tunnel elə bu portu göstərir).
    asyncio.create_task(start_api_server())

if __name__ == '__main__':
    
    # 1. on_startup-u məcburi icra etmək üçün (BOT_USERNAME-i təmin etmək)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(on_startup(dp))
    
    # 2. Polling-i başla
    executor.start_polling(dispatcher=dp, skip_updates=True)
