# V12 Sell diagnostics + scoring hotfix

Bu sürümde:
- `extreme volume/liquidity` artık hard reject değil; scoring içinde risk/exit/confidence cezası olarak değerlendirilir.
- Sadece `volume/liquidity > 250x` gibi anlamsız uç değerler veri hatası sayılır.
- Satış öncesi preflight eklendi:
  - bot cüzdanındaki token bakiyesi
  - SOL fee bakiyesi
  - Jupiter token→SOL route kontrolü
  - tahmini SOL çıkışı
  - price impact
- Satış hatası artık Telegram'a sebep ve teşhis bilgisiyle döner.
- Başarılı satış sonrası beklenen/gerçekleşen SOL, PnL ve güncel bakiye raporlanır.

Not:
Bot yalnızca `WALLET_PRIVATE_KEY` ile yüklenen cüzdandaki tokenları satabilir. Manuel alım farklı cüzdandan yapıldıysa satış bakiyesi 0 görünür.
