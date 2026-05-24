"""Telegram public channel monitor (HTML preview scraping).

Memecoin alfa'sının önemli bir kısmı Telegram'da. Telethon (MT Proto) auth
gerektirir (phone + code, headless setup karmaşık). Daha pragmatik:
public channel'lar için Telegram'ın preview sayfası (`https://t.me/s/<channel>`)
HTML olarak gelir, hiç auth gerekmez.

Akış:
  1. TELEGRAM_CHANNELS env (örn "alpha1,memetrenches,solanaalphas") set edilir
  2. Her TELEGRAM_CHANNELS_POLL_INTERVAL'da her channel için /s/<name> çek
  3. <div class="tgme_widget_message"> blokları, içlerinde son mesajlar
  4. $SYMBOL ve Solana mint mention'larını çıkar
  5. Twitter ile aynı interface — Screener'a bonus skor

NOT: Private channel'lar bu yöntemle erişilemez — Telethon gerekir.
Public channel'lar tipik memecoin alfa kanalları için yeterli.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

from config import config

log = logging.getLogger(__name__)

# Solana mint adresi heuristic + $SYMBOL — twitter.py ile aynı
SOLANA_MINT_RE = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")
TICKER_RE = re.compile(r"\$([A-Za-z0-9]{2,12})\b")

# Mesaj data-post ile gelir: <div class="tgme_widget_message" data-post="channel/123">
MESSAGE_BLOCK_RE = re.compile(
    r'<div\s+class="tgme_widget_message[^"]*"[^>]*data-post="([^"]+)"',
    re.DOTALL,
)
# Mesaj metni — <div class="tgme_widget_message_text">...</div>
MESSAGE_TEXT_RE = re.compile(
    r'<div\s+class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL,
)
HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class TGMention:
    channel: str
    key: str
    ts: float
    post_id: str = ""


@dataclass
class TelegramMentionStore:
    mentions: dict[str, list[TGMention]] = field(default_factory=dict)
    # channel → last_processed_post_id
    last_seen: dict[str, str] = field(default_factory=dict)

    def cleanup(self) -> None:
        cutoff = time.time() - 6 * 3600
        for key in list(self.mentions.keys()):
            self.mentions[key] = [m for m in self.mentions[key] if m.ts > cutoff]
            if not self.mentions[key]:
                del self.mentions[key]

    def record(self, mention: TGMention) -> None:
        self.mentions.setdefault(mention.key, []).append(mention)

    def mentions_for(self, key: str) -> list[TGMention]:
        self.cleanup()
        return list(self.mentions.get(key, []))

    def unique_channels_for(self, key: str) -> int:
        return len({m.channel for m in self.mentions_for(key)})


def _strip_html(s: str) -> str:
    return HTML_TAG_RE.sub("", s).strip()


def _extract_keys(text: str) -> list[str]:
    keys: list[str] = []
    for m in SOLANA_MINT_RE.findall(text):
        keys.append(m)
    for m in TICKER_RE.findall(text):
        keys.append("$" + m.upper())
    return keys


class TelegramChannelScanner:
    def __init__(self) -> None:
        self.store = TelegramMentionStore()
        self._http = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
                ),
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _fetch_channel_html(self, channel: str) -> str | None:
        url = f"https://t.me/s/{channel}"
        try:
            r = await self._http.get(url)
            if r.status_code != 200:
                log.debug("t.me/s/%s -> %d", channel, r.status_code)
                return None
            return r.text
        except httpx.HTTPError as e:
            log.warning("t.me/s/%s fetch error: %s", channel, e)
            return None

    @staticmethod
    def _parse_messages(html: str) -> list[tuple[str, str]]:
        """HTML'den (post_id, text) listesi çıkar. Newest first."""
        posts: list[tuple[str, str]] = []
        # post id'leri ve text bloklarını ayrı yakala, sıralarına göre eşleştir
        post_ids = MESSAGE_BLOCK_RE.findall(html)
        text_blocks = MESSAGE_TEXT_RE.findall(html)
        # Aynı uzunlukta olmayabilir (medya-only mesajlar text içermez)
        # Bu nedenle her message_block'tan sonra ilk text_block'u eşliyoruz
        # Basit yaklaşım: paralel iter, kısa olana göre
        n = min(len(post_ids), len(text_blocks))
        for i in range(n):
            text = _strip_html(text_blocks[i])
            if text:
                posts.append((post_ids[i], text))
        # Telegram preview en eski → en yeni sırayla verir; ters çevir
        return list(reversed(posts))

    async def poll_channel(self, channel: str) -> int:
        html = await self._fetch_channel_html(channel)
        if not html:
            return 0
        posts = self._parse_messages(html)
        if not posts:
            return 0
        last_seen = self.store.last_seen.get(channel, "")
        new_count = 0
        for post_id, text in posts:
            if last_seen and post_id == last_seen:
                break
            for key in _extract_keys(text):
                self.store.record(TGMention(
                    channel=channel, key=key, ts=time.time(),
                    post_id=post_id,
                ))
                new_count += 1
        if posts:
            self.store.last_seen[channel] = posts[0][0]
        return new_count

    async def poll_all(self) -> int:
        if not config.telegram_channels:
            return 0
        channels = [
            c.strip().lstrip("@") for c in config.telegram_channels.split(",")
            if c.strip()
        ]
        total = 0
        for ch in channels:
            try:
                total += await self.poll_channel(ch)
            except Exception:
                log.exception("telegram channel poll error %s", ch)
            await asyncio.sleep(1.0)
        self.store.cleanup()
        if total > 0:
            log.info(
                "telegram channels: %d new mentions, %d tokens tracked",
                total, len(self.store.mentions),
            )
        return total


def format_telegram_status(store: TelegramMentionStore) -> str:
    store.cleanup()
    if not store.mentions:
        return (
            "💬 <b>Telegram channel mentions</b>\n"
            "Henüz mention yok. (TELEGRAM_CHANNELS_ENABLED=true ve "
            "TELEGRAM_CHANNELS set olmalı)"
        )
    items = sorted(
        store.mentions.items(),
        key=lambda kv: -len({m.channel for m in kv[1]}),
    )
    lines = [
        f"💬 <b>Telegram channel mentions</b> (son 6h, "
        f"{len(store.mentions)} token)"
    ]
    for key, mentions in items[:20]:
        channels = sorted({m.channel for m in mentions})
        display_key = key if key.startswith("$") else f"{key[:6]}..{key[-4:]}"
        lines.append(
            f"• <code>{display_key}</code>  "
            f"<b>{len(channels)}</b> channel "
            f"({len(mentions)} mention)\n"
            f"  <i>{', '.join('@' + c for c in channels[:5])}</i>"
        )
    return "\n".join(lines)
