V7 Alert Policy

Bu sürümde /scan_stats bir sağlık panelidir; coin listesi dökmez.

Akış:
1. Hard filter sadece bariz gürültüyü eler.
2. RugCheck authority + Jupiter roundtrip exit testi yapılır.
3. Opportunity/Risk/Exit skorları hesaplanır.
4. Sadece şu eşikleri geçen coin Telegram'a ALINABİLİR RADAR olarak gider:
   - Opportunity >= MIN_ALERT_OPPORTUNITY_SCORE, default 70
   - Risk <= MAX_ALERT_RISK_SCORE, default 62
   - Exit >= MIN_ALERT_EXIT_SCORE, default 55
   - likidite/tx/sell minimumları
5. AL mesajında önerilen SOL miktarı ve çift onaylı AL butonu bulunur.
6. Erken ama henüz alınabilir olmayan adaylar istenirse sessiz watchlist'e alınır.
