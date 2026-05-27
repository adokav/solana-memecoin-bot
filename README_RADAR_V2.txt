Radar V2 hotfix

Bu paket import hatasını düzeltti:
- opportunity.py artık main.py ile uyumlu: score() ve Opportunity var.
- Telegram mesajları EARLY WATCH / CONFIRMED SIGNAL olarak ayrıldı.
- filter.py artık erken fırsatları tamamen elemeden hard scam/noise gate olarak çalışır.
- config.py içine EARLY_* eşikleri eklendi.
- __pycache__ / .pyc dosyaları temizlendi.

Önerilen env:
EARLY_MIN_LIQ_USD=2000
EARLY_MIN_TXNS_H1=10
EARLY_MIN_SELLS_H1=1
MIN_LIQ_USD=8000
MIN_TXNS_H1=35
