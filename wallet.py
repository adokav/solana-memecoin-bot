"""Cüzdan yükleyici."""
import base58
from solders.keypair import Keypair

from config import config


def load_keypair() -> Keypair:
    """Base58 string'ten Keypair (Phantom Export Private Key formatı)."""
    raw = base58.b58decode(config.wallet_private_key.strip())
    return Keypair.from_bytes(raw)
