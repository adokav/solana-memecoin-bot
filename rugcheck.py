"""KATMAN 2: Dolandırıcılık filtreleri.

Kaynaklar:
  - RugCheck.xyz: mint/freeze authority, LP locked %, transfer fee, risks listesi
  - Helius DAS API: holder dağılımı

Geçemezse aday düşer (HARD eler).
"""
import logging
import time
from dataclasses import dataclass, field

import httpx

from config import config

log = logging.getLogger(__name__)

RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
HELIUS_RPC = "https://mainnet.helius-rpc.com"


@dataclass
class SafetyReport:
    passed: bool
    score: float = 0  # 0-10 ekstra puan (skor sistemine eklenir)
    reasons: list[str] = field(default_factory=list)  # neden düştü
    notes: list[str] = field(default_factory=list)   # bilgi notları (geçti ama dikkat)
    mint_revoked: bool | None = None
    freeze_revoked: bool | None = None
    lp_locked_pct: float | None = None
    lp_lock_days_remaining: float | None = None
    insider_supply_pct: float | None = None
    top10_pct: float | None = None
    top1_pct: float | None = None
    holder_count: int | None = None
    danger_risks: list[str] = field(default_factory=list)
    creator: str | None = None
    creator_token_count: int | None = None


class RugCheckClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=20.0,
            headers={"Accept": "application/json", "User-Agent": "memecoin-bot/1.0"},
        )
        # token -> (timestamp, SafetyReport) basit cache
        self._cache: dict[str, tuple[float, SafetyReport]] = {}
        self._cache_ttl = 600  # 10 dakika
        # mint -> (timestamp, full_report_dict | None)  full report cache (10dk)
        self._full_cache: dict[str, tuple[float, dict | None]] = {}
        # mint -> [(ts, holder_count), ...]  holder sayısı zaman serisi
        self._holder_history: dict[str, list[tuple[float, int]]] = {}
        # creator_addr -> (ts, token_count)  creator sorgu cache (24h)
        self._creator_cache: dict[str, tuple[float, int]] = {}
        self._creator_cache_ttl = 86400

    def _record_holders(self, mint: str, count: int) -> None:
        now = time.time()
        cutoff = now - config.holder_history_window_min * 60
        hist = [(ts, n) for ts, n in self._holder_history.get(mint, []) if ts > cutoff]
        hist.append((now, count))
        self._holder_history[mint] = hist

    def _holder_growth_pct(self, mint: str, current: int) -> float | None:
        """Yeterince eski snapshot varsa % değişim (pozitif=büyüme). Yoksa None."""
        hist = self._holder_history.get(mint) or []
        if not hist:
            return None
        oldest_ts, oldest_n = hist[0]
        if (time.time() - oldest_ts) < config.holder_history_min_age_min * 60:
            return None
        if oldest_n <= 0:
            return None
        return (current - oldest_n) / oldest_n * 100

    async def close(self) -> None:
        await self._http.aclose()

    # ---------- RugCheck ----------

    async def _rugcheck_report(self, mint: str) -> dict | None:
        try:
            r = await self._http.get(f"{RUGCHECK_BASE}/tokens/{mint}/report/summary")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            log.warning("rugcheck error for %s: %s", mint, e)
            return None

    async def _rugcheck_full_report(self, mint: str) -> dict | None:
        cached = self._full_cache.get(mint)
        if cached and time.time() - cached[0] < self._cache_ttl:
            return cached[1]
        try:
            r = await self._http.get(f"{RUGCHECK_BASE}/tokens/{mint}/report")
            if r.status_code == 404:
                self._full_cache[mint] = (time.time(), None)
                return None
            r.raise_for_status()
            data = r.json()
            self._full_cache[mint] = (time.time(), data)
            return data
        except httpx.HTTPError as e:
            log.debug("rugcheck full report error for %s: %s", mint, e)
            # Hata da cache'lensin ki rate limit storm'unda her tarama tekrar denemesin
            self._full_cache[mint] = (time.time(), None)
            return None

    async def _enrich_from_full(self, mint: str, summary: dict) -> dict:
        """Summary'de eksik field'ları full report'tan tamamla.
        Full report'u SADECE summary'de gerçekten eksik bir field varsa çek.
        """
        # Hangi field'lar gerekli, yapılandırmaya göre belirle
        needed: list[str] = ["insiderNetworks"]  # insider check her zaman aktif
        if config.dev_wallet_check_enabled:
            needed.extend(["creator", "creatorTokens"])

        if all(summary.get(k) is not None for k in needed):
            return summary

        full = await self._rugcheck_full_report(mint)
        if not full:
            return summary
        merged = dict(summary)
        for key in ("creator", "deployer", "creatorTokens", "insiderNetworks", "topHolders"):
            if merged.get(key) is None and full.get(key) is not None:
                merged[key] = full[key]
        return merged

    def _parse_creator(self, rc: dict) -> tuple[str | None, int | None]:
        """RugCheck verisinden creator + deploy ettiği token sayısı."""
        creator = rc.get("creator") or rc.get("deployer")
        if not creator:
            return None, None
        # Cache
        cached = self._creator_cache.get(creator)
        if cached and time.time() - cached[0] < self._creator_cache_ttl:
            return creator, cached[1]
        ct = rc.get("creatorTokens")
        if isinstance(ct, list):
            count = len(ct)
        elif isinstance(ct, int):
            count = ct
        else:
            return creator, None
        self._creator_cache[creator] = (time.time(), count)
        return creator, count

    def _parse_insider_pct(self, rc: dict) -> float | None:
        """Insider network'ün toplam supply yüzdesi.
        RugCheck'in 'insiderNetworks' veya topHolders[].insider flag'inden derler.
        """
        pct = 0.0
        found = False
        for net in rc.get("insiderNetworks") or []:
            v = net.get("tokenPctSupply") or net.get("supplyPct") or net.get("pct")
            if v is not None:
                try:
                    pct += float(v)
                    found = True
                except (TypeError, ValueError):
                    pass
        if not found:
            # Fallback: topHolders içinde 'insider' flag'i taşıyanların toplamı
            for h in rc.get("topHolders") or []:
                if h.get("insider"):
                    v = h.get("pct") or h.get("uiAmountPct")
                    if v is not None:
                        try:
                            pct += float(v)
                            found = True
                        except (TypeError, ValueError):
                            pass
        return pct if found else None

    # ---------- Helius DAS holder dağılımı ----------

    async def _helius_holders(self, mint: str) -> dict | None:
        if not config.helius_api_key:
            return None
        try:
            url = f"{HELIUS_RPC}/?api-key={config.helius_api_key}"
            payload = {
                "jsonrpc": "2.0",
                "id": "1",
                "method": "getTokenLargestAccounts",
                "params": [mint],
            }
            r = await self._http.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            return data.get("result")
        except httpx.HTTPError as e:
            log.warning("helius holder error: %s", e)
            return None

    async def _token_supply(self, mint: str) -> float | None:
        if not config.helius_api_key:
            return None
        try:
            url = f"{HELIUS_RPC}/?api-key={config.helius_api_key}"
            payload = {
                "jsonrpc": "2.0",
                "id": "1",
                "method": "getTokenSupply",
                "params": [mint],
            }
            r = await self._http.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            ui = (((data.get("result") or {}).get("value") or {}).get("uiAmount"))
            return float(ui) if ui is not None else None
        except (httpx.HTTPError, ValueError) as e:
            log.warning("helius supply error: %s", e)
            return None

    # ---------- Ana check ----------

    async def check(self, mint: str) -> SafetyReport:
        # Cache
        cached = self._cache.get(mint)
        if cached and time.time() - cached[0] < self._cache_ttl:
            return cached[1]

        report = SafetyReport(passed=True)

        # === RugCheck ===
        rc = await self._rugcheck_report(mint)
        if not rc:
            # RugCheck cevap vermezse risk al, geç
            report.passed = False
            report.reasons.append("rugcheck unavailable")
            self._cache[mint] = (time.time(), report)
            return report

        # Mint authority revoked?
        # RugCheck'in 'tokenMeta' veya 'mintAuthority' field'ı kullanılır
        # API format: {"mintAuthority": null} -> revoked, dolu -> revoked değil
        mint_auth = rc.get("mintAuthority")
        report.mint_revoked = mint_auth is None or mint_auth == ""
        if config.require_mint_revoked and not report.mint_revoked:
            report.passed = False
            report.reasons.append("mint authority active")

        # Freeze authority?
        freeze_auth = rc.get("freezeAuthority")
        report.freeze_revoked = freeze_auth is None or freeze_auth == ""
        if config.require_freeze_revoked and not report.freeze_revoked:
            report.passed = False
            report.reasons.append("freeze authority active")

        # Transfer fee
        transfer_fee = rc.get("transferFee") or {}
        fee_pct = float(transfer_fee.get("pct") or 0)
        if fee_pct > 0:
            report.passed = False
            report.reasons.append(f"transfer fee {fee_pct}%")

        # LP locked: yüzde + kalan süre
        markets = rc.get("markets") or []
        lp_locked_pct = 0.0
        lp_lock_until_ts = 0.0  # epoch saniye
        if markets:
            for m in markets:
                lp = m.get("lp") or {}
                pct = float(lp.get("lpLockedPct") or 0)
                lp_locked_pct = max(lp_locked_pct, pct)
                # "lpLockedUntil" ms ya da s olabilir; defensive normalize
                until = lp.get("lpLockedUntil") or lp.get("lockUntil") or lp.get("unlockDate") or 0
                try:
                    until_f = float(until or 0)
                except (TypeError, ValueError):
                    until_f = 0
                if until_f > 1e12:  # ms epoch
                    until_f /= 1000
                lp_lock_until_ts = max(lp_lock_until_ts, until_f)
        report.lp_locked_pct = lp_locked_pct
        if config.require_lp_locked and lp_locked_pct < config.min_lp_locked_pct:
            report.passed = False
            report.reasons.append(f"LP locked only {lp_locked_pct:.0f}%")
        # LP kilit süresi (varsa kontrol et; yoksa atla — burnt LP de olabilir)
        if lp_lock_until_ts > 0:
            days_left = (lp_lock_until_ts - time.time()) / 86400
            report.lp_lock_days_remaining = round(days_left, 1)
            if days_left < config.min_lp_lock_days:
                report.passed = False
                report.reasons.append(f"LP unlocks in {days_left:.0f}d")

        # Risks array
        risks = rc.get("risks") or []
        for risk in risks:
            level = (risk.get("level") or "").lower()
            name = risk.get("name", "")
            if level == "danger":
                report.danger_risks.append(name)
                report.passed = False
                report.reasons.append(f"danger: {name}")
            elif level == "warn":
                report.notes.append(f"warn: {name}")

        # === Full report'tan creator + insider'ı tek API çağrısıyla zenginleştir ===
        rc = await self._enrich_from_full(mint, rc)

        # Dev wallet (creator): serial rugger detection
        if config.dev_wallet_check_enabled:
            creator, count = self._parse_creator(rc)
            report.creator = creator
            report.creator_token_count = count
            if count is not None and count > config.max_creator_tokens:
                report.passed = False
                report.reasons.append(f"creator has {count} tokens (serial)")

        # Insider network: aynı kaynaktan finanse edilmiş cüzdan kümeleri
        insider_pct = self._parse_insider_pct(rc)
        report.insider_supply_pct = insider_pct
        if insider_pct is not None and insider_pct > config.max_insider_supply_pct:
            report.passed = False
            report.reasons.append(f"insider network holds {insider_pct:.1f}%")

        # === Helius: holder dağılımı ===
        holders_data = await self._helius_holders(mint)
        supply = await self._token_supply(mint)

        if holders_data and supply and supply > 0:
            accounts = holders_data.get("value", []) or []
            # Tek tek yüzdeleri hesapla
            holder_pcts = []
            for acc in accounts[:20]:
                amount = float(acc.get("uiAmount") or 0)
                if amount > 0:
                    holder_pcts.append((amount / supply) * 100)
            holder_pcts.sort(reverse=True)

            if holder_pcts:
                # NOT: top1 genellikle LP havuzu olur, onu filtrelemeli
                # Basit yaklaşım: LP havuzu büyüklüğü tahminen %30-60 arası,
                # bunu cüzdan kabul etmiyoruz. Daha sağlamı RugCheck'in topHolders
                # field'ından insider'ları öğrenmek.
                non_lp = [p for p in holder_pcts if p < 50]  # LP'yi at
                if non_lp:
                    report.top1_pct = non_lp[0]
                    report.top10_pct = sum(non_lp[:10])

                    if report.top1_pct > config.max_top1_holder_pct:
                        report.passed = False
                        report.reasons.append(f"top1 holder {report.top1_pct:.1f}%")
                    if report.top10_pct > config.max_top10_holder_pct:
                        report.passed = False
                        report.reasons.append(f"top10 holders {report.top10_pct:.1f}%")

        # Holder sayısı - RugCheck'ten
        total_holders = rc.get("totalHolders") or rc.get("holderCount") or 0
        holder_growth_pct: float | None = None
        if total_holders:
            report.holder_count = int(total_holders)
            if report.holder_count < config.min_holder_count:
                report.passed = False
                report.reasons.append(f"only {report.holder_count} holders")
            # Holder büyüme takibi: belirgin düşüş = insider exit sinyali
            self._record_holders(mint, report.holder_count)
            holder_growth_pct = self._holder_growth_pct(mint, report.holder_count)
            if holder_growth_pct is not None and holder_growth_pct < -config.max_holder_drop_pct:
                report.passed = False
                report.reasons.append(f"holders dropped {holder_growth_pct:.1f}%")

        # === Skor katkısı (max 10) ===
        if report.passed:
            sc = 0.0
            if report.mint_revoked:
                sc += 2
            if report.freeze_revoked:
                sc += 2
            if lp_locked_pct >= 99:
                sc += 3
            elif lp_locked_pct >= 95:
                sc += 2
            if report.top10_pct is not None and report.top10_pct < 15:
                sc += 2
            elif report.top10_pct is not None and report.top10_pct < 25:
                sc += 1
            if report.holder_count and report.holder_count > 500:
                sc += 1
            # Pozitif holder büyümesi bonus (organik ilgi)
            if holder_growth_pct is not None and holder_growth_pct > 5:
                sc += 1
            report.score = min(10.0, sc)

        self._cache[mint] = (time.time(), report)
        return report
