"""
signal_sources.py  ·  Polymarket Trading Bot
──────────────────────────────────────────────
Pipeline de collecte de signaux depuis toutes les sources externes.
Produit des CrucixAlert prêtes à être injectées dans crucix_router.py.

Sources implémentées (26 total) :
  TIER 1 : CME FedWatch, Deribit, Reuters RSS, AP News RSS, BLS, Fed.gov
  TIER 2 : Google News RSS, Kalshi, Polymarket Activity, Binance WS
  TIER 2.5: Twitter/Nitter (comptes haute valeur uniquement)
  ONCHAIN : Glassnode (public), CryptoQuant (public)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import quote_plus

import aiohttp

from core.config import (
    DERIBIT_BASE, REUTERS_FEEDS, AP_FEEDS, GOOGLE_NEWS_BASE,
    KALSHI_API, FED_PRESS_RSS, BLS_API_KEY, BLS_API_BASE,
    POLY_ACTIVITY, POLY_PROFILE, REFERENCE_WALLET,
    NITTER_INSTANCES, TWITTER_ACCOUNTS_TIER1, TWITTER_ACCOUNTS_CRYPTO,
    TWITTER_ACCOUNTS_POLY, TWEET_BLACKLIST,
    CME_FEDWATCH_URL, BINANCE_WS,
    BBC_FEEDS, CNBC_FEEDS, MARKETWATCH_FEEDS, NPR_FEEDS,
    GDELT_API_BASE, METACULUS_API_BASE,
)
from signals.crucix_router import (
    CrucixAlert, AlertCategory, SignalDirection,
)

log = logging.getLogger("signals")


# ═══════════════════════════════════════════════════════════════════════════
# 1. CACHE DES ALERTES (déduplication)
# ═══════════════════════════════════════════════════════════════════════════

class AlertCache:
    """Évite de re-publier la même alerte deux fois (TTL = 2h)."""

    def __init__(self, ttl_minutes: int = 120):
        self._seen: dict[str, datetime] = {}
        self.ttl = timedelta(minutes=ttl_minutes)

    def is_duplicate(self, alert: CrucixAlert) -> bool:
        key = self._hash(alert)
        now = datetime.now(timezone.utc)
        if key in self._seen:
            if now - self._seen[key] < self.ttl:
                return True
        self._seen[key] = now
        # Nettoyer les vieilles entrées
        self._seen = {k: v for k, v in self._seen.items() if now - v < self.ttl}
        return False

    @staticmethod
    def _hash(alert: CrucixAlert) -> str:
        raw = f"{alert.source_id}|{alert.raw_text[:100]}"
        return hashlib.md5(raw.encode()).hexdigest()


_cache = AlertCache()


# ═══════════════════════════════════════════════════════════════════════════
# 2. CME FEDWATCH
# ═══════════════════════════════════════════════════════════════════════════

class CMEFedWatchSource:
    """
    Scrape les probabilités implicites de décision Fed depuis CME FedWatch.
    Polling toutes les heures.
    """

    # Endpoint alternatif (API JSON non-officielle mais stable)
    FEDWATCH_DATA_URL = "https://www.cmegroup.com/CmeWS/mvc/GetFedWatch/ProbHistoricalData"

    async def fetch(self, session: aiohttp.ClientSession) -> list[CrucixAlert]:
        alerts = []
        try:
            async with session.get(
                self.FEDWATCH_DATA_URL,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                if resp.status == 429:
                    log.warning("%s rate limited", self.__class__.__name__)
                    return []
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except Exception as e:
            log.warning(f"CME FedWatch fetch failed: {e}")
            return alerts

        try:
            meetings = data.get("data", [])
            for meeting in meetings[:3]:   # 3 prochains meetings
                meeting_date = meeting.get("meetingDate", "")
                prob_cut     = float(meeting.get("probCutLower25", 0) or 0) / 100
                prob_hike    = float(meeting.get("probHikePlus25", 0) or 0) / 100
                prob_hold    = 1.0 - prob_cut - prob_hike

                if prob_cut > 0.1 or prob_hike > 0.1:
                    direction = (
                        SignalDirection.BULLISH if prob_cut > prob_hike
                        else SignalDirection.BEARISH
                    )
                    magnitude = max(prob_cut, prob_hike)
                    raw_text = (
                        f"CME FedWatch {meeting_date}: "
                        f"P(cut25bps)={prob_cut:.1%} "
                        f"P(hike25bps)={prob_hike:.1%} "
                        f"P(hold)={prob_hold:.1%}"
                    )
                    keywords = ["fed", "fomc", "rate", "cut", "hike"]
                    month = meeting_date[:3].lower() if meeting_date else "unknown"
                    if month:
                        keywords.append(month)

                    alert = CrucixAlert(
                        source_id       = "cme_fedwatch",
                        category        = AlertCategory.FED_MACRO,
                        raw_text        = raw_text,
                        direction       = direction,
                        magnitude       = round(magnitude, 2),
                        market_keywords = keywords,
                        entities={
                            "prob_cut": prob_cut,
                            "prob_hike": prob_hike,
                            "meeting_date": meeting_date,
                            "quantitative": True,
                        },
                    )
                    if not _cache.is_duplicate(alert):
                        alerts.append(alert)
        except Exception as e:
            log.warning(f"CME FedWatch parse error: {e}")

        return alerts


# ═══════════════════════════════════════════════════════════════════════════
# 3. DERIBIT — IMPLIED VOLATILITY & OPTIONS PROBABILITIES
# ═══════════════════════════════════════════════════════════════════════════

class DeribitSource:
    """
    Récupère les deltas d'options BTC/ETH comme probabilités implicites.
    P(BTC > $X à date Y) = delta option call strike X expiry Y
    """

    # Strikes BTC à surveiller (cibles Polymarket fréquentes)
    BTC_STRIKES = [100_000, 120_000, 150_000, 200_000]
    ETH_STRIKES = [5_000, 8_000, 10_000]

    async def _get_instruments(self, session: aiohttp.ClientSession, currency: str) -> list[dict]:
        try:
            async with session.get(
                f"{DERIBIT_BASE}/get_instruments",
                params={
                    "currency": currency,
                    "kind": "option",
                    "expired": "false",
                },
            ) as resp:
                if resp.status == 429:
                    log.warning("%s rate limited", self.__class__.__name__)
                    return []
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                return data.get("result", [])
        except Exception as e:
            log.warning(f"Deribit instruments ({currency}): {e}")
            return []

    async def _get_ticker(self, session: aiohttp.ClientSession, instrument: str) -> Optional[dict]:
        try:
            async with session.get(
                f"{DERIBIT_BASE}/ticker",
                params={"instrument_name": instrument},
            ) as resp:
                if resp.status == 429:
                    log.warning("%s rate limited", self.__class__.__name__)
                    return None
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                return data.get("result")
        except Exception:
            return None

    async def _get_btc_price(self, session: aiohttp.ClientSession) -> Optional[float]:
        try:
            async with session.get(
                f"{DERIBIT_BASE}/get_index_price",
                params={"index_name": "btc_usd"},
            ) as resp:
                if resp.status == 429:
                    log.warning("%s rate limited", self.__class__.__name__)
                    return None
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                return data.get("result", {}).get("index_price")
        except Exception:
            return None

    async def fetch(self, session: aiohttp.ClientSession) -> list[CrucixAlert]:
        alerts = []
        btc_price = await self._get_btc_price(session)
        if btc_price is None:
            return alerts

        instruments = await self._get_instruments(session, "BTC")
        # Filtrer calls seulement, expirant dans 30-400 jours
        now = datetime.now(timezone.utc)
        calls = [
            i for i in instruments
            if i.get("option_type") == "call"
            and i.get("expiration_timestamp", 0) > (
                now.timestamp() + 30 * 86400
            ) * 1000
        ]

        for strike in self.BTC_STRIKES:
            # Trouver le call le plus proche de chaque expiry trimestrielle
            matches = sorted(
                [i for i in calls if abs(i.get("strike", 0) - strike) < 1000],
                key=lambda x: x.get("expiration_timestamp", 0),
            )
            if not matches:
                continue

            instrument_name = matches[0]["instrument_name"]
            ticker = await self._get_ticker(session, instrument_name)
            if not ticker:
                continue

            delta = ticker.get("greeks", {}).get("delta", None)
            iv    = ticker.get("mark_iv", None)
            if delta is None:
                continue

            # delta d'un call = P(BTC > strike)
            p_impl = max(0.01, min(0.99, abs(float(delta))))

            raw_text = (
                f"Deribit BTC call {strike}$ {instrument_name}: "
                f"delta={p_impl:.3f} IV={iv:.1f}% "
                f"(P impliquée BTC>{strike}$ = {p_impl:.1%})"
            )

            direction = (
                SignalDirection.BULLISH if p_impl > 0.12
                else SignalDirection.BEARISH
            )

            alert = CrucixAlert(
                source_id       = "deribit_vol",
                category        = AlertCategory.CRYPTO_PRICE,
                raw_text        = raw_text,
                direction       = direction,
                magnitude       = round(p_impl, 2),
                market_keywords = [
                    "btc", "bitcoin", f"{strike//1000}k", "deribit",
                    "options", "2026",
                ],
                entities={
                    "strike": strike,
                    "p_implied": p_impl,
                    "iv": iv,
                    "instrument": instrument_name,
                    "btc_spot": btc_price,
                    "quantitative": True,
                },
            )
            if not _cache.is_duplicate(alert):
                alerts.append(alert)

        return alerts


# ═══════════════════════════════════════════════════════════════════════════
# 4. RSS NEWS — Reuters, AP, Fed.gov
# ═══════════════════════════════════════════════════════════════════════════

class RSSNewsSource:
    """
    Parser RSS pour Reuters, AP News, Fed.gov press releases.
    """

    # Keywords pour filtrer les headlines pertinentes
    RELEVANT_KEYWORDS = {
        "fed", "federal reserve", "fomc", "interest rate", "cut", "hike",
        "inflation", "cpi", "gdp", "unemployment", "bitcoin", "btc",
        "crypto", "rate cut", "rate hike", "monetary", "recession",
        "treasury", "yield", "powell", "25bps", "50bps", "tariff",
        "election", "geopolitical", "war", "sanctions",
    }

    async def fetch_rss(self, session: aiohttp.ClientSession, url: str,
                        source_id: str,
                        category: AlertCategory) -> list[CrucixAlert]:
        alerts = []
        try:
            async with session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                if resp.status == 429:
                    log.warning("%s rate limited", self.__class__.__name__)
                    return []
                resp.raise_for_status()
                raw_bytes = await resp.read()
            try:
                root = ET.fromstring(raw_bytes)
            except ET.ParseError as e:
                log.warning(f"RSS XML parse error {url}: {e}")
                return alerts
        except Exception as e:
            log.warning(f"RSS fetch {url}: {e}")
            return alerts

        ns = {"dc": "http://purl.org/dc/elements/1.1/"}
        items = root.findall(".//item")

        for item in items[:20]:  # 20 headlines max
            title = item.findtext("title", "").strip()
            desc  = item.findtext("description", "").strip()
            pub   = item.findtext("pubDate", "")
            link  = item.findtext("link", "")
            text  = f"{title} {desc}".lower()

            # Filtrer par keywords pertinents
            if not any(kw in text for kw in self.RELEVANT_KEYWORDS):
                continue

            # Timestamp de publication
            try:
                ts = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z")
            except Exception:
                ts = datetime.now(timezone.utc)

            # Direction basique depuis le texte
            direction = self._infer_direction(text)
            if direction == SignalDirection.UNKNOWN:
                continue

            raw_text = f"{title}. {desc[:200]}"
            keywords = [kw for kw in self.RELEVANT_KEYWORDS if kw in text]

            alert = CrucixAlert(
                source_id       = source_id,
                category        = category,
                raw_text        = raw_text,
                direction       = direction,
                magnitude       = 0.62 if category == AlertCategory.NEWS_TIER1 else 0.42,
                market_keywords = list(keywords)[:10],
                timestamp       = ts,
                source_url      = link,
            )
            if not _cache.is_duplicate(alert):
                alerts.append(alert)

        return alerts

    async def fetch_reuters(self, session: aiohttp.ClientSession) -> list[CrucixAlert]:
        """
        Reuters RSS mort depuis 2024 (DNS failure sur VPS).
        Remplacé par BBC + CNBC + MarketWatch + NPR — même qualité Tier-1.
        """
        alerts = []
        # BBC Business + US/Canada
        for url in BBC_FEEDS:
            alerts.extend(
                await self.fetch_rss(session, url, "bbc_news", AlertCategory.NEWS_TIER1)
            )
        # CNBC — news financières US
        for url in CNBC_FEEDS:
            alerts.extend(
                await self.fetch_rss(session, url, "cnbc_news", AlertCategory.NEWS_TIER1)
            )
        # MarketWatch — marchés et macro
        for url in MARKETWATCH_FEEDS:
            alerts.extend(
                await self.fetch_rss(session, url, "marketwatch", AlertCategory.NEWS_TIER1)
            )
        # NPR Economy — couverture généraliste de qualité
        for url in NPR_FEEDS:
            alerts.extend(
                await self.fetch_rss(session, url, "npr_news", AlertCategory.NEWS_TIER2)
            )
        return alerts

    async def fetch_ap(self, session: aiohttp.ClientSession) -> list[CrucixAlert]:
        """
        AP News ne sert plus de RSS public valide depuis 2023.
        Désactivé — couverture assurée par BBC/CNBC/MarketWatch.
        """
        return []

    async def fetch_fed_gov(self, session: aiohttp.ClientSession) -> list[CrucixAlert]:
        return await self.fetch_rss(
            session, FED_PRESS_RSS, "fed_gov_statement", AlertCategory.FED_MACRO
        )

    @staticmethod
    def _infer_direction(text: str) -> SignalDirection:
        bullish = {
            "cut", "dovish", "support", "rise", "above", "exceed", "beat",
            "accumulation", "inflow", "surge", "strong", "growth", "soft landing",
            "below expectations", "lower than expected", "open to cut",
        }
        bearish = {
            "hike", "hawkish", "miss", "below", "fall", "drop", "weak",
            "outflow", "recession", "contraction", "decline", "war",
            "higher for longer", "hold", "pause", "above expectations",
            "sticky inflation", "liquidation", "tariff",
        }
        bs = sum(1 for t in bullish if t in text)
        be = sum(1 for t in bearish if t in text)
        if bs > be + 1:
            return SignalDirection.BULLISH
        if be > bs + 1:
            return SignalDirection.BEARISH
        if bs == be and bs > 0:
            return SignalDirection.NEUTRAL
        return SignalDirection.UNKNOWN


# ═══════════════════════════════════════════════════════════════════════════
# 5. GOOGLE NEWS RSS
# ═══════════════════════════════════════════════════════════════════════════

class GoogleNewsSource:
    """
    Récupère les 5 headlines les plus récentes pour un keyword donné.
    Utilisé au moment du scoring d'un marché candidat.
    """

    async def fetch_for_market(self, session: aiohttp.ClientSession,
                               question: str, max_items: int = 5) -> list[CrucixAlert]:
        keyword = quote_plus(question[:80])
        url = f"{GOOGLE_NEWS_BASE}?q={keyword}&hl=en&gl=US"

        alerts = []
        try:
            async with session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                if resp.status == 429:
                    log.warning("%s rate limited", self.__class__.__name__)
                    return []
                resp.raise_for_status()
                raw_bytes = await resp.read()
            try:
                root = ET.fromstring(raw_bytes)
            except ET.ParseError as e:
                log.debug(f"Google News XML parse error ({question[:40]}...): {e}")
                return alerts
        except Exception as e:
            log.debug(f"Google News ({question[:40]}...): {e}")
            return alerts

        rss_source = RSSNewsSource()
        items = root.findall(".//item")
        for item in items[:max_items]:
            title = item.findtext("title", "").strip()
            desc  = item.findtext("description", "").strip()
            text  = f"{title} {desc}".lower()
            direction = rss_source._infer_direction(text)
            if direction in (SignalDirection.BULLISH, SignalDirection.BEARISH):
                alert = CrucixAlert(
                    source_id       = "google_news",
                    category        = AlertCategory.NEWS_TIER2,
                    raw_text        = f"{title}. {desc[:200]}",
                    direction       = direction,
                    magnitude       = 0.40,
                    market_keywords = question.lower().split()[:8],
                )
                if not _cache.is_duplicate(alert):
                    alerts.append(alert)
        return alerts


# ═══════════════════════════════════════════════════════════════════════════
# 5B. GDELT PROJECT — News globales temps réel, sans auth
# ═══════════════════════════════════════════════════════════════════════════

class GDELTNewsSource:
    """
    GDELT Project — monitore 100k+ sources d'information mondiales.
    API gratuite, sans auth, latence ~15 minutes.

    Deux usages :
      • fetch_general()    → alertes macro/crypto/politique (collect_all)
      • fetch_for_market() → news spécifiques à un candidat (collect_for_market)
    """

    # Requêtes générales pour le cycle collect_all
    GENERAL_QUERIES = [
        "federal reserve interest rate decision",
        "bitcoin ethereum cryptocurrency price",
        "us election presidential political",
        "tariff trade war sanctions economic",
        "inflation cpi unemployment jobs",
    ]

    async def _fetch_query(
        self,
        session: aiohttp.ClientSession,
        query: str,
        category: AlertCategory,
        magnitude: float = 0.42,
        max_items: int = 15,
        timespan: str = "24h",
    ) -> list[CrucixAlert]:
        try:
            async with session.get(
                GDELT_API_BASE,
                params={
                    "query":      query,
                    "mode":       "artlist",
                    "format":     "json",
                    "maxrecords": min(max_items, 25),
                    "timespan":   timespan,
                    "sort":       "DateDesc",
                },
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 429:
                    log.warning("GDELT rate limited")
                    return []
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except Exception as e:
            log.debug(f"GDELT fetch ({query[:40]}): {e}")
            return []

        rss = RSSNewsSource()
        alerts = []
        for art in (data.get("articles") or [])[:max_items]:
            title = (art.get("title") or "").strip()
            if not title:
                continue
            text = title.lower()
            direction = rss._infer_direction(text)
            if direction == SignalDirection.UNKNOWN:
                continue

            seendate = art.get("seendate", "")
            try:
                ts = datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except Exception:
                ts = datetime.now(timezone.utc)

            alert = CrucixAlert(
                source_id       = "gdelt_news",
                category        = category,
                raw_text        = title,
                direction       = direction,
                magnitude       = magnitude,
                market_keywords = [w for w in query.lower().split() if len(w) > 3][:8],
                timestamp       = ts,
                source_url      = art.get("url", ""),
            )
            if not _cache.is_duplicate(alert):
                alerts.append(alert)

        return alerts

    async def fetch_general(
        self, session: aiohttp.ClientSession
    ) -> list[CrucixAlert]:
        """Alertes générales macro/crypto/politique — pour collect_all."""
        alerts = []
        for query in self.GENERAL_QUERIES:
            alerts.extend(
                await self._fetch_query(session, query, AlertCategory.NEWS_TIER2)
            )
        return alerts

    async def fetch_for_market(
        self,
        session: aiohttp.ClientSession,
        question: str,
        max_items: int = 10,
    ) -> list[CrucixAlert]:
        """News spécifiques pour un marché candidat — pour collect_for_market."""
        keywords = self._extract_keywords(question)
        query = " ".join(keywords[:6])
        if not query.strip():
            return []
        return await self._fetch_query(
            session, query, AlertCategory.NEWS_TIER2,
            magnitude=0.45, max_items=max_items, timespan="48h",
        )

    @staticmethod
    def _extract_keywords(question: str) -> list[str]:
        """Extrait les mots-clés pertinents d'une question Polymarket."""
        stopwords = {
            "will", "be", "the", "a", "an", "of", "in", "to", "at", "by",
            "for", "on", "is", "was", "are", "were", "or", "and", "any",
            "this", "that", "it", "he", "she", "we", "they", "have", "has",
            "before", "end", "above", "below", "more", "less", "than", "year",
            "month", "day", "2025", "2026", "2027", "first", "last", "next",
        }
        words = re.sub(r"[^\w\s]", " ", question.lower()).split()
        return [w for w in words if w not in stopwords and len(w) > 2]


# ═══════════════════════════════════════════════════════════════════════════
# 5C. METACULUS — Cross-référence marchés prédictifs académiques
# ═══════════════════════════════════════════════════════════════════════════

class MetaculusSource:
    """
    Metaculus — base de données de marchés prédictifs académiques.
    API publique sans authentification pour la lecture.

    Principe identique à KalshiSource : si la prédiction Metaculus
    diverge de >5% de Polymarket, c'est un signal fort.
    """

    async def check_divergence(
        self,
        session: aiohttp.ClientSession,
        poly_price: float,
        market_question: str,
    ) -> Optional[CrucixAlert]:
        """
        Cherche la question Metaculus la plus similaire.
        Divergence > 5% → signal directionnel.
        """
        # Utiliser les 7 premiers mots comme query (limiter le bruit)
        keywords = " ".join(market_question.split()[:7])

        try:
            async with session.get(
                METACULUS_API_BASE,
                params={
                    "search":   keywords,
                    "status":   "open",
                    "type":     "forecast",
                    "order_by": "-activity",
                    "limit":    5,
                },
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept":     "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status == 429:
                    log.warning("Metaculus rate limited")
                    return None
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except Exception as e:
            log.debug(f"Metaculus ({keywords[:40]}): {e}")
            return None

        results = data.get("results", [])
        if not results:
            return None

        best = results[0]
        community_pred = best.get("community_prediction") or {}

        # Format Metaculus : {"q2": {"prediction": 0.XX}} ou {"prediction": 0.XX}
        p_meta: Optional[float] = None
        if isinstance(community_pred, dict):
            if "q2" in community_pred:
                p_meta = community_pred["q2"].get("prediction")
            elif "prediction" in community_pred:
                p_meta = community_pred.get("prediction")

        if p_meta is None or not (0.01 <= float(p_meta) <= 0.99):
            return None

        p_meta = float(p_meta)
        divergence = p_meta - poly_price
        if abs(divergence) < 0.05:  # seuil : même que Kalshi
            return None

        direction = (
            SignalDirection.BULLISH if divergence > 0
            else SignalDirection.BEARISH
        )
        title = best.get("title", "")
        q_id  = best.get("id", "")

        raw_text = (
            f"Metaculus '{title[:80]}' → {p_meta:.1%} "
            f"vs Poly {poly_price:.1%} Δ={divergence:+.1%}"
        )

        return CrucixAlert(
            source_id       = "metaculus",
            category        = AlertCategory.PREDICTION_MKT,
            raw_text        = raw_text,
            direction       = direction,
            magnitude       = min(0.85, abs(divergence) * 3),
            market_keywords = market_question.lower().split()[:8],
            source_url      = f"https://www.metaculus.com/questions/{q_id}/",
            entities={
                "p_metaculus":  p_meta,
                "poly_price":   poly_price,
                "divergence":   divergence,
                "question":     title,
                "quantitative": True,
            },
        )


# ═══════════════════════════════════════════════════════════════════════════
# 6. KALSHI — CROSS-PLATFORM ARBITRAGE
# ═══════════════════════════════════════════════════════════════════════════

class KalshiSource:
    """
    Vérifie si les mêmes marchés existent sur Kalshi.
    Divergence > 5% → signal fort.
    """

    async def _search_market(self, session: aiohttp.ClientSession,
                             keyword: str) -> Optional[dict]:
        try:
            async with session.get(
                KALSHI_API,
                params={"search": keyword, "limit": 5, "status": "open"},
            ) as resp:
                if resp.status == 429:
                    log.warning("%s rate limited", self.__class__.__name__)
                    return None
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                markets = data.get("markets", [])
                if markets:
                    return markets[0]
        except Exception as e:
            log.debug(f"Kalshi search ({keyword}): {e}")
        return None

    async def check_divergence(
        self,
        session: aiohttp.ClientSession,
        poly_price: float,
        market_question: str,
        keywords: list[str],
    ) -> Optional[CrucixAlert]:
        """
        Retourne une alerte si Kalshi diverge de >5% de Polymarket.
        """
        keyword = " ".join(keywords[:3])
        k_market = await self._search_market(session, keyword)
        if not k_market:
            return None

        # Prix Kalshi (format: yes_price en cents ou fraction selon l'API)
        k_price_raw = k_market.get("yes_ask") or k_market.get("last_price") or 0
        try:
            k_price = float(k_price_raw)
        except (ValueError, TypeError):
            return None
        if k_price > 1.5:  # en cents → convertir
            k_price /= 100.0

        divergence = k_price - poly_price
        if abs(divergence) < 0.05:
            return None

        direction = (
            SignalDirection.BULLISH if divergence > 0
            else SignalDirection.BEARISH
        )
        raw_text = (
            f"Kalshi divergence: Polymarket={poly_price:.1%} "
            f"Kalshi={k_price:.1%} "
            f"Δ={divergence:+.1%} "
            f"(Polymarket {'en retard' if divergence > 0 else 'en avance'})"
        )

        return CrucixAlert(
            source_id       = "kalshi",
            category        = AlertCategory.PREDICTION_MKT,
            raw_text        = raw_text,
            direction       = direction,
            magnitude       = min(0.90, abs(divergence) * 3),
            market_keywords = keywords[:8],
            entities={
                "poly_price": poly_price,
                "kalshi_price": k_price,
                "divergence": divergence,
                "quantitative": True,
            },
        )


# ═══════════════════════════════════════════════════════════════════════════
# 7. POLYMARKET ACTIVITY API — whale & volume detection
# ═══════════════════════════════════════════════════════════════════════════

class PolymarketActivitySource:
    """
    Détecte les volume spikes et les mouvements de wallet de référence.
    """

    async def check_volume_spike(
        self, session: aiohttp.ClientSession,
        market_id: str, expected_daily_vol: float
    ) -> Optional[CrucixAlert]:
        """
        Volume spike = volume dernières 2h > 15% du volume journalier attendu.
        """
        try:
            async with session.get(
                f"{POLY_ACTIVITY}",
                params={"market": market_id, "limit": 50},
            ) as resp:
                if resp.status == 429:
                    log.warning("%s rate limited", self.__class__.__name__)
                    return None
                resp.raise_for_status()
                trades = await resp.json(content_type=None)
        except Exception as e:
            log.debug(f"Poly activity ({market_id}): {e}")
            return None

        now    = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=2)
        vol_2h = 0.0
        for t in trades:
            try:
                ts  = datetime.fromisoformat(
                    t.get("timestamp", "").replace("Z", "+00:00")
                )
                if ts >= cutoff:
                    vol_2h += float(t.get("usdcSize", 0) or 0)
            except Exception:
                continue

        spike_threshold = expected_daily_vol * 0.15
        if vol_2h < spike_threshold:
            return None

        return CrucixAlert(
            source_id       = "polymarket_activity",
            category        = AlertCategory.PREDICTION_MKT,
            raw_text        = (
                f"Volume spike: {vol_2h:.0f} USDC en 2h "
                f"(vs {expected_daily_vol:.0f} vol journalier). "
                f"Spike {vol_2h/max(expected_daily_vol,1)*100:.0f}% du volume journalier."
            ),
            direction       = SignalDirection.BULLISH,
            magnitude       = min(0.80, vol_2h / max(expected_daily_vol, 1)),
            market_keywords = ["volume", "spike", "polymarket"],
            entities={"vol_2h": vol_2h, "expected_daily": expected_daily_vol},
        )

    async def check_whale_wallet(self, session: aiohttp.ClientSession,
                                 wallet_address: str) -> list[CrucixAlert]:
        """
        Surveille les trades récents d'un wallet de référence.
        """
        if not wallet_address:
            return []
        alerts = []
        try:
            async with session.get(
                POLY_ACTIVITY,
                params={"user": wallet_address, "type": "TRADE", "limit": 10},
            ) as resp:
                if resp.status == 429:
                    log.warning("%s rate limited", self.__class__.__name__)
                    return []
                resp.raise_for_status()
                trades = await resp.json(content_type=None)
        except Exception as e:
            log.debug(f"Whale wallet ({wallet_address[:10]}...): {e}")
            return []

        for t in trades:
            market_id = t.get("market", "")
            side      = t.get("side", "")
            size      = float(t.get("usdcSize", 0) or 0)
            if size < 100:  # ignorer petits trades
                continue
            direction = (
                SignalDirection.BULLISH if side == "BUY"
                else SignalDirection.BEARISH
            )
            raw_text = (
                f"Whale wallet {wallet_address[:12]}... "
                f"{side} ${size:.0f} sur {market_id}"
            )
            alert = CrucixAlert(
                source_id       = "polymarket_activity",
                category        = AlertCategory.PREDICTION_MKT,
                raw_text        = raw_text,
                direction       = direction,
                magnitude       = min(0.75, size / 1000),
                market_keywords = ["whale", "polymarket", market_id],
                entities={"wallet": wallet_address, "size": size, "market": market_id},
            )
            if not _cache.is_duplicate(alert):
                alerts.append(alert)
        return alerts


# ═══════════════════════════════════════════════════════════════════════════
# 8. BINANCE WEBSOCKET — prix BTC en temps réel
# ═══════════════════════════════════════════════════════════════════════════

class BinancePriceTracker:
    """
    Suit le prix BTC en temps réel via Binance WebSocket.
    Produit des alertes de momentum (pas utilisé pour HFT).
    """

    def __init__(self):
        self.current_price: Optional[float] = None
        self.price_history: list[float] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Lance le WebSocket en arrière-plan."""
        self._running = True
        self._task = asyncio.create_task(self._ws_loop())

    async def _ws_loop(self):
        import websockets
        reconnect_delay = 5  # secondes, backoff exponentiel
        while self._running:
            try:
                async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
                    reconnect_delay = 5  # reset après connexion réussie
                    async for message in ws:
                        data = json.loads(message)
                        price = float(data.get("c", 0))  # close price
                        if price > 0:
                            self.current_price = price
                            self.price_history.append(price)
                            self.price_history = self.price_history[-1000:]
            except Exception as e:
                log.debug(f"Binance WS error: {e}, reconnexion dans {reconnect_delay}s")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 300)  # max 5 minutes

    def get_momentum_signal(self, window_hours: int = 24) -> Optional[CrucixAlert]:
        """
        BTC +3%/semaine → ajuster P_true hausse.
        Signal macro uniquement — pas pour HFT.
        """
        if len(self.price_history) < 10 or self.current_price is None:
            return None

        ref_price = self.price_history[0]
        change    = (self.current_price - ref_price) / ref_price

        if abs(change) < 0.03:  # moins de 3% → pas de signal
            return None

        direction = (
            SignalDirection.BULLISH if change > 0
            else SignalDirection.BEARISH
        )
        return CrucixAlert(
            source_id       = "binance_ws",
            category        = AlertCategory.CRYPTO_PRICE,
            raw_text        = (
                f"BTC momentum: {change:+.1%} sur {len(self.price_history)} ticks. "
                f"Prix actuel: ${self.current_price:,.0f}"
            ),
            direction       = direction,
            magnitude       = min(0.70, abs(change) * 3),
            market_keywords = ["btc", "bitcoin", "crypto", "price", "momentum"],
            entities={
                "btc_price": self.current_price,
                "change_pct": change,
                "quantitative": True,
            },
        )

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()


# ═══════════════════════════════════════════════════════════════════════════
# 9. NITTER RSS — Twitter via instance Nitter
# ═══════════════════════════════════════════════════════════════════════════

class NitterSource:
    """
    Récupère les tweets via Nitter RSS (proxy gratuit sans API key).
    Filtre strict : comptes haute valeur uniquement.
    """

    async def _fetch_user_feed(self, session: aiohttp.ClientSession,
                               username: str) -> list[dict]:
        for instance in NITTER_INSTANCES:
            url = f"{instance}/{username}/rss"
            try:
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                ) as resp:
                    if resp.status == 429:
                        log.warning("%s rate limited", self.__class__.__name__)
                        return []
                    resp.raise_for_status()
                    raw_bytes = await resp.read()
                try:
                    root = ET.fromstring(raw_bytes)
                except ET.ParseError:
                    continue
                items = root.findall(".//item")
                tweets = []
                for item in items[:10]:
                    tweets.append({
                        "title":   item.findtext("title", ""),
                        "desc":    item.findtext("description", ""),
                        "pubDate": item.findtext("pubDate", ""),
                        "link":    item.findtext("link", ""),
                        "author":  username,
                    })
                return tweets
            except Exception:
                continue
        return []

    def _filter_tweet(self, tweet: dict) -> bool:
        """Filtre anti-bruit strict."""
        text = (tweet["title"] + " " + tweet["desc"]).lower()
        for kw in TWEET_BLACKLIST:
            if kw.lower() in text:
                return False
        return True

    async def fetch(self, session: aiohttp.ClientSession) -> list[CrucixAlert]:
        alerts = []
        all_accounts = (
            TWITTER_ACCOUNTS_TIER1
            + TWITTER_ACCOUNTS_CRYPTO
            + TWITTER_ACCOUNTS_POLY
        )
        rss = RSSNewsSource()

        for username in all_accounts:
            tweets = await self._fetch_user_feed(session, username)
            for tweet in tweets:
                if not self._filter_tweet(tweet):
                    continue
                text = (tweet["title"] + " " + tweet["desc"]).lower()
                direction = rss._infer_direction(text)
                if direction not in (SignalDirection.BULLISH, SignalDirection.BEARISH):
                    continue

                # Timestamp
                try:
                    ts = datetime.strptime(tweet["pubDate"], "%a, %d %b %Y %H:%M:%S %z")
                except Exception:
                    ts = datetime.now(timezone.utc)

                # Tier du compte
                is_tier1 = username in TWITTER_ACCOUNTS_TIER1
                source_id = "twitter_t1" if is_tier1 else "twitter_t2"
                category  = AlertCategory.SOCIAL_TIER1 if is_tier1 else AlertCategory.NEWS_TIER2
                magnitude = 0.55 if is_tier1 else 0.35

                raw = tweet["title"][:300]
                keywords = [kw for kw in rss.RELEVANT_KEYWORDS if kw in text]

                alert = CrucixAlert(
                    source_id       = source_id,
                    category        = category,
                    raw_text        = f"@{username}: {raw}",
                    direction       = direction,
                    magnitude       = magnitude,
                    market_keywords = list(keywords)[:8],
                    timestamp       = ts,
                    source_url      = tweet.get("link"),
                    entities={"username": username, "tier1": is_tier1},
                )
                if not _cache.is_duplicate(alert):
                    alerts.append(alert)

        return alerts


# ═══════════════════════════════════════════════════════════════════════════
# 10. BLS — CPI / EMPLOI
# ═══════════════════════════════════════════════════════════════════════════

class BLSSource:
    """
    Récupère les données macro officielles BLS (CPI, unemployment).
    Nécessite une clé API gratuite (bls.gov).
    """

    SERIES = {
        "cpi_all":     "CUSR0000SA0",   # CPI All Urban Consumers
        "unemployment": "LNS14000000",  # Unemployment Rate
    }

    def __init__(self):
        # Cache des dernières valeurs parsées — alimenté par fetch()
        self._macro_cache: dict = {}

    def get_macro_data(self) -> dict:
        """Retourne les données macro les plus récentes (dict vide si pas encore fetchées)."""
        return dict(self._macro_cache)

    async def fetch(self, session: aiohttp.ClientSession) -> list[CrucixAlert]:
        if not BLS_API_KEY:
            log.debug("BLS_API_KEY non configurée, skip BLS")
            return []

        alerts = []
        try:
            payload = {
                "seriesid": list(self.SERIES.values()),
                "startyear": str(datetime.now().year - 1),
                "endyear": str(datetime.now().year),
                "registrationkey": BLS_API_KEY,
            }
            async with session.post(BLS_API_BASE, json=payload) as resp:
                if resp.status == 429:
                    log.warning("%s rate limited", self.__class__.__name__)
                    return []
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except Exception as e:
            log.warning(f"BLS fetch error: {e}")
            return []

        rss = RSSNewsSource()
        for series in data.get("Results", {}).get("series", []):
            sid   = series.get("seriesID", "")
            items = series.get("data", [])
            if not items:
                continue
            latest = items[0]
            value  = float(latest.get("value", 0))
            period = latest.get("periodName", "")

            if sid == self.SERIES["cpi_all"]:
                self._macro_cache["cpi_yoy"] = value   # ← mise à jour cache
                direction = (
                    SignalDirection.BEARISH if value > 3.5  # inflation haute → bearish Fed cut
                    else SignalDirection.BULLISH
                )
                raw_text = f"BLS CPI: {value:.1f}% YoY ({period})"
                keywords = ["cpi", "inflation", "fed", "rate"]
            elif sid == self.SERIES["unemployment"]:
                self._macro_cache["unemployment"] = value   # ← mise à jour cache
                direction = (
                    SignalDirection.BULLISH if value > 4.5  # chômage élevé → cut probable
                    else SignalDirection.BEARISH
                )
                raw_text = f"BLS Unemployment: {value:.1f}% ({period})"
                keywords = ["unemployment", "fed", "rate", "cut"]
            else:
                continue

            alert = CrucixAlert(
                source_id       = "bls_cpi",
                category        = AlertCategory.MACRO_INDICATOR,
                raw_text        = raw_text,
                direction       = direction,
                magnitude       = 0.65,
                market_keywords = keywords,
                entities={"value": value, "period": period, "quantitative": True},
            )
            if not _cache.is_duplicate(alert):
                alerts.append(alert)

        return alerts


# ═══════════════════════════════════════════════════════════════════════════
# 11. SIGNAL AGGREGATOR — point d'entrée unique
# ═══════════════════════════════════════════════════════════════════════════

class SignalAggregator:
    """
    Orchestre toutes les sources et retourne une liste consolidée d'alertes.
    Appelé par le main loop à chaque cycle (toutes les 10 minutes).
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session     = session
        self.cme         = CMEFedWatchSource()
        self.deribit     = DeribitSource()
        self.rss         = RSSNewsSource()
        self.google      = GoogleNewsSource()
        self.gdelt       = GDELTNewsSource()
        self.metaculus   = MetaculusSource()
        self.kalshi      = KalshiSource()
        self.poly_act    = PolymarketActivitySource()
        self.nitter      = NitterSource()
        self.bls         = BLSSource()
        self.btc_tracker = BinancePriceTracker()

        # Timestamps du dernier fetch par source
        self._last_fetch: dict[str, datetime] = {}

    def get_latest_macro_data(self) -> dict:
        """Retourne les dernières données macro issues de BLSSource (si disponibles)."""
        return self.bls.get_macro_data()

    def _should_fetch(self, source: str, interval_seconds: int) -> bool:
        last = self._last_fetch.get(source)
        if last is None:
            return True
        return (datetime.now(timezone.utc) - last).total_seconds() >= interval_seconds

    def _mark_fetched(self, source: str):
        self._last_fetch[source] = datetime.now(timezone.utc)

    async def collect_all(
        self,
        open_positions: Optional[list] = None,
        market_candidates: Optional[list] = None,
    ) -> list[CrucixAlert]:
        """
        Collecte toutes les alertes disponibles.
        Respecte les intervalles de polling par source.
        """
        alerts: list[CrucixAlert] = []

        # CME FedWatch — toutes les heures
        if self._should_fetch("cme", 3600):
            try:
                new = await self.cme.fetch(self.session)
                alerts.extend(new)
                log.info(f"CME FedWatch: {len(new)} alertes")
                self._mark_fetched("cme")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"CME error: {e}")

        # Deribit — toutes les 15 minutes
        if self._should_fetch("deribit", 900):
            try:
                new = await self.deribit.fetch(self.session)
                alerts.extend(new)
                log.info(f"Deribit: {len(new)} alertes")
                self._mark_fetched("deribit")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"Deribit error: {e}")

        # BBC + CNBC + MarketWatch + NPR + Fed.gov — toutes les 5 minutes
        # (Reuters/AP désactivés : DNS mort / XML cassé)
        if self._should_fetch("rss", 300):
            try:
                r_alerts = await self.rss.fetch_reuters(self.session)   # → BBC+CNBC+MktWatch+NPR
                f_alerts = await self.rss.fetch_fed_gov(self.session)
                alerts.extend(r_alerts + f_alerts)
                log.info(f"RSS (BBC/CNBC/MarketWatch/NPR/Fed): {len(r_alerts)+len(f_alerts)} alertes")
                self._mark_fetched("rss")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"RSS error: {e}")

        # GDELT — news globales toutes les 10 minutes
        if self._should_fetch("gdelt", 600):
            try:
                new = await self.gdelt.fetch_general(self.session)
                alerts.extend(new)
                log.info(f"GDELT général: {len(new)} alertes")
                self._mark_fetched("gdelt")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"GDELT error: {e}")

        # Twitter/Nitter — toutes les 10 minutes
        if self._should_fetch("nitter", 600):
            try:
                new = await self.nitter.fetch(self.session)
                alerts.extend(new)
                log.info(f"Nitter: {len(new)} alertes")
                self._mark_fetched("nitter")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"Nitter error: {e}")

        # BLS — toutes les 6 heures
        if self._should_fetch("bls", 21600):
            try:
                new = await self.bls.fetch(self.session)
                alerts.extend(new)
                log.info(f"BLS: {len(new)} alertes")
                self._mark_fetched("bls")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"BLS error: {e}")

        # BTC momentum (Binance)
        btc_signal = self.btc_tracker.get_momentum_signal()
        if btc_signal and not _cache.is_duplicate(btc_signal):
            alerts.append(btc_signal)

        # Whale wallet — toutes les 10 minutes
        if self._should_fetch("whale", 600) and REFERENCE_WALLET:
            try:
                new = await self.poly_act.check_whale_wallet(self.session, REFERENCE_WALLET)
                alerts.extend(new)
                log.info(f"Whale: {len(new)} alertes")
                self._mark_fetched("whale")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"Whale error: {e}")

        log.info(f"Signal cycle total: {len(alerts)} alertes collectées")
        return alerts

    async def collect_for_market(self, question: str, price: float,
                                 keywords: list[str]) -> list[CrucixAlert]:
        """
        Collecte les signaux spécifiques pour un marché candidat
        (appelé lors du scoring initial).

        Sources (par ordre d'importance) :
          1. GDELT — recherche news globales sur la question (free, rapide)
          2. Google News RSS — redondant avec GDELT mais utile comme filet
          3. Kalshi cross-check — cross-plateforme prédictif
          4. Metaculus — cross-plateforme prédictif académique
        """
        alerts: list[CrucixAlert] = []

        # GDELT — news spécifiques au marché (source principale)
        try:
            gdelt_alerts = await self.gdelt.fetch_for_market(self.session, question)
            alerts.extend(gdelt_alerts)
            if gdelt_alerts:
                log.info(f"GDELT pour marché ({question[:40]}...): {len(gdelt_alerts)} alertes")
        except Exception as e:
            log.debug(f"GDELT for market: {e}")

        # Google News RSS (filet de sécurité — peut être lent ou rate-limited)
        try:
            alerts.extend(await self.google.fetch_for_market(self.session, question))
        except Exception as e:
            log.debug(f"Google News for market: {e}")

        # Kalshi cross-check
        try:
            k_alert = await self.kalshi.check_divergence(self.session, price, question, keywords)
            if k_alert:
                alerts.append(k_alert)
                log.info(f"Kalshi divergence: {k_alert.raw_text[:80]}")
        except Exception as e:
            log.debug(f"Kalshi check: {e}")

        # Metaculus cross-check (marchés prédictifs académiques)
        try:
            m_alert = await self.metaculus.check_divergence(self.session, price, question)
            if m_alert:
                alerts.append(m_alert)
                log.info(f"Metaculus divergence: {m_alert.raw_text[:80]}")
        except Exception as e:
            log.debug(f"Metaculus check: {e}")

        log.info(
            f"collect_for_market '{question[:50]}...' → {len(alerts)} signaux "
            f"(gdelt+google+kalshi+metaculus)"
        )
        return alerts
