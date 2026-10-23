# Coretemp Sentinel

Linux çekirdeğindeki **coretemp** gelişmelerini izleyen, yeni kayıt bulduğunda Telegram'a
bildirim atan küçük bir bot. Tek bağımlılık Python 3 standart kütüphanesidir
(`urllib`, `json`, `sqlite3`).

## Ne yapıyor?

Her koşuda iki kaynağı tarar:

| Kaynak | Sorgu |
|---|---|
| Patchwork (linux-hwmon) | `project=linux-hwmon&q=coretemp` — yeni yamalar |
| Kernel Bugzilla | `component=Hardware Monitoring`, son 15 kayıt, anahtar kelime filtresi (`coretemp`, `intel`, `temperature`, `thermal`, `tjmax` …) |

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

## Notlar

- Token hiçbir zaman repoya yazılmaz; yalnızca GitHub secret olarak durur.
- `sentinel.db` bilinçli olarak commit edilir (state), `.gitignore`'a eklenmez.
