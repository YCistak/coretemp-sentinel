# Coretemp Sentinel

Linux çekirdeğindeki **coretemp** gelişmelerini izleyen, yeni kayıt bulduğunda Telegram'a
bildirim atan küçük bir bot. Tek bağımlılık Python 3 standart kütüphanesidir
(`urllib`, `json`, `sqlite3`).

## Ne yapıyor?

Her koşuda dört kaynağı tarar (**coretemp** ve **nct6775** odaklı, genel hwmon gürültüsü yok):

| Kaynak | Sorgu | Ne bildirir |
|---|---|---|
| Patchwork (linux-hwmon) | `q=coretemp`, `q=nct6775`, `submitter=ycistak@gmail.com` (hepsi `order=-date&per_page=50`) | yeni yama **ve** aynı yamanın `state` değişimi; kendi yamaların da ayrı sorguyla izlenir |
| lore.kernel.org | `linux-hwmon/?q=coretemp&x=A`, `q=nct6775` (Atom) | son 30 gündeki ilgili mail'ler (başlık/gövdede terim geçen) |
| Kernel Bugzilla | `rest/bug?summary=coretemp`, `summary=nct6775` | özetinde terim geçen hatalar |
| git.kernel.org | **mainline** (torvalds) ve **hwmon-next** (groeck/linux-staging, `?h=hwmon-next`) Atom'ları; `coretemp.c`, `nct6775-core.c`, `nct6775-platform.c` | commit'ler. hwmon-next için "test zamanı", mainline'a geçiş ayrı bildirim. Commit'te "Bertizlio" geçerse ⭐. Mainline Atom'u çökerse GitHub API + `GITHUB_TOKEN` |

**Önemli notlar**

- Patchwork API `order` verilmezse en *eski* yamaları döndürür; bu yüzden her sorguda `order=-date` vardır.
- **Kaynak-başı taban çizgisi:** her sorgu/kaynak anahtarı (`patchwork:q=nct6775`, `git:hwmon-next:...` vb.)
  ilk kez görüldüğünde dönen kayıtlar mesajsız "görüldü" işaretlenir (`meta` tablosu, `init:*`). Yeni
  bir arama terimi eklemek eski kayıtları yağdırmaz.
- **Bildirim kaybı yok:** kayıt yalnızca Telegram mesajı başarıyla gidince "görüldü" olur; gönderilemezse
  sonraki koşuda tekrar denenir. (429'da bir kez bekleyip yeniden dener; HTML hatasında düz metin dener.)
- **hwmon-next rebase edilebilir:** git kimliği `ağaç | yazar | başlık`tır, sha değil; rebase aynı commit'i
  tekrar bildirmez.
- **Sessiz arıza alarmı:** bir kaynak art arda 6 koşu ulaşılamazsa tek seferlik "⚠️ ... ulaşılamıyor",
  düzelince tek seferlik "✅ tekrar bağlandı" gider (sayaç DB'de, `meta` tablosu `fail:*`/`alerted:*`).
- Tek seferlik "🛠️ Sentinel güncellendi" mesajı `flag:update-msg-v1` bayrağıyla bir kez gider.
- Yerel `~/.config/coretemp-sentinel/config.json` içindeki eski `patchwork_url`/`bugzilla_url`
  değerleri varsayılanı ezer; yerel çalıştırmada bunları silin.

Mesaj formatı kısa: `[kaynak] başlık` + link + tek satır özet + **boş satır + 2 cümlelik
Türkçe açıklama**. Açıklama, mesajın tipine göre değişir (aşağıdaki tablo); kural tabanlı
ve deterministiktir, LLM/API kullanmaz. Bir koşuda en fazla
**10 mesaj** gider; fazlası en yeniden geriye doğru kesilir ve tek satır
"N kayıt daha var" notu düşülür. Bir kaynak 4xx/5xx dönerse diğerleri çalışmaya devam eder
(hata stderr'e yazılır, workflow fail olmaz); yalnız **tümü** birden çökerse çıkış kodu 1 olur.

### Açıklama tipleri

| Tip | Ne zaman | Türkçe açıklamanın özü |
|---|---|---|
| `lore_patch_new` | `[PATCH ...]` ile başlayan tek mail | yeni yama önerisi, incelenip test edilmeli |
| `lore_patch_thread` | yeni yama + altına cevaplar | thread'in son mesajlarına bak |
| `lore_applied` | gövdede `Applied.` | kabul edildi, hwmon-next kuyruğunda |
| `lore_review_tag` | `Reviewed-by` / `Acked-by` / `Tested-by` | destek onayı, kabul olasılığı arttı |
| `lore_changes` | `NACK`/`NAK`, `please fix`, `changes requested`, `v2 requested` | düzeltme istendi, v2 gerekli |
| `lore_reply` | diğer cevaplar | tartışmayı bütün okumak gerek |
| `patchwork_new` | yeni yama kaydı | henüz incelenmemiş, test edilebilir |
| `patchwork_accepted` / `rejected` / `changes` / `superseded` | state değişimi | kabul / ret / düzeltme / yeni sürüm |
| `patchwork_other` | diğer state'ler (`under-review`, `not-applicable`, …) | karar değil, süreci takip et |
| `git_commit` | mainline commit'i | mainline'a girdi, çakışma kontrolü |
| `git_next` | hwmon-next commit'i | test zamanı; rebase olabilir |
| `bugzilla_open` / `bugzilla_resolved` | yeni/güncel hata · RESOLVED/CLOSED | yeniden üretmeyi dene · çözümü teyit et |
| `default` | bilinmeyen tip | nötr açıklama |

Kurallar sırayla uygulanır: `Applied.` → review etiketi → değişiklik talebi → yeni yama →
genel cevap. Açıklama her zaman **tam 2 cümle**; bilinmeyen tipte nötr varsayılan kullanılır.

**Thread gruplama (lore):** aynı konudaki mailler tek bildirimde toplanır. Atom beslemesinde
thread kimliği olmadığı için `Re:`/`Fw:` önekleri ve `[PATCH v3 2/4]` gibi etiketler
temizlenip normalize edilmiş konu başlığı anahtar olarak kullanılır. Bildirimde temsilci
olarak **en yeni mail** gösterilir: başlık + link + katılımcılar (en fazla 3 isim + `+N`) +
`N yeni mail` + en yeni mailin 300 karaktere kısaltılmış özeti + 2 cümle açıklama.
Açıklama tipi, en yeni mailin kararına göre seçilir (`Applied.` gibi net kararlar önce gelir).

**Uzunluk:** mail özeti **300 karakterde** kesilir, mesajın tamamı Telegram'ın 4096 karakter
sınırını aşarsa `clamp_message` ile kısaltılır.

**Durum takibi:** `seen` tablosunda `state` kolonu vardır (eski DB'ler `ALTER TABLE` ile
kendiliğinden güncellenir). Migration sonrası `state` değeri boş olan eski kayıtlar sessizce
doldurulur; böylece ilk koşuda sahte "durum değişti" bildirimi gitmez.

Görülen kayıtlar `seen` tablosunda (`sentinel.db`) tutulur; daha önce bildirilmiş bir kayıt
için **ikinci kez mesaj atılmaz**. DB boşsa ilk koşu "baseline" sayılır: mevcut kayıtlar
sessizce hafızaya alınır ve tek bir "sistem aktif" mesajı gönderilir.

## GitHub Actions kurulumu (7/24, makine kapalıyken de çalışır)

- Workflow: `.github/workflows/sentinel.yml`
- Cron: **her 15 dakikada bir** (`*/15 * * * *`) + elle tetikleme (`workflow_dispatch`)
- Runner: `ubuntu-latest`, Python 3.12
- Gerekli secret'lar (Settings → Secrets and variables → Actions):

| Secret | Açıklama |
|---|---|
| `TELEGRAM_TOKEN` | Bot token'ı (@BotFather) |
| `CHAT_ID` | Bildirimin gideceği sohbet kimliği |

Betik token/chat_id'yi önce ortam değişkeninden (`TELEGRAM_TOKEN`, `CHAT_ID`) okur;
bulamazsa `~/.config/coretemp-sentinel/config.json` dosyasına düşer. DB yolu
`SENTINEL_DB` ile değiştirilebilir, varsayılan `./sentinel.db`.

Kaynak adresleri koda gömülü varsayılanlarla gelir, istenirse ortam değişkeni veya
`config.json` ile ezilir:

| Ayar | Ortam değişkeni | Varsayılan |
|---|---|---|
| Patchwork (taban) | `SENTINEL_PATCHWORK_URL` | `patchwork.kernel.org/api/1.2/patches/?project=linux-hwmon&order=-date&per_page=50` (+ `patchwork_queries`) |
| lore (taban) | `SENTINEL_LORE_URL` | `lore.kernel.org/linux-hwmon/?x=A` (+ `lore_terms`) |
| Bugzilla (taban) | `SENTINEL_BUGZILLA_URL` | `bugzilla.kernel.org/rest/bug?limit=20&order=…` (+ `bugzilla_terms`) |
| git | yalnız `config.json` | `git_trees` (atom/api şablonları) ve `git_files` listeleri |

> lore.kernel.org özel `User-Agent` istiyor: varsayılan curl/urllib UA'sı **403** alır,
> `CoretempSentinel/1.0` ile 200 döner.

Her koşudan sonra `sentinel.db` değiştiyse workflow dosyayı
`chore: update seen state [skip ci]` mesajıyla repoya geri commit'ler. **Bu commit kritiktir:**
state repoda kalıcı olmazsa her koşu aynı kayıtları yeniden "yeni" sayıp spam atar.

### Uyarı: 60 gün hareketsizlik

GitHub, bir repoda **60 gün boyunca hiç commit/etkileşim olmazsa** zamanlanmış (schedule)
workflow'ları otomatik olarak devre dışı bırakır. Bu repoda state commit'i genelde hareket
ürettiği için risk düşüktür; yine de uzun süre yeni kayıt çıkmazsa Actions sekmesinden
workflow'u elle yeniden etkinleştirmek gerekir (`gh workflow enable sentinel.yml`).

Ayrıca GitHub cron'ları yoğun saatlerde birkaç dakika gecikebilir; "tam 15 dakika" garantisi yoktur.

## Yerel systemd timer'dan farkı

| | Yerel timer | GitHub Actions |
|---|---|---|
| Çalışma koşulu | Makine açık olmalı | Makineden bağımsız, 7/24 |
| State | `~/.local/state/coretemp-sentinel/sentinel.db` | Repodaki `sentinel.db` |
| Ayar kaynağı | `~/.config/coretemp-sentinel/config.json` | Repo secret'ları |
| Maliyet | — | Public repoda Actions dakikası sınırsız |

İkisi **ayrı state** tutar; aynı kayıt iki yerden bir kez değil, iki kez bildirilebilir.
Yerel timer kurulumu korunmuştur, bu repo onu değiştirmez.

## Yerelde çalıştırma

```bash
export TELEGRAM_TOKEN=...   # veya config.json kullan
export CHAT_ID=...
SENTINEL_DB=./sentinel.db python3 sentinel.py
```

**Kuru çalıştırma (Telegram atmaz, gerçek DB'ye dokunmaz):**

```bash
DRY_RUN=1 python3 sentinel.py     # gerçek DB'nin /tmp kopyasıyla mesajları basar
```

Kuru koşu artık belleğe değil, `sentinel.db`'nin geçici bir kopyasına yazar; böylece gerçek
state bozulmadan **gerçek kayıtlarla** "yeni mi, durum değişti mi" karşılaştırması görülür.
Farklı bir DB denemek için `SENTINEL_DRY_DB=/yol/db` kullanılabilir.

Testler:

```bash
python3 -m unittest discover -s tests -v   # ağ erişimi gerekmez
```

Tek bir kaynağı denemek için: `python3 -c "import sentinel; ..."` ile ilgili
`check_*` fonksiyonunu çağırın (aşağıdaki test bölümüne bakın).

## Notlar

- Token hiçbir zaman repoya yazılmaz; yalnızca GitHub secret olarak durur.
- `sentinel.db` bilinçli olarak commit edilir (state), `.gitignore`'a eklenmez.

**Keepalive:** `.github/workflows/keepalive.yml` her ayın 1'inde `.keepalive` dosyasına tarih yazıp commit atar; amaç, 60 gün hareketsizlik yüzünden GitHub'ın `sentinel.yml` schedule'ını kendiliğinden kapatmasını önlemektir.
