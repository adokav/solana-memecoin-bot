# Solana Memecoin Sniper Bot

DexScreener'dan fırsat tarar, RugCheck + Jupiter ile dolandırıcılık testi yapar, Telegram onayıyla otomatik alır, kademeli TP/SL ile çıkar. Render üzerinde 7/24 çalışır.

## Mimari

```
DexScreener (profile + boost + token search)
        │
        ▼
  KATMAN 1 — Fırsat Avı
   ├─ EARLY profili (1-24h yaş)
   └─ TREND profili (24h-7g yaş)
        │
        ▼
  Skor 0-100 (momentum + vol/liq + buy pressure + sosyal + yaş + likidite kalitesi)
        │
        ▼  (skor ≥ MIN_SCORE_TO_ALERT)
  KATMAN 2 — Dolandırıcılık Filtreleri
   ├─ RugCheck: mint/freeze revoke, LP locked %, danger risks
   ├─ Helius: top holder dağılımı
   └─ Jupiter: SOL→token→SOL roundtrip simülasyonu (HONEYPOT testi)
        │
        ▼
   Telegram alert [✅ AL] [❌ Geç]
        │
        ▼
   Jupiter buy → Position storage
        │
        ▼
   Monitor (her 20s fiyat)
   ├─ TP1 +30% → kalanın %30'u → SL breakeven'a çekilir
   ├─ TP2 +80% → kalanın %40'ı
   ├─ TP3 +200% → kalanın %50'si
   ├─ Moon bag (kalan ~%21) → trailing stop %25
   └─ SL -35% → tam kapanış
```

## Setup

### 1. Lokal kurulum (test için)

```bash
git clone <senin-repo>
cd solana-memecoin-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # içini doldur
mkdir -p data          # lokal data klasörü
python main.py
```

### 2. Render kurulum

**A) Repo'yu hazırla**
- GitHub'da yeni private repo aç
- Bu dosyaları push'la
- `.env` ASLA push'lanmasın (`.gitignore` zaten engelliyor)

**B) Render'da Background Worker oluştur**

1. Render dashboard → **New +** → **Background Worker**
2. GitHub repo'yu seç
3. Ayarlar:
   - **Name:** memecoin-sniper
   - **Region:** Frankfurt (TR'ye en yakın)
   - **Branch:** main
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
   - **Instance Type:** Starter ($7/ay)

4. **Disk ekle** (kritik — pozisyonlar buraya yazılır):
   - Name: positions-disk
   - Mount Path: `/data`
   - Size: 1 GB

5. **Environment Variables** sekmesinde Environment Group'unu bağla VEYA tek tek ekle:

| Key | Açıklama |
|-----|----------|
| `TOKEN` | Yeni Telegram bot token'ı (`@BotFather`) |
| `CHAT_ID` | Telegram chat ID'n (`@userinfobot`) |
| `WALLET_PRIVATE_KEY` | Phantom export → base58 |
| `SOLANA_RPC_URL` | `https://mainnet.helius-rpc.com/?api-key=XXX` |
| `HELIUS_API_KEY` | Helius dashboard'dan |
| `DATA_DIR` | `/data` (Disk mount path ile aynı) |
| `BUY_AMOUNT_SOL` | `0.01` (test için küçük başla) |

Diğer parametrelerin hepsi `.env.example`'da default ile geliyor — istersen Render'da override edersin.

6. **Deploy** → logları izle. İlk başlangıçta Telegram'a "Bot başladı" mesajı gelmeli.

### 3. Telegram doğrulama

- Yeni botu Telegram'da bul, `/start` yaz → karşılama mesajı gelmeli
- `/health` → bot canlı mı, son tarama ne zamandı
- `/status` → açık pozisyonlar (başta boş)

## Komutlar

| Komut | Açıklama |
|-------|----------|
| `/start` | Karşılama |
| `/status` | Açık pozisyonlar + PnL + TP hits |
| `/health` | Son tarama, açık pozisyon sayısı |
| `/perf` | Sinyal performansı (alert'ten sonra 1h/24h zirve) |
| `/pnl [gün]` | Kapanan pozisyon raporu (profile/skor/sebep bazlı) |
| `/paper [gün]` | Paper trading raporu (her alert sanal pozisyon olarak açılır) |
| `/macro` | Son makro snapshot (SOL, BTC dom, F&G, pump.fun aktivitesi) |
| `/halt [sebep]` | Yeni alımları durdur (devre kesiciyi manuel aç) |
| `/resume` | Devre kesiciyi kapat, alımlar tekrar serbest |
| `/close <symbol>` | Açık pozisyonu manuel kapat |
| `/analog` | Bugüne benzer geçmiş makro ortamlarda sinyal performansı |
| `/wallets` | Takip edilen smart wallet listesi |
| `/addwallet <adres> [label]` | Smart wallet ekle |
| `/rmwallet <adres>` | Smart wallet çıkar |
| `/candidates` | Otomatik keşfedilen aday wallet'lar |

## Skor Sistemi (max 110)

KATMAN 1 (max 100):
- Momentum (h1+h6): 25
- Volume/Liquidity: 20
- Net buyer pressure: 15
- Volume acceleration (m5×12 vs h1): 10
- Social (boost + tw + tg + site): 10
- Age sweet spot: 5
- Liquidity quality (liq/FDV): 5
- Holder health: 10 (KATMAN 2'den)

KATMAN 2 katkısı (max 10): mint revoke + freeze revoke + LP burn + holder dağılımı

**Eşikler:**
- 🟡 Sarı (50-70): Alert atılır, dikkat
- 🟢 Yeşil (70+): Yüksek güven

## Kademeli Çıkış Stratejisi

Örnek: 100 token aldın varsayalım.

| Olay | Aksiyon | Kalan |
|------|---------|-------|
| Giriş | 100 token | 100 |
| +%30 (TP1) | %30'unu sat → 30 token | 70 |
| +%80 (TP2) | Kalanın %40'ı → 28 token | 42 |
| +%200 (TP3) | Kalanın %50'si → 21 token | 21 |
| Moon bag | Trailing %25 ile takip | 0-21 |

**Breakeven SL:** TP1 hit olduktan sonra SL %0'a çekilir → en kötü senaryoda 0 PnL.

## Güvenlik

⚠️ **Risk uyarıları:**
- Memecoin alımı yüksek risklidir. Kaybetmeyi göze alabileceğin miktarla başla.
- Bot için **ayrı, dedicated** bir Solana cüzdanı kullan, ana cüzdanını bağlama.
- Private key Render Environment Variables'da şifreli durur ama bu plaintext erişim demektir — Render hesabını 2FA ile koru.
- İlk hafta `BUY_AMOUNT_SOL=0.005` gibi minik tutarla test et.
- Filtreler **riski azaltır**, sıfırlamaz. Hâlâ kötü token'a bulaşmak mümkün.

## Parametre Ayarlama

Render Environment'dan oynayabilirsin, kod değişmeden yeniden deploy edilir:

**Daha az sinyal istiyorum (kalite):**
- `MIN_SCORE_TO_ALERT=65`
- `EARLY_MIN_LIQUIDITY=30000`
- `MAX_TOP10_HOLDER_PCT=20`

**Portföyü korumak için risk limiti (önerilir):**
- `MAX_OPEN_POSITIONS=3`
- `MAX_TOTAL_EXPOSURE_SOL=0.03` (örn. her işlem 0.01 SOL ise max 3 açık işlem)


**Daha çok sinyal istiyorum (hız):**
- `MIN_SCORE_TO_ALERT=40`
- `EARLY_MIN_PRICE_H1=8`
- `EARLY_MIN_TXNS_H1=50`

**Daha agresif kâr alma:**
- `TP1_TRIGGER_PCT=20` (erken kâr realize)
- `TRAILING_STOP_PCT=15` (dar trailing)

## Dosyalar

```
config.py           — env okur, dataclass
dexscreener.py      — DexScreener API
screener.py         — KATMAN 1 (profil + skor)
rugcheck.py         — KATMAN 2 (RugCheck + Helius)
jupiter.py          — Quote + buy + sell + honeypot sim
wallet.py           — Keypair loader
storage.py          — Pozisyon JSON (Disk'te)
telegram_handler.py — Bot + butonlar + komutlar
monitor.py          — Kademeli TP/SL/trailing
main.py             — Orkestratör
render.yaml         — Render blueprint
```

## Sorun giderme

- **"Missing required env var: TOKEN"** → Render env'de `TOKEN` yok veya boş
- **"buy failed: no route"** → Jupiter o tokenı henüz indexlemedi, yeni yeni tokenlarda olur
- **Bot mesaj atmıyor** → Yeni bota `/start` attın mı? Atmadıysan Telegram engellemiştir
- **Sürekli "RUG SKIP"** → RugCheck filtrelerin sıkı, `REQUIRE_LP_LOCKED=false` ile test et
- **Hiç aday gelmiyor** → `MIN_SCORE_TO_ALERT=30`'a düşür, filtre eşiklerini gevşet

## Yürütme kalitesi (Jupiter + Jito)

| Env | Default | Açıklama |
|-----|---------|----------|
| `PRIORITY_FEE_LEVEL` | `veryHigh` | Jupiter priority fee seviyesi (`medium`/`high`/`veryHigh`) |
| `MAX_PRIORITY_FEE_LAMPORTS` | `5000000` | Priority fee tavanı (0.005 SOL) |
| `DYNAMIC_SLIPPAGE` | `true` | Jupiter dinamik slippage |
| `DYNAMIC_SLIPPAGE_MAX_BPS` | `1500` | Dynamic slippage tavan |
| `BUY_SLIPPAGE_BPS` | `500` | Alımda sabit slippage tavanı |
| `SELL_SLIPPAGE_BPS` | `700` | Satışta sabit slippage tavanı |
| `JITO_ENABLED` | `false` | Jito bundle yolu (priority fee yarışını bypass) |
| `JITO_TIP_LAMPORTS` | `100000` | Bundle tip miktarı (~$0.02). Daha yüksek = daha öncelikli |
| `JITO_BLOCK_ENGINE_URL` | `mainnet.block-engine.jito.wtf` | Block engine endpoint |

## Adaptive position sizing

Default kapalı (`ADAPTIVE_SIZING_ENABLED=false`). Açıldığında paper PnL'inden
her skor bucket'ı (55-65, 65-75, 75-85, 85+) için bir çarpan hesaplar:
ortalama PnL %≤0 → 0.5×, %30+ → 1.5×, %80+ → 2.0×. Yetersiz örnek (default
5'ten az) varsa flat (1.0×) kalır. `BUY_AMOUNT_SOL × multiplier` ile alır.

**Açma şartı:** `/paper 14` veya `/paper 30` çıktısında her bucket'ta en az
`ADAPTIVE_SIZING_MIN_SAMPLES` (5) örnek görünmeli — yoksa körlemesine çarpan.

## Kaynaklar (KATMAN 1)

- DexScreener: `latest_profiles`, `latest_boosted`, `top_boosted`
- Pump.fun graduation hook: bonding curve tamamlanan tokenlar (Raydium'a geçer geçmez)
  - `PUMPFUN_ENABLED=true` (default), `PUMPFUN_FETCH_LIMIT=30`

## Auto-trade & devre kesici

**Auto-trade** (default kapalı — `AUTO_TRADE_ENABLED=true` ile aç):
Bir aday `AUTO_TRADE_MIN_SCORE` (85) eşiğini geçer, `AUTO_TRADE_MIN_SAFETY_SCORE`
(8/10) güvenlik puanını alır ve honeypot impact'i `AUTO_TRADE_MAX_PRICE_IMPACT`
(%2) altında ise Telegram tap'i beklemeden otomatik alır. Eşik altı sinyaller
yine manuel onay ile gelir. Aktif etmeden önce 1 hafta paper data toplayıp
edge görmek önerilir.

**Devre kesici** (her zaman aktif):
- Günlük kayıp `DAILY_LOSS_STOP_SOL` (0.05) limitini aşarsa gün sonuna kadar
  yeni alım yok.
- `MAX_CONSECUTIVE_LOSSES` (5) ardışık kayıp → manuel `/resume`'a kadar durur.
- Manuel `/halt [sebep]` ile istediğin an durdurulur.
- Durum `data/circuit_breaker.json`'da, restart sonrası devam eder.

## Paper trading & makro snapshot

**Paper trading** (default açık, `PAPER_TRADING_ENABLED=false` ile kapatılır):
Her alert için DexScreener fiyatından sanal pozisyon açılır, real monitor
ile aynı TP/SL/trailing/breakeven mantığıyla kapatılır. Slippage muhafazakar
tahmin için bilerek yüksek tutulur. Sonuç `data/paper_positions.json`'da,
`/paper` ile raporlanır. Gerçek para riski yok — bot'un kendi stratejisinin
**gerçek** performans verisi 1-2 haftada birikir.

**Makro snapshot** (default açık, `MACRO_SNAPSHOT_ENABLED=false` ile kapatılır):
Saatte bir SOL fiyat/24h değişim, BTC dominance, toplam piyasa cap, Fear &
Greed, pump.fun graduation aktivitesi `data/macro.jsonl`'a yazılır. Tarih
arşivi birikince gelecekte "bugüne benzer geçmiş günler" analog backtest
için kullanılır. Şu an sadece arşiv toplar.

## Pyramid / DCA

`PYRAMID_ENABLED=true` ile aktif. TP1 hit olduktan sonra fiyat yeni ATH
yaparsa pozisyona ekleme yapılır:
- Tetik: `TP1_TRIGGER_PCT + (n+1) × PYRAMID_TRIGGER_STEP_PCT` (default +60%, +90%)
- Her ekleme: `BUY_AMOUNT_SOL × PYRAMID_SIZE_RATIO` (default 0.5×)
- Max `PYRAMID_MAX_ADDS` adet ekleme (default 2)
- Eklenince blended entry hesaplanır, trailing referansı sıfırlanır
- Total exposure cap'i hâlâ uygulanır

Paper trading'de de aynı mantık simüle edilir.

## Analog regime backtest

Her sinyal anındaki makro snapshot signal_log'a gömülür. `/analog` komutu
bugünkü makroya (SOL Δ24h, BTC dom, F&G, pump grad rate) ağırlıklı
euclidean benzerlik ile en yakın geçmiş sinyalleri bulup ortalama 24h
zirve performansını raporlar. **En az 5 makro-etiketli finalize sinyal**
biriktikten sonra anlamlı çalışır (1-2 hafta).

## Smart wallet tracking (en güçlü erken sinyal)

`SMART_WALLETS_ENABLED=true` (default) ile aktif. Takip edilen cüzdanların
SOL → memecoin swap'larını Helius enhanced transactions API ile her
`SMART_WALLETS_POLL_INTERVAL` (60s) saniyede bir çekeriz. Bir tokene
`SMART_BUY_WINDOW_MIN` (60dk) içinde N+ smart wallet alımı görürse:
- Skor sistemine **+0…+25 puan** ekler (komponent: `smart_signal`)
- N ≥ `SMART_MIN_BUYS_FOR_INJECT` (2) ise DexScreener'da olmasa bile
  token scan'e enjekte edilir — fiyat hareketinden önce yakalama

Cüzdan listesi `data/smart_wallets.json`'da. İlk seed için env:
`SMART_WALLETS=adres1:label1,adres2:label2`. Canlıda `/addwallet` ve
`/rmwallet` ile yönetilir; `/wallets` ile listelenir.

### Wallet quality scorer (otomatik temizlik)

Her smart wallet alımı için 24h zirvesi `data/wallet_outcomes.json`'da
takip edilir. Cüzdan başına quality skoru:
- avg_peak_24h × 0.5 + 50 (peak komponenti, 0-100 clamp)
- hit_rate_30 (≥%30 vuran finalize oranı)
- quality = 0.6 × peak + 0.4 × hit_rate

`WALLET_AUTO_DISABLE_QUALITY` (default 30) altında ve
`WALLET_AUTO_DISABLE_MIN_SAMPLES` (default 15) finalize sample varsa
cüzdan otomatik disable edilir:
- Polling'den çıkartılır (Helius API budget korunur)
- Screener'ın smart_signal sayısına dahil edilmez
- `/wallets` listesinde ✗ ile gösterilir

Yeni kalite hesabı her `WALLET_OUTCOMES_INTERVAL` (default 10dk) bir
çalışır. Disable olduğunda Telegram'a uyarı düşer.

### Helius WebSocket — smart wallet real-time

`HELIUS_WS_ENABLED=true` (default), Helius mainnet WS endpoint'ine bağlanır.
Her aktif (non-disabled) smart wallet için `logsSubscribe` mention filter
açılır. Wallet'lardan biri tx yaparsa WS notification gelir, `smart_wallet_loop`
hemen uyanır (polling timer'ı beklemeden) → tracker.poll_all() çalışır.

- Reaksiyon: 60s polling → sub-saniye
- Reconnect: exponential backoff (5s → 60s), bağlantı kopuk iken polling
  normal interval'da devam eder (graceful degradation)
- Idempotent: poll `last_processed_sig` ile dedup yapar, gereksiz çağrı zarar
  vermez

### Sector classification + portfolio cap

`sector.py` keyword tabanlı sınıflandırma:
- 10 sector: ai / dog / cat / frog / political / anime / food / tech / celeb / religion
- Tüm tokenlar bir sector'e ya da "other"e düşer

`MAX_POSITIONS_PER_SECTOR` (2) — aynı non-"other" sector'den max 2 açık
pozisyon. Narrative korelasyon koruması: "AI" ısındığında 5 AI tokeni aynı
anda dump'a uğrarsa portföy çakılmaz.

"other" sector'lar cap'siz (her token'ı klasifiye edemiyoruz).

### Slippage-adaptive sizing

`SLIPPAGE_ADAPTIVE_SIZING=true` (default), honeypot sim'den gelen
`price_impact_pct`'a göre size çarpanı:
- impact ≤ 1% → 1.0× (full size)
- 1% < impact ≤ 3% → 0.8×
- 3% < impact ≤ 5% → 0.6×
- impact > 5% → 0.4×

Kelly variance adjustment — yüksek slippage = yüksek varyans → küçük poz.
Mevcut sizing pipeline'ının (bandit/adaptive + wallet profile)
**üzerine** çarpılır.

### Fast-poll loop

`FAST_POLL_ENABLED=true` (default), her `FAST_POLL_INTERVAL` (15s) bir
pump.fun graduate + DS latest_boosted endpoint'lerini pollar. Yeni mint
görürse screener'ın **priority queue**'sine ekler — bir sonraki scan
cycle'da bu mint'ler ÖNCE işlenir.

Sonuç: time-sensitive token'ları (graduate olur olmaz, boost atılır
atılmaz) ortalama 7-8 saniyede yakalıyoruz, full scan cycle'ı (60s)
beklemeden.

Heavy işler (pairs_for_token, RugCheck, honeypot) hâlâ normal scan
cycle'da çalışır — DS rate limit korunur. Sadece listing endpoint'leri
hızlı pollanır.

### Position correlation manager

Aynı creator'dan veya kısa süre içinde çok fazla pozisyon açılmasına
karşı koruma:
- `MAX_POSITIONS_PER_CREATOR` (1) — bir creator'dan eş zamanlı max
  açık pozisyon. Strict: tek (creator'lar genellikle aynı anda promote
  ettikleri tokenları "pump and dump" yapar)
- `MAX_POSITIONS_IN_WINDOW` (3) + `MAX_POSITIONS_PER_WINDOW_MIN` (30) —
  son 30dk içinde max 3 yeni pozisyon. Sistemik exposure kontrolü.

İki check `on_buy`'da `MAX_OPEN_POSITIONS`'tan sonra çalışır.

### Slippage-aware ML feature

ML feature vektörüne `entry_price_impact_pct` eklendi (FEATURE_VERSION=2).
Honeypot sim'in döndürdüğü Jupiter price impact tahmini → ML modeli
"likidite kalitesi" sinyali olarak öğrenir.

**Önemli:** Eski ML modeli (15 feature) artık uyumsuz. `/train` ile
yeniden eğit. Bot eski modelle çalıştığını fark ederse `ml_predicted`
score componenti otomatik 0 döner (nötr) — bot crash etmez.

### MEV / sandwich detection

`MEV_MONITOR_ENABLED=true` (default). Her başarılı alımdan sonra:
- Quote.outAmount vs cüzdana inen gerçek bakiye karşılaştırılır
- Fill ratio < `MEV_DETECT_FILL_THRESHOLD` (0.92) → sandwich şüphesi
- Per-DEX `data/mev_stats.json` istatistik
- DEX'in son `MEV_MIN_SWAPS_FOR_COOLDOWN` (10) swap'ında suspect ratio
  ≥ `MEV_COOLDOWN_THRESHOLD_PCT` (35%) ise `MEV_COOLDOWN_HOURS` (4) saatliğine
  cooldown → screener o DEX'i atlar
- `/mev` per-DEX istatistik

### Twitter influencer scanner (best-effort)

`TWITTER_ENABLED=true` + `TWITTER_HANDLES=ansem,cented,...` ile aktif.
- Nitter RSS scraping ile her `TWITTER_POLL_INTERVAL` (10dk) handle'lerin
  son tweet'leri çekilir
- $SYMBOL ve Solana mint adresi mention'ları çıkartılır
- 6h sliding window'da unique handle sayısı = score bonus
- Screener'da `twitter_mentions` componenti, her unique handle
  `TWITTER_MENTION_SCORE` (5pt) — max 15pt
- `/twitter` son 6h mention listesi

**NOT:** Nitter public instance'ları kararsız (`TWITTER_NITTER_BASE` env'ten
override edilebilir). Resmi Twitter API ($100/ay) için ileride entegrasyon.
Default kapalı.

### Telegram charts

`CHARTS_ENABLED=true` (default), `matplotlib` ile PNG üretip Telegram'a
`send_photo`. Komutlar:
- `/chart pnl` — real equity curve
- `/chart paper` — paper equity curve
- `/chart daily` — son 14 gün günlük PnL bar chart
- `/chart score` — kapanmış pozisyonların skor dağılımı (win vs loss)

### Auto-tuner

`AUTOTUNE_ENABLED=true` (default), her `AUTOTUNE_INTERVAL_HOURS` (24)
bir kapanmış pozisyonlar analiz edilir. Counterfactual simulation
yapmaz — özet istatistikten parametre yönü önerir:
- TP3: kazananların ort zirvesi vs mevcut
- Trailing: yıkananlarda peak'ten ort drawdown
- Min score: bucket bazlı WR inflection
- Profile dengesizliği uyarısı
- `/tune` manuel rapor; öneri otomatik Telegram'a düşer
- Öneriler otomatik uygulanmaz — env + restart manuel

### Pin snapshots (parametre + perf tarihçesi)

`PIN_AUTO_ENABLED=true` (default) → her `PIN_AUTO_INTERVAL_HOURS` (168 =
haftalık) bir otomatik snapshot. Manuel: `/pin <ad> [notlar]`.

- `data/pins.jsonl` — append-only
- 40+ tunable parametre + 7g/all-time perf metrikleri saklanır
- `/pin` — liste
- `/pin show <ad>` — detay
- `/pin diff <a> <b>` — config + perf farkları

Veri biriktikçe parametre revizyonu için referans noktası: önceki
versiyona göre WR ve net SOL nasıl değişmiş, hangi config'i değiştirdik?

### Thompson sampling sizing bandit

`SIZING_BANDIT_ENABLED=true` (default kapalı) → mevcut adaptive_sizing'in
yerine online RL geçer. Her `(profile, score_bucket, multiplier)` için
Beta(α, β) distribution tutar. Karar verirken her arm'dan sample alır,
en yüksek olanı seçer (Thompson sampling). Pozisyon kapandığında
ilgili arm güncellenir.

Avantaj:
- Yeterli sample yokken bile keşfeder (epsilon-greedy değil — Bayesian)
- Online — manuel re-train yok
- `/bandit` ile her arm'ın WR tahmini görülür

Multiplier'lar: 0.5×, 1.0×, 1.5×, 2.0× (BUY_AMOUNT_SOL üzerinden).

### Volume signals — buy ratio velocity + liq dispersion

İki yeni screener score componenti:
- **buy_velocity** (max 10pt): son 30dk'da buy_ratio'nun saatlik değişim
  hızı. Pozitif = bullish accumulation. Erken aşamada 1.3× ağırlıkla.
- **liq_dispersion** (max 5pt): kaç farklı pool'da ≥$1k likidite var.
  Tek pool = 0, 3+ pool = 5. Trend tokenlarda 1.2× ağırlıkla (sağlık
  göstergesi); erken'de 0.5× (yeni token tek pool'da normal).

### Multi-wallet rotation + risk profiles

`WALLET_POOL_ENABLED=true` + `WALLET_POOL_KEYS` formatı:
- `key1,key2` → her ikisi de "balanced"
- `key1:aggressive,key2:conservative` → profile belirtilir
- `key1:aggressive:1.8` → profile + custom size multiplier

Profile default'ları:
- 🔥 aggressive: 1.5× size
- ⚖️ balanced: 1.0× size
- 🛡 conservative: 0.5× size

Picker (`pick_for_buy(score_total)`) skor bantına göre profil seçer:
- score ≥ 85 (high conviction) → aggressive havuzundan
- 70 ≤ score < 85 → balanced
- score < 70 → conservative
- Profile boşsa fallback chain: balanced → primary

Pre-grad pump alımları sabit aggressive (yüksek risk/getiri).
Pozisyonun `size_multiplier`'ı diğer sizing (bandit/adaptive) üzerine
çarpılır → wallet profili ek bir risk katmanı.

### Telegram alpha channel monitor

`TELEGRAM_CHANNELS_ENABLED=true` + `TELEGRAM_CHANNELS=channel1,channel2,...`
ile aktif. Public channel'ların HTML preview sayfası (`t.me/s/<channel>`)
scraping ile her `TELEGRAM_CHANNELS_POLL_INTERVAL` (5dk) çekilir.

- Mesajlardan $SYMBOL ve Solana mint mention'ları çıkar
- 6h sliding window'da unique channel sayısı → `telegram_mentions` score
- Her unique channel `TELEGRAM_MENTION_SCORE` (5pt) — max 15pt
- Profile weights: early 1.3× (alfa kanalları en hızlı sinyal), trend 1.0×
- `/tgchannels` son 6h listesi
- Private channel'lar için Telethon gerekir — bu modül sadece public

### Pyramid bandit

`PYRAMID_BANDIT_ENABLED=true` ile aktif. Pyramid add tetiklendiğinde
`pyramid_size_ratio` Thompson sampling ile seçilir:
- Arms: 0.3, 0.5, 0.75, 1.0 (BUY_AMOUNT_SOL üzerinden)
- Her (profile, score_bucket, ratio) için Beta(α, β)
- Pozisyon kapanınca tüm pyramid_adds'in ratio'su pozisyonun final
  outcome'ı ile güncellenir (win/loss)
- `/bandit` artık entry + pyramid arm'larını birlikte gösterir
- Default kapalı — pyramid_size_ratio fixed (0.5) kalır

### Cross-DEX price awareness

Yeni screener score componenti `price_consistency` (max 5pt):
- ≥$1k likiditeli pool'lar arasında max/min fiyat oranı
- Oran ≤1.01 (%1 fark) → 1.0 (mükemmel)
- Oran ≥1.10 (%10 fark) → 0.0 (mixed/stale liquidity)
- Trend profile 1.3× (stale pool = red flag), early 0.8×

### Holder graph snapshot (insider exit detection)

Entry'de `rugcheck.top_holders(mint, n=20)` ile top 20 holder snapshot
alınır (`Position.entry_holders`). Hold sırasında her safety check
window'unda (5dk) tekrar çekilir:
- Entry holder'ından N tanesi (default 3) bakiyesinin ≥%50'sini
  düşürdüyse → insider exit signal → kapat
- Genelde fiyat hareketinden önce çalışır (insider'lar bilgi avantajı)

Config: `HOLD_INSIDER_EXIT_MIN_DROP_PCT` (50), `HOLD_INSIDER_EXIT_MIN_WALLETS` (3).

### Web dashboard

`DASHBOARD_ENABLED=true` + `DASHBOARD_TOKEN=xxx` ile aiohttp server açılır.
- `/?token=xxx` → HTML dashboard (açık pozisyonlar, PnL kartları, pin
  geçmişi, ML durumu, auto-refresh 30s)
- `/api/status?token=xxx` → JSON status
- Render PORT env'i otomatik kullanılır (Web Service mode), yoksa
  `DASHBOARD_PORT` (10000)

**Render deploy notu:** Bot şu an Background Worker. Dashboard external
erişim için:
1. Worker'ı Web Service'e çevir (Render bu durumda PORT env'i veriyor), VEYA
2. Aynı disk'i (data/) mount eden ayrı bir Web Service oluştur

Worker olarak kalsa bile dashboard local'de bind eder; sorunsuz.

### Pin snapshots (parametre + perf tarihçesi)

### ML scoring layer

`ML_ENABLED=true` (default) ile aktif olur, ama model **eğitildiğinde**
devreye girer. Eğitim için minimum `ML_MIN_SAMPLES` (30) kapanan trade
gerekir (paper + real birleşik).

- `ml.py`: logistic regression classifier (StandardScaler ile feature
  normalize). Binary label: `pnl_pct >= ML_WIN_THRESHOLD_PCT` (30%).
  15 feature: score breakdown bileşenleri + profile encoding + entry
  liquidity (log) + entry top10 + UTC saat.
- `/train` komutu: tüm closed pozisyonları (paper + real) toplar, model
  eğitir, `data/ml_model.pkl`'a kaydeder. Class dengesizliği veya az
  sample varsa graceful skip.
- `/mlstatus`: model sample sayısı, test accuracy, eğitilme zamanı.
- Screener inference: top max_alerts_per_scan adaylar için win
  probability hesaplanır, `ml_predicted` score componenti olarak
  eklenir. Prob 0.5 → 0 puan, 1.0 → +20, 0.0 → -20 (lineer mapping).

Model yoksa veya scikit-learn yüklü değilse graceful skip — bot
normal çalışır. Önerilen kullanım: 2-3 hafta paper veri biriktikten
sonra `/train`, hafta başlarında re-train.

### Quality-weighted smart signal

Smart wallet alımları artık **wallet quality_score**'a göre ağırlıklı
katkı yapar. Q=50 (nötr) → 1.0× ağırlık, Q=80 → 1.6×, Q=20 → 0.4×.
Toplam ağırlık × 7 + 1 puan, max 25. Yani 2 yüksek-kalite cüzdan, 4
düşük-kalite cüzdandan daha çok puan getirir.

### Graduation transition (pump → Raydium)

Pump pozisyonu graduate olduğunda bot **otomatik full-exit yerine**:
1. DexScreener'da Raydium pair'i arar
2. On-chain'den gerçek token bakiyesini çeker
3. Position'ı `is_pump_pos=False` + `pair_address=<raydium>` ile
   günceller, regular Monitor'a devreder

Böylece post-grad pump'a (genelde 2-3x) binebiliyor. DS pair'i
indexlemediyse (rare) full-exit fallback'i devreye girer.

### PumpPortal bonding curve trading

`PUMPPORTAL_ENABLED=true` ile aktif. Pre-grad alert'lara `🐸 AL via
PumpPortal` butonu eklenir. Buton tıklanınca bot PumpPortal local-mode
API'sinden unsigned tx alır, lokalde imzalar, RPC'ye gönderir. 1%
PumpPortal trading fee tx'in içinde.

Pump pozisyonları özel monitor loop'ta tutulur:
- Fiyat pump.fun `virtual_sol_reserves / virtual_token_reserves`'tan
- TP partial yok (bonding curve likiditesi ince) — sadece trailing + SL
- Token graduate olunca otomatik full exit (pump.fun sell aksi halde
  çalışmaz, Raydium'a göç eder)

Config:
- `PUMPPORTAL_BUY_AMOUNT_SOL` (0.01)
- `PUMPPORTAL_SLIPPAGE_PCT` (15) — bonding curve thin, daha yüksek
- `PUMPPORTAL_PRIORITY_FEE_SOL` (0.001)
- `PUMP_MONITOR_INTERVAL` (30s)

### LunarCrush sosyal sinyali

`LUNARCRUSH_API_KEY` set edilirse top adaylar için coin metrics
sorgulanır (galaxy_score, alt_rank, social_volume). 1 saat cache.
Skor sistemine `social_external` componenti eklenir — galaxy_score 0-100
→ max 15 puan (profile-aware ağırlık). Trend'de 1.3× weight (oturmuş
projeler LunarCrush'ta tracked), early'de 0.9× (yeni memecoin coverage
zayıf).

### Pump.fun pre-graduation alert (sosyal velocity ile)

Bot direkt bonding curve trade edemez (Jupiter route yok), ama yaklaşan
graduation'ları yüksek sosyal aktiviteyle filtreleyip alert atar.

Filtreler (hepsi geçmesi şart):
- `PREPUMP_MIN_PROGRESS_PCT` (70) — bonding curve ilerlemesi
- `PREPUMP_MIN_MC_USD` (30k) — minimum market cap
- `PREPUMP_MIN_REPLIES` (30) — bot/spam değil, gerçek community
- `PREPUMP_MIN_VELOCITY_PER_HOUR` (10) — son saatlerde reply artış hızı

Reply velocity Twitter mention velocity'nin **ücretsiz proxy**'sidir —
pump.fun coin sayfalarındaki yorum sayısının saatlik artış hızı.
Twitter API'sı $100/ay olduğu için, memecoin community engagement için
pump.fun yorumları en pragmatik ücretsiz sinyaldir.

Cooldown 24 saat (aynı coin tekrar alert atmaz). Her
`PREPUMP_CHECK_INTERVAL` (5dk) bir tarama yapılır.

**Kullanıcı için:** Alert geldiğinde manuel pump.fun'da satın alabilir.
Bot graduation olunca aynı tokeni zaten yakalıyor (mevcut pipeline);
pre-grad alert bu yüzden bilgi avantajı + erken giriş fırsatı.

### Smart wallet exit signals & hold-time safety re-check

Smart wallet polling artık alımları VE satışları yakalıyor. Pozisyon
açıkken:
- 2+ smart wallet aynı tokeni `SMART_EXIT_WINDOW_MIN` (30dk) içinde
  full-exit ederse (sell SOL ≥ buy SOL × 0.8) → **anında kapanış**
- 1 smart wallet exit yaparsa → trailing yarıya iner (örn %25 → %12.5)
- Eşleşen buy görmediysek minimum `SMART_EXIT_MIN_SOL` (0.5 SOL) eşiği
  ile gürültü filtrelenir

Aynı tick'te hold-time KATMAN 2 re-check çalışır:
- **Likidite drain**: liquidity giriş anına göre `HOLD_LIQ_DRAIN_PCT`
  (35%) düşerse → kapanış (LP çekiliyor = rug in progress)
- **Top10 holder spike**: her `HOLD_SAFETY_CHECK_INTERVAL` (5dk) bir
  RugCheck/Helius tekrar sorgulanır; giriş anındaki top10 oranı
  `HOLD_TOP10_SPIKE_PP` (8pp) sıçramışsa → kapanış (whale akıması)

Üçü de `HOLD_SAFETY_CHECK_ENABLED=true` / `SMART_EXIT_SIGNALS_ENABLED=true`
default açık; istenirse env ile kapatılabilir.

### Otomatik wallet discovery

`DISCOVERY_ENABLED=true` (default) ile bot kendi geçmişinden öğrenir:
- `signal_log`'taki finalize sinyaller arasında peak ≥
  `DISCOVERY_WINNER_THRESHOLD_PCT` (default %100) olanları "winner" kabul eder
- Her winner için Helius'tan o token'ın ilk `DISCOVERY_EARLY_WINDOW_H`
  (default 1 saat) içindeki alıcılarını çeker
- Bu cüzdanları `data/wallet_candidates.json`'a candidate olarak yazar,
  kaç farklı winner'da yakalandığını sayar
- `DISCOVERY_MIN_WINNERS_TO_PROMOTE` (default 2) kazananı tutturan candidate
  → otomatik smart_wallets'a terfi + Telegram bildirimi

Discovery her `DISCOVERY_INTERVAL` (default 1 saat) çalışır, her turda
max `DISCOVERY_MAX_WINNERS_PER_RUN` (5) winner işlenir.

`/candidates` komutu candidate havuzunu, kaç winner'da yakalandıklarını
gösterir. Bot/MEV gibi yanlış terfi edenler quality scorer tarafından
15+ örnek sonrası otomatik disable edilir.

## Profile-aware scoring

`PROFILE_AWARE_SCORING=true` (default) ile her skor componenti `early` ve
`trend` profillerinde farklı ağırlıkla hesaplanır. Örnek: m5 ivmesi early'de
1.3×, trend'de 0.7×; FDV/liq kalitesi early'de 0.7×, trend'de 1.3×.
Tablodaki ağırlıklar `screener.py:PROFILE_WEIGHTS`'te.

## TODO

- [ ] Birden çok TP seviyesine RugCheck snapshot'ı
- [ ] Profile (early/trend) bazlı sizing — şu an sadece skor bucket
- [ ] Sosyal sinyal entegrasyonu (Twitter mention velocity)
