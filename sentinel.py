#!/usr/bin/env python3
"""
Coretemp Sentinel - Linux kernel coretemp surucu radarı.

coretemp ile ilgili kaynaklari izler:
  - Patchwork (linux-hwmon; q=coretemp, submitter=kendi e-postan)
    -> yeni yama + durum (state) degisimi
  - lore.kernel.org (linux-hwmon; coretemp) -> mail listesi
  - Kernel Bugzilla (summary=coretemp)
  - git.kernel.org commit'leri: mainline (torvalds) + hwmon-next + hwmon (groeck)
    coretemp.c

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
    "patchwork_queries": ["q=coretemp", "submitter=ycistak@gmail.com",
                          "submitter=asafbertizlioglu@gmail.com"],
    "lore_url": "https://lore.kernel.org/linux-hwmon/?x=A",
    "lore_terms": ["coretemp"],
    "bugzilla_url": "https://bugzilla.kernel.org/rest/bug?limit=20&order=changeddate%20desc",
    "bugzilla_terms": ["coretemp"],
    "git_files": ["drivers/hwmon/coretemp.c"],
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
        {"tree": "hwmon",
         "label": "⚡ hwmon (acil düzeltme dalı) — hemen test et, birkaç gün içinde Linus'a gider",
         "atom": ("https://git.kernel.org/pub/scm/linux/kernel/git/groeck/"
                  "linux-staging.git/atom/{path}?h=hwmon"),
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

# --------------------------------------------------------------------------
# Düz Türkçe mesaj üretimi: önce karar (renk), sonra ne oldu, sonra ne yapmalı
# --------------------------------------------------------------------------

# Sahibin e-posta adresleri (yamaları bu adreslerden gider).
MY_EMAILS = ["ycistak@gmail.com", "asafbertizlioglu@gmail.com"]
# Atom beslemesinde e-posta yok, yalnızca isim var; ismi tanımak için ipuçları.
MY_NAME_HINTS = ["bertizlio", "ycistak"]

VERDICTS = {
    "red": "🔴 Senin yaman hakkında — bakman lazım",
    "yellow": "🟡 coretemp.c'yi değiştiren bir yama — incele ve test et",
    "white": "⚪ Bilgi: coretemp sadece geçerken anılıyor",
}

_RE_COREMAIN_DIFF = re.compile(
    r"(?m)^(?:diff --git |\+\+\+ |--- ).*drivers/hwmon/coretemp\.c")
_RE_VERSION = re.compile(r"\[[^\]]*?\bv(\d+)\b[^\]]*\]", re.I)
_RE_NO_ISSUES = re.compile(r"no (?:issues|problems)|did not find any|found no", re.I)
_RE_TAGS = re.compile(r"(?mi)^\s*(reviewed-by|acked-by|tested-by)\s*:")
_RE_WITHDRAW = re.compile(r"please disregard|withdraw", re.I)
_RE_QUESTION = re.compile(
    r"is that a theoretic or a real problem|have you observed it on a real system", re.I)


TODO_REVIEW = ("İncele ve test et: Claude'a yamayı Türkçe açıklat, canli-test.sh ile dene; "
               "anladıysan Reviewed-by + Tested-by cevabı hazırlat. Maintainer başvurusu "
               "için en değerli iş bu.")
TODO_GLANCE = "Göz at: coretemp'i etkiliyorsa yorum yapılabilir; etkilemiyorsa geç."
TODO_BUG = ("Senin işlemcinde (i7-14700F) yeniden üretilebilir mi bak; üretilirse yama "
            "fırsatı.")


def is_mine(*texts):
    """Metinlerde sahibin e-postası ya da adı geçiyor mu?"""
    blob = " ".join(t for t in texts if t).lower()
    return any(e in blob for e in MY_EMAILS) or any(h in blob for h in MY_NAME_HINTS)


def touches_coretemp_diff(text):
    """Metindeki diff drivers/hwmon/coretemp.c dosyasına dokunuyor mu?"""
    return bool(_RE_COREMAIN_DIFF.search(text or ""))


def render_message(level, info_lines, what, todo, link, original=None, verdict=None):
    """Tek biçim: karar, bilgi satırları, ne oldu, ne yapmalı, (İngilizce özet), link.

    info_lines ve what içindeki metinler çağıran tarafından zaten kaçışlanmış (html.escape)
    olmalıdır; burada yalnızca sabit Türkçe etiketler eklenir.
    """
    parts = [f"<b>{html.escape(verdict or VERDICTS[level])}</b>"]
    if info_lines:
        parts.append("\n".join(info_lines))
    parts.append("💬 <b>Ne oldu?</b> " + " ".join(what[:3]))
    parts.append("👉 <b>Ne yapmalısın?</b> " + todo)
    if original:
        parts.append("Orijinal (İngilizce):\n<i>" + html.escape(short(original, 150)) + "</i>")
    if link:
        parts.append("🔗 " + link)
    return clamp_message("\n\n".join(parts))


def _english_excerpt(summary):
    """Son mesajın alıntı olmayan ilk satırlarından en çok 150 karakter."""
    lines = []
    for line in (summary or "").splitlines():
        s = line.strip()
        if not s or s.startswith((">", "&gt;", "diff ", "---", "+++", "@@")):
            continue
        if re.match(r"^On\s+.*wrote:", s, re.I):
            continue
        if s in ("--", "-- "):
            break
        lines.append(s)
    return short(" ".join(lines), 150)


def lore_relevance(group):
    """kırmızı: sahibin yaması/adı; sarı: başlıkta coretemp ya da coretemp.c diff'i; beyaz: geçerken."""
    items = group.get("items", [])
    blob = [group.get("title", "")] + list(group.get("authors", []))
    blob += [i.get("title", "") + " " + i.get("summary", "") for i in items]
    if is_mine(*blob):
        return "red"
    if any("coretemp" in i.get("title", "").lower() for i in items) \
            or any(touches_coretemp_diff(i.get("summary", "")) for i in items):
        return "yellow"
    return "white"


def _lore_events(items):
    """Thread'deki olayları (ad, ayrıntı) olarak sırayla çıkarır."""
    events = []
    for item in sorted(items, key=lambda i: i.get("updated") or ""):
        body = item.get("summary", "")
        author = item.get("author", "") or "Biri"
        if _RE_APPLIED.search(body):
            events.append(("applied", author))
        if "sashiko" in author.lower() or re.search(r"\bbot\b", author.lower()):
            events.append(("bot_ok" if _RE_NO_ISSUES.search(body) else "bot", author))
        if _RE_TAGS.search(body):
            events.append(("review", author))
        if _RE_CHANGES.search(body):
            events.append(("changes", author))
        if _RE_WITHDRAW.search(body):
            events.append(("withdraw", author))
        if _RE_QUESTION.search(body):
            events.append(("question", author))
    seen, result = set(), []
    for ev in events:
        if ev[0] not in seen:
            seen.add(ev[0])
            result.append(ev)
    return result


def next_version(titles):
    """Başlıklardaki en yüksek [PATCH vN ...] sürümünden sonrakini verir (yoksa 2)."""
    best = 1
    for title in titles:
        for m in _RE_VERSION.finditer(title or ""):
            best = max(best, int(m.group(1)))
    return best + 1


def explain_lore_group(group, level):
    """(cümleler, yapılacak) — sade Türkçe, en çok 3 cümle."""
    items = group.get("items", [])
    ordered = sorted(items, key=lambda i: i.get("updated") or "")
    firsts = [i for i in ordered if not _RE_REPLY_PREFIX.match(i.get("title", ""))]
    esc = html.escape

    if firsts:
        first = firsts[0]
        plain = short(_RE_BRACKET_TAG.sub(" ", display_subject(first["title"])), 90)
        m = _RE_VERSION.search(first["title"])
        ver = ""
        if m and int(m.group(1)) >= 2:
            n = m.group(1)
            ver = f" (yamanın {n}. düzeltilmiş hali, v{n})"
        elif m:
            ver = f" (v{m.group(1)})"
        intro = (f"{esc(first.get('author') or 'Biri')} \"{esc(plain)}\" adlı yamayı "
                 f"gönderdi{ver}.")
    else:
        n = group.get("count", len(items))
        intro = f"Daha önce gönderilmiş bir yamanın altına {n} yeni cevap yazıldı."

    texts = {
        "applied": lambda a: (f"{esc(a)} yamayı kabul etti (Applied): yani yama çekirdeğe "
                              "girecek."),
        "bot_ok": lambda a: (f"Otomatik yapay zekâ incelemesi ({esc(a)}) yamada sorun "
                             "bulmadı."),
        "bot": lambda a: f"Otomatik yapay zekâ incelemesi ({esc(a)}) yamaya bir not bıraktı.",
        "review": lambda a: ("Biri yamayı inceledi/test etti ve onay verdi "
                             "(Reviewed-by/Tested-by)."),
        "changes": lambda a: "Yamada düzeltme istendi; yazar yeni bir sürüm göndermeli.",
        "withdraw": lambda a: "Yazar yamayı geri çekti (yok sayın dedi).",
        "question": lambda a: (f"{esc(a)}, sorunun gerçek bir donanımda görülüp "
                               "görülmediğini sordu."),
    }
    events = _lore_events(items)
    kinds = {e[0] for e in events}
    sentences = [texts[k](a) for k, a in events]
    # Kabul + temiz bot incelemesi tek cümlede toplanır (en çok 3 cümle sınırı için).
    ev = dict(events)
    if "applied" in ev and "bot_ok" in ev:
        combo = (f"{esc(ev['applied'])} yamayı kabul etti (Applied): yani yama çekirdeğe "
                 f"girecek; otomatik yapay zekâ incelemesi ({esc(ev['bot_ok'])}) de sorun "
                 "bulmadı.")
        sentences = [combo] + [texts[k](a) for k, a in events if k not in ("applied", "bot_ok")]

    reason = None
    if level == "white":
        reason = ("Bu yazışma yalnızca içinde \"coretemp\" kelimesi geçtiği için listeye "
                  "düştü; coretemp.c'ye dokunmuyor.")
    elif level == "red":
        reason = "Yazışmada senin adın ya da e-postan geçiyor."
    keep = 3 - 1 - (1 if reason else 0)
    sentences = [intro] + sentences[:keep] + ([reason] if reason else [])

    if level == "white":
        todo = TODO_GLANCE
    elif level == "red":
        if "changes" in kinds:
            nxt = next_version(i.get("title", "") for i in items)
            todo = f"Linke tıklayıp yorumları oku, yamanı düzeltip v{nxt} gönder."
        elif "applied" in kinds:
            todo = "Linke tıklayıp kabul mesajını oku; başka bir şey yapman gerekmiyor."
        else:
            todo = "Linke tıklayıp cevabı oku; soru varsa cevap yaz."
    else:
        todo = TODO_REVIEW
    return sentences, todo


def explain_patchwork_state(state, is_new):
    """Patchwork durumunu sade Türkçe cümle olarak verir: (cümle, yapılacak)."""
    state = (state or "").lower()
    if is_new:
        return ("Patchwork'e (yamaların takip sayfası) yeni bir yama düştü; henüz kimse "
                "karar vermedi.",
                "Linke tıklayıp yamaya göz at.")
    table = {
        "accepted": ("Yama kabul edildi: çekirdeğe girecek.", "Hiçbir şey; sevinebilirsin."),
        "rejected": ("Yama reddedildi: bu haliyle çekirdeğe girmeyecek.",
                     "Linke tıklayıp sebebini oku."),
        "changes-requested": ("Yamada değişiklik istendi.",
                              "Yorumları oku, yamayı düzeltip yeni sürüm gönder."),
        "superseded": ("Yamanın daha yeni bir sürümü geldi, bu eskisi geçersiz oldu.",
                       "Hiçbir şey; yeni sürümü takip et."),
    }
    if state in table:
        return table[state]
    return ("Yamanın durumu değişti ama kabul ya da ret anlamına gelmiyor.",
            "İstersen linke tıklayıp bak.")


def explain_bugzilla_status(status):
    status_up = (status or "").upper()
    if status_up in ("RESOLVED", "CLOSED", "VERIFIED"):
        return ("coretemp ile ilgili bir hata kaydı kapatıldı: sorun çözülmüş sayılıyor.",
                "Linke tıklayıp çözümün ne olduğuna bak.")
    return ("Kernel Bugzilla'da (çekirdek hata takip sayfası) coretemp ile ilgili bir "
            "hata kaydı açıldı ya da güncellendi.",
            "Linke tıklayıp hatanın coretemp.c ile ilgili olup olmadığına bak.")


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


def patchwork_relevance(patch):
    """kırmızı: sahibin yaması; sarı: adında coretemp ya da diff coretemp.c; beyaz: geçerken."""
    sub = patch.get("submitter") or {}
    if is_mine(sub.get("name"), sub.get("email"), patch.get("name")):
        return "red"
    if "coretemp" in (patch.get("name") or "").lower() \
            or touches_coretemp_diff(patch.get("diff") or ""):
        return "yellow"
    return "white"


def _patchwork_todo(level, todo):
    """Başkasının yamasında (sarı/beyaz) inceleme-test yönlendirmesi; kırmızıda eski metin."""
    if level == "yellow":
        return TODO_REVIEW
    if level == "white":
        return TODO_GLANCE
    return todo


def _patchwork_sentences(level, sentence):
    cümleler = [sentence]
    if level == "red":
        cümleler.append("Bu senin gönderdiğin yama.")
    elif level == "white":
        cümleler.append("Yama coretemp.c'ye dokunmuyor, yalnızca aramaya takıldı.")
    return cümleler


def check_patchwork(conn, config, is_baseline, messages):
    """Yeni yama + ayni yamanin state degisimi (coretemp, kendi yamalarin).

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
        raw_name = patch.get("name") or "Bilinmeyen Yama"
        title = html.escape(raw_name)
        submitter_raw = (patch.get("submitter") or {}).get("name") or "Bilinmiyor"
        submitter = html.escape(submitter_raw)
        web_url = patch.get("web_url") or f"https://patchwork.kernel.org/patch/{p_id}/"
        date_str = (patch.get("date") or "")[:10]
        level = patchwork_relevance(patch)

        exists, old_state = get_seen_state(conn, p_id, "patchwork")
        if not exists:
            if p_id not in loud:
                # Yeni sorgunun eski gecmisi: sessizce gorulmus say.
                mark_seen(conn, p_id, "patchwork", title, p_state)
                continue
            sentence, todo = explain_patchwork_state(p_state, is_new=True)
            messages.append({
                "source": "patchwork",
                "id": p_id,
                "date": patch.get("date") or "",
                "marks": [(p_id, "patchwork", title, p_state)],
                "text": render_message(
                    level, [f"🆕 <i>{title}</i>", f"({date_str} · {submitter})"],
                    _patchwork_sentences(level, sentence),
                    _patchwork_todo(level, todo), web_url),
            })
        elif p_state and p_state != old_state:
            # state'i henuz izlenmeyen eski kayitlar (migration sonrasi NULL)
            # sessizce doldurulur; aksi halde ilk kosuda 30 sahte degisim mesaji gider.
            if not old_state:
                mark_seen(conn, p_id, "patchwork", title, p_state)
                continue
            sentence, todo = explain_patchwork_state(p_state, is_new=False)
            sentence += (f" (durum: {html.escape(STATE_TR.get(old_state, old_state))} → "
                         f"{html.escape(STATE_TR.get(p_state, p_state))})")
            messages.append({
                "source": "patchwork",
                "id": p_id,
                "date": patch.get("date") or "",
                "marks": [(p_id, "patchwork", title, p_state)],
                "text": render_message(
                    level, [f"🔄 <i>{title}</i>", f"({date_str} · {submitter})"],
                    _patchwork_sentences(level, sentence),
                    _patchwork_todo(level, todo), web_url),
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
    """Bir thread'in tüm yeni maillerini tek, sade Türkçe bildirim metnine çevirir."""
    level = lore_relevance(group)
    sentences, todo = explain_lore_group(group, level)
    authors = ", ".join(group["authors"][:3])
    if len(group["authors"]) > 3:
        authors += f" +{len(group['authors']) - 3}"
    meta = f"{authors or '?'} · {group['date'][:10]}"
    if group["count"] > 1:
        meta += f" · {group['count']} yeni mail"
    info = [f"📧 <i>{html.escape(short(group['title'], 160))}</i>",
            f"({html.escape(meta)})"]
    newest = max(group["items"], key=lambda i: i.get("updated") or "")
    original = _english_excerpt(newest.get("summary", "")) if level != "white" else None
    text = render_message(level, ["\n".join(info)], sentences, todo, group["link"],
                          original=original)
    return {
        "source": "lore",
        "id": group["key"],
        "date": group["date"],
        "text": text,
        "marks": [(i["id"], "lore", i["title"], None) for i in group["items"]],
    }


def check_lore(conn, config, is_baseline, messages):
    """linux-hwmon lore aramasinda coretemp gecen mailler (thread bazli).

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
    """Kernel Bugzilla: yalnizca ozetinde coretemp gecen kayitlar."""
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
            level = "red" if is_mine(creator, bug.get("assigned_to"), summary_raw) else "yellow"
            sentence, todo = explain_bugzilla_status(status)
            if level == "yellow" and status.upper() not in ("RESOLVED", "CLOSED", "VERIFIED"):
                todo = TODO_BUG
            what = [sentence]
            if level == "red":
                what.append("Kayıtta senin adın ya da e-postan geçiyor.")
            messages.append({
                "source": "bugzilla",
                "id": b_id,
                "date": bug.get("last_change_time") or "",
                "marks": [(b_id, "bugzilla", summary_raw, status)],
                "text": render_message(
                    level,
                    [f"🐛 #{b_id} <i>{html.escape(short(summary_raw, 140))}</i>",
                     f"({html.escape(status)} · {html.escape(creator)})"],
                    what, todo, web_url,
                    verdict=None if level == "red" else
                    "🟡 coretemp ile ilgili bir hata kaydı — göz at"),
            })
    for key in ok_keys:
        finish_key(conn, key)
    return all_ok


PRE_MAINLINE_TREES = ("hwmon-next", "hwmon")


GIT_SENTENCES = {
    "mainline": ("coretemp.c'yi değiştiren bir commit Linus'un ana çekirdeğine (mainline) "
                 "girdi."),
    "hwmon-next": ("coretemp.c'yi değiştiren bir commit Guenter'ın hwmon-next ağacına girdi; "
                   "bu, mainline'dan önceki deneme ağacıdır ve yeniden yazılabilir."),
    "hwmon": ("coretemp.c'yi değiştiren bir commit Guenter'ın acil düzeltme dalına (hwmon) "
              "girdi; genelde birkaç gün içinde Linus'a gider."),
}
GIT_TODO = "Bilgi: birleştirildi."
GIT_TODO_RED = "Linke tıklayıp commit'e bak; adının nasıl geçtiğini kontrol et."


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
    """coretemp dosyasinin commit'leri: mainline + hwmon-next.

    Her (agac, dosya) cifti kendi taban cizgisine sahiptir. Ayni commit mainline'a
    gecince ayri bir bildirim olarak gider.
    """
    star_words = [w.lower() for w in resolve_setting(config, "star_words")]
    all_ok = True
    ok_keys = []
    queued = set()
    queued_pre = set()  # hwmon / hwmon-next arasi cift bildirimi onler

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
                # Ayni commit (yazar+baslik) kardes on-ağaçta zaten varsa tekrar bildirme.
                if tree in PRE_MAINLINE_TREES:
                    pair = (author, title_raw)
                    if pair in queued_pre or any(
                            is_seen(conn, git_commit_id(t, author, title_raw), "git")
                            for t in PRE_MAINLINE_TREES if t != tree):
                        continue
                    queued_pre.add(pair)
                # Eski surumde mainline coretemp commit'leri duz sha ile tutuluyordu.
                if tree == "mainline" and is_seen(conn, sha, "git"):
                    continue
                queued.add(cid)
                if silent:
                    mark_seen(conn, cid, "git", title_raw, None)
                    continue

                level = "red" if (starred or is_mine(author, item["summary"])) else "yellow"
                info = [
                    f"{'⭐ ' if starred else ''}💾 <b>[git · {html.escape(tree_cfg['label'])}]</b> "
                    f"{html.escape(short(title_raw, 160))}",
                    f"({item['updated'][:10]} · {html.escape(author or '?')} · "
                    f"<code>{html.escape(path)}</code> · <code>{html.escape(sha[:12])}</code>)",
                ]
                what = [GIT_SENTENCES.get(tree, GIT_SENTENCES["mainline"])]
                if tree == "mainline":
                    for pre in PRE_MAINLINE_TREES:
                        if is_seen(conn, git_commit_id(pre, author, title_raw), "git"):
                            what.append("🚀 hwmon'dan mainline'a geçti." if pre == "hwmon"
                                        else "🚀 hwmon-next'ten mainline'a geçti.")
                            break
                if level == "red":
                    what.append("Commit'te senin adın ya da e-postan geçiyor.")
                todo = GIT_TODO_RED if level == "red" else GIT_TODO
                messages.append({
                    "source": "git",
                    "id": cid,
                    "date": item["updated"],
                    "marks": [(cid, "git", title_raw, None)],
                    "text": render_message(
                        level, ["\n".join(info)], what, todo, item["link"],
                        verdict=None if level == "red" else
                        "🟡 coretemp.c'yi değiştiren bir commit — bilgi"),
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
        ("Kernel Bugzilla (coretemp)", check_bugzilla),
        ("git.kernel.org (mainline + hwmon-next + hwmon)", check_git_commits),
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
        # Tek seferlik 'guncellendi' mesaji: bayrak yalnizca basarili gonderimde yazilir.
        if meta_get(conn, "flag:update-msg-v1") is None and MY_PATCHES:
            if notify(config, token, chat_id, build_update_message(config)):
                meta_set(conn, "flag:update-msg-v1", "1")


if __name__ == "__main__":
    main()
