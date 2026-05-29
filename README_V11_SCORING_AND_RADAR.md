# V11 Probability Scoring + Manual Radar Fix

Bu paket şu geliştirmeleri içerir:

## Skor motoru
Yeni `opportunity.py` vahşi memecoin piyasasına göre yeniden ağırlıklandırıldı:

- Survival: likidite, sell akışı, buy/sell dengesi, safety bilgisi
- Expansion: tx sıcaklığı, volume/liquidity, momentum
- Exit: likidite + gerçek sell akışı + Jupiter exit
- Timing: ideal erken pencere 10 dk - 4 saat
- Confidence: veri güvenilirliği
- Edge: expansion + exit + timing + survival - risk
- Wash/crowded risk cezası

## Telegram / manuel radar
- `/radar <mint>` ve `/analyze <mint>` artık tek satır, çok satır, `<mint>` ve backtick formatlarını temizler.
- Manuel radar analizinden sonra AL / Pozisyonu Kapat / Solscan aksiyon paneli gelir.
- `/scan_stats` artık sağlık paneli + son gerçek skor değerlendirmesi özetini gösterir.

## Alert mantığı
- Sadece `ALINABİLİR` kararı alan coinler ayrı Telegram alerti alır.
- `İZLE` kararı alanlar sessiz watchlist'e alınır; güçlenirse veya bozulursa uyarı gelir.
