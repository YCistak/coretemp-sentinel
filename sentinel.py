#!/usr/bin/env python3
"""
Coretemp Sentinel - Linux Kernel & Bugzilla Intelligence Bot
Monitors Patchwork (hwmon), Kernel Bugzilla, and upstream activities for Asaf Nuri Bertizlioglu.
"""

import sys
import os
import json
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timezone

CONFIG_PATH = os.path.expanduser("~/.config/coretemp-sentinel/config.json")
DEFAULT_DB_PATH = "./sentinel.db"

def load_config():
    """Konfigurasyonu dosyadan okur; dosya yoksa bos sozluk doner.

    GitHub Actions gibi config dosyasinin bulunmadigi ortamlarda
    degerler ortam degiskenlerinden gelir (TELEGRAM_TOKEN, CHAT_ID).
    """
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def apply_env_overrides(config):
    """Ortam degiskenleri config dosyasinin onune gecer."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if token:
        config["telegram_token"] = token.strip()
    if chat_id:
        config["chat_id"] = chat_id.strip()
    return config

def resolve_db_path(config):
    """SENTINEL_DB > config db_path > ./sentinel.db"""
    env_db = os.environ.get("SENTINEL_DB")
    if env_db:
        return env_db
    return config.get("db_path", DEFAULT_DB_PATH)

def init_db(db_path):
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn

def is_seen(conn, item_id, source):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM seen WHERE id = ? AND source = ?", (str(item_id), source))
    return cur.fetchone() is not None

def mark_seen(conn, item_id, source, title):
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO seen (id, source, title, created_at) VALUES (?, ?, ?, ?)",
                (str(item_id), source, title, datetime.now(timezone.utc).isoformat()))
    conn.commit()

def count_seen(conn):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM seen")
    return cur.fetchone()[0]

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "CoretempSentinel/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Telegram send error: {e}", file=sys.stderr)
        return None

def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "CoretempSentinel/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Fetch error for {url}: {e}", file=sys.stderr)
        return None

def check_patchwork(conn, config, is_baseline):
    token = config["telegram_token"]
    chat_id = config["chat_id"]
    
    # 1. Direct coretemp search on linux-hwmon
    url = "https://patchwork.kernel.org/api/1.2/patches/?project=linux-hwmon&q=coretemp"
    data = fetch_json(url)
    if not data:
        return

    for patch in data:
        p_id = str(patch.get("id"))
        title = patch.get("name", "Bilinmeyen Yama")
        submitter = patch.get("submitter", {}).get("name", "Bilinmiyor")
        email = patch.get("submitter", {}).get("email", "")
        web_url = patch.get("web_url", f"https://patchwork.kernel.org/patch/{p_id}/")
        date_str = patch.get("date", "")[:10]

        if not is_seen(conn, p_id, "patchwork"):
            mark_seen(conn, p_id, "patchwork", title)
            if not is_baseline:
                # Urgent alert
                msg = (
                    f"🚨 <b>YENİ CORETEMP YAMASI DÜŞTÜ!</b>\n\n"
                    f"📝 <b>Başlık:</b> {title}\n"
                    f"👤 <b>Gönderen:</b> {submitter} &lt;{email}&gt;\n"
                    f"📅 <b>Tarih:</b> {date_str}\n"
                    f"🔗 <a href='{web_url}'>Patchwork Üzerinde İncele</a>\n\n"
                    f"⚡ <i>Aksiyon: Kendi i7-14700F donanımında test et ve Tested-by/Reviewed-by hazırla!</i>"
                )
                send_telegram(token, chat_id, msg)

def check_bugzilla(conn, config, is_baseline):
    token = config["telegram_token"]
    chat_id = config["chat_id"]
    
    url = "https://bugzilla.kernel.org/rest/bug?component=Hardware%20Monitoring&limit=15&order=changeddate%20desc"
    data = fetch_json(url)
    if not data or "bugs" not in data:
        return

    keywords = ["coretemp", "intel", "temperature", "dts", "thermal", "temp", "tjmax"]

    for bug in data.get("bugs", []):
        b_id = str(bug.get("id"))
        summary = bug.get("summary", "")
        status = bug.get("status", "")
        creator = bug.get("creator", "Bilinmiyor")
        web_url = f"https://bugzilla.kernel.org/show_bug.cgi?id={b_id}"
        
        # Check relevance
        summary_lower = summary.lower()
        if not any(k in summary_lower for k in keywords):
            continue

        if not is_seen(conn, b_id, "bugzilla"):
            mark_seen(conn, b_id, "bugzilla", summary)
            if not is_baseline:
                msg = (
                    f"🐛 <b>YENİ KERNEL BUGZILLA KAYDI!</b>\n\n"
                    f"🆔 <b>Bug ID:</b> #{b_id}\n"
                    f"📝 <b>Özet:</b> {summary}\n"
                    f"📊 <b>Durum:</b> {status}\n"
                    f"👤 <b>Raporlayan:</b> {creator}\n"
                    f"🔗 <a href='{web_url}'>Bugzilla Kaydını Aç</a>\n\n"
                    f"⚡ <i>Fırsat: Hatayı incele, çözümü ilk sunan maintainer adayı sen ol!</i>"
                )
                send_telegram(token, chat_id, msg)

def main():
    config = apply_env_overrides(load_config())

    token = config.get("telegram_token") or ""
    chat_id = config.get("chat_id") or ""
    if not token or not chat_id:
        sys.exit("HATA: telegram_token/chat_id bulunamadi "
                 "(ne TELEGRAM_TOKEN+CHAT_ID ortam degiskeni ne de config dosyasi).")

    db_path = resolve_db_path(config)
    conn = init_db(db_path)
    
    total_seen_before = count_seen(conn)
    is_baseline = (total_seen_before == 0)

    if is_baseline:
        print("[*] Ilk calistirma: Mevcut kayitlar taban cizgisi (baseline) olarak kaydediliyor...")

    check_patchwork(conn, config, is_baseline)
    check_bugzilla(conn, config, is_baseline)

    if is_baseline:
        total_now = count_seen(conn)
        token = config["telegram_token"]
        chat_id = config["chat_id"]
        baseline_msg = (
            f"🎯 <b>Coretemp Sentinel Radar Sistemi Aktif!</b>\n\n"
            f"✅ <b>Linux Patchwork (hwmon):</b> Bağlandı.\n"
            f"✅ <b>Kernel Bugzilla (Hardware Monitoring):</b> Bağlandı.\n"
            f"📊 <b>Başlangıç Kaydı:</b> {total_now} geçmiş kayıt hafızaya alındı.\n\n"
            f"🛡️ <i>Şu andan itibaren Linux çekirdeğine yeni bir coretemp yaması veya ilgili Bugzilla hatası düştüğünde anında buraya bildirim alacaksınız.</i>"
        )
        send_telegram(token, chat_id, baseline_msg)
        print(f"[+] Baseline tamamlandi: {total_now} kayit islendi, hos geldin mesaji gonderildi.")
    else:
        print(f"[+] Kontrol tamamlandi. Toplam bilinen kayit: {count_seen(conn)}")

if __name__ == "__main__":
    main()
