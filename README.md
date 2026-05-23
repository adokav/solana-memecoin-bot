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
