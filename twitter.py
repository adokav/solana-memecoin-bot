"""Twitter influencer scanner (Nitter RSS best-effort).

Twitter resmi API ücretli ($100/ay), Twitter scraping yasak ve fragile.
Pratik orta yol: Nitter public instance'larından RSS feed çek.
  - Nitter open-source Twitter frontend
  - /<user>/rss → atom feed
  - Mirror'lar zaman zaman düşer; bir tane verir, hata varsa skip

Akış:
  1. TWITTER_HANDLES env'inden takip edilecek influencer'lar (örn KOL'lar)
  2. Her TWITTER_POLL_INTERVAL'da hepsinin son tweet'lerini çek
  3. $SYMBOL veya Solana contract address mention'larını çıkar
  4. recent_mentions: {token_mint_or_symbol: [(handle, ts), ...]}
  5. Screener'a bonus skor olarak feed

Default kapalı. Aktif etmeden TWITTER_HANDLES set edilmeli.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import httpx

from config import config

log = logging.getLogger(__name__)

# Solana base58 mint adresi heuristic: 32-44 karakter base58
SOLANA_MINT_RE = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")
# $SYMBOL — 2-12 karakter alfa-numerik
TICKER_RE = re.compile(r"\$([A-Za-z0-9]{2,12})\b")


@dataclass
class Mention:
    handle: str
    key: str  # token_mint veya $SYMBOL
    ts: float
    tweet_id: str = ""


@dataclass
class TwitterStore:
    # key (mint veya $SYMBOL) → recent mentions
    mentions: dict[str, list[Mention]] = field(default_factory=dict)
    # handle → last processed tweet_id
    last_seen: dict[str, str] = field(default_factory=dict)

    def cleanup(self) -> None:
        cutoff = time.time() - 6 * 3600  # 6h sliding window
        for key in list(self.mentions.keys()):
            self.mentions[key] = [m for m in self.mentions[key] if m.ts > cutoff]
            if not self.mentions[key]:
                del self.mentions[key]

    def record(self, mention: Mention) -> None:
        self.mentions.setdefault(mention.key, []).append(mention)

    def mentions_for(self, key: str) -> list[Mention]:
        self.cleanup()
        return list(self.mentions.get(key, []))

    def unique_handles_for(self, key: str) -> int:
        return len({m.handle for m in self.mentions_for(key)})


class TwitterScanner:
    def __init__(self) -> None:
        self.store = TwitterStore()
        self._http = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
                ),
                "Accept": "application/rss+xml,application/xml",
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _fetch_user_rss(self, handle: str) -> list[dict]:
        url = f"{config.twitter_nitter_base.rstrip('/')}/{handle}/rss"
        try:
            r = await self._http.get(url)
            if r.status_code != 200:
                log.debug("nitter %s -> %d", handle, r.status_code)
                return []
            root = ET.fromstring(r.text)
            items = []
            for item in root.findall(".//item"):
                guid_el = item.find("guid")
                desc_el = item.find("description")
                pub_el = item.find("pubDate")
                if guid_el is None or desc_el is None:
                    continue
                tweet_id = (guid_el.text or "").rstrip("#m")
                items.append({
                    "tweet_id": tweet_id,
                    "text": desc_el.text or "",
                    "pub": pub_el.text if pub_el is not None else "",
                })
            return items
        except (httpx.HTTPError, ET.ParseError) as e:
            log.warning("nitter %s fetch error: %s", handle, e)
            return []

    @staticmethod
    def _extract_mentions(text: str) -> list[str]:
        """Tweet metninden $SYMBOL veya Solana mint çıkarır."""
        keys: list[str] = []
        for m in SOLANA_MINT_RE.findall(text):
            keys.append(m)
        for m in TICKER_RE.findall(text):
            keys.append("$" + m.upper())
        return keys

    async def poll_handle(self, handle: str) -> int:
        items = await self._fetch_user_rss(handle)
        if not items:
            return 0
        last_seen_id = self.store.last_seen.get(handle, "")
        new_mention_count = 0
        for item in items:
            tweet_id = item["tweet_id"]
            if not tweet_id:
                continue
            if last_seen_id and tweet_id == last_seen_id:
                break
            keys = self._extract_mentions(item["text"])
            for k in keys:
                self.store.record(Mention(
                    handle=handle, key=k, ts=time.time(),
                    tweet_id=tweet_id,
                ))
                new_mention_count += 1
        if items:
            self.store.last_seen[handle] = items[0]["tweet_id"]
        return new_mention_count

    async def poll_all(self) -> int:
        if not config.twitter_handles:
            return 0
        handles = [
            h.strip().lstrip("@") for h in config.twitter_handles.split(",")
            if h.strip()
        ]
        total = 0
        for h in handles:
            try:
                total += await self.poll_handle(h)
            except Exception:
                log.exception("twitter poll error %s", h)
            await asyncio.sleep(1.0)  # rate-limit nezaketi
        self.store.cleanup()
        if total > 0:
            log.info(
                "twitter: %d new mentions, %d tokens tracked",
                total, len(self.store.mentions),
            )
        return total


def format_twitter_status(store: TwitterStore) -> str:
    store.cleanup()
    if not store.mentions:
        return (
            "🐦 <b>Twitter mentions</b>\n"
            "Henüz mention yok. (TWITTER_ENABLED=true ve "
            "TWITTER_HANDLES set olmalı)"
        )
    items = sorted(
        store.mentions.items(),
        key=lambda kv: -len({m.handle for m in kv[1]}),
    )
    lines = [f"🐦 <b>Twitter mentions</b> (son 6h, {len(store.mentions)} token)"]
    for key, mentions in items[:20]:
        handles = sorted({m.handle for m in mentions})
        display_key = key if key.startswith("$") else f"{key[:6]}..{key[-4:]}"
        lines.append(
            f"• <code>{display_key}</code>  "
            f"<b>{len(handles)}</b> handle  "
            f"({len(mentions)} mention)\n"
            f"  <i>{', '.join('@' + h for h in handles[:5])}</i>"
        )
    return "\n".join(lines)
