import os
import logging
import time
import random
import re
from typing import Optional, Tuple, List, Set

import requests
import feedparser
from bs4 import BeautifulSoup
from flask import Flask
from telegram import Bot

# ===== ЛОГИ =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ===== НАСТРОЙКИ И СЕКРЕТЫ =====
TOKEN = os.getenv("8267958305:AAHaHEhrR0X-hZCm9V22cxz2AbJGBOgDSQQ")                 # токен бота от BotFather
CHAT_ID = os.getenv("@cripta_tg_000")# можно указать @имя_канала или числовой -100...
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# каждые 6 часов будем дергать эндпоинт /run через Cron на Render
POST_PARAGRAPHS = (3, 5)   # публикуем 3–5 абзацев
MAX_TEXT_LEN = 3500        # чтобы не утыкаться в лимиты Telegram, если текста слишком много

# ===== ОФИЦИАЛЬНЫЕ/КРУПНЫЕ ИСТОЧНИКИ (русскоязычные или с русскоязычным контентом) =====
RSS_FEEDS: List[str] = [
    "https://forklog.com/feed/",                # Forklog (RU)
    "https://bits.media/feed/",                 # Bits.media (RU)
    "https://beincrypto.ru/feed/",              # BeInCrypto (RU)
    "https://www.crypto.ru/feed/",              # Crypto.ru (RU)
    "https://procryptonews.ru/rss",             # ProCryptoNews (RU)
    "https://vc.ru/crypto/rss",                 # VC.ru Крипто (RU)
    "https://ru.cointelegraph.com/rss",         # Cointelegraph (RU)
    "https://coinpost.ru/rss",                  # CoinPost (RU)
    "https://ru.cryptonews.com/feed/",          # CryptoNews (RU)
    "https://cryptopotato.com/ru/feed/",        # CryptoPotato (RU)
    "https://news.bitcoin.com/feed/",           # Bitcoin.com (EN/RU материалы встречаются → фильтр по кириллице отсеет англ)
    "https://cryptoslate.com/feed/",            # CryptoSlate (EN → пройдёт только если текст на кириллице)
]

# ===== ПАМЯТЬ ПРО ОПУБЛИКОВАННОЕ (чтобы не было дублей даже после перезапуска) =====
POSTED_FILE = "posted.txt"
posted_links: Set[str] = set()

def load_posted():
    global posted_links
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            for line in f:
                url = line.strip()
                if url:
                    posted_links.add(url)

def remember_posted(url: str):
    posted_links.add(url)
    with open(POSTED_FILE, "a", encoding="utf-8") as f:
        f.write(url + "\n")

# ===== ВСПОМОГАТЕЛЬНОЕ =====
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")

def is_russian_text(text: str) -> bool:
    """Проверяем, что в тексте есть достаточное кол-во кириллицы."""
    if not text:
        return False
    # условно: есть хотя бы 40 кириллических символов
    return len(CYRILLIC_RE.findall(text)) >= 40

def fetch_html(url: str) -> Optional[BeautifulSoup]:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=12)
        if r.status_code == 200 and r.content:
            return BeautifulSoup(r.content, "html.parser")
    except Exception as e:
        logging.warning(f"Не удалось загрузить страницу: {url} → {e}")
    return None

def extract_image(entry, soup: Optional[BeautifulSoup]) -> Optional[str]:
    # 1) media_content из RSS
    if hasattr(entry, "media_content") and entry.media_content:
        url = entry.media_content[0].get("url")
        if url and url.startswith("http"):
            return url
    # 2) media_thumbnail из RSS
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        url = entry.media_thumbnail[0].get("url")
        if url and url.startswith("http"):
            return url
    # 3) ссылки с type=image
    if hasattr(entry, "links"):
        for link in entry.links:
            if link.get("type", "").startswith("image"):
                url = link.get("href")
                if url and url.startswith("http"):
                    return url
    # 4) meta og:image / twitter:image со страницы
    if soup:
        for prop in ["meta[property='og:image']", "meta[name='twitter:image']"]:
            tag = soup.select_one(prop)
            if tag:
                url = tag.get("content")
                if url and url.startswith("http"):
                    return url
        # запасной вариант: первая видимая картинка в статье
        img = soup.find("img")
        if img:
            url = img.get("src") or img.get("data-src")
            if url and url.startswith("http"):
                return url
    return None

def extract_paragraphs(soup: BeautifulSoup) -> List[str]:
    # собираем абзацы
    paragraphs = []
    for p in soup.find_all("p"):
        txt = p.get_text(strip=True)
        if txt and len(txt) > 40:  # отсеиваем короткое
            paragraphs.append(txt)
        if len(paragraphs) >= 12:  # ограничим чтение
            break
    return paragraphs

def build_message(title: str, paragraphs: List[str], link: str) -> str:
    # берём 3–5 абзацев
    take = random.randint(POST_PARAGRAPHS[0], POST_PARAGRAPHS[1])
    content = "\n\n".join(paragraphs[:take]).strip()
    # ограничим общий размер
    text = f"📰 {title}\n\n{content}\n\n🔗 Подробнее: {link}"
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN - 60].rstrip() + f"\n\n🔗 Подробнее: {link}"
    return text

def get_one_russian_news() -> Optional[Tuple[str, str, Optional[str]]]:
    """Возвращает (message_text, image_url, source_link) или None, если нет подходящей новости."""
    # перемешиваем источники, чтобы не зацикливаться на одном
    feeds = RSS_FEEDS[:]
    random.shuffle(feeds)

    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            logging.warning(f"Не удалось распарсить RSS: {feed_url} → {e}")
            continue

        for entry in feed.entries:
            link = entry.get("link")
            if not link or link in posted_links:
                continue

            # грузим страницу
            soup = fetch_html(link)
            if not soup:
                continue

            # абзацы и проверка языка
            paragraphs = extract_paragraphs(soup)
            if not paragraphs:
                # запасной вариант: summary из RSS
                summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
                if not is_russian_text(summary):
                    continue
                paragraphs = [summary]

            # проверяем, что текст действительно на русском
            all_text = " ".join(paragraphs[:5])
            if not is_russian_text(all_text):
                continue

            title = entry.get("title", "Без названия").strip()
            if not is_russian_text(title):
                # иногда заголовок в RSS на EN, а статья — RU. Оставляем, если контент RU.
                pass

            # картинка
            img = extract_image(entry, soup)

            # соберём сообщение
            message = build_message(title, paragraphs, link)

            # запоминаем и отдаём
            remember_posted(link)
            return message, img, link

    return None

# ===== TELEGRAM =====
bot = Bot(token=TOKEN)

def post_news_once():
    if not TOKEN or not CHAT_ID:
        logging.error("Не задан BOT_TOKEN или CHAT_ID (проверьте переменные окружения).")
        return

    news = get_one_russian_news()
    if not news:
        logging.info("Нет подходящих свежих новостей (на русском).")
        return

    message, image_url, _ = news

    try:
        if image_url:
            # caption у фото ограничен ~1024 символами → длинный текст шлём отдельным сообщением
            bot.send_photo(chat_id=CHAT_ID, photo=image_url, caption="📰 Новость дня")
            bot.send_message(chat_id=CHAT_ID, text=message)
        else:
            bot.send_message(chat_id=CHAT_ID, text=message)
        logging.info("✅ Новость опубликована.")
    except Exception as e:
        logging.error(f"Ошибка публикации: {e}")

# ===== FLASK (для Render и Cron) =====
app = Flask(__name__)

@app.route("/")
def root():
    return "Бот работает ✅"

@app.route("/run")
def run_once():
    post_news_once()
    return "ОК"

if __name__ == "__main__":
    load_posted()
    # локальный запуск (на Render запустит gunicorn из Procfile)
    app.run(host="0.0.0.0", port=8080)
