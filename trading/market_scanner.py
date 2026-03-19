"""
market_scanner.py  ·  Polymarket Trading Bot
─────────────────────────────────────────────
Scanner de marchés Polymarket via Gamma API.
Responsabilité unique : trouver les candidats pour S1 et S2.
Le scoring probabiliste est délégué à prob_model.py.

Basé exactement sur la spécification de polymarket_strategies.md.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from core.config import (
    GAMMA_API,
    MARKET_BLACKLIST_KEYWORDS,
    MARKET_BLACKLIST_SOURCES,
    S1_VOL_MIN, S1_VOL_MAX, S1_DAYS_MIN, S1_DAYS_MAX,
    S1_PRICE_FAV_MIN, S1_PRICE_FAV_MAX, S1_PRICE_LONG_MIN, S1_PRICE_LONG_MAX,
    S2_VOL_MIN, S2_DAYS_MIN, S2_DAYS_MAX, S2_PRICE_MIN, S2_PRICE_MAX,
    S2_CATEGORIES,
)

# Threshold for classifying a market as a longshot (price below this level)
S1_LONGSHOT_THRESHOLD = S1_PRICE_LONG_MAX  # 0.10 from config

log = logging.getLogger("scanner")


# ═══════════════════════════════════════════════════════════════════════════
# MARKET SCANNER
# ═══════════════════════════════════════════════════════════════════════════

class MarketScanner:
    """
    Scanner de marchés Polymarket.
    Basé sur les champs réels de la Gamma API.

    Responsabilité unique : trouver les candidats.
    Le scoring probabiliste est fait par prob_model.py.
    """

    def fetch_active_markets(self, limit: int = 500) -> list:
        """
        Récupère les marchés actifs via pagination.
        Rate limiting : 0.5s entre requêtes.
        """
        markets    = []
        offset     = 0
        batch_limit = 100

        while len(markets) < limit:
            try:
                resp = requests.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "active":    "true",
                        "closed":    "false",
                        "limit":     batch_limit,
                        "offset":    offset,
                        "order":     "volume24hr",
                        "ascending": "false",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                batch = resp.json()

                if not batch:
                    break

                markets.extend(batch)
                offset += batch_limit

                if len(batch) < batch_limit:
                    break

                time.sleep(0.5)

            except requests.RequestException as e:
                log.error(f"Gamma API error: {e}")
                break

        return markets

    def extract_yes_token_id(self, market: dict) -> Optional[str]:
        """
        Extrait le token_id de l'outcome YES.
        Nécessaire pour placer un ordre sur le CLOB.
        conditionId ≠ token_id — erreur fréquente.
        """
        tokens  = market.get("tokens", [])
        yes_tok = next(
            (t for t in tokens if t.get("outcome") == "Yes"), None
        )
        return yes_tok.get("token_id") if yes_tok else None

    def extract_no_token_id(self, market: dict) -> Optional[str]:
        """Token ID de l'outcome NO (pour clôture de position)."""
        tokens = market.get("tokens", [])
        no_tok = next(
            (t for t in tokens if t.get("outcome") == "No"), None
        )
        return no_tok.get("token_id") if no_tok else None

    def parse_yes_price(self, market: dict) -> Optional[float]:
        """
        Prix YES depuis outcomePrices[0].
        Fallback sur lastTradePrice si absent.
        """
        try:
            prices = market.get("outcomePrices", [])
            if isinstance(prices, list) and len(prices) >= 1:
                return float(prices[0])
            ltp = market.get("lastTradePrice")
            if ltp is not None:
                return float(ltp)
        except (ValueError, TypeError):
            pass
        return None

    def days_to_resolution(self, market: dict) -> Optional[float]:
        """Jours avant résolution depuis endDate."""
        end_str = market.get("endDate")
        if not end_str:
            return None
        try:
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return round((end - now).total_seconds() / 86400, 1)
        except (ValueError, AttributeError):
            return None

    def is_hard_resolution(self, market: dict) -> bool:
        """
        Résolution vérifiable objectivement.
        Exclut les marchés subjectifs ou à source non fiable.
        """
        question = market.get("question", "").lower()
        source   = market.get("resolutionSource", "").lower()

        for kw in MARKET_BLACKLIST_KEYWORDS:
            if kw in question:
                return False
        for src in MARKET_BLACKLIST_SOURCES:
            if src in source:
                return False
        return True

    def filter_strategy_1(
        self,
        market: dict,
        price: float,
        days: float,
        vol24: float,
    ) -> bool:
        """
        Stratégie 1 — Hybride Tetlock.
        Prix favoris : 0.70-0.92€ OU longshots : 0.01-0.10€
        Volume 24h   : 5,000 - 80,000€
        Résolution   : 5 - 21 jours
        """
        if not (S1_VOL_MIN <= vol24 <= S1_VOL_MAX):
            return False
        if not (S1_DAYS_MIN <= days <= S1_DAYS_MAX):
            return False
        favori   = S1_PRICE_FAV_MIN  <= price <= S1_PRICE_FAV_MAX
        longshot = S1_PRICE_LONG_MIN <= price <= S1_PRICE_LONG_MAX
        if not (favori or longshot):
            return False
        return True

    def filter_strategy_2(
        self,
        market: dict,
        price: float,
        days: float,
        vol24: float,
    ) -> bool:
        """
        Stratégie 2 — Longshots Macro.
        Prix      : 0.01 - 0.08€ strictement
        Volume 24h: > 10,000€
        Résolution: 14 - 90 jours
        Catégorie : crypto / economics / politics / finance / sports
        """
        category = market.get("category", "").lower()

        if vol24 < S2_VOL_MIN:
            return False
        if not (S2_DAYS_MIN <= days <= S2_DAYS_MAX):
            return False
        if not (S2_PRICE_MIN <= price <= S2_PRICE_MAX):
            return False
        if category not in S2_CATEGORIES:
            return False
        return True

    def get_candidates(self) -> dict:
        """
        Point d'entrée principal.
        Retourne les candidats pour chaque stratégie.

        Format retourné :
        {
          "strategy_1": [{"market_id", "token_id", "token_id_no", "question",
                          "price", "volume_24h", "liquidity",
                          "days_to_res", "category", "resolution_source",
                          "is_longshot"}, ...],
          "strategy_2": [...]
        }
        """
        all_markets = self.fetch_active_markets(limit=500)
        candidates  = {"strategy_1": [], "strategy_2": []}
        skipped_reasons: dict[str, int] = {}

        for m in all_markets:
            token_id = self.extract_yes_token_id(m)
            if token_id is None:
                skipped_reasons["no_yes_token"] = skipped_reasons.get("no_yes_token", 0) + 1
                continue

            price = self.parse_yes_price(m)
            days  = self.days_to_resolution(m)
            vol24 = float(m.get("volume24hr", 0) or 0)

            if price is None or days is None:
                skipped_reasons["missing_price_days"] = skipped_reasons.get("missing_price_days", 0) + 1
                continue
            if days <= 0:
                skipped_reasons["expired"] = skipped_reasons.get("expired", 0) + 1
                continue
            if not self.is_hard_resolution(m):
                skipped_reasons["soft_resolution"] = skipped_reasons.get("soft_resolution", 0) + 1
                continue

            entry = {
                "market_id":         m.get("id"),
                "token_id":          token_id,
                "token_id_no":       self.extract_no_token_id(m),
                "question":          m.get("question"),
                "price":             price,
                "volume_24h":        vol24,
                "liquidity":         float(m.get("liquidity", 0) or 0),
                "days_to_res":       days,
                "category":          m.get("category", "").lower(),
                "resolution_source": m.get("resolutionSource", ""),
                "condition_id":      m.get("conditionId"),
                "end_date":          m.get("endDate"),
                "is_longshot":       price < S1_LONGSHOT_THRESHOLD,
            }

            added = False
            if self.filter_strategy_1(m, price, days, vol24):
                candidates["strategy_1"].append(entry)
                added = True
            if self.filter_strategy_2(m, price, days, vol24):
                candidates["strategy_2"].append(entry)
                added = True
            if not added:
                skipped_reasons["filter_miss"] = skipped_reasons.get("filter_miss", 0) + 1

        log.info(
            f"Scanner: {len(all_markets)} marchés analysés | "
            f"S1: {len(candidates['strategy_1'])} | "
            f"S2: {len(candidates['strategy_2'])} candidats"
        )
        log.debug(f"Skipped: {skipped_reasons}")

        return candidates

    def fetch_market_by_id(self, market_id: str) -> Optional[dict]:
        """Récupère un marché spécifique par son ID."""
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets/{market_id}",
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.error(f"fetch_market_by_id({market_id}): {e}")
            return None

    def get_market_price(self, market_id: str) -> Optional[float]:
        """Prix YES actuel d'un marché (pour suivi de position)."""
        m = self.fetch_market_by_id(market_id)
        if m:
            return self.parse_yes_price(m)
        return None

    def get_current_days(self, market_id: str) -> Optional[float]:
        """Jours restants actuels pour un marché."""
        m = self.fetch_market_by_id(market_id)
        if m:
            return self.days_to_resolution(m)
        return None

    def is_market_resolved(self, market_id: str) -> tuple[bool, Optional[int]]:
        """
        Vérifie si un marché est résolu.
        Retourne (is_resolved, outcome) où outcome = 1 (YES) ou 0 (NO).
        """
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets/{market_id}",
                timeout=10,
            )
            resp.raise_for_status()
            m = resp.json()
            closed = m.get("closed", False)
            if not closed:
                return False, None

            # Déterminer l'outcome depuis outcomePrices
            prices = m.get("outcomePrices", [])
            if isinstance(prices, list) and len(prices) >= 2:
                yes_price = float(prices[0])
                if yes_price >= 0.99:
                    return True, 1
                elif yes_price <= 0.01:
                    return True, 0
            return True, None
        except Exception as e:
            log.error(f"is_market_resolved({market_id}): {e}")
            return False, None
