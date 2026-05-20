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

## TODO

- [ ] Manuel `/close <symbol>` komutu
- [ ] PnL özet raporu `/pnl` (haftalık)
- [ ] Birden çok TP seviyesine RugCheck snapshot'ı
- [ ] Pump.fun graduation hook'u (yeni listings için)
