#!/usr/bin/env python3
"""
Coretemp Sentinel - Linux kernel coretemp surucu radarı.

coretemp ve nct6775 ile ilgili kaynaklari izler:
  - Patchwork (linux-hwmon; q=coretemp, q=nct6775, submitter=kendi e-postan)
    -> yeni yama + durum (state) degisimi
  - lore.kernel.org (linux-hwmon; coretemp, nct6775) -> mail listesi
  - Kernel Bugzilla (summary=coretemp, summary=nct6775)
  - git.kernel.org commit'leri: mainline (torvalds) + hwmon-next (groeck)
    coretemp.c, nct6775-core.c, nct6775-platform.c

Tek bagimlilik Python 3 standart kutuphanesidir.
DRY_RUN=1 ile telegram atmadan, gecici (bellek ici) DB ile sadece parse eder.
"""

import atexit
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
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
# Patchwork en ESKI yamalari dondurur; bu yuzden her zaman order=-date verilir.
# *_url alanlari taban adrestir, sorgu parcalari (q=..., submitter=...) sonradan eklenir.
DEFAULTS = {
    "patchwork_url": ("https://patchwork.kernel.org/api/1.2/patches/"
                      "?project=linux-hwmon&order=-date&per_page=50"),
    "patchwork_queries": ["q=coretemp", "q=nct6775", "submitter=ycistak@gmail.com"],
    "lore_url": "https://lore.kernel.org/linux-hwmon/?x=A",
    "lore_terms": ["coretemp", "nct6775"],
    "bugzilla_url": "https://bugzilla.kernel.org/rest/bug?limit=20&order=changeddate%20desc",
    "bugzilla_terms": ["coretemp", "nct6775"],
    "git_files": ["drivers/hwmon/coretemp.c",
                  "drivers/hwmon/nct6775-core.c",
                  "drivers/hwmon/nct6775-platform.c"],
    "git_trees": [
        {"tree": "mainline", "label": "mainline",
         "atom": ("https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/"
                  "linux.git/atom/{path}"),
         "api": ("https://api.github.com/repos/torvalds/linux/commits"
                 "?path={path}&per_page=15")},
        {"tree": "hwmon-next", "label": "hwmon-next — test zamanı",
         "atom": ("https://git.kernel.org/pub/scm/linux/kernel/git/groeck/"
                  "linux-staging.git/atom/{path}?h=hwmon-next"),
         "api": None},
    ],
    "star_words": ["bertizlio"],
    "alert_after": 6,
    "lore_days": 30,
    "max_messages": 10,
}

# Kaynak anahtari -> ortam degiskeni adi
ENV_KEYS = {
    "patchwork_url": "SENTINEL_PATCHWORK_URL",
    "lore_url": "SENTINEL_LORE_URL",
    "bugzilla_url": "SENTINEL_BUGZILLA_URL",
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

_DRY_TMP_DB = None


def init_db(db_path, dry_run=False):
    """DB'yi acar. dry_run'da gercek DB'nin kopyasi kullanilir.

    Kopya sayesinde kuru kosu gercek state'i bozmaz ama gercek kayitlarla
    karsilastirma yapar (bellek ici bos DB her kosuyu 'baseline' yapardi).
    """
    global _DRY_TMP_DB
    if dry_run:
        source = os.environ.get("SENTINEL_DRY_DB") or db_path
        if source and os.path.exists(source) and os.path.getsize(source) > 0:
            handle = tempfile.NamedTemporaryFile(
                prefix="coretemp-sentinel-dry-", suffix=".db", delete=False)
            handle.close()
            shutil.copy2(source, handle.name)
            _DRY_TMP_DB = handle.name
            atexit.register(_cleanup_dry_db, handle.name)
            print(f"[i] DRY_RUN: gercek DB kopyasi kullaniliyor ({handle.name})")
            conn = sqlite3.connect(handle.name)
        else:
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
    # Kaynak-basi taban cizgisi, saglik sayaclari ve tek seferlik bayraklar.
    cur.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    # Eski DB'yi bozmadan state kolonu ekle (migration).
    cur.execute("PRAGMA table_info(seen)")
    columns = {row[1] for row in cur.fetchall()}
    if "state" not in columns:
        cur.execute("ALTER TABLE seen ADD COLUMN state TEXT")
    conn.commit()
    return conn


def _cleanup_dry_db(path):
    try:
        os.unlink(path)
    except OSError:
        pass


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


def meta_get(conn, key, default=None):
    cur = conn.cursor()
    cur.execute("SELECT v FROM meta WHERE k = ?", (key,))
    row = cur.fetchone()
    return default if row is None else row[0]


def meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, str(value)))
    conn.commit()


def meta_del(conn, key):
    conn.execute("DELETE FROM meta WHERE k = ?", (key,))
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
            summary = "\n".join(l.strip() for l in "".join(content_el.itertext()).splitlines() if l.strip())
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
# Turkce derinlemesine aciklama ve analiz motoru
# --------------------------------------------------------------------------

MAX_MESSAGE_LEN = 4096   # Telegram tek mesaj siniri



# --------------------------------------------------------------------------
# Geriye dönük uyumluluk ve sınıflandırma yardımcıları
# --------------------------------------------------------------------------

EXPLANATIONS = {
    "lore_patch_new": (
        "Bu, listeye yeni gelen bir yama önerisi. Kabul edilmesi için önce "
        "incelenip test edilmesi gerekiyor; diff'i okuyup kendi makinende deneyebilirsin."
    ),
    "lore_patch_thread": (
        "Yeni bir yama önerisi geldi ve altına cevaplar yazıldı. Yamanın kabul "
        "durumunu anlamak için thread'in son mesajlarını ve maintainer yorumlarını oku."
    ),
    "lore_applied": (
        "Yama kabul edildi ve hwmon-next kuyruğuna alındı. Bir sonraki merge "
        "window'unda mainline çekirdeğe girer, acil bir şey yapman gerekmiyor."
    ),
    "lore_review_tag": (
        "Yamaya başka bir geliştirici destek veya test onayı verdi. Onay sayısı "
        "arttıkça maintainer'ın yamayı alma olasılığı yükselir."
    ),
    "lore_changes": (
        "Yama bu haliyle kabul edilmedi, düzeltme istendi. Yeni bir sürüm (v2) "
        "gönderilmesi gerekiyor; yorumları okuyup yamayı güncelle."
    ),
    "lore_reply": (
        "Bu, mevcut bir yama tartışmasına gelen cevap. Argümanı takip etmek için "
        "thread'i bütün olarak okumak gerekiyor."
    ),
    "patchwork_new": (
        "Patchwork'e yeni bir yama düştü, henüz incelenmemiş durumda. İlgini "
        "çekiyorsa yorum yazabilir veya test edip Tested-by verebilirsin."
    ),
    "patchwork_accepted": (
        "Yama kabul edildi, hwmon-next'e girecek. Senin yaman ise artık mainline "
        "yolunda demektir."
    ),
    "patchwork_rejected": (
        "Yama reddedildi, bu haliyle çekirdeğe girmeyecek. Sebebini yorumlardan "
        "öğrenip yeni sürüm hazırlayabilirsin."
    ),
    "patchwork_changes": (
        "Maintainer yamada değişiklik istedi. Belirtilen noktaları düzeltip v2 "
        "göndermen gerekiyor."
    ),
    "patchwork_superseded": (
        "Bu yama daha yeni bir sürüm tarafından geçersiz kılındı. Son sürümü "
        "takip et, eski olanla uğraşma."
    ),
    "patchwork_other": (
        "Yamanın Patchwork durumu \"{detail}\" olarak değişti; bu kabul ya da ret "
        "anlamına gelmiyor. Yama sayfasındaki yorumlara bakıp sürecin nereye "
        "gittiğini takip et."
    ),
    "git_commit": (
        "coretemp sürücüsüne bir değişiklik merge edildi, yani artık mainline "
        "çekirdekte. Değişikliği okuyup senin yamanla çakışıp çakışmadığını kontrol et."
    ),
    "bugzilla_open": (
        "coretemp ile ilgili bir hata kaydı açıldı ya da güncellendi. Kullanıcı "
        "şikayeti sürücü hatasına işaret ediyorsa aynı durumu kendi makinende "
        "yeniden üretmeyi denemek işe yarar."
    ),
    "bugzilla_resolved": (
        "Hata kaydı kapatıldı, sorun çözülmüş sayılıyor. Çözümün doğru olup "
        "olmadığını yamaya bakarak teyit edebilirsin."
    ),
    "default": (
        "Bu, coretemp ile ilgili bir gelişme. Ayrıntıyı görmek için bağlantıyı "
        "açıp değişikliğin senin yamanı etkileyip etkilemediğine bak."
    ),
}

_RE_APPLIED = re.compile(r"\bapplied\.", re.I)
_RE_REVIEW = re.compile(r"(?mi)^\s*(reviewed-by|acked-by|tested-by)\s*:")
_RE_CHANGES = re.compile(
    r"\b(?:nack|nak)\b|please fix|changes requested|v2 requested|"
    r"please (?:re)?(?:send|submit|post) (?:a )?v2",
    re.I,
)
_RE_PATCH_SUBJECT = re.compile(r"^\[\s*(?:RFC\s*)?PATCH", re.I)
_RE_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd)\s*:\s*)+", re.I)
_RE_BRACKET_TAG = re.compile(r"\[[^\]]*\]")
_RE_SENTENCE = re.compile(r"[^.!?]+[.!?]")

def classify_lore(title, body):
    title = title or ""
    body = body or ""
    if _RE_APPLIED.search(body):
        return "lore_applied"
    if _RE_REVIEW.search(body):
        return "lore_review_tag"
    if _RE_CHANGES.search(body):
        return "lore_changes"
    if _RE_PATCH_SUBJECT.match(title.strip()):
        return "lore_patch_new"
    return "lore_reply"

def classify_patchwork(state, is_new):
    if is_new:
        return "patchwork_new"
    state = (state or "").lower()
    mapping = {
        "accepted": "patchwork_accepted",
        "rejected": "patchwork_rejected",
        "changes-requested": "patchwork_changes",
        "superseded": "patchwork_superseded",
    }
    return mapping.get(state, "patchwork_other")

def classify_bugzilla(status):
    return ("bugzilla_resolved"
            if (status or "").upper() in ("RESOLVED", "CLOSED", "VERIFIED")
            else "bugzilla_open")

def explain(kind, detail=""):
    template = EXPLANATIONS.get(kind, EXPLANATIONS["default"])
    return template.format(detail=detail)

_RE_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd)\s*:\s*)+", re.I)

def with_explanation(lines, kind, detail=""):
    """Govde satirlarinin sonuna bos satir + aciklama ekler."""
    body = "\n".join(lines).rstrip()
    if kind == "git_next":
        aciklama = "🧪 <b>HWMON-NEXT:</b> Commit Guenter Roeck'un hwmon-next ağacına girdi, mainline'dan önce test edebilirsin. Bu ağaç yeniden yazılabilir (rebase); mainline'a geçince ayrıca haber vereceğim."
    elif kind == "git_commit":
        aciklama = "💾 <b>GIT COMMIT:</b> coretemp/nct6775 sürücüsüne mainline çekirdekte yeni bir commit merge edildi. Değişikliğin senin yamanla veya sürücü mantığıyla ilişkisini inceleyebilirsin."
    else:
        aciklama = f"ℹ️ <b>DURUM:</b> {kind}"
    return f"{body}\n\n{aciklama}"

def analyze_lore_group(group):
    """
    Kuru 2 cumlelik sablonlar yerine; kimin ne dedigini, Guenter'in fircasini,
    kabulleri ve Asaf'in maintainerlik hedefine etkisini ayrintili aciklar.
    """
    title = group.get("title", "")
    authors = group.get("authors", [])
    items = group.get("items", [])
    
    # Tum gruptaki metinleri birlestir ve en yeni mesaji bul
    full_text = " ".join(item.get("summary", "") for item in items)
    newest = max(items, key=lambda i: i.get("updated") or "") if items else {}
    newest_author = newest.get("author", "")
    newest_summary = newest.get("summary", "")
    
    # Alinti olmayan satirlari ayikla
    reply_lines = []
    for line in newest_summary.splitlines():
        line_s = line.strip()
        if not line_s or line_s.startswith(">") or line_s.startswith("&gt;"):
            continue
        if re.match(r"^On\s+.*wrote:", line_s, re.I):
            continue
        if line_s in ("--", "---", "-- "):
            break
        reply_lines.append(line_s)
    clean_reply = " ".join(reply_lines)
    
    # Onemli kisiler
    has_guenter = any("guenter" in a.lower() or "roeck" in a.lower() for a in authors)
    has_ximing = any("ximing" in a.lower() or "zhang" in a.lower() for a in authors)
    has_asaf = any("asaf" in a.lower() or "ycistak" in a.lower() for a in authors)
    
    parts = []
    
    # 1. FIRCA / SERT RET / AZAR DURUMU
    if re.search(r"please refrain|overloaded with patches|result in me getting even further behind|not getting fixed", full_text, re.I):
        parts.append(
            "⚠️ <b>MAINTAINER FIRÇASI / SERT RET!</b>\n"
            "Linux hwmon lideri <b>Guenter Roeck</b> bu yamayı kesin bir dille reddetti ve Ximing Zhang'ı çok sert azarladı:\n"
            "<i>'Zaten gerçek dünyadaki gerçek sorunları çözen yamalarla boğulmuş durumdayım. "
            "Lütfen bir daha masa başında uydurulmuş benzer teorik yamalar gönderip durma, vaktimi alıyorsun!'</i>\n\n"
            "📌 <b>Maintainer'lık Hedefimiz İçin Anlamı:</b> Rakibiniz Ximing teorik kod spamlamaktan dolayı maintainer nezdinde çok ciddi eksi puan aldı. "
            "Guenter'ın sadece 'gerçek donanımda kanıtlanmış sorunları' istediğini net olarak görmüş olduk."
        )
    # 2. GERI VITES / DISREGARD DURUMU
    elif re.search(r"please disregard|it is theoretical|not observed the failure|withdrawing", full_text, re.I):
        parts.append(
            "🔄 <b>YAMA GERİ ÇEKİLDİ (Disregard):</b>\n"
            "Geliştirici (Ximing Zhang), hatayı gerçek bir sistemde görmediğini, yalnızca yapay hata enjeksiyonuyla üretilmiş "
            "teorik bir kurgu olduğunu itiraf etti ve Guenter'dan yamasının çöpe atılmasını rica etti."
        )
    # 3. KABUL (Applied) DURUMU
    elif re.search(r"\bapplied\.", full_text, re.I):
        if has_asaf and "document sysfs numbering" in title.lower():
            parts.append(
                "🎉 <b>MÜJDE: SENİN YAMAN RESMEN KABUL EDİLDİ (Applied)!</b>\n"
                "Linux donanım izleme lideri <b>Guenter Roeck</b> senin yamanı tek kelime itiraz etmeden kabul etti ('Applied'). "
                "Yaman resmi <code>hwmon-next</code> dalına alındı ve ana Linux çekirdeğine girecek! İlk çekirdek başarın resmileşti."
            )
        elif has_asaf and has_ximing:
            parts.append(
                "🎉 <b>TEST ETTİĞİN YAMA KABUL EDİLDİ:</b>\n"
                "Ximing'in bu yaması, senin verdiğin <code>Tested-by: Asaf Nuri Bertizlioğlu</code> onayı sayesinde "
                "Guenter Roeck tarafından kabul edildi. Adın çekirdek git geçmişinde resmi testçi olarak yer aldı!"
            )
        else:
            parts.append(
                "🎉 <b>YAMA KABUL EDİLDİ (Applied):</b>\n"
                "Maintainer Guenter Roeck bu yamayı resmi olarak kabul etti ve çekirdek kuyruğuna (hwmon-next) ekledi."
            )
    # 4. SORGULAMA DURUMU (Teorik mi gercek mi?)
    elif re.search(r"is that a theoretic or a real problem|have you observed it on a real system", full_text, re.I):
        parts.append(
            "❓ <b>MAINTAINER SORGULAMASI:</b>\n"
            "Guenter Roeck bu yamanın gerçek bir donanımda test edilip edilmediğini sordu. "
            "Geliştiricinin yapay hata enjeksiyonu yaptığından şüphelenerek hesap sordu."
        )
    # 5. TEST / ONAY DURUMU
    elif re.search(r"\b(reviewed-by|tested-by):", full_text, re.I):
        parts.append(
            "✅ <b>İNCELEME VE TEST ONAYI:</b>\n"
            "Geliştiriciler yamayı test edip onay etiketlerini (Tested-by / Reviewed-by) ekledi. "
            "Bu durum yamanın güvenilirliğini artırır."
        )
    # 6. YENI YAMA
    elif re.match(r"^\[\s*(?:RFC\s*)?PATCH", title.strip(), re.I):
        if has_ximing:
            parts.append(
                "📝 <b>RAKİP GELİŞTİRİCİ YAMASI (Ximing Zhang):</b>\n"
                "Ximing Zhang coretemp için yeni bir yama önerisi gönderdi. "
                "Kendi i7-14700F donanımımızda inceleyip test edebilir veya eksik bulursak yorum yazabiliriz."
            )
        elif has_asaf:
            parts.append("📝 <b>SENİN GÖNDERDİĞİN YAMA:</b> Listeye sunduğun resmi çekirdek yama önerisi.")
        else:
            parts.append(f"📝 <b>YENİ YAMA ÖNERİSİ:</b> {newest_author} tarafından listeye yeni bir coretemp yaması sunuldu.")
    else:
        parts.append(f"💬 <b>TARTIŞMA / YORUM:</b> {newest_author} yama tartışmasına teknik yanıt verdi.")
        
    # Yazarın son cevabından somut alıntı
    if clean_reply:
        quote = clean_reply
        if len(quote) > 300:
            quote = quote[:297] + "..."
        parts.append(f"🗣️ <b>{newest_author} ne yazdı:</b>\n<i>\"{html.escape(quote)}\"</i>")
        
    return "\n\n".join(parts)


def explain_patchwork(state, is_new, patch_name=""):
    """Patchwork durumunu maintainer hedefine gore Turkce aciklar."""
    state = (state or "").lower()
    if is_new:
        return (
            "⏳ <b>PATCHWORK DURUMU: YENİ (New)</b>\n"
            "Yama Patchwork sistemine yeni düştü, henüz maintainer tarafından karara bağlanmadı. "
            "İnceleme ve test için sırada bekliyor."
        )
    if state == "accepted":
        return (
            "🎉 <b>PATCHWORK DURUMU: KABUL EDİLDİ (Accepted)!</b>\n"
            "Maintainer yamayı onayladı ve çekirdeğe dahil etti. Senin yaman ise artık resmen Linux çekirdeğinin parçası demektir!"
        )
    if state == "rejected":
        return (
            "❌ <b>PATCHWORK DURUMU: REDDEDİLDİ (Rejected)</b>\n"
            "Yama maintainer tarafından reddedildi, bu haliyle çekirdeğe girmeyecek."
        )
    if state == "superseded":
        return (
            "🔄 <b>PATCHWORK DURUMU: ESKİ SÜRÜM (Superseded)</b>\n"
            "Bu yama daha yeni bir sürüm (v2/v3) geldiği için geçersiz kaldı, arşivlendi."
        )
    return f"ℹ️ <b>PATCHWORK DURUMU: {state.upper()}</b>\nYama inceleme sürecinde durum güncellendi."


def explain_bugzilla(status, summary=""):
    status_up = (status or "").upper()
    if status_up in ("RESOLVED", "CLOSED", "VERIFIED"):
        return (
            "✅ <b>BUGZILLA DURUMU: ÇÖZÜLDÜ (Closed)</b>\n"
            "Bildirilen donanım hatası kapatıldı, sorun çözülmüş sayılıyor."
        )
    return (
        "🐛 <b>KERNEL BUGZILLA HATASI:</b>\n"
        "Kullanıcılar tarafından çekirdekte gerçek bir donanım/sıcaklık sorunu bildirildi. "
        "Bu hatayı kendi i7-14700F makinemizde inceleyip ilk yamayı sunmak maintainer yolunda en büyük kozdur!"
    )


def clamp_message(text, limit=MAX_MESSAGE_LEN):
    """Telegram'in 4096 karakter sinirini asmadan keser."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def normalize_subject(title):
    """Thread gruplamasi icin konu basligini sadelestirir.

    'Re: [PATCH v3 2/4] hwmon: ...' -> 'hwmon: ...' (kucuk harf).
    """
    text = _RE_REPLY_PREFIX.sub("", title or "")
    text = _RE_BRACKET_TAG.sub(" ", text)
    text = " ".join(text.split()).strip().lower()
    return text or "(konusuz)"


def display_subject(title):
    """Gorunen baslik: yalnizca Re:/Fw: onekleri temizlenir, [PATCH v2] kalir."""
    return _RE_REPLY_PREFIX.sub("", " ".join((title or "").split())).strip() or "(konusuz)"


# --------------------------------------------------------------------------
# Bildirim kuyrugu
# --------------------------------------------------------------------------

def _telegram_post(token, chat_id, text, html_mode=True):
    """Tek istek. (ok, hata_kodu, retry_after) doner; token asla yazdirilmaz."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if html_mode:
        payload["parse_mode"] = "HTML"
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return bool(body.get("ok")), None, 0
    except urllib.error.HTTPError as e:
        retry_after = 0
        try:
            err = json.loads(e.read().decode("utf-8"))
            retry_after = int((err.get("parameters") or {}).get("retry_after") or 0)
        except Exception:
            pass
        print(f"Telegram send error: HTTP {e.code}", file=sys.stderr)
        return False, e.code, retry_after
    except Exception as e:
        # URLError metni adres icerebilir; yalnizca tur adi yazilir.
        print(f"Telegram send error: {type(e).__name__}", file=sys.stderr)
        return False, 0, 0


def send_telegram(token, chat_id, text):
    """Mesaji yollar; basariliysa True. 429'da bir kez bekleyip tekrar dener,
    HTML ayristirma hatasinda (400) etiketsiz duz metinle tekrar dener."""
    ok, code, retry_after = _telegram_post(token, chat_id, text)
    if not ok and code == 429 and 0 < retry_after <= 30:
        time.sleep(retry_after + 1)
        ok, code, retry_after = _telegram_post(token, chat_id, text)
    if not ok and code == 400:
        plain = html.unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", text))
        ok, code, _ = _telegram_post(token, chat_id, plain, html_mode=False)
    return ok


def apply_marks(conn, msg):
    """Mesaja bagli seen kayitlarini DB'ye isler (gonderim basarili olunca)."""
    for item_id, source, title, state in msg.get("marks", []):
        mark_seen(conn, item_id, source, title, state)


def dispatch(conn, config, messages, is_baseline):
    """Bildirimleri gonderir; tek kosuda en fazla max_messages mesaj.

    messages: [{"source","id","date","text","marks"}] — en yeni once gonderilir,
    limiti asanlar icin tek satir "N kayit daha var" notu dusulur.
    Seen isareti ("marks") yalnizca mesaj BASARIYLA gidince islenir; gonderilemeyen
    kayit sonraki kosuda tekrar denenir. Limiti asan (filtrelenen) kayitlar ve
    baseline kosusu kayitlari ise hemen isaretlenir.
    """
    if is_baseline or not messages:
        for msg in messages:
            apply_marks(conn, msg)
        return 0

    max_messages = int(resolve_setting(config, "max_messages"))
    ordered = sorted(messages, key=lambda m: m.get("date") or "", reverse=True)
    to_send, dropped = ordered[:max_messages], ordered[max_messages:]

    sent = 0
    for msg in to_send:
        text = clamp_message(msg["text"])
        if config.get("dry_run"):
            print(f"\n--- DRY_RUN mesaj ({msg['source']}) [{len(text)} karakter] ---\n{text}")
            ok = True
        else:
            ok = send_telegram(config["telegram_token"], config["chat_id"], text)
        if ok:
            apply_marks(conn, msg)
            sent += 1
        else:
            print(f"[uyari] {msg['source']} mesaji gonderilemedi, sonraki kosuda tekrar denenecek.",
                  file=sys.stderr)

    for msg in dropped:
        apply_marks(conn, msg)
    if dropped:
        note = f"ℹ️ <b>{len(dropped)} kayıt daha var</b> (bir sonraki koşuda gönderilmeyecek, filtrelendi)."
        if config.get("dry_run"):
            print(f"\n--- DRY_RUN not ---\n{note}")
        else:
            send_telegram(config["telegram_token"], config["chat_id"], note)
    return sent


# --------------------------------------------------------------------------
# Kaynaklar
# --------------------------------------------------------------------------

# Saglik (kaynak ulasilabilirligi) ve bonus mesaj icin kosu-ici durum.
_HEALTH = {}        # anahtar -> son denemede ulasildi mi
MY_PATCHES = []     # kullanicinin kendi yamalari (submitter sorgusu)
STARRED = []        # ⭐ isaretli git commit'leri (tree, baslik, tarih)

STATE_TR = {
    "accepted": "kabul edildi", "new": "yeni", "rejected": "reddedildi",
    "changes-requested": "değişiklik istendi", "superseded": "eski sürüm",
    "under-review": "incelemede", "rfc": "RFC", "not-applicable": "uygulanamaz",
    "handled-elsewhere": "başka yerde ele alındı", "deferred": "ertelendi",
}


def note_health(key, ok):
    _HEALTH[key] = bool(ok)


def first_time(conn, key):
    """Bu sorgu/kaynak anahtari daha once hic basariyla islenmediyse True.

    Ilk islemede donen kayitlar mesajsiz (sessizce) gorulmus isaretlenir; boylece
    yeni eklenen bir sorgu eski kayitlari 'yeni' diye yagdirmaz.
    """
    return meta_get(conn, "init:" + key) is None


def finish_key(conn, key):
    meta_set(conn, "init:" + key, "1")


def check_patchwork(conn, config, is_baseline, messages):
    """Yeni yama + ayni yamanin state degisimi (coretemp, nct6775, kendi yamalarin).

    Sorgular order=-date ile en yeniden eskiye gelir (yoksa API en eski yamalari verir).
    """
    base = resolve_setting(config, "patchwork_url")
    queries = resolve_setting(config, "patchwork_queries")
    patches = {}        # id -> yama
    loud = set()        # taban cizgisi gecmis sorgulardan gelen id'ler
    ok_keys = []
    all_ok = True

    for q in queries:
        key = "patchwork:" + q
        data = fetch_json(base + ("&" if "?" in base else "?") + q)
        if not isinstance(data, list):
            note_health(key, False)
            all_ok = False
            continue
        note_health(key, True)
        ok_keys.append(key)
        silent = first_time(conn, key)
        for patch in data:
            p_id = str(patch.get("id"))
            patches[p_id] = patch
            if not silent:
                loud.add(p_id)
            if q.startswith("submitter="):
                MY_PATCHES.append(patch)

    for p_id, patch in patches.items():
        p_state = patch.get("state") or ""
        title = html.escape(patch.get("name") or "Bilinmeyen Yama")
        submitter = html.escape((patch.get("submitter") or {}).get("name") or "Bilinmiyor")
        web_url = patch.get("web_url") or f"https://patchwork.kernel.org/patch/{p_id}/"
        date_str = (patch.get("date") or "")[:10]

        exists, old_state = get_seen_state(conn, p_id, "patchwork")
        if not exists:
            if p_id not in loud:
                # Yeni sorgunun eski gecmisi: sessizce gorulmus say.
                mark_seen(conn, p_id, "patchwork", title, p_state)
                continue
            messages.append({
                "source": "patchwork",
                "id": p_id,
                "date": patch.get("date") or "",
                "marks": [(p_id, "patchwork", title, p_state)],
                "text": f"🆕 <b>[patchwork]</b> {title}\n{web_url}\n📅 {date_str} · 👤 {submitter}\n\n" + explain_patchwork(p_state, is_new=True, patch_name=title),
            })
        elif p_state and p_state != old_state:
            # state'i henuz izlenmeyen eski kayitlar (migration sonrasi NULL)
            # sessizce doldurulur; aksi halde ilk kosuda 30 sahte degisim mesaji gider.
            if not old_state:
                mark_seen(conn, p_id, "patchwork", title, p_state)
                continue
            messages.append({
                "source": "patchwork",
                "id": p_id,
                "date": patch.get("date") or "",
                "marks": [(p_id, "patchwork", title, p_state)],
                "text": f"🔄 <b>[patchwork]</b> {title}\n{web_url}\n📊 Durum Değişimi: {html.escape(old_state or "?")} → <b>{html.escape(p_state)}</b>\n\n" + explain_patchwork(p_state, is_new=False, patch_name=title),
            })

    for key in ok_keys:
        finish_key(conn, key)
    return all_ok


def build_lore_groups(items):
    """Yeni lore maillerini thread'e gore gruplar, en yeni once siralar.

    Her grup: key, title, link, authors, count, date, summary, kinds,
    newest_kind alanlarini tasir. Thread kimligi Atom beslemesinde olmadigi
    icin normalize edilmis konu basligi kullanilir.
    """
    groups = {}
    for item in items:
        key = normalize_subject(item["title"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"authors": [], "count": 0, "kinds": set(), "items": []}
        group["count"] += 1
        if item["author"] and item["author"] not in group["authors"]:
            group["authors"].append(item["author"])
        group["kinds"].add(classify_lore(item["title"], item["summary"]))
        group["items"].append(item)

    result = []
    for group in groups.values():
        # Grubun temsilcisi en yeni maildir.
        newest = max(group["items"], key=lambda i: i["updated"] or "")
        result.append({
            "key": normalize_subject(newest["title"]),
            "title": display_subject(newest["title"]),
            "link": newest["link"],
            "authors": group["authors"],
            "count": group["count"],
            "date": newest["updated"],
            "summary": newest["summary"],
            "kinds": group["kinds"],
            "newest_kind": classify_lore(newest["title"], newest["summary"]),
            "items": group["items"],
        })
    result.sort(key=lambda g: g["date"] or "", reverse=True)
    return result


def lore_group_kind(group):
    """Grup icin aciklama tipini sec: en yeni mailin karari onceliklidir."""
    newest = group["newest_kind"]
    if newest in ("lore_applied", "lore_review_tag", "lore_changes"):
        return newest
    if "lore_patch_new" in group["kinds"]:
        return "lore_patch_new" if group["count"] == 1 else "lore_patch_thread"
    return newest


def compose_lore_message(group):
    """Bir thread'in tum yeni maillerini tek ve derinlemesine Turkce bildirim metnine cevirir."""
    authors = ", ".join(group["authors"][:3])
    if len(group["authors"]) > 3:
        authors += f" +{len(group['authors']) - 3}"
    meta = f"{authors or '?'} · {group['date'][:10]}"
    if group["count"] > 1:
        meta += f" · {group['count']} yeni mail"
        
    explanation = analyze_lore_group(group)
    
    text = (
        f"📧 <b>[lore]</b> {html.escape(group['title'])}\n"
        f"{group['link']}\n"
        f"📅 {meta}\n\n"
        f"{explanation}"
    )
    return {
        "source": "lore",
        "id": group["key"],
        "date": group["date"],
        "text": clamp_message(text),
        "marks": [(i["id"], "lore", i["title"], None) for i in group["items"]],
    }


def check_lore(conn, config, is_baseline, messages):
    """linux-hwmon lore aramasinda coretemp/nct6775 gecen mailler (thread bazli).

    Feed sorgusu 200 kayit ve yillara yayilan gecmis dondurur; bu yuzden her
    zaman son lore_days gun ile sinirlanir, geriye donuk spam olmaz. Ayni thread'e
    ait yeni mailler tek bildirimde toplanir. Yeni eklenen bir arama terimi
    ilk kosuda sessizce taban cizgisi olur.
    """
    base = resolve_setting(config, "lore_url")
    days = int(resolve_setting(config, "lore_days"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    all_ok = True
    ok_keys = []
    new_items = []
    queued = set()

    for term in resolve_setting(config, "lore_terms"):
        key = "lore:" + term
        body = http_get(base + ("&" if "?" in base else "?") + "q=" + urllib.parse.quote(term), timeout=30)
        items = parse_atom(body) if body is not None else None
        if items is None:
            note_health(key, False)
            all_ok = False
            continue
        note_health(key, True)
        ok_keys.append(key)
        silent = first_time(conn, key)

        for item in items:
            title_raw = item["title"]
            summary_raw = item["summary"]
            # Besleme sorgusu zaten daraltiyor; yine de yerelde dogrula.
            if term not in (title_raw + " " + summary_raw).lower():
                continue
            date = parse_iso(item["updated"])
            if date is None or date < cutoff:
                continue
            m_id = item["id"] or item["link"]
            if m_id in queued or is_seen(conn, m_id, "lore"):
                continue
            queued.add(m_id)
            if silent:
                mark_seen(conn, m_id, "lore", title_raw, None)
                continue
            new_items.append({
                "id": m_id,
                "title": title_raw,
                "link": item["link"],
                "updated": item["updated"],
                "author": item["author"],
                "summary": summary_raw,
            })

    for group in build_lore_groups(new_items):
        messages.append(compose_lore_message(group))
    for key in ok_keys:
        finish_key(conn, key)
    return all_ok


def check_bugzilla(conn, config, is_baseline, messages):
    """Kernel Bugzilla: yalnizca ozetinde coretemp/nct6775 gecen kayitlar."""
    base = resolve_setting(config, "bugzilla_url")
    all_ok = True
    ok_keys = []
    queued = set()

    for term in resolve_setting(config, "bugzilla_terms"):
        key = "bugzilla:" + term
        data = fetch_json(base + ("&" if "?" in base else "?") + "summary=" + urllib.parse.quote(term))
        if data is None or "bugs" not in data:
            note_health(key, False)
            all_ok = False
            continue
        note_health(key, True)
        ok_keys.append(key)
        silent = first_time(conn, key)

        for bug in data.get("bugs", []):
            b_id = str(bug.get("id"))
            summary_raw = bug.get("summary") or ""
            if term not in summary_raw.lower():
                continue
            if b_id in queued or is_seen(conn, b_id, "bugzilla"):
                continue
            queued.add(b_id)
            status = bug.get("status") or ""
            if silent:
                mark_seen(conn, b_id, "bugzilla", summary_raw, status)
                continue
            creator = bug.get("creator") or "Bilinmiyor"
            web_url = f"https://bugzilla.kernel.org/show_bug.cgi?id={b_id}"
            messages.append({
                "source": "bugzilla",
                "id": b_id,
                "date": bug.get("last_change_time") or "",
                "marks": [(b_id, "bugzilla", summary_raw, status)],
                "text": clamp_message(f"🐛 <b>[bugzilla]</b> #{b_id} {html.escape(short(summary_raw, 140))}\n{web_url}\n📊 {html.escape(status)} · 👤 {html.escape(creator)}\n\n" + explain_bugzilla(status, summary_raw)),
            })
    for key in ok_keys:
        finish_key(conn, key)
    return all_ok


def git_commit_id(tree, author, title):
    """hwmon-next rebase edilebilir (sha degisir); kimlik agac + yazar + baslik."""
    return f"{tree}|{author}|{title}"


def fetch_git_items(tree_cfg, path):
    """Bir agac/dosya icin commit listesi. Atom, olmazsa (varsa) GitHub API."""
    atom_url = tree_cfg["atom"].format(path=path)
    body = http_get(atom_url, timeout=30)
    items = parse_atom(body) if body is not None else None
    # Anubis korumasi HTML dondururse ayristirma basarisiz olur -> None
    if items:
        return items
    api = tree_cfg.get("api")
    if not api:
        return items  # None (ulasilamadi) ya da bos ama gecerli liste
    # Token SADECE istek basliginda kullanilir, hicbir yere yazilmaz.
    data = fetch_json(api.format(path=path), auth_token=os.environ.get("GITHUB_TOKEN") or None)
    if not data:
        return None
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
    return items


def check_git_commits(conn, config, is_baseline, messages):
    """coretemp / nct6775 dosyalarinin commit'leri: mainline + hwmon-next.

    Her (agac, dosya) cifti kendi taban cizgisine sahiptir. Ayni commit mainline'a
    gecince ayri bir bildirim olarak gider.
    """
    star_words = [w.lower() for w in resolve_setting(config, "star_words")]
    all_ok = True
    ok_keys = []
    queued = set()

    for tree_cfg in resolve_setting(config, "git_trees"):
        tree = tree_cfg["tree"]
        for path in resolve_setting(config, "git_files"):
            key = f"git:{tree}:{path}"
            items = fetch_git_items(tree_cfg, path)
            if items is None:
                note_health(key, False)
                all_ok = False
                continue
            note_health(key, True)
            ok_keys.append(key)
            silent = first_time(conn, key)

            for item in items:
                # Atom id'si "urn:sha1:<sha>", link ise ".../commit/?id=<sha>" biciminde.
                sha = item["id"]
                if sha.startswith("urn:sha1:"):
                    sha = sha[len("urn:sha1:"):]
                if "id=" in item["link"]:
                    sha = item["link"].split("id=")[-1] or sha
                title_raw = item["title"]
                author = item["author"]
                haystack = f"{title_raw} {author} {item['summary']}".lower()
                starred = any(w in haystack for w in star_words)
                if starred:
                    STARRED.append((tree_cfg["label"], title_raw, item["updated"][:10]))

                cid = git_commit_id(tree, author, title_raw)
                if not sha or cid in queued or is_seen(conn, cid, "git"):
                    continue
                # Eski surumde mainline coretemp commit'leri duz sha ile tutuluyordu.
                if tree == "mainline" and is_seen(conn, sha, "git"):
                    continue
                queued.add(cid)
                if silent:
                    mark_seen(conn, cid, "git", title_raw, None)
                    continue

                lines = [
                    f"{'⭐ ' if starred else ''}💾 <b>[git · {html.escape(tree_cfg['label'])}]</b> {html.escape(title_raw)}",
                    item["link"],
                    f"{item['updated'][:10]} · {html.escape(author or '?')}",
                    f"📄 <code>{html.escape(path)}</code> · <code>{sha[:12]}</code>",
                ]
                if tree == "mainline" and is_seen(conn, git_commit_id("hwmon-next", author, title_raw), "git"):
                    lines.append("🚀 hwmon-next'ten mainline'a geçti.")
                kind = "git_next" if tree == "hwmon-next" else "git_commit"
                messages.append({
                    "source": "git",
                    "id": cid,
                    "date": item["updated"],
                    "marks": [(cid, "git", title_raw, None)],
                    "text": clamp_message(with_explanation(lines, kind)),
                })
    for key in ok_keys:
        finish_key(conn, key)
    return all_ok


def notify(config, token, chat_id, text):
    """Tek mesaj yollar (dry_run'da yazdirir); basariliysa True."""
    if config.get("dry_run"):
        print(f"\n--- DRY_RUN mesaj ---\n{text}")
        return True
    return send_telegram(token, chat_id, clamp_message(text))


def update_health(conn, config, token, chat_id):
    """Art arda alert_after+ basarisiz kaynak icin bir kez uyar, duzelince bir kez haber ver."""
    limit = int(resolve_setting(config, "alert_after"))
    for key, ok in _HEALTH.items():
        fail_key, alert_key = "fail:" + key, "alerted:" + key
        name = html.escape(key)
        if ok:
            if meta_get(conn, alert_key):
                if notify(config, token, chat_id, f"✅ <b>{name}</b> tekrar bağlandı."):
                    meta_del(conn, alert_key)
                    meta_del(conn, fail_key)
            elif meta_get(conn, fail_key) is not None:
                meta_del(conn, fail_key)
            continue
        count = int(meta_get(conn, fail_key, 0) or 0) + 1
        meta_set(conn, fail_key, count)
        if count >= limit and not meta_get(conn, alert_key):
            if notify(config, token, chat_id,
                      f"⚠️ <b>{name}</b> {count} koşudur ulaşılamıyor."):
                meta_set(conn, alert_key, 1)


def build_update_message(config):
    """Tek seferlik 'Sentinel guncellendi' mesaji: kaynaklar + kendi yamalarin."""
    lines = ["🛠️ <b>Sentinel güncellendi</b>", "", "<b>İzlenen kaynaklar:</b>"]
    lines.append("• Patchwork: " + ", ".join(
        html.escape(q) for q in resolve_setting(config, "patchwork_queries")))
    lines.append("• lore: " + ", ".join(resolve_setting(config, "lore_terms")))
    lines.append("• Bugzilla: " + ", ".join(resolve_setting(config, "bugzilla_terms")))
    trees = ", ".join(t["label"] for t in resolve_setting(config, "git_trees"))
    files = ", ".join(os.path.basename(f) for f in resolve_setting(config, "git_files"))
    lines.append(f"• git: {html.escape(trees)} ({html.escape(files)})")
    if MY_PATCHES:
        lines += ["", "<b>Senin yamaların:</b>"]
        for patch in sorted(MY_PATCHES, key=lambda x: x.get("date") or "", reverse=True):
            st = patch.get("state") or "?"
            lines.append(f"• {html.escape(patch.get('name') or '?')} — "
                         f"{html.escape(STATE_TR.get(st, st))}")
    if STARRED:
        lines += ["", "<b>⭐ Adının geçtiği commit'ler:</b>"]
        seen_star = set()
        for label, title, date in STARRED:
            if (label, title) in seen_star:
                continue
            seen_star.add((label, title))
            lines.append(f"• {html.escape(title)} — {html.escape(label)} ({date})")
    lines += ["", "<i>Gönderilemeyen bildirim sonraki koşuda tekrar denenir; bir kaynak "
              "6 koşu ulaşılamazsa uyarı gelir.</i>"]
    return "\n".join(lines)


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
        ("Kernel Bugzilla (coretemp, nct6775)", check_bugzilla),
        ("git.kernel.org (mainline + hwmon-next)", check_git_commits),
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
            note_health(label, False)

    sent = dispatch(conn, config, messages, is_baseline)

    # Sessiz ariza alarmi (basarisizlik sayaci DB'de)
    update_health(conn, config, token, chat_id)

    for key, ok in sorted(_HEALTH.items()):
        print(f"[{'+' if ok else '!'}] {key}: {'ulasildi' if ok else 'ULASILAMADI'}")

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
            f"🛡️ <i>Şu andan itibaren coretemp ve nct6775 ile ilgili yama, mail, hata ve commit "
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
        # Tek seferlik 'guncellendi' mesaji: bayrak yalnizca basarili gonderimde yazilir.
        if meta_get(conn, "flag:update-msg-v1") is None and MY_PATCHES:
            if notify(config, token, chat_id, build_update_message(config)):
                meta_set(conn, "flag:update-msg-v1", "1")


if __name__ == "__main__":
    main()
