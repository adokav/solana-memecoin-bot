"""Token sector classification — basit keyword tabanlı.

Memecoin'de sector'lar güçlü korelasyon yaratır: AI narrative ısındığında
tüm AI tokenları beraber pumplar. Aynı sector'den fazla pozisyon = riskli
konsantrasyon.

classify(symbol, name) → sector etiketi (eşleşme yoksa "other")

Sector listesi geliştirilebilir. Veri biriktikçe (paper data analizi) yeni
sector'lar eklenebilir veya keyword'ler revize edilir.
"""
from __future__ import annotations

import re


SECTOR_KEYWORDS: dict[str, list[str]] = {
    "ai": ["ai", "gpt", "brain", "neural", "agi", "llm", "robot", "agent"],
    "dog": ["doge", "shib", "shiba", "inu", "bonk", "wif", "puppy", "akita", "corgi", "husky"],
    "cat": ["cat", "meow", "kitty", "feline", "neko", "purr"],
    "frog": ["pepe", "frog", "kek", "wojak"],
    "political": ["trump", "biden", "kamala", "tate", "obama", "harris", "potus", "maga"],
    "anime": ["anime", "naruto", "otaku", "kawaii", "chan", "kun", "manga"],
    "food": ["pizza", "banana", "burger", "taco", "soup", "rice", "noodle", "donut"],
    "tech": ["bitcoin", "satoshi", "vitalik", "elon", "musk", "tesla", "moon", "rocket"],
    "celeb": ["kanye", "ye", "drake", "kendrick", "swift", "rihanna"],
    "religion": ["jesus", "god", "allah", "buddha"],
}


# Keyword cache: lower-case + word boundary regex
_COMPILED: dict[str, list[re.Pattern]] = {
    sector: [re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE) for kw in kws]
    for sector, kws in SECTOR_KEYWORDS.items()
}


def classify(symbol: str, name: str = "") -> str:
    """Symbol ve (varsa) name'den sector çıkar. Eşleşme yoksa 'other'.

    Birden fazla sector eşleşirse SECTOR_KEYWORDS sırasındaki ilk eşleşme döner.
    """
    text = f"{symbol or ''} {name or ''}"
    for sector, patterns in _COMPILED.items():
        for p in patterns:
            if p.search(text):
                return sector
    return "other"


def all_sectors() -> list[str]:
    return list(SECTOR_KEYWORDS.keys()) + ["other"]
