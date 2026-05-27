# Solana Memecoin Sniper Bot (lean v2)

Matematik temelli, otomatik memecoin trading bot. Solana üzerinde fresh
launches'ı yakalar, rug riski olmayanları otomatik alır, dinamik anapara
kurtarma + pyramid (anti-martingale) + trailing exit ile yönetir.

## Strateji çekirdeği (matematik)

**Expected Value:**
$$EV = p_{win} \cdot \mathbb{E}[\text{gain}|\text{win}] - (1-p_{win}) \cdot \mathbb{E}[\text{loss}|\text{loss}]$$

Memecoin için tipik parametreler ($p_{win} \approx 0.2$, win avg +%150, loss
cap -%35) ile EV pozitif — ama bu strateji + risk yönetiminin asimetrik
olmasını gerektirir:

1. **Loss capped** — SL -%35 (gambler's ruin'i önle)
2. **Win unbounded** — moon bag trailing (uzun kuyruktan yararlan)
3. **Principal recovered early** — TP1'de dinamik sell ile anapara kasada
4. **Anti-martingale** — winner'lara ekle, loser'a ekleme

## Akış

```
[Aday tespit] DexScreener (latest/boosted/top) + pump.fun (graduated)
       ↓
[5 Hard Gate] liq ≥ $1k · age 15dk-7g · txns/h ≥ 5 · buy ≥ %40 · h1 ≥ -%30
       ↓
[Safety]      mint revoke + freeze revoke + honeypot sim (Jupiter roundtrip)
       ↓
[Risk Gate]   max_open · max_exposure · daily_loss_cap · consecutive_losses
       ↓
[Auto BUY]    Jupiter swap (fixed BUY_AMOUNT_SOL, Kelly-conservative)
       ↓
[Monitor]
   ├─ TP1 (+%50): dinamik sell = 1/(1+0.5)×1.05 = %70 → anapara kasada
   ├─ Pyramid: TP1 sonrası ATH'lerde (+%100, +%150) BUY × 0.5 ekle
   ├─ TP2 (+%200): kalan moon bag'in %50'si
   ├─ TP3 (+%500): kalanın %50'si daha
   ├─ Trailing (-%25 peak'ten): moon bag exit
   ├─ Breakeven SL (TP1 sonrası): kayıp imkansız
   ├─ Hard SL (-%35 pre-TP1)
   └─ LP drain (-%40): rug in progress → exit
```

## Dosyalar (15)

| Dosya | İş |
|-------|----|
| `main.py` | Orchestrator — scan_loop + monitor_loop + telegram |
| `config.py` | Tüm parametreler tek yerde |
| `screener.py` | Mint topla, filter geçirir |
| `candidate.py` | DexScreener pair → Candidate dataclass |
| `filter.py` | 5 hard gate (boolean pipeline) |
| `safety.py` | mint/freeze revoke + honeypot sim |
| `monitor.py` | TP1 dynamic + pyramid + trailing + SL + LP drain |
| `risk.py` | Circuit breaker + caps + daily loss |
| `stats.py` | EV calculator (matematik ölçüm) |
| `storage.py` | Position dataclass + JSON persist |
| `jupiter.py` | Jupiter swap (buy/sell/honeypot quote) |
| `pumpfun.py` | recently_graduated kaynağı |
| `dexscreener.py` | DS API client |
| `telegram_hub.py` | Alert + 8 komut |
| `wallet.py` | Keypair loader |

## Telegram komutları

| Komut | İş |
|-------|----|
| `/start` | Karşılama |
| `/status` | Açık pozisyonlar + canlı PnL |
| `/pnl` | Kapanan pozisyon özeti |
| `/stats` | **Matematik EV ölçümü** (asıl başarı metriği) |
| `/scan_stats` | Son 5 tarama diagnostic — filtre nerede tıkanıyor |
| `/halt [sebep]` | Yeni alımları durdur |
| `/resume` | Risk gate'i tekrar aç |
| `/close <symbol>` | Pozisyonu manuel kapat |

## Env (Render)

**Zorunlu:**
- `TOKEN` — Telegram bot token
- `CHAT_ID` — Telegram chat ID
- `WALLET_PRIVATE_KEY` — Phantom export base58
- `SOLANA_RPC_URL` — Helius mainnet RPC

**Önemli default'lar (override etmeden çalışır):**
- `BUY_AMOUNT_SOL=0.01` (Kelly conservative)
- `MAX_OPEN_POSITIONS=3`
- `MAX_TOTAL_EXPOSURE_SOL=0.05`
- `DAILY_LOSS_STOP_SOL=0.05`
- `MIN_LIQ_USD=1000` (5 hard gate)
- `MIN_AGE_H=0.25` (15 dakika fresh window)
- `MAX_AGE_H=168` (7 gün — early ve trend birleşik)
- `MIN_TXNS_H1=5`
- `MIN_BUY_RATIO=0.40`
- `MIN_PRICE_H1=-30`
- `TP1_TRIGGER_PCT=50` (anapara kurtarma noktası)
- `TP2_TRIGGER_PCT=200`, `TP3_TRIGGER_PCT=500`
- `STOP_LOSS_PCT=35`, `TRAILING_STOP_PCT=25`

## Hipotez doğrulama

`/stats` komutu son N trade'in gerçek matematiğini gösterir:
- $p_{win}$ — kazanma oranı
- $\mathbb{E}[\text{gain}|\text{win}]$, $\mathbb{E}[\text{loss}|\text{loss}]$
- $EV$ per trade
- $z$-skoru — EV pozitif olmasının istatistiksel güveni

|z| > 2 + EV > 0 → strateji matematiksel olarak çalışıyor.
EV ≤ 0 → hipotez yanlış, strateji değişmeli.

## Güvenlik

- Bot için **ayrı, dedicated** Solana cüzdanı kullan
- Daily loss cap (-0.05 SOL) felaket senaryosunu sınırlar
- Ardışık 5 kayıp → manuel `/resume`'a kadar otomatik durur
- Render env'ini 2FA ile koru

## Setup

```bash
git clone <repo>
cd solana-memecoin-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # doldur
mkdir -p data
python main.py
```

Render Background Worker olarak deploy. `DATA_DIR=/data` ile disk mount.
