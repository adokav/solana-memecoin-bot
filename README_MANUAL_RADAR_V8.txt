
Manual Radar V8

Yeni komutlar:
- /radar <token_mint>
- /analyze <token_mint>

Akış:
1. Kullanıcı token mint gönderir.
2. Bot DexScreener verisini, güvenlik sonucunu, Opportunity/Risk/Exit skorlarını hesaplar.
3. Karar DEVAM veya İZLE ise coin otomatik watchlist'e alınır.
4. Formasyon bozulursa gerekçeli Telegram uyarısı gelir.
5. Uyarıdaki "Pozisyonu Kapat" butonu çift onayla cüzdandaki token bakiyesini satar.
6. Satış sonrası çıkan SOL, PnL (bot alımı kayıtlıysa) ve güncel SOL bakiye raporlanır.

Not:
Manuel olarak başka yerden alınan coinlerde giriş maliyeti bilinmediği için PnL yalnızca botun kendi AL butonuyla yaptığı alımlarda hesaplanabilir.
