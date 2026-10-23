# Coretemp Sentinel

Linux çekirdeğindeki **coretemp** gelişmelerini izleyen, yeni kayıt bulduğunda Telegram'a
bildirim atan küçük bir bot. Tek bağımlılık Python 3 standart kütüphanesidir
(`urllib`, `json`, `sqlite3`).

## Ne yapıyor?

Her koşuda dört kaynağı tarar (hepsi **sadece coretemp** odaklı, genel hwmon gürültüsü yok):

| Kaynak | Sorgu | Ne bildirir |
|---|---|---|
| Patchwork (linux-hwmon) | `project=linux-hwmon&q=coretemp` | yeni yama **ve** aynı yamanın `state` değişimi (`accepted`/`rejected`/`superseded`/`changes-requested`/`under-review`) |
| lore.kernel.org | `linux-hwmon/?q=coretemp&x=A` (Atom) | son 30 gündeki coretemp mail'leri (başlık/gövdede coretemp geçen) |
| Kernel Bugzilla | `rest/bug?summary=coretemp` | özetinde coretemp geçen hatalar |
| git.kernel.org | `.../linux.git/atom/drivers/hwmon/coretemp.c` | coretemp.c'ye dokunan commit'ler (Atom çökerse GitHub API + `GITHUB_TOKEN`) |

Mesaj formatı kısa: `[kaynak] başlık — link` + tek satır özet. Bir koşuda en fazla
**10 mesaj** gider; fazlası en yeniden geriye doğru kesilir ve tek satır
"N kayıt daha var" notu düşülür. Bir kaynak 4xx/5xx dönerse diğerleri çalışmaya devam eder
(hata stderr'e yazılır, workflow fail olmaz); yalnız **tümü** birden çökerse çıkış kodu 1 olur.

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
| Patchwork | `SENTINEL_PATCHWORK_URL` | `patchwork.kernel.org/api/1.2/patches/?project=linux-hwmon&q=coretemp` |
| lore | `SENTINEL_LORE_URL` | `lore.kernel.org/linux-hwmon/?q=coretemp&x=A` |
| Bugzilla | `SENTINEL_BUGZILLA_URL` | `bugzilla.kernel.org/rest/bug?summary=coretemp…` |
| git Atom | `SENTINEL_GIT_ATOM_URL` | `git.kernel.org/…/atom/drivers/hwmon/coretemp.c` |
| git API (yedek) | `SENTINEL_GIT_API_URL` | `api.github.com/repos/torvalds/linux/commits?path=…` |

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
DRY_RUN=1 python3 sentinel.py          # belleğe yazar, sadece mesajları basar
```

Tek bir kaynağı denemek için: `python3 -c "import sentinel; ..."` ile ilgili
`check_*` fonksiyonunu çağırın (aşağıdaki test bölümüne bakın).

## Notlar

- Token hiçbir zaman repoya yazılmaz; yalnızca GitHub secret olarak durur.
- `sentinel.db` bilinçli olarak commit edilir (state), `.gitignore`'a eklenmez.

**Keepalive:** `.github/workflows/keepalive.yml` her ayın 1'inde `.keepalive` dosyasına tarih yazıp commit atar; amaç, 60 gün hareketsizlik yüzünden GitHub'ın `sentinel.yml` schedule'ını kendiliğinden kapatmasını önlemektir.
