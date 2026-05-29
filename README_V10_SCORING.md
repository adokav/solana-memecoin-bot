# V10 Probability Scoring Engine

Bu sürümde radar skoru basit "pump görüldü → puan ver" mantığından çıkarıldı.

Yeni model:
- Survival Score: rug/ölüm/tek taraflı flow riski
- Expansion Score: hacim, tx yoğunluğu ve momentumdan büyüme proxy'si
- Exit Score: likidite, sell akışı ve Jupiter çıkış sinyali
- Timing Score: çok erken/verisiz ile çok geç/dağıtım arasındaki pencere
- Confidence Score: örneklem büyüklüğü ve veri güvenilirliği
- Edge Score: expansion + exit + timing + survival - risk

ALINABİLİR uyarısı için varsayılan minimumlar:
- MIN_ALERT_EDGE_SCORE=68
- MIN_ALERT_CONFIDENCE_SCORE=55
- MIN_ALERT_SURVIVAL_SCORE=58
- MIN_ALERT_EXIT_SCORE=55
- MAX_ALERT_RISK_SCORE=62

Manuel radar:
- /radar <mint>
- /analyze <mint>

Watchlist:
- Edge/Confidence/Survival güçlenirse formasyon güçleniyor uyarısı verir.
- Edge/Confidence düşerse, likidite/price/buy pressure bozulursa gerekçeli çıkış uyarısı verir.
