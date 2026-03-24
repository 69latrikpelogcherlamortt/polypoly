"""
live_feeds.py  ·  Polymarket Trading Bot
────────────────────────────────────────
Sources d'information prédictives — "Superforce" Edge Layer.

4 couches d'avantage informationnel :

  1. FREDLive        — Données macro FRED temps réel (CPI, chômage, taux, courbe)
  2. FinnhubLive     — News + sentiment NLP temps réel (10x plus rapide que RSS)
  3. KalshiScanner   — Cross-market arbitrage Polymarket ↔ Kalshi
  4. LLMAnalyzer     — Claude API pour analyse de sources primaires (FOMC, filings, etc.)

Principe : le p_model ne doit pas refléter le consensus — il doit l'anticiper.
"""

from __future__ import annotations

import json
import logging
import time
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from core.config import (
    FRED_API_KEY, FINNHUB_API_KEY, ANTHROPIC_API_KEY,
)

log = logging.getLogger("live_feeds")

# ═══════════════════════════════════════════════════════════════════════════
# 1. FRED LIVE — Données macro temps réel de la Federal Reserve
# ═══════════════════════════════════════════════════════════════════════════

# Séries FRED essentielles pour le p_model
FRED_SERIES = {
    # Inflation
    "cpi_yoy":        "CPIAUCSL",        # CPI All Urban (monthly)
    "core_cpi":       "CPILFESL",        # Core CPI ex food+energy
    "pce_yoy":        "PCEPI",           # PCE Price Index (Fed preferred)
    "breakeven_5y":   "T5YIE",           # 5Y breakeven inflation (daily)
    "breakeven_10y":  "T10YIE",          # 10Y breakeven inflation (daily)

    # Employment
    "unemployment":   "UNRATE",          # Unemployment rate (monthly)
    "nfp":            "PAYEMS",          # Nonfarm payrolls (monthly)
    "claims":         "ICSA",            # Weekly initial jobless claims

    # Growth
    "gdp_growth":     "A191RL1Q225SBEA", # Real GDP quarterly
    "retail_sales":   "RSXFS",           # Retail sales (monthly)

    # Rates & Yield Curve
    "fed_funds":      "DFF",             # Effective fed funds rate (daily)
    "t3m":            "DGS3MO",          # 3-month Treasury (daily)
    "t2y":            "DGS2",            # 2-year Treasury (daily)
    "t5y":            "DGS5",            # 5-year Treasury (daily)
    "t10y":           "DGS10",           # 10-year Treasury (daily)
    "t30y":           "DGS30",           # 30-year Treasury (daily)
    "spread_10y3m":   "T10Y3M",          # 10Y-3M spread (recession indicator)
    "spread_10y2y":   "T10Y2Y",          # 10Y-2Y spread

    # Financial Conditions
    "vix":            "VIXCLS",          # VIX (daily)
    "sp500":          "SP500",           # S&P 500 (daily)
    "financial_stress": "STLFSI2",       # St. Louis Financial Stress Index
}


class FREDLive:
    """
    Récupère les données macro en temps réel depuis FRED.
    Remplace les fallbacks hardcodés par des données fraîches.
    """

    BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(self, api_key: str = FRED_API_KEY):
        self.api_key = api_key
        self._cache: dict[str, tuple[float, float]] = {}  # series → (value, timestamp)
        self._cache_ttl = 3600  # 1 heure

    def _fetch_latest(self, series_id: str) -> Optional[float]:
        """Récupère la dernière observation d'une série FRED."""
        try:
            resp = requests.get(self.BASE_URL, params={
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 5,
            }, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            for obs in data.get("observations", []):
                val = obs.get("value", ".")
                if val != ".":
                    return float(val)
            return None
        except Exception as e:
            log.warning(f"FRED fetch failed for {series_id}: {e}")
            return None

    def get(self, key: str) -> Optional[float]:
        """Récupère une valeur avec cache."""
        now = time.time()
        if key in self._cache:
            val, ts = self._cache[key]
            if now - ts < self._cache_ttl:
                return val

        series_id = FRED_SERIES.get(key)
        if not series_id:
            return None

        val = self._fetch_latest(series_id)
        if val is not None:
            self._cache[key] = (val, now)
        return val

    def get_macro_data(self) -> dict:
        """
        Retourne un dict complet de données macro pour le MacroFedModel.
        Remplace les fallbacks statiques de prob_model.py.
        """
        macro = {}

        # CPI YoY — calculé depuis le niveau CPI
        cpi = self.get("cpi_yoy")
        if cpi is not None:
            # FRED donne le niveau CPI, pas le YoY. On approxime.
            # Pour le vrai YoY il faudrait le CPI d'il y a 12 mois.
            # On utilise le breakeven comme proxy plus frais.
            breakeven = self.get("breakeven_5y")
            if breakeven is not None:
                macro["cpi_yoy"] = breakeven  # déjà en %
            else:
                macro["cpi_yoy"] = 3.0  # fallback conservateur

        unemployment = self.get("unemployment")
        if unemployment is not None:
            macro["unemployment"] = unemployment

        gdp = self.get("gdp_growth")
        if gdp is not None:
            macro["gdp_growth"] = gdp

        fed_funds = self.get("fed_funds")
        if fed_funds is not None:
            macro["fed_funds_rate"] = fed_funds

        # Yield curve spread (10Y - 3M)
        spread = self.get("spread_10y3m")
        if spread is not None:
            macro["yield_curve"] = spread / 100.0  # FRED donne en %

        # Données supplémentaires pour Nelson-Siegel
        maturities = []
        yields_data = []
        for mat, key in [(0.25, "t3m"), (2.0, "t2y"), (5.0, "t5y"),
                          (10.0, "t10y"), (30.0, "t30y")]:
            val = self.get(key)
            if val is not None:
                maturities.append(mat)
                yields_data.append(val / 100.0)  # FRED donne en %

        if len(maturities) >= 3:
            macro["maturities"] = maturities
            macro["yields"] = yields_data

        # Indicateurs supplémentaires
        vix = self.get("vix")
        if vix is not None:
            macro["vix"] = vix

        stress = self.get("financial_stress")
        if stress is not None:
            macro["financial_stress"] = stress

        log.info(f"FRED macro data: {len(macro)} indicators fetched")
        return macro

    def get_yield_curve_signal(self) -> dict:
        """
        Signal dédié de la courbe des taux pour le p_model.
        Inversions → récession → probabilité de rate cut augmente.
        """
        spread_10y3m = self.get("spread_10y3m")
        spread_10y2y = self.get("spread_10y2y")

        signal = {"source": "fred_yield_curve"}

        if spread_10y3m is not None:
            signal["spread_10y3m"] = spread_10y3m / 100.0
            if spread_10y3m < -0.5:
                signal["regime"] = "deeply_inverted"
                signal["rate_cut_bias"] = 0.75
            elif spread_10y3m < 0:
                signal["regime"] = "inverted"
                signal["rate_cut_bias"] = 0.60
            elif spread_10y3m < 0.5:
                signal["regime"] = "flat"
                signal["rate_cut_bias"] = 0.45
            else:
                signal["regime"] = "normal"
                signal["rate_cut_bias"] = 0.30

        if spread_10y2y is not None:
            signal["spread_10y2y"] = spread_10y2y / 100.0

        return signal


# ═══════════════════════════════════════════════════════════════════════════
# 2. FINNHUB LIVE — News + Sentiment temps réel
# ═══════════════════════════════════════════════════════════════════════════

class FinnhubLive:
    """
    News en temps réel + sentiment NLP pré-calculé.
    10x plus rapide et plus riche que les flux RSS.
    """

    BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str = FINNHUB_API_KEY):
        self.api_key = api_key
        self._last_news_ids: set[int] = set()

    def _get(self, endpoint: str, params: dict = None) -> Optional[dict | list]:
        params = params or {}
        params["token"] = self.api_key
        try:
            resp = requests.get(f"{self.BASE_URL}/{endpoint}",
                                params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning(f"Finnhub {endpoint} failed: {e}")
            return None

    def get_market_news(self, category: str = "general") -> list[dict]:
        """
        News récentes avec sentiment.
        Categories: general, forex, crypto, merger
        """
        news = self._get("news", {"category": category})
        if not news:
            return []

        results = []
        for item in news[:20]:
            news_id = item.get("id", 0)
            if news_id in self._last_news_ids:
                continue
            self._last_news_ids.add(news_id)

            results.append({
                "id":        news_id,
                "headline":  item.get("headline", ""),
                "summary":   item.get("summary", ""),
                "source":    item.get("source", ""),
                "url":       item.get("url", ""),
                "datetime":  datetime.fromtimestamp(
                    item.get("datetime", 0), tz=timezone.utc
                ),
                "category":  item.get("category", ""),
            })
        return results

    def get_news_sentiment(self, symbol: str = "AAPL") -> Optional[dict]:
        """
        Sentiment agrégé d'un symbole — score bullish/bearish.
        Fonctionne pour: AAPL, BTC, SPY, etc.
        """
        data = self._get("news-sentiment", {"symbol": symbol})
        if not data:
            return None
        return {
            "symbol":              symbol,
            "buzz_articles_week":  data.get("buzz", {}).get("articlesInLastWeek", 0),
            "buzz_score":          data.get("buzz", {}).get("buzz", 0),
            "sentiment_score":     data.get("sentiment", {}).get("companyNewsScore", 0),
            "sentiment_bullish":   data.get("sentiment", {}).get("bullishPercent", 0),
            "sentiment_bearish":   data.get("sentiment", {}).get("bearishPercent", 0),
            "sector_avg_bullish":  data.get("sectorAverageBullishPercent", 0),
        }

    def get_economic_calendar(self) -> list[dict]:
        """
        Calendrier éco — CPI, FOMC, NFP, etc.
        Permet d'anticiper les mouvements de marché.
        """
        now = datetime.now(timezone.utc)
        from_date = now.strftime("%Y-%m-%d")
        to_date = (now + timedelta(days=14)).strftime("%Y-%m-%d")

        data = self._get("calendar/economic", {
            "from": from_date,
            "to": to_date,
        })
        if not data or not isinstance(data, dict):
            return []

        events = []
        for item in data.get("economicCalendar", [])[:30]:
            events.append({
                "event":    item.get("event", ""),
                "country":  item.get("country", ""),
                "date":     item.get("time", ""),
                "impact":   item.get("impact", ""),
                "actual":   item.get("actual"),
                "estimate": item.get("estimate"),
                "previous": item.get("prev"),
            })
        return events

    def get_fed_related_news(self) -> list[dict]:
        """
        Filtre les news liées à la Fed, aux taux, et à l'inflation.
        Ce sont les plus pertinentes pour les marchés macro Polymarket.
        """
        all_news = self.get_market_news("general")
        fed_keywords = [
            "fed", "federal reserve", "fomc", "rate cut", "rate hike",
            "inflation", "cpi", "unemployment", "jobs report", "nonfarm",
            "powell", "treasury", "yield", "monetary policy", "interest rate",
            "gdp", "recession", "tariff", "trade war",
        ]
        crypto_keywords = [
            "bitcoin", "btc", "ethereum", "crypto", "sec", "etf",
            "stablecoin", "defi", "regulation", "binance", "coinbase",
        ]

        relevant = []
        for item in all_news:
            text = (item["headline"] + " " + item["summary"]).lower()
            is_fed = any(kw in text for kw in fed_keywords)
            is_crypto = any(kw in text for kw in crypto_keywords)
            if is_fed or is_crypto:
                item["relevance"] = "fed" if is_fed else "crypto"
                relevant.append(item)
        return relevant


# ═══════════════════════════════════════════════════════════════════════════
# 3. KALSHI SCANNER — Cross-market arbitrage
# ═══════════════════════════════════════════════════════════════════════════

class KalshiScanner:
    """
    Scanner de divergences Polymarket ↔ Kalshi.
    Quand les deux marchés divergent > 5%, c'est un signal exploitable.
    """

    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self):
        self._cache: dict[str, tuple[dict, float]] = {}
        self._cache_ttl = 300  # 5 min

    def search_markets(self, query: str, limit: int = 5) -> list[dict]:
        """Recherche de marchés Kalshi par mot-clé."""
        try:
            resp = requests.get(f"{self.BASE_URL}/markets", params={
                "status": "open",
                "limit": limit,
            }, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            markets = []
            q_lower = query.lower()
            for m in data.get("markets", []):
                title = (m.get("title", "") + " " + m.get("subtitle", "")).lower()
                if any(kw in title for kw in q_lower.split()):
                    yes_price = m.get("yes_bid", 0) or m.get("last_price", 0)
                    markets.append({
                        "ticker":    m.get("ticker", ""),
                        "title":     m.get("title", ""),
                        "yes_price": yes_price / 100.0 if yes_price > 1 else yes_price,
                        "volume":    m.get("volume", 0),
                        "close_time": m.get("close_time", ""),
                    })
            return markets
        except Exception as e:
            log.warning(f"Kalshi search failed: {e}")
            return []

    def find_divergence(self, question: str, poly_price: float) -> Optional[dict]:
        """
        Cherche le marché Kalshi le plus similaire et calcule la divergence.
        Retourne le signal si divergence > 5%.
        """
        # Extraire les mots-clés de la question
        keywords = re.sub(r'[^\w\s]', '', question.lower())
        # Garder les mots significatifs
        stopwords = {"will", "the", "be", "by", "in", "at", "to", "of", "a", "an", "is", "it"}
        kw_list = [w for w in keywords.split() if w not in stopwords and len(w) > 2]
        search_q = " ".join(kw_list[:5])

        kalshi_markets = self.search_markets(search_q)
        if not kalshi_markets:
            return None

        best_match = kalshi_markets[0]
        kalshi_price = best_match["yes_price"]
        divergence = kalshi_price - poly_price

        if abs(divergence) < 0.05:
            return None  # Pas assez de divergence

        return {
            "source":       "kalshi_divergence",
            "kalshi_title":  best_match["title"],
            "kalshi_price":  round(kalshi_price, 4),
            "poly_price":    round(poly_price, 4),
            "divergence":    round(divergence, 4),
            "direction":     "bullish" if divergence > 0 else "bearish",
            "signal_strength": (
                "fort" if abs(divergence) > 0.10 else "moyen"
            ),
        }


# ═══════════════════════════════════════════════════════════════════════════
# 4. LLM ANALYZER — Claude API pour analyse prédictive
# ═══════════════════════════════════════════════════════════════════════════

class LLMAnalyzer:
    """
    Utilise Claude pour analyser des sources primaires et estimer
    des probabilités que le marché n'a pas encore intégrées.

    C'est le composant le plus puissant — un LLM peut :
    - Lire les minutes FOMC (20 pages) en 30s et en extraire P(cut)
    - Décomposer un événement complexe en sous-probabilités (Fermi)
    - Analyser le sentiment d'un corpus de news
    - Détecter des signaux faibles dans des filings SEC
    """

    API_URL = "https://api.anthropic.com/v1/messages"
    MODEL = "claude-haiku-4-5-20251001"  # Rapide et pas cher (~$0.01/analyse)

    def __init__(self, api_key: str = ANTHROPIC_API_KEY):
        self.api_key = api_key
        self._cache: dict[str, tuple[dict, float]] = {}
        self._cache_ttl = 1800  # 30 min

    def _call_claude(self, system: str, user: str,
                      max_tokens: int = 1024) -> Optional[str]:
        """Appel direct à l'API Claude."""
        try:
            resp = requests.post(self.API_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.MODEL,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]
        except Exception as e:
            log.warning(f"Claude API call failed: {e}")
            return None

    def estimate_probability(self, question: str,
                              context: str = "") -> Optional[dict]:
        """
        Demande à Claude d'estimer la probabilité d'un événement
        en utilisant le raisonnement structuré (Fermi + base rates).

        Retourne: {p_estimate, confidence, reasoning, sub_probabilities}
        """
        cache_key = f"prob:{question[:100]}"
        now = time.time()
        if cache_key in self._cache:
            val, ts = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                return val

        system = """Tu es un superforecaster expert en estimation de probabilités.
Tu dois estimer la probabilité qu'un événement se produise en utilisant :
1. Les base rates historiques (classe de référence)
2. La décomposition de Fermi (sous-questions indépendantes)
3. Les informations contextuelles fournies
4. L'ajustement bayésien basé sur les signaux récents

IMPORTANT : Réponds UNIQUEMENT en JSON valide avec cette structure exacte :
{
  "p_estimate": 0.XX,
  "confidence": "high/medium/low",
  "reasoning": "explication courte",
  "sub_probabilities": [
    {"factor": "description", "p": 0.XX}
  ],
  "key_uncertainties": ["incertitude 1", "incertitude 2"]
}"""

        user_msg = f"""Estime la probabilité que cet événement se réalise :

QUESTION : {question}

{f'CONTEXTE ADDITIONNEL : {context}' if context else ''}

Date actuelle : {datetime.now(timezone.utc).strftime('%Y-%m-%d')}

Décompose en sous-probabilités et donne ton estimation finale."""

        response = self._call_claude(system, user_msg)
        if not response:
            return None

        try:
            # Extraire le JSON de la réponse
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                result = json.loads(json_match.group())
                self._cache[cache_key] = (result, now)
                log.info(f"LLM estimate for '{question[:50]}': p={result.get('p_estimate')}")
                return result
        except (json.JSONDecodeError, KeyError) as e:
            log.warning(f"Failed to parse LLM response: {e}")

        return None

    def analyze_news_batch(self, news_items: list[dict],
                            market_question: str) -> Optional[dict]:
        """
        Analyse un lot de news et estime leur impact sur un marché spécifique.
        Retourne: {direction, magnitude, key_signals}
        """
        if not news_items:
            return None

        headlines = "\n".join([
            f"- [{n.get('source', '?')}] {n['headline']}"
            for n in news_items[:15]
        ])

        system = """Tu es un analyste de marchés prédictifs.
Analyse les news ci-dessous et estime leur impact sur la probabilité d'un événement.

Réponds UNIQUEMENT en JSON :
{
  "direction": "bullish/bearish/neutral",
  "magnitude": 0.XX,
  "confidence": "high/medium/low",
  "key_signals": ["signal 1", "signal 2"],
  "reasoning": "explication courte"
}

magnitude = l'ajustement de probabilité suggéré (ex: 0.05 = +5 points)"""

        user_msg = f"""MARCHÉ : {market_question}

NEWS RÉCENTES :
{headlines}

Quel est l'impact net de ces news sur la probabilité ?"""

        response = self._call_claude(system, user_msg, max_tokens=512)
        if not response:
            return None

        try:
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                return json.loads(json_match.group())
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    def decompose_event(self, question: str) -> Optional[list[dict]]:
        """
        Décompose un événement complexe en sous-probabilités indépendantes.
        Utilisé pour les marchés événementiels (politique, géopolitique).

        Retourne: liste de {factor, p, reasoning}
        """
        system = """Tu es un superforecaster expert en décomposition de Fermi.
Décompose l'événement en 3-5 sous-questions INDÉPENDANTES dont le produit
donne la probabilité finale.

Réponds UNIQUEMENT en JSON :
{
  "sub_questions": [
    {"factor": "description", "p": 0.XX, "reasoning": "justification courte"}
  ],
  "p_combined": 0.XX,
  "method": "multiplicative/weighted_average"
}"""

        user_msg = f"""Décompose cet événement en sous-probabilités :

QUESTION : {question}
DATE : {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"""

        response = self._call_claude(system, user_msg, max_tokens=768)
        if not response:
            return None

        try:
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                data = json.loads(json_match.group())
                return data.get("sub_questions", [])
        except (json.JSONDecodeError, KeyError):
            pass
        return None


# ═══════════════════════════════════════════════════════════════════════════
# 5. SUPERFORCE AGGREGATOR — Combine toutes les couches
# ═══════════════════════════════════════════════════════════════════════════

class SuperforceAggregator:
    """
    Point d'entrée unique pour toutes les sources d'information prédictives.
    Appelé par ProbabilisticScorer pour enrichir le contexte de scoring.
    """

    def __init__(self):
        self.fred = FREDLive()
        self.finnhub = FinnhubLive()
        self.kalshi = KalshiScanner()
        self.llm = LLMAnalyzer()

    def enrich_scoring_context(self, question: str,
                                market_price: float,
                                category: str = "") -> dict:
        """
        Enrichit le contexte de scoring avec toutes les sources live.
        Retourne un dict avec les données supplémentaires pour le p_model.
        """
        enrichment = {}

        # ── 1. Données macro FRED ──────────────────────────────────────
        try:
            macro = self.fred.get_macro_data()
            if macro:
                enrichment["macro_data"] = macro
                enrichment["macro_source"] = "fred_live"
        except Exception as e:
            log.warning(f"FRED enrichment failed: {e}")

        # ── 2. News Finnhub ────────────────────────────────────────────
        try:
            news = self.finnhub.get_fed_related_news()
            if news:
                enrichment["news_items"] = news
                enrichment["news_count"] = len(news)

                # Analyse LLM du lot de news
                news_analysis = self.llm.analyze_news_batch(news, question)
                if news_analysis:
                    enrichment["news_analysis"] = news_analysis
        except Exception as e:
            log.warning(f"Finnhub enrichment failed: {e}")

        # ── 3. Cross-market Kalshi ─────────────────────────────────────
        try:
            divergence = self.kalshi.find_divergence(question, market_price)
            if divergence:
                enrichment["kalshi_divergence"] = divergence
        except Exception as e:
            log.warning(f"Kalshi enrichment failed: {e}")

        # ── 4. Estimation LLM ─────────────────────────────────────────
        try:
            # Contexte = données macro + news pour informer Claude
            context_parts = []
            if "macro_data" in enrichment:
                m = enrichment["macro_data"]
                context_parts.append(
                    f"Fed funds rate: {m.get('fed_funds_rate', '?')}%, "
                    f"Unemployment: {m.get('unemployment', '?')}%, "
                    f"Yield curve 10Y-3M: {m.get('yield_curve', '?')}"
                )
            if "news_items" in enrichment:
                top_headlines = [n["headline"] for n in enrichment["news_items"][:5]]
                context_parts.append("Recent news: " + "; ".join(top_headlines))

            context = " | ".join(context_parts) if context_parts else ""

            llm_estimate = self.llm.estimate_probability(question, context)
            if llm_estimate:
                enrichment["llm_estimate"] = llm_estimate
        except Exception as e:
            log.warning(f"LLM enrichment failed: {e}")

        # ── 5. Calendrier économique ───────────────────────────────────
        try:
            calendar = self.finnhub.get_economic_calendar()
            if calendar:
                # Garder seulement les événements high-impact des 7 prochains jours
                high_impact = [e for e in calendar if e.get("impact") == "high"]
                if high_impact:
                    enrichment["upcoming_events"] = high_impact[:5]
        except Exception as e:
            log.warning(f"Calendar enrichment failed: {e}")

        log.info(
            f"Superforce enrichment: {len(enrichment)} layers "
            f"(macro={'yes' if 'macro_data' in enrichment else 'no'}, "
            f"news={enrichment.get('news_count', 0)}, "
            f"kalshi={'yes' if 'kalshi_divergence' in enrichment else 'no'}, "
            f"llm={'yes' if 'llm_estimate' in enrichment else 'no'})"
        )
        return enrichment
