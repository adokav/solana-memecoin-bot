Memecoin Radar V4 audit/hotfix

Ana düzeltme:
- /scan_stats ve /scan_status artık sadece CommandHandler'a bağlı değil.
- Telegram reply keyboard slash-command gönderse bile Universal Text Router önce yakalar.
- scan_stats / scan_status / stats / /scan_stats / /scan_status / /stats aliasları aynı fonksiyona gider.
- Inline callback scan_stats ayrıca korunur.

Radar iyileştirmeleri:
- Early Watch ve Confirmed Signal ayrımı korunur.
- Opportunity/Risk/Exit üçlü skoru kullanılır.
- Alert kayıtlarında mode ve exit_score saklanır.
- Status Early/Confirmed sayısını gösterir.
- Otomatik alım yok; alım çift onaylıdır.
