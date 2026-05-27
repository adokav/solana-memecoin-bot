# Solana Memecoin Alert Bot

Bu sürüm otomatik alım yapan sniper değildir. Botun amacı, memecoin piyasasında
dolandırıcılık/rug riskini azaltarak aday coinleri filtrelemek, Telegram'a
fırsat uyarısı göndermek ve uyarıdan sonra formasyon bozulursa haber vermektir.

## Akış

```text
DexScreener + pump.fun adayları
→ sıkı hard filter
→ RugCheck mint/freeze kontrolü
→ Jupiter roundtrip satılabilirlik simülasyonu
→ Telegram "ALIM ADAYI" mesajı
→ watchlist takibi
→ formasyon bozulursa Telegram uyarısı
→ opsiyonel "Pozisyonu Kapat" butonu
```

Bot **otomatik alım yapmaz**. Alım kararı manuel kalır.

## Güvenlik mantığı

Hard filtreler şunları eler:

- SOL/USDC dışı quote pair'ler
- çok düşük veya aşırı büyük likidite
- çok yeni/çok eski pair
- düşük h1 işlem aktivitesi
- sağlıksız buy/sell dengesi
- sıfır sell / tek taraflı flow şüphesi
- düşük veya aşırı hacim/likidite oranı
- h1 çöküşü veya aşırı uzamış pump

Safety katmanı:

- mint authority risk kontrolü
- freeze authority risk kontrolü
- Jupiter SOL → token → SOL quote roundtrip testi

Bu kontroller riski sıfırlamaz; memecoin'de stop ve çıkış her zaman likiditeye bağlıdır.

## Telegram

Komutlar:

```text
/start
/status
/scan_stats
/ignore <token_mint>
```

Alert mesajında:

- DexScreener linki
- Solscan linki
- Yoksay butonu

Formasyon bozulma mesajında:

- bozulma sebepleri
- DexScreener linki
- Yoksay butonu
- Pozisyonu Kapat butonu

`Pozisyonu Kapat` butonu sadece `WALLET_PRIVATE_KEY` tanımlıysa çalışır ve
botun cüzdanındaki ilgili token bakiyesinin tamamını Jupiter ile SOL'a satmayı dener.

## Dosyalar

| Dosya | Görev |
|---|---|
| `main.py` | Bot orchestrator |
| `config.py` | Tüm ayarlar |
| `candidate.py` | DexScreener pair → Candidate |
| `filter.py` | Hard filter |
| `opportunity.py` | Fırsat/risk skoru |
| `watchlist.py` | Alert sonrası formasyon takibi |
| `telegram_hub.py` | Telegram komutları ve butonlar |
| `safety.py` | RugCheck + Jupiter roundtrip |
| `jupiter.py` | Quote ve opsiyonel satış |
| `screener.py` | Aday toplama |
| `dexscreener.py` | DexScreener client |
| `pumpfun.py` | pump.fun graduate kaynağı |
| `storage.py` | JSON kalıcılık |

## Kurulum

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

`.env` içinde en az şunlar gerekir:

```text
TOKEN=
CHAT_ID=
SOLANA_RPC_URL=
```

Hızlı kapatma butonu için ayrıca:

```text
WALLET_PRIVATE_KEY=
```

Ayrı ve küçük bakiyeli bir cüzdan kullanılması önerilir.
