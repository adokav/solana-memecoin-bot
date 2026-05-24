"""Ana orkestratör.

Akış:
  scan_loop: DexScreener → KATMAN 1 (profil + skor) → KATMAN 2 (RugCheck + honeypot)
             → Telegram alert (buton)
  monitor_loop: açık pozisyonları kademeli çıkış mantığı ile yönet
  heartbeat: kendi sağlığını kontrol et, /health komutuna cevap için
"""
import asyncio
import logging
import signal
import time
from dataclasses import asdict

from analog import analog_report
from circuit_breaker import CircuitBreaker
from config import config
from dexscreener import DexScreener
from helius import Helius
from jupiter import Jupiter, LAMPORTS_PER_SOL
from lunarcrush import LunarCrush
from macro import MacroCollector, append_snapshot, format_snapshot, latest_snapshot

# ML opsiyonel: scikit-learn yüklü değilse graceful skip
try:
    from ml import (
        format_ml_status,
        load_model,
        save_model,
        train_from_positions,
    )
    _ML_OK = True
except Exception:
    _ML_OK = False
from monitor import Monitor
from paper import PaperMonitor, PaperStore
from pin import (
    create_pin,
    find_pin,
    format_diff,
    format_pin_detail,
    format_pins_list,
    list_pins,
)
from pnl import format_report, summarize
from prepump import PrePumpDetector, format_prepump_alert
from pumpfun import PUMP_GRADUATION_MC_USD, PumpFun
from pumpportal import PumpPortal, PumpPortalError
from rugcheck import RugCheckClient, SafetyReport
from screener import Candidate, Screener
from signal_log import SignalLog
from sizing import size_for_candidate
from sizing_bandit import (
    BanditStore,
    choose_sizing as bandit_choose_sizing,
    format_bandit_status,
    update_from_position as bandit_update_from_position,
)
from smart_wallets import (
    SmartWalletStore,
    SmartWalletTracker,
    WalletOutcomeStore,
    format_wallets_text,
    update_wallet_stats,
)
from wallet_discovery import (
    CandidateStore,
    WalletDiscovery,
    format_candidates_text,
)
from storage import Position, Store
from telegram_handler import TelegramHub, set_buy_callback, set_pump_buy_callback
from wallet import load_keypair
from wallet_pool import WalletPool, format_pool_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-15s | %(message)s",
)
log = logging.getLogger("main")


class Bot:
    def __init__(self) -> None:
        kp = load_keypair()
        self.wallet_pool = WalletPool(kp)
        self.bandit_store = BanditStore.load()
        self.ds = DexScreener()
        self.pf = PumpFun() if config.pumpfun_enabled else None
        self.prepump: PrePumpDetector | None = (
            PrePumpDetector(self.pf)
            if self.pf is not None and config.prepump_enabled else None
        )
        self.lunar: LunarCrush | None = (
            LunarCrush() if config.lunarcrush_enabled and config.lunarcrush_api_key else None
        )
        self.ml_bundle = None
        if _ML_OK and config.ml_enabled:
            try:
                self.ml_bundle = load_model()
                if self.ml_bundle is not None:
                    log.info(
                        "ml model loaded: n=%d acc=%.2f",
                        self.ml_bundle.n_samples, self.ml_bundle.test_accuracy,
                    )
            except Exception:
                log.exception("ml model load failed")
        self.helius: Helius | None = (
            Helius() if config.smart_wallets_enabled and config.helius_api_key else None
        )
        self.smart_store: SmartWalletStore | None = (
            SmartWalletStore.load() if config.smart_wallets_enabled else None
        )
        self.outcome_store: WalletOutcomeStore | None = (
            WalletOutcomeStore.load() if config.smart_wallets_enabled else None
        )
        self.smart_tracker: SmartWalletTracker | None = (
            SmartWalletTracker(
                self.smart_store, self.helius, self.ds, self.outcome_store,
            )
            if self.smart_store is not None
            and self.helius is not None
            and self.outcome_store is not None
            else None
        )
        # Wallet auto-discovery: kazanan sinyallerin ilk alıcılarından öğrenir
        self.candidate_store: CandidateStore | None = (
            CandidateStore.load()
            if config.discovery_enabled and config.smart_wallets_enabled
            else None
        )
        self.discovery: WalletDiscovery | None = None  # signal_log init sonrası set edilecek
        self.jup = Jupiter(kp)
        self.rug = RugCheckClient()
        self.pumpportal: PumpPortal | None = (
            PumpPortal(kp, self.jup.rpc)
            if config.pumpportal_enabled else None
        )
        self.store = Store.load()
        self.tg = TelegramHub()
        self.screener = Screener(
            self.ds, self.pf, self.smart_store, self.lunar, self.ml_bundle,
        )
        self.signal_log = SignalLog()
        self.monitor = Monitor(
            self.ds, self.jup, self.store, self.tg,
            smart=self.smart_store, rug=self.rug,
        )
        if (
            self.candidate_store is not None
            and self.smart_store is not None
            and self.helius is not None
        ):
            self.discovery = WalletDiscovery(
                self.helius, self.ds, self.signal_log,
                self.candidate_store, self.smart_store,
            )
        self.paper_store = PaperStore.load() if config.paper_trading_enabled else None
        self.paper_monitor = (
            PaperMonitor(self.ds, self.paper_store, smart=self.smart_store)
            if self.paper_store is not None else None
        )
        self.macro = MacroCollector(self.pf) if config.macro_snapshot_enabled else None
        self.breaker = CircuitBreaker()
        self._stop = asyncio.Event()
        self._last_scan_ts: float = 0
        self._last_scan_count: int = 0
        self._last_alert_ts: float = 0
        self.wallet_pubkey = str(kp.pubkey())
        log.info("wallet: %s", self.wallet_pubkey)

    # ---------- Buy callback (Telegram butonu basıldığında) ----------

    async def on_buy(self, c: Candidate, safety: SafetyReport) -> None:
        if self.store.find_by_pair(c.pair_address):
            await self.tg.info(f"⚠️ ${c.base_symbol} için zaten açık pozisyon var.")
            return

        # Devre kesici: önceden açıksa veya post-trade check yeni tetiklerse iptal
        if self.breaker.is_open():
            await self.tg.info(
                f"⛔ Alım iptal — devre kesici açık.\n"
                f"Sebep: <code>{self.breaker.state.reason}</code>"
            )
            return
        halted_now, reason = self.breaker.check_post_close(self.store.positions)
        if halted_now:
            await self.tg.info(f"⛔ Devre kesici tetiklendi: <code>{reason}</code>")
            return

        open_positions = self.store.open_positions()
        if len(open_positions) >= config.max_open_positions:
            await self.tg.info(
                f"⛔ Yeni alım engellendi: açık pozisyon limiti dolu "
                f"(<code>{len(open_positions)}/{config.max_open_positions}</code>)."
            )
            return

        # Sizing: bandit önce, kapalıysa adaptive_sizing, o da kapalıysa flat
        paper_positions = self.paper_store.positions if self.paper_store else None
        chosen_multiplier: float | None = None
        if config.sizing_bandit_enabled:
            total_score = c.score + safety.score
            sized, size_note, mult = bandit_choose_sizing(
                total_score, c.profile, paper_positions,
                config.buy_amount_sol, self.bandit_store,
            )
            buy_amount = sized
            chosen_multiplier = mult
        else:
            buy_amount, size_note = size_for_candidate(
                c.score + safety.score, paper_positions, config.buy_amount_sol,
            )
            # adaptive_sizing flat ise multiplier 1.0
            chosen_multiplier = (
                buy_amount / config.buy_amount_sol if config.buy_amount_sol > 0 else 1.0
            )

        if buy_amount <= 0:
            await self.tg.info(
                f"⏭ <b>${c.base_symbol}</b> pas — size 0\n"
                f"<i>{size_note}</i>"
            )
            return

        current_exposure = sum(p.sol_spent for p in open_positions)
        projected_exposure = current_exposure + buy_amount
        if projected_exposure > config.max_total_exposure_sol:
            await self.tg.info(
                "⛔ Yeni alım engellendi: toplam risk limiti aşılacak.\n"
                f"Mevcut: <code>{current_exposure:.4f} SOL</code>\n"
                f"Yeni sonrası: <code>{projected_exposure:.4f} SOL</code>\n"
                f"Limit: <code>{config.max_total_exposure_sol:.4f} SOL</code>"
            )
            return

        log.info("BUY %s amount=%.4f SOL (%s)", c.base_symbol, buy_amount, size_note)
        try:
            sig, tokens_raw = await self.jup.buy(c.base_token, buy_amount)
        except Exception as e:
            log.exception("buy failed")
            await self.tg.info(f"❌ Alım hatası ${c.base_symbol}: <code>{e}</code>")
            return

        pos = Position(
            pair_address=c.pair_address,
            base_token=c.base_token,
            symbol=c.base_symbol,
            entry_price_usd=c.price_usd,
            peak_price_usd=c.price_usd,
            amount_raw=tokens_raw,
            remaining_raw=tokens_raw,
            sol_spent=buy_amount,
            opened_at=time.time(),
            tx_open=sig,
            profile=c.profile,
            score=c.score + safety.score,
            original_entry_price_usd=c.price_usd,
            entry_liquidity_usd=c.liquidity_usd,
            entry_top10_pct=safety.top10_pct,
            last_safety_check_ts=time.time(),
            sizing_multiplier=chosen_multiplier,
        )
        self.store.add(pos)

        await self.tg.info(
            f"✅ <b>${c.base_symbol}</b> ALINDI!\n"
            f"Giriş: <code>${c.price_usd:.8f}</code>\n"
            f"Harcanan: <code>{buy_amount:.4f} SOL</code>  <i>({size_note})</i>\n\n"
            f"<b>Kademeli çıkış planı:</b>\n"
            f"• TP1 +{config.tp1_trigger:.0f}% → kalanın %{config.tp1_sell:.0f}'i\n"
            f"• TP2 +{config.tp2_trigger:.0f}% → kalanın %{config.tp2_sell:.0f}'i\n"
            f"• TP3 +{config.tp3_trigger:.0f}% → kalanın %{config.tp3_sell:.0f}'i\n"
            f"• Moon bag: trailing %{config.trailing_stop:.0f}\n"
            f"• SL: -{config.stop_loss:.0f}% (TP1 sonrası breakeven)\n\n"
            f"<a href=\"https://solscan.io/tx/{sig}\">solscan</a>"
        )

    # ---------- Auto-trade helpers ----------

    def _auto_eligible(self, c: Candidate, safety: SafetyReport, impact: float) -> bool:
        if not config.auto_trade_enabled:
            return False
        total = c.score + safety.score
        if total < config.auto_trade_min_score:
            return False
        if safety.score < config.auto_trade_min_safety_score:
            return False
        if impact > config.auto_trade_max_price_impact:
            return False
        if self.breaker.is_open():
            return False
        return True

    async def _auto_buy(self, c: Candidate, safety: SafetyReport) -> None:
        try:
            await self.tg.info(
                f"🤖 <b>AUTO-BUY</b> tetiklendi: <b>${c.base_symbol}</b>  "
                f"<code>skor {c.score + safety.score:.0f}</code>"
            )
            await self.on_buy(c, safety)
        except Exception:
            log.exception("auto-buy error for %s", c.base_symbol)

    # ---------- Pre-grad pump buy callback ----------

    async def on_pump_buy(self, mint: str, symbol: str, sol_amount: float) -> None:
        if self.pumpportal is None:
            await self.tg.info("⚠️ PumpPortal kapalı (PUMPPORTAL_ENABLED=false).")
            return
        if self.breaker.is_open():
            await self.tg.info(
                f"⛔ Pump alım iptal — devre kesici açık.\n"
                f"Sebep: <code>{self.breaker.state.reason}</code>"
            )
            return
        # Aynı mint için açık pump pozisyon varsa skip
        for p in self.store.open_positions():
            if p.base_token == mint and p.is_pump_pos:
                await self.tg.info(f"⚠️ ${symbol} için zaten açık pump pozisyon var.")
                return

        log.info("PUMPPORTAL BUY %s amount=%s SOL", symbol, sol_amount)
        try:
            sig = await self.pumpportal.buy(
                mint, sol_amount,
                slippage_pct=config.pumpportal_slippage_pct,
                priority_fee_sol=config.pumpportal_priority_fee_sol,
            )
        except PumpPortalError as e:
            log.exception("pumpportal buy failed")
            await self.tg.info(
                f"❌ PumpPortal alımı başarısız ${symbol}: <code>{e}</code>"
            )
            return
        except Exception as e:
            log.exception("pumpportal buy unexpected")
            await self.tg.info(
                f"❌ Pump alım hatası ${symbol}: <code>{e}</code>"
            )
            return

        # Anlık fiyatı çekmeye çalış (peak/SL hesabı için referans)
        entry_price_usd = 0.0
        if self.pf is not None:
            coin_data = await self.pf.coin_info(mint)
            if coin_data:
                entry_price_usd = self._pump_price_from_coin(coin_data)

        pos = Position(
            pair_address="",  # pump pozisyonları DS pair'i yok
            base_token=mint,
            symbol=symbol,
            entry_price_usd=entry_price_usd or 1e-9,  # 0 olmasın diye epsilon
            peak_price_usd=entry_price_usd or 1e-9,
            amount_raw=1,  # pump pos partial sell yok, placeholder
            remaining_raw=1,
            sol_spent=sol_amount,
            opened_at=time.time(),
            tx_open=sig,
            profile="pump",
            score=0,
            original_entry_price_usd=entry_price_usd or 1e-9,
            is_pump_pos=True,
        )
        self.store.add(pos)

        await self.tg.info(
            f"🐸 <b>${symbol}</b> PUMP ALINDI!\n"
            f"Harcanan: <code>{sol_amount} SOL</code>\n"
            f"Giriş fiyatı: <code>${entry_price_usd:.8f}</code>\n\n"
            f"<b>Pump exit planı:</b>\n"
            f"• Trailing stop -%{config.trailing_stop:.0f}\n"
            f"• Hard SL -%{config.stop_loss:.0f}\n"
            f"• Graduation = otomatik full exit\n\n"
            f"<a href=\"https://solscan.io/tx/{sig}\">solscan</a>"
        )

    @staticmethod
    def _pump_price_from_coin(coin_data: dict) -> float:
        """Pump.fun coin verisinden token fiyatını (USD) hesaplar."""
        try:
            vsr = float(coin_data.get("virtual_sol_reserves") or 0)
            vtr = float(coin_data.get("virtual_token_reserves") or 0)
            sol_usd = float(coin_data.get("sol_price") or 0)
        except (TypeError, ValueError):
            return 0.0
        if vsr <= 0 or vtr <= 0:
            return 0.0
        # Decimals: SOL=9, pump tokens=6
        sol_amount = vsr / 1e9
        token_amount = vtr / 1e6
        if token_amount <= 0:
            return 0.0
        price_in_sol = sol_amount / token_amount
        if sol_usd <= 0:
            # sol_price coin endpoint'inde varsa kullan, yoksa MC'den türet
            mc = float(coin_data.get("usd_market_cap") or 0)
            total_supply = float(coin_data.get("total_supply") or 0) / 1e6
            if mc > 0 and total_supply > 0:
                return mc / total_supply
            return 0.0
        return price_in_sol * sol_usd

    async def _token_balance_raw(self, mint: str) -> int:
        """Cüzdandaki SPL token bakiyesini raw birim olarak döner.

        Pump→Raydium transition'da gerçek miktarı bilmek için.
        """
        try:
            from solders.pubkey import Pubkey
            from solana.rpc.types import TokenAccountOpts
            owner = Pubkey.from_string(self.wallet_pubkey)
            mint_pk = Pubkey.from_string(mint)
            opts = TokenAccountOpts(mint=mint_pk)
            resp = await self.jup.rpc.get_token_accounts_by_owner_json_parsed(
                owner, opts,
            )
            accounts = resp.value if resp and resp.value else []
            total = 0
            for acc in accounts:
                try:
                    info = acc.account.data.parsed["info"]
                    amount = int(info["tokenAmount"]["amount"])
                    total += amount
                except (KeyError, TypeError, AttributeError):
                    continue
            return total
        except Exception:
            log.exception("token balance lookup failed for %s", mint[:8])
            return 0

    async def _try_graduation_transition(
        self, pos: Position, coin_data: dict,
    ) -> bool:
        """Graduate olan pump pozisyonunu Raydium-tracked'a transition et.

        Başarılıysa True döner — bot artık bu pozisyonu regular Monitor'la
        izler. Başarısızsa False (caller fallback olarak full-exit yapar).
        """
        # 1. DS'de Raydium pair'i ara
        pairs = await self.ds.pairs_for_token("solana", pos.base_token)
        if not pairs:
            log.warning(
                "graduation transition: DS pair not yet indexed for %s, "
                "will retry next tick", pos.symbol,
            )
            return False
        pairs.sort(
            key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
            reverse=True,
        )
        top_pair = pairs[0]
        pair_address = top_pair.get("pairAddress")
        if not pair_address:
            return False

        # 2. Gerçek token miktarını on-chain'den çek
        token_balance = await self._token_balance_raw(pos.base_token)
        if token_balance <= 0:
            log.warning(
                "graduation transition: zero token balance for %s, "
                "full-exit fallback", pos.symbol,
            )
            return False

        # 3. Anlık fiyatı al
        try:
            current_price = float(top_pair.get("priceUsd") or 0)
        except (TypeError, ValueError):
            current_price = 0.0
        if current_price <= 0:
            current_price = pos.entry_price_usd

        # 4. Pozisyonu Raydium-tracked'a çevir
        pos.is_pump_pos = False
        pos.pair_address = pair_address
        pos.amount_raw = token_balance
        pos.remaining_raw = token_balance
        # entry_price_usd'i koru (orijinal giriş, PnL doğru hesaplansın)
        # Liquidity baseline güncelle — yeni Raydium pair, drain check
        # için referans
        try:
            new_liq = float((top_pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            new_liq = pos.entry_liquidity_usd or 0
        if new_liq > 0:
            pos.entry_liquidity_usd = new_liq
        # Peak fiyatını mevcut Raydium fiyatına resetle (yeni AMM, trailing
        # referansı sıfırlansın)
        if current_price > pos.peak_price_usd:
            pos.peak_price_usd = current_price
        self.store.update()

        await self.tg.info(
            f"🎓 <b>${pos.symbol}</b> GRADUATED → Raydium'a transition\n"
            f"Pair: <code>{pair_address[:8]}..{pair_address[-4:]}</code>\n"
            f"Token bakiye: <code>{token_balance:,}</code> raw\n"
            f"Anlık fiyat: <code>${current_price:.8f}</code>\n"
            f"Bot artık regular Monitor'la izleyecek (post-grad pump'a açık)"
        )
        return True

    async def _pump_close_all(self, pos: Position, price: float, reason: str) -> None:
        if self.pumpportal is None:
            log.warning("pump close attempted but pumpportal is None")
            return
        try:
            sig = await self.pumpportal.sell(
                pos.base_token, percent=100,
                slippage_pct=config.pumpportal_slippage_pct,
                priority_fee_sol=config.pumpportal_priority_fee_sol,
            )
        except Exception as e:
            log.exception("pump close failed for %s", pos.symbol)
            await self.tg.info(
                f"❌ Pump satışı başarısız ${pos.symbol}: <code>{e}</code>"
            )
            return

        # SOL geliri için yaklaşık hesap: price × ratio
        pnl_pct = (
            (price - pos.entry_price_usd) / pos.entry_price_usd * 100
            if pos.entry_price_usd > 0 else 0
        )
        approx_received = pos.sol_spent * (1 + pnl_pct / 100)
        pos.sol_received_total = approx_received
        pos.remaining_raw = 0
        pos.pnl_pct = pnl_pct
        pos.status = "closed"
        pos.closed_at = time.time()
        pos.close_reason = reason
        self.store.update()

        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        await self.tg.info(
            f"{emoji} <b>${pos.symbol}</b> PUMP KAPANDI ({reason})\n"
            f"PnL: <code>{pnl_pct:+.2f}%</code>\n"
            f"Giriş: <code>{pos.sol_spent:.4f} SOL</code> → "
            f"Çıkış (tahmini): <code>{approx_received:.4f} SOL</code>\n"
            f"<a href=\"https://solscan.io/tx/{sig}\">solscan</a>"
        )

    async def _tick_pump_position(self, pos: Position) -> None:
        if self.pf is None or self.pumpportal is None:
            return
        coin_data = await self.pf.coin_info(pos.base_token)
        if not coin_data:
            return

        # Graduate olduysa: post-grad pump'a binmek için Raydium'a transition et
        # (DS pair'i bulamazsak fallback olarak PumpPortal full-exit)
        if bool(coin_data.get("complete")):
            transitioned = await self._try_graduation_transition(pos, coin_data)
            if transitioned:
                # Position artık is_pump_pos=False, regular Monitor'a devredilecek
                return
            # Fallback: bonding curve'de hâlâ trade edilebiliyorken kapan
            current_price = self._pump_price_from_coin(coin_data)
            await self._pump_close_all(
                pos, current_price or pos.entry_price_usd,
                "graduated (Raydium pair bulunamadı, full exit)",
            )
            return

        price = self._pump_price_from_coin(coin_data)
        if price <= 0:
            return

        if price > pos.peak_price_usd:
            pos.peak_price_usd = price
            self.store.update()

        pnl_pct = (price - pos.entry_price_usd) / pos.entry_price_usd * 100
        drawdown = (pos.peak_price_usd - price) / pos.peak_price_usd * 100

        # Pump pos için partial TP yok — trailing veya SL
        trail_pct = pos.trailing_stop_override_pct or config.trailing_stop
        if drawdown >= trail_pct and pnl_pct > 0:
            # Sadece kârda iken trailing — başlangıçta dump'tan SL koruyor
            await self._pump_close_all(
                pos, price, f"trailing -{drawdown:.1f}% from peak",
            )
            return
        if pnl_pct <= -config.stop_loss:
            await self._pump_close_all(pos, price, f"SL {pnl_pct:.1f}%")
            return

    # ---------- Loop: otomatik pin snapshot ----------

    async def auto_pin_loop(self) -> None:
        # Boot'tan 30dk sonra başla, sonra her PIN_AUTO_INTERVAL_HOURS saatte bir
        await asyncio.sleep(1800)
        while not self._stop.is_set():
            if config.pin_auto_enabled:
                try:
                    paper_pos = (
                        self.paper_store.positions
                        if self.paper_store else None
                    )
                    name = f"auto_{time.strftime('%Y%m%d_%H%M', time.gmtime())}"
                    pin = create_pin(
                        name=name,
                        real_positions=self.store.positions,
                        paper_positions=paper_pos,
                        notes="otomatik haftalık snapshot",
                        by="auto",
                    )
                    real_n = pin.perf_snapshot.get("real_all_time", {}).get("total", 0)
                    paper_n = pin.perf_snapshot.get("paper_all_time", {}).get("total", 0)
                    log.info(
                        "auto-pin: %s real=%d paper=%d",
                        name, real_n, paper_n,
                    )
                except Exception:
                    log.exception("auto-pin error")
            await asyncio.sleep(config.pin_auto_interval_hours * 3600)

    # ---------- Loop: pump position monitor ----------

    async def pump_monitor_loop(self) -> None:
        await asyncio.sleep(15)
        while not self._stop.is_set():
            try:
                for pos in list(self.store.open_positions()):
                    if not pos.is_pump_pos:
                        continue
                    try:
                        await self._tick_pump_position(pos)
                    except Exception:
                        log.exception("pump tick error for %s", pos.symbol)
            except Exception:
                log.exception("pump monitor loop error")
            await asyncio.sleep(config.pump_monitor_interval)

    # ---------- /halt /resume ----------

    async def halt_text(self, reason: str) -> str:
        self.breaker.halt(reason, until_ts=0.0)
        return self.breaker.status_text(self.store.positions)

    async def resume_text(self) -> str:
        self.breaker.resume("manual")
        return self.breaker.status_text(self.store.positions)

    # ---------- /close ----------

    async def close_text(self, arg: str) -> str:
        ok, msg = await self.monitor.manual_close(arg)
        return ("✅ " if ok else "⚠️ ") + msg

    # ---------- /analog ----------

    async def analog_text(self) -> str:
        return analog_report(self.signal_log)

    # ---------- /wallets, /addwallet, /rmwallet ----------

    async def wallets_text(self) -> str:
        if self.smart_store is None:
            return "📭 Smart wallet tracking kapalı (SMART_WALLETS_ENABLED=false)."
        return format_wallets_text(self.smart_store)

    async def addwallet_text(self, addr: str, label: str) -> str:
        if self.smart_store is None:
            return "Smart wallet tracking kapalı."
        if not (32 <= len(addr) <= 64):
            return f"⚠️ Adres formatı geçersiz: <code>{addr}</code>"
        if self.smart_store.add_wallet(addr, label):
            disp = f"{addr[:6]}..{addr[-4:]}" + (f" [{label}]" if label else "")
            return f"✅ Eklendi: <code>{disp}</code>"
        return f"⚠️ Zaten kayıtlı: <code>{addr[:6]}..{addr[-4:]}</code>"

    async def rmwallet_text(self, addr: str) -> str:
        if self.smart_store is None:
            return "Smart wallet tracking kapalı."
        if self.smart_store.remove_wallet(addr):
            return f"✅ Çıkarıldı: <code>{addr[:6]}..{addr[-4:]}</code>"
        return f"⚠️ Listede yok: <code>{addr[:6]}..{addr[-4:]}</code>"

    async def candidates_text(self) -> str:
        if self.candidate_store is None:
            return "📭 Wallet discovery kapalı (DISCOVERY_ENABLED=false)."
        return format_candidates_text(self.candidate_store)

    # ---------- /train, /mlstatus ----------

    async def train_text(self) -> str:
        if not _ML_OK:
            return "⚠️ ML kütüphaneleri yüklü değil (scikit-learn)."
        # Real + paper closed pozisyonların birleşimi
        positions = list(self.store.positions)
        if self.paper_store is not None:
            positions += list(self.paper_store.positions)
        try:
            bundle = train_from_positions(positions)
        except Exception as e:
            log.exception("ml train error")
            return f"❌ Eğitim hatası: <code>{e}</code>"
        if bundle is None:
            closed = sum(1 for p in positions if p.status == "closed")
            return (
                f"⚠️ Yetersiz veri: <code>{closed}</code> kapanan trade, "
                f"min <code>{config.ml_min_samples}</code> gerekli "
                "(veya class dengesizliği). Paper biriksin sonra tekrar dene."
            )
        save_model(bundle)
        self.ml_bundle = bundle
        self.screener.ml_bundle = bundle
        return (
            f"✅ <b>ML model eğitildi</b>\n"
            f"Sample: <code>{bundle.n_samples}</code>\n"
            f"Test accuracy: <code>{bundle.test_accuracy * 100:.0f}%</code>\n"
            f"Train win rate: <code>{bundle.win_rate * 100:.0f}%</code>\n"
            f"Bot artık skor sistemine <code>ml_predicted</code> "
            f"componentini ekleyecek."
        )

    async def mlstatus_text(self) -> str:
        if not _ML_OK:
            return "⚠️ ML kütüphaneleri yüklü değil (scikit-learn)."
        return format_ml_status(self.ml_bundle)

    # ---------- /pin, /pins, /bandit, /wallets_pool ----------

    async def pin_text(self, arg: str) -> str:
        """
        Kullanım:
          /pin                    → liste
          /pin <ad>               → snapshot al
          /pin show <ad>          → detay
          /pin diff <a> <b>       → karşılaştır
        """
        arg = (arg or "").strip()
        if not arg:
            return format_pins_list(list_pins())

        tokens = arg.split()
        sub = tokens[0]

        if sub == "show" and len(tokens) >= 2:
            p = find_pin(tokens[1])
            if not p:
                return f"⚠️ Pin yok: <code>{tokens[1]}</code>"
            return format_pin_detail(p)

        if sub == "diff" and len(tokens) >= 3:
            a = find_pin(tokens[1])
            b = find_pin(tokens[2])
            if not a:
                return f"⚠️ Pin yok: <code>{tokens[1]}</code>"
            if not b:
                return f"⚠️ Pin yok: <code>{tokens[2]}</code>"
            return format_diff(a, b)

        # Yeni pin oluştur
        name = tokens[0]
        notes = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        paper_pos = self.paper_store.positions if self.paper_store else None
        pin = create_pin(
            name=name,
            real_positions=self.store.positions,
            paper_positions=paper_pos,
            notes=notes,
            by="manual",
        )
        return f"📌 Pin oluşturuldu: <b>{pin.name}</b>"

    async def bandit_text(self) -> str:
        return format_bandit_status(self.bandit_store)

    async def walletpool_text(self) -> str:
        return format_pool_status(self.wallet_pool)

    # ---------- /status ----------

    async def status_text(self) -> str:
        opens = self.store.open_positions()
        if not opens:
            return "📭 Açık pozisyon yok."
        lines = [f"📂 <b>{len(opens)} açık pozisyon</b>\n"]
        for p in opens:
            pair = await self.ds.pair("solana", p.pair_address)
            price_now = float((pair or {}).get("priceUsd") or 0) if pair else 0
            tps = ",".join(str(h.level) for h in p.tp_hits) or "—"
            if price_now > 0:
                pnl = ((price_now - p.entry_price_usd) / p.entry_price_usd) * 100
                lines.append(
                    f"• <b>${p.symbol}</b>  <code>{pnl:+.1f}%</code>  "
                    f"TP:[{tps}]  "
                    f"kalan <code>{p.remaining_raw / max(p.amount_raw, 1) * 100:.0f}%</code>"
                )
            else:
                lines.append(f"• ${p.symbol} (fiyat alınamadı)")
        return "\n".join(lines)

    # ---------- /perf ----------

    async def perf_text(self) -> str:
        s = self.signal_log.stats()
        if s.get("total", 0) == 0:
            pending = len(self.signal_log.pending())
            return (
                f"📊 <b>Sinyal performansı</b>\n"
                f"Henüz finalize sinyal yok (24h beklenir).\n"
                f"Beklemede: <code>{pending}</code>"
            )
        return (
            f"📊 <b>Sinyal performansı</b> (finalize: {s['total']})\n"
            f"Ort. zirve 1h: <code>{s['avg_peak_1h']:+.1f}%</code>\n"
            f"Ort. zirve 24h: <code>{s['avg_peak_24h']:+.1f}%</code>\n"
            f"+30% isabet (24h): <code>{s['hit_rate_30pct_24h']:.0f}%</code>\n"
            f"+100% isabet (24h): <code>{s['hit_rate_100pct_24h']:.0f}%</code>\n"
            f"Beklemede: <code>{s['pending']}</code>"
        )

    # ---------- /pnl ----------

    async def pnl_text(self, days: int) -> str:
        summary = summarize(self.store.positions, days=days)
        return format_report(summary)

    # ---------- /paper ----------

    async def paper_text(self, days: int) -> str:
        if self.paper_store is None:
            return "📭 Paper trading kapalı (PAPER_TRADING_ENABLED=false)."
        summary = summarize(self.paper_store.positions, days=days)
        text = format_report(summary)
        return "🧪 <b>PAPER</b>\n" + text

    # ---------- /macro ----------

    async def macro_text(self) -> str:
        return format_snapshot(latest_snapshot())

    # ---------- /health ----------

    async def health_text(self) -> str:
        last_scan_ago = time.time() - self._last_scan_ts if self._last_scan_ts else -1
        auto = "AÇIK 🤖" if config.auto_trade_enabled else "kapalı"
        smart_line = ""
        if self.smart_store is not None:
            tracked = len(self.smart_store.wallets)
            active = sum(1 for w in self.smart_store.wallets.values() if not w.disabled)
            disabled = tracked - active
            self.smart_store.cleanup_recent_buys()
            tokens_seen = len(self.smart_store.recent_buys)
            smart_line = (
                f"Smart wallet: <code>{active}/{tracked}</code> aktif"
                + (f" (<code>{disabled}</code> disabled)" if disabled else "")
                + f", son {config.smart_buy_window_min}dk içinde "
                f"<code>{tokens_seen}</code> token sinyali\n"
            )
        return (
            f"💓 <b>Bot sağlığı</b>\n"
            f"Cüzdan: <code>{self.wallet_pubkey[:8]}...{self.wallet_pubkey[-6:]}</code>\n"
            f"Son tarama: <code>{last_scan_ago:.0f}s</code> önce "
            f"({self._last_scan_count} aday)\n"
            f"Açık pozisyon: <code>{len(self.store.open_positions())}</code>\n"
            f"Tarama: her <code>{config.scan_interval}s</code>\n"
            f"Pozisyon takip: her <code>{config.monitor_interval}s</code>\n"
            f"Auto-trade: <code>{auto}</code> "
            f"(min skor <code>{config.auto_trade_min_score:.0f}</code>)\n"
            f"{smart_line}\n"
            f"{self.breaker.status_text(self.store.positions)}"
        )

    # ---------- Loop: tarama ----------

    async def scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                candidates = await self.screener.scan()
                self._last_scan_ts = time.time()
                self._last_scan_count = len(candidates)
                log.info("scan: %d candidate(s) pass layer-1", len(candidates))

                sent = 0
                for c in candidates:
                    if sent >= config.max_alerts_per_scan:
                        break

                    # KATMAN 2A: RugCheck + holder dağılımı
                    safety = await self.rug.check(c.base_token)
                    if not safety.passed:
                        log.info("RUG SKIP %s: %s", c.base_symbol, "; ".join(safety.reasons))
                        self.screener.mark_alerted(c.base_token)  # cooldown'a koy ki tekrar gelmesin
                        continue

                    # Skoru güncelle (safety katkısı)
                    c.score_breakdown["holder_health"] = round(safety.score, 1)

                    # KATMAN 2B: Honeypot simülasyonu
                    ok, reason, loss_pct, impact = await self.jup.roundtrip_sim(c.base_token)
                    if not ok:
                        log.info("HONEYPOT SKIP %s: %s", c.base_symbol, reason)
                        self.screener.mark_alerted(c.base_token)
                        continue
                    log.info(
                        "PASS %s score=%.1f+%.1f loss=%.1f%% impact=%.2f%%",
                        c.base_symbol, c.score, safety.score, loss_pct, impact,
                    )

                    await self.tg.alert(c, safety)
                    self.screener.mark_alerted(c.base_token, c.score + safety.score)
                    if self.paper_store is not None:
                        self.paper_store.open(c, safety, config.buy_amount_sol)

                    # Auto-trade: yüksek güven + safety + düşük impact ise
                    # Telegram tap'i beklemeden otomatik al
                    if self._auto_eligible(c, safety, impact):
                        asyncio.create_task(self._auto_buy(c, safety))

                    if config.signal_tracking_enabled:
                        macro_now = latest_snapshot()
                        macro_dict = asdict(macro_now) if macro_now else None
                        self.signal_log.add(
                            token=c.base_token,
                            pair=c.pair_address,
                            symbol=c.base_symbol,
                            profile=c.profile,
                            entry_price_usd=c.price_usd,
                            score=c.score,
                            safety_score=safety.score,
                            score_breakdown=c.score_breakdown,
                            macro=macro_dict,
                        )
                    self._last_alert_ts = time.time()
                    sent += 1

            except Exception:
                log.exception("scan loop error")

            await asyncio.sleep(config.scan_interval)

    # ---------- Loop: pozisyon takibi ----------

    async def monitor_loop(self) -> None:
        prev_closed_pairs: set[str] = {
            p.pair_address for p in self.store.positions if p.status == "closed"
        }
        while not self._stop.is_set():
            try:
                await self.monitor.tick()
                # Bandit: yeni kapanan pozisyonları kaydet (sizing_multiplier varsa)
                if config.sizing_bandit_enabled:
                    for p in self.store.positions:
                        if p.status != "closed":
                            continue
                        if p.pair_address in prev_closed_pairs:
                            continue
                        prev_closed_pairs.add(p.pair_address)
                        try:
                            bandit_update_from_position(self.bandit_store, p)
                        except Exception:
                            log.exception("bandit update failed for %s", p.symbol)
                # Pozisyon kapanmış olabilir → devre kesici eşiklerini değerlendir
                halted_now, reason = self.breaker.check_post_close(self.store.positions)
                if halted_now:
                    await self.tg.info(
                        f"⛔ <b>Devre kesici tetiklendi</b>\n"
                        f"Sebep: <code>{reason}</code>\n"
                        f"<i>/resume ile elle açabilirsin.</i>"
                    )
            except Exception:
                log.exception("monitor loop error")
            await asyncio.sleep(config.monitor_interval)

    # ---------- Loop: paper trading takibi ----------

    async def paper_monitor_loop(self) -> None:
        while not self._stop.is_set():
            if self.paper_monitor is not None:
                try:
                    await self.paper_monitor.tick()
                except Exception:
                    log.exception("paper monitor loop error")
            await asyncio.sleep(config.monitor_interval)

    # ---------- Loop: smart wallet polling ----------

    async def smart_wallet_loop(self) -> None:
        # Bot start'ın hemen ardından bir miktar gecikme
        await asyncio.sleep(10)
        while not self._stop.is_set():
            if self.smart_tracker is not None:
                try:
                    await self.smart_tracker.poll_all()
                except Exception:
                    log.exception("smart wallet loop error")
            await asyncio.sleep(config.smart_wallets_poll_interval)

    # ---------- Loop: pump.fun pre-graduation alerts ----------

    async def prepump_loop(self) -> None:
        await asyncio.sleep(60)
        while not self._stop.is_set():
            if self.prepump is not None:
                try:
                    alerts = await self.prepump.scan()
                    for coin, velocity in alerts:
                        log.info(
                            "prepump alert: $%s mc=$%.0f progress=%.0f%% vel=%.1f/h",
                            coin.symbol, coin.usd_market_cap,
                            coin.progress_pct, velocity,
                        )
                        text = format_prepump_alert(coin, velocity)
                        try:
                            if self.pumpportal is not None:
                                # Inline butonlu alert — manuel onayla PumpPortal alımı
                                await self.tg.pump_alert(
                                    coin.mint, coin.symbol, text,
                                    config.pumpportal_buy_amount_sol,
                                )
                            else:
                                # PumpPortal kapalı → sadece bilgi alert
                                await self.tg.info(text)
                        except Exception:
                            log.exception("prepump telegram alert failed")
                except Exception:
                    log.exception("prepump loop error")
            await asyncio.sleep(config.prepump_check_interval)

    # ---------- Loop: wallet auto-discovery ----------

    async def discovery_loop(self) -> None:
        # İlk çalıştırma sinyal verisi birikene kadar beklesin
        await asyncio.sleep(120)
        while not self._stop.is_set():
            if self.discovery is not None:
                try:
                    promoted_n, promoted = await self.discovery.run_once()
                    if promoted_n > 0:
                        # Telegram bildirimi: terfi edenler
                        lines = [
                            f"🎯 <b>{promoted_n} yeni smart wallet</b> "
                            f"otomatik keşfedildi:"
                        ]
                        for addr, label, hits in promoted[:10]:
                            short = f"{addr[:6]}..{addr[-4:]}"
                            lines.append(
                                f"• <code>{short}</code>  "
                                f"<i>{label}</i>  ({hits} kazanan)"
                            )
                        if promoted_n > 10:
                            lines.append(f"<i>...ve {promoted_n - 10} daha</i>")
                        await self.tg.info("\n".join(lines))
                except Exception:
                    log.exception("discovery loop error")
            await asyncio.sleep(config.discovery_interval)

    # ---------- Loop: wallet outcome tracking + quality scoring ----------

    async def wallet_outcomes_loop(self) -> None:
        await asyncio.sleep(30)
        while not self._stop.is_set():
            if self.outcome_store is not None and self.smart_store is not None:
                try:
                    updated = 0
                    finalized = 0
                    for o in self.outcome_store.pending():
                        pair = await self.ds.pair("solana", o.pair_address)
                        if not pair:
                            continue
                        try:
                            price = float(pair.get("priceUsd") or 0)
                        except (TypeError, ValueError):
                            price = 0.0
                        if price > 0 and o.entry_price_usd > 0:
                            pct = (price - o.entry_price_usd) / o.entry_price_usd * 100
                            age_h = (time.time() - o.entry_ts) / 3600
                            if age_h <= 24 and pct > o.peak_pct_24h:
                                o.peak_pct_24h = pct
                                o.peak_price_24h = price
                            if age_h > 24:
                                o.final_24h = True
                                finalized += 1
                            o.last_check_ts = time.time()
                            updated += 1
                    if updated:
                        self.outcome_store.save()
                    if finalized:
                        new_disabled = update_wallet_stats(
                            self.smart_store, self.outcome_store,
                        )
                        log.info(
                            "wallet outcomes: %d updated, %d finalized, %d newly disabled",
                            updated, finalized, new_disabled,
                        )
                        if new_disabled > 0:
                            await self.tg.info(
                                f"⚠️ <b>{new_disabled} smart wallet</b> kalite eşiğinin "
                                f"altına düştüğü için otomatik disable edildi. "
                                f"Detay için /wallets."
                            )
                except Exception:
                    log.exception("wallet outcomes loop error")
            await asyncio.sleep(config.wallet_outcomes_interval)

    # ---------- Loop: makro snapshot ----------

    async def macro_loop(self) -> None:
        # İlk snapshot biraz beklesin — diğer init işleri bitsin
        await asyncio.sleep(15)
        while not self._stop.is_set():
            if self.macro is not None:
                try:
                    snap = await self.macro.collect()
                    append_snapshot(snap)
                    log.info(
                        "macro: SOL=%.2f (%.1f%%) BTC.D=%.1f F&G=%d pump=%d",
                        snap.sol_price_usd, snap.sol_change_24h,
                        snap.btc_dominance, snap.fear_greed,
                        snap.pump_graduated_recent,
                    )
                except Exception:
                    log.exception("macro loop error")
            await asyncio.sleep(config.macro_snapshot_interval)

    # ---------- Loop: sinyal performans takibi (backtest data) ----------

    async def tracking_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(config.signal_tracking_interval)
            if not config.signal_tracking_enabled:
                continue
            try:
                pending = self.signal_log.pending()
                if not pending:
                    continue
                updated = 0
                for sig in pending:
                    pair = await self.ds.pair("solana", sig.pair)
                    price = float((pair or {}).get("priceUsd") or 0) if pair else 0
                    if price > 0:
                        self.signal_log.update_with_price(sig, price)
                        updated += 1
                if updated:
                    self.signal_log.save()
                    log.info("signal tracking: updated %d/%d pending", updated, len(pending))
            except Exception:
                log.exception("tracking loop error")

    # ---------- Loop: heartbeat ----------

    async def heartbeat_loop(self) -> None:
        # Sadece iç log; spam olmaması için Telegram'a göndermez.
        while not self._stop.is_set():
            await asyncio.sleep(config.heartbeat_interval)
            log.info(
                "♥ heartbeat | open=%d last_scan=%.0fs ago",
                len(self.store.open_positions()),
                time.time() - self._last_scan_ts if self._last_scan_ts else -1,
            )

    # ---------- Lifecycle ----------

    async def run(self) -> None:
        set_buy_callback(self.on_buy)
        set_pump_buy_callback(self.on_pump_buy)
        self.tg.set_status_callback(self.status_text)
        self.tg.set_health_callback(self.health_text)
        self.tg.set_perf_callback(self.perf_text)
        self.tg.set_pnl_callback(self.pnl_text)
        self.tg.set_paper_callback(self.paper_text)
        self.tg.set_macro_callback(self.macro_text)
        self.tg.set_halt_callback(self.halt_text)
        self.tg.set_resume_callback(self.resume_text)
        self.tg.set_close_callback(self.close_text)
        self.tg.set_analog_callback(self.analog_text)
        self.tg.set_wallets_callback(self.wallets_text)
        self.tg.set_addwallet_callback(self.addwallet_text)
        self.tg.set_rmwallet_callback(self.rmwallet_text)
        self.tg.set_candidates_callback(self.candidates_text)
        self.tg.set_train_callback(self.train_text)
        self.tg.set_mlstatus_callback(self.mlstatus_text)
        self.tg.set_pin_callback(self.pin_text)
        self.tg.set_bandit_callback(self.bandit_text)
        self.tg.set_walletpool_callback(self.walletpool_text)

        await self.tg.start()
        await self.tg.info(
            f"🤖 <b>Memecoin Sniper başladı</b>\n"
            f"Cüzdan: <code>{self.wallet_pubkey[:8]}...{self.wallet_pubkey[-6:]}</code>\n"
            f"Tarama her {config.scan_interval}s, min skor {config.min_score_to_alert}\n"
            f"Auto-trade: <code>{'AÇIK' if config.auto_trade_enabled else 'kapalı'}</code>  "
            f"Devre kesici: <code>{'açık' if self.breaker.is_open() else 'kapalı'}</code>\n"
            f"Komutlar: alttaki butonlar veya menu ikonundan",
            with_keyboard=True,
        )

        # Sinyal yakalama (Render restart için graceful shutdown)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass

        tasks = [
            asyncio.create_task(self.scan_loop(), name="scan"),
            asyncio.create_task(self.monitor_loop(), name="monitor"),
            asyncio.create_task(self.heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self.tracking_loop(), name="tracking"),
        ]
        if self.paper_monitor is not None:
            tasks.append(asyncio.create_task(self.paper_monitor_loop(), name="paper"))
        if self.macro is not None:
            tasks.append(asyncio.create_task(self.macro_loop(), name="macro"))
        if self.smart_tracker is not None:
            tasks.append(asyncio.create_task(self.smart_wallet_loop(), name="smart"))
            tasks.append(asyncio.create_task(self.wallet_outcomes_loop(), name="wallet_outcomes"))
        if self.discovery is not None:
            tasks.append(asyncio.create_task(self.discovery_loop(), name="discovery"))
        if self.prepump is not None:
            tasks.append(asyncio.create_task(self.prepump_loop(), name="prepump"))
        if self.pumpportal is not None:
            tasks.append(asyncio.create_task(self.pump_monitor_loop(), name="pump_monitor"))
        if config.pin_auto_enabled:
            tasks.append(asyncio.create_task(self.auto_pin_loop(), name="auto_pin"))

        await self._stop.wait()
        log.info("shutting down...")

        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await self.tg.info("🛑 Bot durduruldu (graceful).")
        except Exception:
            pass

        await self.tg.stop()
        await self.ds.close()
        await self.jup.close()
        await self.rug.close()
        if self.pf is not None:
            await self.pf.close()
        if self.macro is not None:
            await self.macro.close()
        if self.helius is not None:
            await self.helius.close()
        if self.lunar is not None:
            await self.lunar.close()
        if self.pumpportal is not None:
            await self.pumpportal.close()


def main() -> None:
    asyncio.run(Bot().run())


if __name__ == "__main__":
    main()
