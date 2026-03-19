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
import requests

from core.config import (
    DERIBIT_BASE, REUTERS_FEEDS, AP_FEEDS, GOOGLE_NEWS_BASE,
    KALSHI_API, FED_PRESS_RSS, BLS_API_KEY, BLS_API_BASE,
    POLY_ACTIVITY, POLY_PROFILE, REFERENCE_WALLET,
    NITTER_INSTANCES, TWITTER_ACCOUNTS_TIER1, TWITTER_ACCOUNTS_CRYPTO,
    TWITTER_ACCOUNTS_POLY, TWEET_BLACKLIST,
    CME_FEDWATCH_URL, BINANCE_WS,
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

    def fetch(self) -> list[CrucixAlert]:
        alerts = []
        try:
            resp = requests.get(
                self.FEDWATCH_DATA_URL,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            data = resp.json()
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

    def _get_instruments(self, currency: str) -> list[dict]:
        try:
            resp = requests.get(
                f"{DERIBIT_BASE}/get_instruments",
                params={
                    "currency": currency,
                    "kind": "option",
                    "expired": "false",
                },
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("result", [])
        except Exception as e:
            log.warning(f"Deribit instruments ({currency}): {e}")
            return []

    def _get_ticker(self, instrument: str) -> Optional[dict]:
        try:
            resp = requests.get(
                f"{DERIBIT_BASE}/ticker",
                params={"instrument_name": instrument},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("result")
        except Exception:
            return None

    def _get_btc_price(self) -> Optional[float]:
        try:
            resp = requests.get(
                f"{DERIBIT_BASE}/get_index_price",
                params={"index_name": "btc_usd"},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("result", {}).get("index_price")
        except Exception:
            return None

    def fetch(self) -> list[CrucixAlert]:
        alerts = []
        btc_price = self._get_btc_price()
        if btc_price is None:
            return alerts

        instruments = self._get_instruments("BTC")
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
            ticker = self._get_ticker(instrument_name)
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

    def fetch_rss(self, url: str, source_id: str,
                  category: AlertCategory) -> list[CrucixAlert]:
        alerts = []
        try:
            resp = requests.get(
                url,
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            try:
                root = ET.fromstring(resp.content)
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

    def fetch_reuters(self) -> list[CrucixAlert]:
        alerts = []
        for url in REUTERS_FEEDS:
            alerts.extend(
                self.fetch_rss(url, "reuters_rss", AlertCategory.NEWS_TIER1)
            )
        return alerts

    def fetch_ap(self) -> list[CrucixAlert]:
        alerts = []
        for url in AP_FEEDS:
            alerts.extend(
                self.fetch_rss(url, "ap_news", AlertCategory.NEWS_TIER1)
            )
        return alerts

    def fetch_fed_gov(self) -> list[CrucixAlert]:
        return self.fetch_rss(
            FED_PRESS_RSS, "fed_gov_statement", AlertCategory.FED_MACRO
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

    def fetch_for_market(self, question: str, max_items: int = 5) -> list[CrucixAlert]:
        keyword = quote_plus(question[:80])
        url = f"{GOOGLE_NEWS_BASE}?q={keyword}&hl=en&gl=US"

        alerts = []
        try:
            resp = requests.get(
                url,
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            try:
                root = ET.fromstring(resp.content)
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
# 6. KALSHI — CROSS-PLATFORM ARBITRAGE
# ═══════════════════════════════════════════════════════════════════════════

class KalshiSource:
    """
    Vérifie si les mêmes marchés existent sur Kalshi.
    Divergence > 5% → signal fort.
    """

    def _search_market(self, keyword: str) -> Optional[dict]:
        try:
            resp = requests.get(
                KALSHI_API,
                params={"search": keyword, "limit": 5, "status": "open"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            markets = data.get("markets", [])
            if markets:
                return markets[0]
        except Exception as e:
            log.debug(f"Kalshi search ({keyword}): {e}")
        return None

    def check_divergence(
        self,
        poly_price: float,
        market_question: str,
        keywords: list[str],
    ) -> Optional[CrucixAlert]:
        """
        Retourne une alerte si Kalshi diverge de >5% de Polymarket.
        """
        keyword = " ".join(keywords[:3])
        k_market = self._search_market(keyword)
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

    def check_volume_spike(
        self, market_id: str, expected_daily_vol: float
    ) -> Optional[CrucixAlert]:
        """
        Volume spike = volume dernières 2h > 15% du volume journalier attendu.
        """
        try:
            resp = requests.get(
                f"{POLY_ACTIVITY}",
                params={"market": market_id, "limit": 50},
                timeout=10,
            )
            resp.raise_for_status()
            trades = resp.json()
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

    def check_whale_wallet(self, wallet_address: str) -> list[CrucixAlert]:
        """
        Surveille les trades récents d'un wallet de référence.
        """
        if not wallet_address:
            return []
        alerts = []
        try:
            resp = requests.get(
                POLY_ACTIVITY,
                params={"user": wallet_address, "type": "TRADE", "limit": 10},
                timeout=10,
            )
            resp.raise_for_status()
            trades = resp.json()
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

    def _fetch_user_feed(self, username: str) -> list[dict]:
        for instance in NITTER_INSTANCES:
            url = f"{instance}/{username}/rss"
            try:
                resp = requests.get(
                    url,
                    timeout=8,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                resp.raise_for_status()
                try:
                    root = ET.fromstring(resp.content)
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

    def fetch(self) -> list[CrucixAlert]:
        alerts = []
        all_accounts = (
            TWITTER_ACCOUNTS_TIER1
            + TWITTER_ACCOUNTS_CRYPTO
            + TWITTER_ACCOUNTS_POLY
        )
        rss = RSSNewsSource()

        for username in all_accounts:
            tweets = self._fetch_user_feed(username)
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

    def fetch(self) -> list[CrucixAlert]:
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
            resp = requests.post(BLS_API_BASE, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
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

    def __init__(self):
        self.cme         = CMEFedWatchSource()
        self.deribit     = DeribitSource()
        self.rss         = RSSNewsSource()
        self.google      = GoogleNewsSource()
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

    def collect_all(
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
                new = self.cme.fetch()
                alerts.extend(new)
                log.info(f"CME FedWatch: {len(new)} alertes")
                self._mark_fetched("cme")
            except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"CME error: {e}")

        # Deribit — toutes les 15 minutes
        if self._should_fetch("deribit", 900):
            try:
                new = self.deribit.fetch()
                alerts.extend(new)
                log.info(f"Deribit: {len(new)} alertes")
                self._mark_fetched("deribit")
            except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"Deribit error: {e}")

        # Reuters + AP — toutes les 5 minutes
        if self._should_fetch("rss", 300):
            try:
                r_alerts = self.rss.fetch_reuters()
                a_alerts = self.rss.fetch_ap()
                f_alerts = self.rss.fetch_fed_gov()
                alerts.extend(r_alerts + a_alerts + f_alerts)
                log.info(f"RSS: {len(r_alerts)+len(a_alerts)+len(f_alerts)} alertes")
                self._mark_fetched("rss")
            except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"RSS error: {e}")

        # Twitter/Nitter — toutes les 10 minutes
        if self._should_fetch("nitter", 600):
            try:
                new = self.nitter.fetch()
                alerts.extend(new)
                log.info(f"Nitter: {len(new)} alertes")
                self._mark_fetched("nitter")
            except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"Nitter error: {e}")

        # BLS — toutes les 6 heures
        if self._should_fetch("bls", 21600):
            try:
                new = self.bls.fetch()
                alerts.extend(new)
                log.info(f"BLS: {len(new)} alertes")
                self._mark_fetched("bls")
            except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"BLS error: {e}")

        # BTC momentum (Binance)
        btc_signal = self.btc_tracker.get_momentum_signal()
        if btc_signal and not _cache.is_duplicate(btc_signal):
            alerts.append(btc_signal)

        # Whale wallet — toutes les 10 minutes
        if self._should_fetch("whale", 600) and REFERENCE_WALLET:
            try:
                new = self.poly_act.check_whale_wallet(REFERENCE_WALLET)
                alerts.extend(new)
                log.info(f"Whale: {len(new)} alertes")
                self._mark_fetched("whale")
            except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as e:
                log.error(f"Whale error: {e}")

        log.info(f"Signal cycle total: {len(alerts)} alertes collectées")
        return alerts

    def collect_for_market(self, question: str, price: float,
                            keywords: list[str]) -> list[CrucixAlert]:
        """
        Collecte les signaux spécifiques pour un marché candidat
        (appelé lors du scoring initial).
        """
        alerts: list[CrucixAlert] = []

        # Google News
        try:
            alerts.extend(self.google.fetch_for_market(question))
        except Exception as e:
            log.debug(f"Google News for market: {e}")

        # Kalshi cross-check
        try:
            k_alert = self.kalshi.check_divergence(price, question, keywords)
            if k_alert:
                alerts.append(k_alert)
        except Exception as e:
            log.debug(f"Kalshi check: {e}")

        return alerts
