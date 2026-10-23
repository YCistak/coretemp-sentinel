#!/usr/bin/env python3
"""
Coretemp Sentinel - Linux kernel coretemp surucu radarı.

Yalnizca coretemp ile ilgili kaynaklari izler:
  - Patchwork (linux-hwmon, q=coretemp)  -> yeni yama + durum (state) degisimi
  - lore.kernel.org (linux-hwmon, q=coretemp) -> mail listesi
  - Kernel Bugzilla (summary=coretemp)
  - git.kernel.org coretemp.c commit'leri

Tek bagimlilik Python 3 standart kutuphanesidir.
DRY_RUN=1 ile telegram atmadan, gecici (bellek ici) DB ile sadece parse eder.
"""

import html
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

CONFIG_PATH = os.path.expanduser("~/.config/coretemp-sentinel/config.json")
DEFAULT_DB_PATH = "./sentinel.db"

UA = "CoretempSentinel/1.0"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

# Varsayilan kaynak adresleri. Hepsi ortam degiskeni veya config dosyasi ile
# ezilebilir (bkz. resolve_setting).
DEFAULTS = {
    "patchwork_url": "https://patchwork.kernel.org/api/1.2/patches/?project=linux-hwmon&q=coretemp",
    "lore_url": "https://lore.kernel.org/linux-hwmon/?q=coretemp&x=A",
    "bugzilla_url": "https://bugzilla.kernel.org/rest/bug?summary=coretemp&limit=20&order=changeddate%20desc",
    "git_atom_url": ("https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/"
                     "linux.git/atom/drivers/hwmon/coretemp.c"),
    "git_api_url": ("https://api.github.com/repos/torvalds/linux/commits"
                    "?path=drivers/hwmon/coretemp.c&per_page=15"),
    "lore_days": 30,
    "max_messages": 10,
}

# Kaynak anahtari -> ortam degiskeni adi
ENV_KEYS = {
    "patchwork_url": "SENTINEL_PATCHWORK_URL",
    "lore_url": "SENTINEL_LORE_URL",
    "bugzilla_url": "SENTINEL_BUGZILLA_URL",
    "git_atom_url": "SENTINEL_GIT_ATOM_URL",
    "git_api_url": "SENTINEL_GIT_API_URL",
}


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
    config["dry_run"] = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")
    return config


def resolve_setting(config, key):
    """Ortam degiskeni > config dosyasi > koddaki varsayilan."""
    env_name = ENV_KEYS.get(key)
    if env_name:
        value = os.environ.get(env_name)
        if value:
            return value.strip()
    if key in config:
        return config[key]
    return DEFAULTS[key]


def resolve_db_path(config):
    """SENTINEL_DB > config db_path > ./sentinel.db"""
    env_db = os.environ.get("SENTINEL_DB")
    if env_db:
        return env_db
    return config.get("db_path", DEFAULT_DB_PATH)


# --------------------------------------------------------------------------
# Veritabani
# --------------------------------------------------------------------------

def init_db(db_path, dry_run=False):
    if dry_run:
        # Deneme kosusu gercek state'i kirletmesin.
        conn = sqlite3.connect(":memory:")
    else:
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
    # Eski DB'yi bozmadan state kolonu ekle (migration).
    cur.execute("PRAGMA table_info(seen)")
    columns = {row[1] for row in cur.fetchall()}
    if "state" not in columns:
        cur.execute("ALTER TABLE seen ADD COLUMN state TEXT")
    conn.commit()
    return conn


def is_seen(conn, item_id, source):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM seen WHERE id = ? AND source = ?", (str(item_id), source))
    return cur.fetchone() is not None


def get_seen_state(conn, item_id, source):
    """Kayit varsa (True, state), yoksa (False, None)."""
    cur = conn.cursor()
    cur.execute("SELECT state FROM seen WHERE id = ? AND source = ?", (str(item_id), source))
    row = cur.fetchone()
    if row is None:
        return False, None
    return True, row[0]


def mark_seen(conn, item_id, source, title, state=None):
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO seen (id, source, title, created_at, state) "
        "VALUES (?, ?, ?, ?, ?)",
        (str(item_id), source, title, datetime.now(timezone.utc).isoformat(), state),
    )
    conn.commit()


def count_seen(conn):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM seen")
    return cur.fetchone()[0]


# --------------------------------------------------------------------------
# Ag yardimcilari
# --------------------------------------------------------------------------

def http_get(url, timeout=20, headers=None, auth_token=None):
    """Metin govdesi doner; hata durumunda None (stderr'e yazar)."""
    req_headers = {"User-Agent": UA}
    if headers:
        req_headers.update(headers)
    if auth_token:
        req_headers["Authorization"] = f"token {auth_token}"
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        print(f"[hata] {url} -> HTTP {e.code}", file=sys.stderr)
    except Exception as e:
        print(f"[hata] {url} -> {e}", file=sys.stderr)
    return None


def fetch_json(url, timeout=20, auth_token=None):
    body = http_get(url, timeout=timeout, auth_token=auth_token)
    if body is None:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        print(f"[hata] {url} -> gecersiz JSON: {e}", file=sys.stderr)
        return None


def parse_atom(xml_text):
    """Atom beslemesini sozluk listesine cevirir.

    Her kayit: id, title, link, updated, author, summary
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[hata] Atom ayristirilamadi: {e}", file=sys.stderr)
        return None

    items = []
    for entry in root.findall("a:entry", ATOM_NS):
        link_el = entry.find("a:link", ATOM_NS)
        link = link_el.get("href") if link_el is not None else ""
        item_id = entry.findtext("a:id", default="", namespaces=ATOM_NS) or link
        author_el = entry.find("a:author", ATOM_NS)
        author = ""
        if author_el is not None:
            author = (author_el.findtext("a:name", default="", namespaces=ATOM_NS) or "").strip()
        date = (entry.findtext("a:updated", default="", namespaces=ATOM_NS)
                or entry.findtext("a:published", default="", namespaces=ATOM_NS))
        content_el = entry.find("a:content", ATOM_NS)
        summary = ""
        if content_el is not None:
            summary = " ".join("".join(content_el.itertext()).split())
        items.append({
            "id": item_id,
            "title": " ".join((entry.findtext("a:title", default="", namespaces=ATOM_NS) or "").split()),
            "link": link,
            "updated": (date or "").strip(),
            "author": author,
            "summary": summary,
        })
    return items


def parse_iso(value):
    """'2026-10-21T14:01:21Z' -> aware datetime; basarisizsa None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def short(text, limit=160):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# Bildirim kuyrugu
# --------------------------------------------------------------------------

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Telegram send error: {e}", file=sys.stderr)
        return None


def dispatch(conn, config, messages, is_baseline):
    """Bildirimleri gonderir; tek kosuda en fazla max_messages mesaj.

    messages: [{"source","id","date","text"}] — en yeni once gonderilir,
    limiti asanlar icin tek satir "N kayit daha var" notu dusulur.
    """
    if is_baseline or not messages:
        return 0

    max_messages = int(resolve_setting(config, "max_messages"))
    ordered = sorted(messages, key=lambda m: m.get("date") or "", reverse=True)
    to_send, dropped = ordered[:max_messages], ordered[max_messages:]

    for msg in to_send:
        if config.get("dry_run"):
            print(f"\n--- DRY_RUN mesaj ({msg['source']}) ---\n{msg['text']}")
        else:
            send_telegram(config["telegram_token"], config["chat_id"], msg["text"])

    if dropped:
        note = f"ℹ️ <b>{len(dropped)} kayıt daha var</b> (bir sonraki koşuda gönderilmeyecek, filtrelendi)."
        if config.get("dry_run"):
            print(f"\n--- DRY_RUN not ---\n{note}")
        else:
            send_telegram(config["telegram_token"], config["chat_id"], note)
    return len(to_send)


# --------------------------------------------------------------------------
# Kaynaklar
# --------------------------------------------------------------------------

def check_patchwork(conn, config, is_baseline, messages):
    """Yeni coretemp yamasi + ayni yamanin state degisimi."""
    url = resolve_setting(config, "patchwork_url")
    data = fetch_json(url)
    if data is None:
        return False

    for patch in data:
        p_id = str(patch.get("id"))
        p_state = patch.get("state") or ""
        title = html.escape(patch.get("name") or "Bilinmeyen Yama")
        submitter = html.escape((patch.get("submitter") or {}).get("name") or "Bilinmiyor")
        web_url = patch.get("web_url") or f"https://patchwork.kernel.org/patch/{p_id}/"
        date_str = (patch.get("date") or "")[:10]

        exists, old_state = get_seen_state(conn, p_id, "patchwork")
        if not exists:
            mark_seen(conn, p_id, "patchwork", title, p_state)
            messages.append({
                "source": "patchwork",
                "id": p_id,
                "date": patch.get("date") or "",
                "text": (f"🆕 <b>[patchwork]</b> {title}\n"
                         f"{web_url}\n"
                         f"{date_str} · {submitter} · {html.escape(p_state)}"),
            })
        elif p_state and p_state != old_state:
            mark_seen(conn, p_id, "patchwork", title, p_state)
            # state'i henuz izlenmeyen eski kayitlar (migration sonrasi NULL)
            # sessizce doldurulur; aksi halde ilk kosuda 30 sahte degisim mesaji gider.
            if not old_state:
                continue
            messages.append({
                "source": "patchwork",
                "id": p_id,
                "date": patch.get("date") or "",
                "text": (f"🔄 <b>[patchwork]</b> {title}\n"
                         f"{web_url}\n"
                         f"durum: {html.escape(old_state or '?')} → {html.escape(p_state)}"),
            })
    return True


def check_lore(conn, config, is_baseline, messages):
    """linux-hwmon lore aramasinda 'coretemp' gecen mailler.

    Feed sorgusu (q=coretemp&x=A) 200 kayit ve yillara yayilan gecmis dondurur;
    bu yuzden her zaman son lore_days gun ile sinirlanir, geriye donuk spam olmaz.
    """
    url = resolve_setting(config, "lore_url")
    body = http_get(url, timeout=30)
    if body is None:
        return False
    items = parse_atom(body)
    if items is None:
        return False

    days = int(resolve_setting(config, "lore_days"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    for item in items:
        title_raw = item["title"]
        summary_raw = item["summary"]
        # Besleme sorgusu zaten daraltiyor; yine de yerelde dogrula.
        if "coretemp" not in (title_raw + " " + summary_raw).lower():
            continue

        date = parse_iso(item["updated"])
        if date is None or date < cutoff:
            continue

        m_id = item["id"] or item["link"]
        if is_seen(conn, m_id, "lore"):
            continue
        mark_seen(conn, m_id, "lore", title_raw, None)

        summary_line = short(summary_raw, 160)
        messages.append({
            "source": "lore",
            "id": m_id,
            "date": item["updated"],
            "text": (f"📧 <b>[lore]</b> {html.escape(title_raw)}\n"
                     f"{item['link']}\n"
                     f"{html.escape(item['author'] or '?')} · {item['updated'][:10]}\n"
                     + (f"<i>{html.escape(summary_line)}</i>" if summary_line else "")),
        })
    return True


def check_bugzilla(conn, config, is_baseline, messages):
    """Kernel Bugzilla: yalnizca ozetinde 'coretemp' gecen kayitlar.

    Eski surumdeki component=Hardware Monitoring + anahtar kelime filtresi
    yerine dar bir summary sorgusu kullanilir; boylece ilgisiz hwmon
    gurultusu gelmez. Eski generic kayitlar DB'de kalir, temizlenmez.
    """
    url = resolve_setting(config, "bugzilla_url")
    data = fetch_json(url)
    if data is None or "bugs" not in data:
        return False

    for bug in data.get("bugs", []):
        b_id = str(bug.get("id"))
        summary_raw = bug.get("summary") or ""
        if "coretemp" not in summary_raw.lower():
            continue
        status = bug.get("status") or ""
        creator = bug.get("creator") or "Bilinmiyor"
        web_url = f"https://bugzilla.kernel.org/show_bug.cgi?id={b_id}"

        if is_seen(conn, b_id, "bugzilla"):
            continue
        mark_seen(conn, b_id, "bugzilla", summary_raw, status)

        messages.append({
            "source": "bugzilla",
            "id": b_id,
            "date": bug.get("last_change_time") or "",
            "text": (f"🐛 <b>[bugzilla]</b> #{b_id} {html.escape(short(summary_raw, 140))}\n"
                     f"{web_url}\n"
                     f"{html.escape(status)} · {html.escape(creator)}"),
        })
    return True


def check_git_commits(conn, config, is_baseline, messages):
    """drivers/hwmon/coretemp.c commit'leri.

    Tercih: git.kernel.org Atom (kimlik dogrulama/rate limit yok).
    Basarisizsa GitHub API'ye duser; token SADECE istek basliginda kullanilir,
    hicbir yere yazilmaz.
    """
    atom_url = resolve_setting(config, "git_atom_url")
    items = None
    body = http_get(atom_url, timeout=30)
    if body is not None:
        items = parse_atom(body)

    if not items:
        api_url = resolve_setting(config, "git_api_url")
        token = os.environ.get("GITHUB_TOKEN") or None
        data = fetch_json(api_url, auth_token=token)
        if not data:
            return False
        items = []
        for c in data:
            commit = c.get("commit") or {}
            items.append({
                "id": c.get("sha") or "",
                "title": " ".join((commit.get("message") or "").splitlines()[:1]),
                "link": c.get("html_url") or "",
                "updated": ((commit.get("committer") or {}).get("date") or ""),
                "author": ((commit.get("author") or {}).get("name") or ""),
                "summary": " ".join((commit.get("message") or "").splitlines()[1:]).strip(),
            })

    for item in items:
        # Atom id'si "urn:sha1:<sha>", link ise ".../commit/?id=<sha>" biciminde;
        # DB'de kisa ve kararli kimlik olarak duz sha tutulur.
        sha = item["id"]
        if sha.startswith("urn:sha1:"):
            sha = sha[len("urn:sha1:"):]
        if "id=" in item["link"]:
            sha = item["link"].split("id=")[-1] or sha
        if not sha or is_seen(conn, sha, "git"):
            continue
        title_raw = item["title"]
        # Yol filtresi zaten coretemp.c'ye dokunan commit'leri getirir; baslik
        # coretemp gecmese de (x86/msr, merge tag) dosyaya dokundugu icin tutulur.
        mark_seen(conn, sha, "git", title_raw, None)

        messages.append({
            "source": "git",
            "id": sha,
            "date": item["updated"],
            "text": (f"💾 <b>[git]</b> {html.escape(title_raw)}\n"
                     f"{item['link']}\n"
                     f"{item['updated'][:10]} · {html.escape(item['author'] or '?')}\n"
                     f"<code>{sha[:12]}</code>"),
        })
    return True


def main():
    config = apply_env_overrides(load_config())
    dry_run = config.get("dry_run", False)

    token = config.get("telegram_token") or ""
    chat_id = config.get("chat_id") or ""
    if not dry_run and (not token or not chat_id):
        sys.exit("HATA: telegram_token/chat_id bulunamadi "
                 "(ne TELEGRAM_TOKEN+CHAT_ID ortam degiskeni ne de config dosyasi).")

    db_path = resolve_db_path(config)
    conn = init_db(db_path, dry_run=dry_run)

    total_seen_before = count_seen(conn)
    is_baseline = (total_seen_before == 0)

    if is_baseline:
        print("[*] Ilk calistirma: Mevcut kayitlar taban cizgisi (baseline) olarak kaydediliyor...")

    sources = [
        ("Patchwork (linux-hwmon)", check_patchwork),
        ("lore.kernel.org (linux-hwmon, son 30 gün)", check_lore),
        ("Kernel Bugzilla (summary=coretemp)", check_bugzilla),
        ("git.kernel.org (drivers/hwmon/coretemp.c)", check_git_commits),
    ]

    messages = []
    status = {}
    for label, func in sources:
        # Tek kaynak cokse bile digerleri devam eder; hata ilgili fonksiyonda
        # stderr'e yazilir, workflow'u dusurmez.
        try:
            status[label] = bool(func(conn, config, is_baseline, messages))
        except Exception as e:
            print(f"[hata] {label}: {e}", file=sys.stderr)
            status[label] = False

    sent = dispatch(conn, config, messages, is_baseline)

    if not any(status.values()):
        print("[!] Tum kaynaklar basarisiz oldu.", file=sys.stderr)
        sys.exit(1)

    if is_baseline:
        total_now = count_seen(conn)
        lines = "\n".join(
            f"{'✅' if ok else '⚠️'} <b>{label}:</b> {'Bağlandı' if ok else 'Ulaşılamadı'}"
            for label, ok in status.items()
        )
        baseline_msg = (
            f"🎯 <b>Coretemp Sentinel Radar Sistemi Aktif!</b>\n\n"
            f"{lines}\n"
            f"📊 <b>Başlangıç Kaydı:</b> {total_now} geçmiş kayıt hafızaya alındı.\n\n"
            f"🛡️ <i>Şu andan itibaren coretemp ile ilgili yama, mail, hata ve commit "
            f"düştüğünde buraya bildirim alacaksınız.</i>"
        )
        if dry_run:
            print(f"\n--- DRY_RUN baseline ---\n{baseline_msg}")
        else:
            send_telegram(token, chat_id, baseline_msg)
        print(f"[+] Baseline tamamlandi: {total_now} kayit islendi, hos geldin mesaji gonderildi.")
    else:
        print(f"[+] Kontrol tamamlandi. {sent} bildirim gonderildi. "
              f"Toplam bilinen kayit: {count_seen(conn)}")


if __name__ == "__main__":
    main()
