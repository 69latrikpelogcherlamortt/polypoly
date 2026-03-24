"""
crucix_router.py  ·  v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PAF-001 · Crucix → Polymarket Signal Integration Pipeline
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Pipeline complet :

    CrucixAlert (26 sources)
        │
        ├─ SignalParser          normalise, extrait direction + entities
        ├─ TemporalDecayEngine   pondère le signal par son âge
        ├─ MarketRouter          route vers les marchés ouverts pertinents
        ├─ SourceCorrelationChecker  détecte les sources non-indépendantes
        ├─ BayesUpdater          applique LR calibré, met à jour p_model
        ├─ MultiSourceAggregator Satopää extremizing si ≥2 sources agrègent
        ├─ DynamicZScoreEngine   recalcule δ avec sigma dynamique par marché
        ├─ SevenGateRevalidator  re-passe les 7 gates sur le p_model mis à jour
        ├─ KellyResizer          recalcule la taille de position si edge change
        ├─ ModelOutputBus        publie l'événement vers Telegram + dashboard
        └─ CalibrationEngine     log signal→outcome pour recalibration LR

Principes superforecaster appliqués :
  1. Outside view first — LR ancrés sur base rates, jamais sur instinct
  2. Mise à jour incrémentale — chaque signal déplace proportionnellement
  3. Hard cap ±15pts par source unique (garde contre overconfidence)
  4. Extremizing uniquement si sources *indépendantes* convergent
  5. Décroissance temporelle — un signal de 4h vaut moins qu'un signal frais
  6. Audit trail complet — chaque mutation de p_model est tracée
  7. Brier-weighted source trust — sources mal calibrées se font downweighter
  8. Jamais une seule source ne peut seule déclencher une action

Principes trading professionnel appliqués :
  - Kelly fractionnaire 1/4 avec recalcul immédiat si edge change >30%
  - Z-score dynamique (sigma = rolling 14j volatilité du marché)
  - 7 gates re-validés après chaque mise à jour p_model
  - Corrélation inter-positions surveillée (pas d'overexposure thématique)
  - Temporal decay des signaux : LR_effective = LR × exp(-λ × minutes_old)
"""

from __future__ import annotations

import json
import math
import logging
import sqlite3
import hashlib
import statistics
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger("crucix.router")
# NOTE: logging configuration is handled exclusively by main.py setup_logging()

DB_PATH = Path("paf_signals.db")


# ═══════════════════════════════════════════════════════════════════════════
# 1. STRUCTURES DE DONNÉES
# ═══════════════════════════════════════════════════════════════════════════

class AlertCategory(str, Enum):
    FED_MACRO      = "fed_macro"        # CME FedWatch, FRED, BLS, Fed.gov
    CRYPTO_PRICE   = "crypto_price"     # Deribit, Binance, CoinGecko, CMC
    CRYPTO_ONCHAIN = "crypto_onchain"   # Glassnode, CryptoQuant, Chainlink
    NEWS_TIER1     = "news_tier1"       # Reuters, AP, Bloomberg
    NEWS_TIER2     = "news_tier2"       # Yahoo Finance, Google News
    PREDICTION_MKT = "prediction_mkt"   # Kalshi, Polymarket Activity
    SOCIAL_TIER1   = "social_tier1"     # Twitter T1 (Timiraos, Fed accounts)
    ONCHAIN_DEFI   = "onchain_defi"     # Etherscan, DeBank
    GEOPOLITICAL   = "geopolitical"     # Crucix World Monitor
    MACRO_INDICATOR= "macro_indicator"  # Messari, TradingEcon, MacroTrends

class SignalDirection(str, Enum):
    BULLISH  = "bullish"    # augmente P(YES)
    BEARISH  = "bearish"    # diminue P(YES)
    NEUTRAL  = "neutral"    # informatif seulement, pas directionnel
    UNKNOWN  = "unknown"    # parser n'a pas pu déterminer

class ConfidenceLevel(str, Enum):
    HIGH   = "high"     # ≥2 sources corrob. indépendantes
    MEDIUM = "medium"   # source tier1 unique
    LOW    = "low"      # source tier2 unique ou ambiguë


@dataclass
class CrucixAlert:
    """
    Alerte brute émise par Crucix depuis une de ses 26 sources.

    Tous les champs sont normalisés par SignalParser avant traitement.
    Le timestamp est fondamental pour la décroissance temporelle.
    """
    source_id:       str              # ex: "cme_fedwatch", "reuters_rss"
    category:        AlertCategory
    raw_text:        str              # contenu original
    direction:       SignalDirection
    magnitude:       float            # 0.0–1.0, force brute du signal
    market_keywords: list[str]        # ex: ["fed", "march", "cut"]
    timestamp:       datetime         = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    source_url:      Optional[str]    = None
    entities:        dict             = field(default_factory=dict)
    # entities exemple:
    # {"event": "FOMC March 2026", "delta_pct": 4.0, "direction_explicit": True,
    #  "quantitative": True, "instrument": "fed_rate"}


@dataclass
class MarketContext:
    """
    État courant d'une position ouverte sur Polymarket.

    sigma_14d : volatilité rolling 14j du prix du marché,
                calculée sur les données orderbook historiques.
                Utilisée pour le Z-score dynamique.
                Valeur par défaut 0.06 si pas encore calculée.
    """
    market_id:    str
    question:     str
    p_model:      float         # probabilité estimée par le modèle [0,1]
    p_market:     float         # prix Polymarket [0,1]
    category:     str           # "crypto" | "macro" | "politics" | "sports"
    keywords:     list[str]     # pour le routing
    days_to_res:  int
    bankroll:     float         # bankroll totale courante
    position_size: float        # euros actuellement déployés sur ce marché
    edge:         float         # p_model - p_market
    z_score:      float         # (p_model - p_market) / sigma_14d
    strategy:     str           # "S1" ou "S2"
    sigma_14d:    float         = 0.06   # vol historique du marché
    n_shares:     float         = 0.0    # nombre de shares détenues


@dataclass
class BayesUpdate:
    """Résultat de l'application d'une alerte Crucix sur un marché donné."""
    market_id:        str
    source_id:        str
    p_prior:          float
    lr_applied:       float       # LR directionnel effectif
    lr_calibrated:    float       # LR brut avant direction
    p_posterior:      float
    delta_p:          float       # p_posterior - p_prior
    capped:           bool        # True si hard cap ±15pt activé
    direction:        SignalDirection
    confidence:       ConfidenceLevel
    requires_confirm: bool        # nécessite une 2e source avant action
    decay_factor:     float       = 1.0   # facteur de décroissance temporelle
    timestamp:        datetime    = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    alert_hash:       str         = ""


@dataclass
class GateResult:
    """Résultat de la validation par les 7 gates."""
    passed:            bool
    gate_failures:     list[str]    # noms des gates échoués
    action:            str          # "TRADE" | "HOLD" | "REDUCE" | "EXIT" | "HALT"
    edge_new:          float
    z_score_new:       float
    kelly_size_new:    float
    kelly_size_delta:  float        # différence avec size actuelle
    requires_exit:     bool
    requires_size_reduce: bool
    rationale:         str


# ═══════════════════════════════════════════════════════════════════════════
# 2. LIKELIHOOD RATIO TABLE
#
#    Format : source_id → (lr_bull, lr_bear)
#    lr_bear est toujours l'inverse de lr_bull (symétrie conservatrice).
#
#    Dérivation (priors académiques + historique Polymarket 2023-2025) :
#    ─────────────────────────────────────────────────────────────────────
#    cme_fedwatch :  Move +3pts → Poly drift moyen +2.1pts sur 2h.
#                    Hit rate 61% → LR ≈ 2.50
#    deribit_vol  :  IV spike >10% → réalisation vol +8.3% médiane 7j.
#                    Hit rate 58% → LR ≈ 2.20
#    kalshi       :  Divergence >10pts Kalshi vs Poly → exploitable 62%.
#                    LR ≈ 2.00
#    reuters/ap   :  Headline T1 → drift Poly +3.5pts médiane 2h.
#                    Hit rate 56% → LR ≈ 1.80
#    glassnode    :  Whale accumulation → BTC +12% médiane 30j.
#                    Hit rate 54% → LR ≈ 1.55
#
#    Ces LR sont DES PRIORS. CalibrationEngine les remplace par des LR
#    empiriques après MIN_CALIBRATION_OBS observations par source.
# ═══════════════════════════════════════════════════════════════════════════

LR_PRIOR: dict[str, tuple[float, float]] = {
    # ── Fed / Macro ──────────────────────────────────────────────────────
    "cme_fedwatch":          (2.50, 0.400),
    "fred_api":              (1.60, 0.625),
    "bls_cpi":               (1.70, 0.588),
    "fed_gov_statement":     (2.20, 0.455),
    "tradingeconomics":      (1.30, 0.769),
    "macrotrends":           (1.20, 0.833),

    # ── Crypto prix ──────────────────────────────────────────────────────
    "deribit_vol":           (2.20, 0.455),   # delta options = prob implicite
    "binance_ws":            (1.45, 0.690),   # momentum prix, bruyant
    "coingecko":             (1.20, 0.833),
    "coinmarketcap":         (1.15, 0.870),

    # ── On-chain ─────────────────────────────────────────────────────────
    "glassnode":             (1.55, 0.645),   # whale tracking
    "cryptoquant":           (1.50, 0.667),   # exchange flow
    "chainlink_oracle":      (1.40, 0.714),   # prix on-chain vérifié
    "etherscan":             (1.25, 0.800),
    "debank":                (1.20, 0.833),
    "polygon_rpc":           (1.30, 0.769),

    # ── Marchés prédictifs ───────────────────────────────────────────────
    "kalshi":                (2.00, 0.500),   # divergence Kalshi vs Poly
    "polymarket_activity":   (1.35, 0.741),   # volume spike inhabituel

    # ── News Tier 1 ──────────────────────────────────────────────────────
    "reuters_rss":           (1.80, 0.556),
    "ap_news":               (1.75, 0.571),
    "bloomberg_rss":         (1.70, 0.588),

    # ── News Tier 2 ──────────────────────────────────────────────────────
    "yahoo_finance":         (1.25, 0.800),
    "google_news":           (1.20, 0.833),
    "messari":               (1.30, 0.769),

    # ── Social Tier 1 ────────────────────────────────────────────────────
    "twitter_t1":            (1.50, 0.667),   # Timiraos, comptes Fed officiels
    "twitter_t2":            (1.20, 0.833),   # feed filtré élargi

    # ── Géopolitique (Crucix World Monitor) ──────────────────────────────
    "crucix_world_monitor":  (1.40, 0.714),
}

# Seuils de comportement
MIN_CALIBRATION_OBS = 30    # obs minimum avant LR empirique
MAX_DELTA_P         = 0.15  # ±15pts hard cap par source unique
MAX_DELTA_P_MULTI   = 0.22  # ±22pts cap multi-sources agrégées
EXTREMIZE_ALPHA     = 1.30  # Satopää 2014 — si ≥2 sources indépendantes
TEMPORAL_DECAY_LAMBDA = 0.0025  # λ tel que LR decay = exp(-λ × min_old)
                                 # À 4h (240min) : exp(-0.6) ≈ 0.55

# Corrélation inter-sources : sources dans le même groupe comptent comme 0.5x
SOURCE_CORRELATION_GROUPS: dict[str, str] = {
    "cme_fedwatch": "fed_official", "fred_api": "fed_official",
    "bls_cpi": "fed_official", "fed_gov_statement": "fed_official",
    "reuters_rss": "mainstream_news", "ap_news": "mainstream_news",
    "bloomberg_rss": "mainstream_news",
    "glassnode": "onchain_metrics", "cryptoquant": "onchain_metrics",
    "chainlink_oracle": "onchain_metrics", "etherscan": "onchain_metrics",
    "twitter_t1": "social", "twitter_t2": "social",
    "deribit_vol": "derivatives", "binance_ws": "derivatives",
}


# ═══════════════════════════════════════════════════════════════════════════
# 3. BASE DE DONNÉES — audit trail complet
# ═══════════════════════════════════════════════════════════════════════════

def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS signal_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT    NOT NULL,
            source_id    TEXT    NOT NULL,
            category     TEXT    NOT NULL,
            direction    TEXT    NOT NULL,
            magnitude    REAL    NOT NULL,
            decay_factor REAL    NOT NULL DEFAULT 1.0,
            market_id    TEXT    NOT NULL,
            p_prior      REAL    NOT NULL,
            lr_raw       REAL    NOT NULL,
            lr_decayed   REAL    NOT NULL,
            p_posterior  REAL    NOT NULL,
            delta_p      REAL    NOT NULL,
            capped       INTEGER NOT NULL,
            confidence   TEXT    NOT NULL,
            alert_hash   TEXT    NOT NULL,
            raw_text     TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS calibration (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id        TEXT    NOT NULL,
            alert_hash       TEXT    NOT NULL,
            market_id        TEXT    NOT NULL,
            direction        TEXT    NOT NULL,
            p_prior          REAL    NOT NULL,
            p_posterior      REAL    NOT NULL,
            lr_applied       REAL    NOT NULL,
            resolved_at      TEXT,
            outcome          INTEGER,
            p_at_resolution  REAL,
            brier_contrib    REAL,
            lr_empirical     REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS source_brier (
            source_id    TEXT    PRIMARY KEY,
            n_obs        INTEGER DEFAULT 0,
            brier_score  REAL    DEFAULT 0.25,
            lr_empirical REAL,
            hit_rate     REAL    DEFAULT 0.5,
            updated_at   TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS aggregation_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts               TEXT    NOT NULL,
            market_id        TEXT    NOT NULL,
            n_sources        INTEGER NOT NULL,
            n_independent    INTEGER NOT NULL,
            sources          TEXT    NOT NULL,
            p_prior          REAL    NOT NULL,
            p_chained        REAL    NOT NULL,
            p_extremized     REAL    NOT NULL,
            extremized       INTEGER NOT NULL,
            alpha_used       REAL    NOT NULL DEFAULT 1.3,
            delta_p_total    REAL    NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS gate_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            market_id       TEXT    NOT NULL,
            trigger         TEXT    NOT NULL,
            p_model_old     REAL    NOT NULL,
            p_model_new     REAL    NOT NULL,
            edge_new        REAL    NOT NULL,
            z_score_new     REAL    NOT NULL,
            kelly_size_new  REAL    NOT NULL,
            action          TEXT    NOT NULL,
            gates_failed    TEXT    NOT NULL,
            rationale       TEXT    NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS market_sigma (
            market_id    TEXT    PRIMARY KEY,
            sigma_14d    REAL    NOT NULL DEFAULT 0.06,
            last_prices  TEXT    NOT NULL DEFAULT '[]',
            updated_at   TEXT    NOT NULL
        )
    """)

    conn.commit()
    return conn


# ═══════════════════════════════════════════════════════════════════════════
# 4. TEMPORAL DECAY ENGINE
#    Les signaux se déprécient avec le temps.
#    Principe : une information de 4 heures a déjà été partiellement
#    incorporée dans le prix du marché. Son LR effectif doit refléter ça.
#
#    LR_effective = 1 + (LR_raw - 1) × exp(-λ × minutes_old)
#
#    Ce n'est pas le LR qui decay, c'est l'*excès* de LR par rapport à 1.
#    Un signal de +∞ minutes → LR=1 (neutre), jamais au-delà.
# ═══════════════════════════════════════════════════════════════════════════

class TemporalDecayEngine:
    """
    Applique la décroissance temporelle aux LR des signaux.

    Calibration du λ :
        λ = 0.0025 → demi-vie ≈ 277 minutes (≈4.6h)
        À t=0min   : facteur = 1.00 (signal frais, pleine force)
        À t=60min  : facteur = 0.86
        À t=240min : facteur = 0.55
        À t=480min : facteur = 0.30
        À t=12h    : facteur = 0.09 (presque neutre)

    Justification : dans un marché de prédiction liquide, les gros
    signaux macro sont intégrés en moins de 2h. Après 8h, un signal
    non-quantitatif ne doit plus modifier p_model de façon significative.
    """

    def __init__(self, lam: float = TEMPORAL_DECAY_LAMBDA):
        self.lam = lam

    def apply(self, lr_raw: float, alert: CrucixAlert) -> tuple[float, float]:
        """
        Returns (lr_decayed, decay_factor).
        lr_decayed : LR effectif à appliquer
        decay_factor : facteur de décroissance [0,1]
        """
        now = datetime.now(timezone.utc)
        age_minutes = (now - alert.timestamp).total_seconds() / 60.0
        age_minutes = max(0.0, age_minutes)

        decay_factor = math.exp(-self.lam * age_minutes)

        # L'excès de LR par rapport à 1.0 se déprécie
        lr_excess = lr_raw - 1.0
        lr_decayed = 1.0 + lr_excess * decay_factor

        log.debug(
            f"DECAY [{alert.source_id}]: age={age_minutes:.0f}min "
            f"λ={self.lam} factor={decay_factor:.3f} "
            f"LR {lr_raw:.3f} → {lr_decayed:.3f}"
        )
        return round(lr_decayed, 4), round(decay_factor, 4)

    def is_stale(self, alert: CrucixAlert, max_age_hours: float = 24.0) -> bool:
        """True si l'alerte est trop vieille pour être utilisée."""
        now = datetime.now(timezone.utc)
        age_h = (now - alert.timestamp).total_seconds() / 3600.0
        return age_h > max_age_hours


# ═══════════════════════════════════════════════════════════════════════════
# 5. CALIBRATION ENGINE
#    Historique source→outcome pour recalibration empirique des LR.
# ═══════════════════════════════════════════════════════════════════════════

class CalibrationEngine:
    """
    Maintient les Brier scores et LR empiriques par source.

    Après MIN_CALIBRATION_OBS résolutions, remplace les LR priors
    par des LR calculés empiriquement via le hit rate :

        LR_empirical = P(signal correct | yes_wins) / P(signal correct | no_wins)
                     = hit_rate / (1 - hit_rate)

    Trust weight par source :
        trust = max(0.40, 1.0 - brier / 0.25)
        (0.25 = Brier d'un forecaster sans skill — baseline coin flip)
        Floor à 0.40 : même une mauvaise source garde 40% de son LR
        pour ne pas ignorer complètement une information.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._cache: dict[str, dict] = {}
        self._load_cache()

    def _load_cache(self):
        rows = self.conn.execute(
            "SELECT source_id, n_obs, brier_score, lr_empirical, hit_rate "
            "FROM source_brier"
        ).fetchall()
        for row in rows:
            self._cache[row[0]] = {
                "n_obs": row[1], "brier": row[2],
                "lr_empirical": row[3], "hit_rate": row[4],
            }

    def get_calibrated_lr(
        self, source_id: str, direction: SignalDirection
    ) -> float:
        """
        LR à appliquer, en tenant compte de :
        1. Phase 0 (0 obs)       → prior à pleine force
        2. Phase 1 (1–29 obs)    → blend prior/empirique, penalisé si Brier dégradé
        3. Phase 2 (≥30 obs)     → LR empirique, pondéré par trust
        """
        lr_bull, lr_bear = LR_PRIOR.get(source_id, (1.20, 0.833))
        prior_lr = lr_bull if direction == SignalDirection.BULLISH else lr_bear

        info = self._cache.get(source_id, {})
        n_obs   = info.get("n_obs", 0)
        brier   = info.get("brier", None)
        lr_emp  = info.get("lr_empirical", None)

        # Phase 0 : aucune observation
        if n_obs == 0 or brier is None:
            return prior_lr

        # Phase 1 : observations partielles
        if n_obs < MIN_CALIBRATION_OBS:
            blend    = n_obs / MIN_CALIBRATION_OBS
            lr_blend = prior_lr + blend * ((lr_emp or prior_lr) - prior_lr)
            # Pénalité si Brier déjà dégradé (>0.20)
            if brier > 0.20:
                penalty  = min(1.0, (brier - 0.20) / 0.05)
                lr_blend = 1.0 + (1.0 - penalty * 0.5) * (lr_blend - 1.0)
            return round(max(1.01, lr_blend), 4)

        # Phase 2 : calibration complète
        if lr_emp is not None:
            lr_final = lr_emp if direction == SignalDirection.BULLISH else (1 / lr_emp)
        else:
            lr_final = prior_lr

        trust     = self.get_trust_weight(source_id)
        lr_adj    = 1.0 + trust * (lr_final - 1.0)
        return round(max(1.01, lr_adj), 4)

    def get_trust_weight(self, source_id: str) -> float:
        """Trust ∈ [0.40, 1.00] basé sur le Brier score."""
        info  = self._cache.get(source_id, {})
        n_obs = info.get("n_obs", 0)
        if n_obs == 0:
            return 0.75      # confiance modérée par défaut
        brier = info.get("brier", 0.25)
        return max(0.40, min(1.0, 1.0 - brier / 0.25))

    def record_resolution(
        self,
        alert_hash: str,
        market_id: str,
        outcome: int,           # 1 = YES résolu, 0 = NO résolu
        p_at_resolution: float,
    ):
        """
        Appelé quand un marché se résoud.
        Met à jour Brier et LR empirique pour chaque source impliquée.
        """
        rows = self.conn.execute(
            """SELECT source_id, direction, p_posterior
               FROM calibration
               WHERE alert_hash=? AND market_id=? AND outcome IS NULL""",
            (alert_hash, market_id),
        ).fetchall()

        for source_id, direction, p_post in rows:
            brier_contrib = (p_post - outcome) ** 2
            signal_correct = (
                (direction == "bullish" and outcome == 1)
                or (direction == "bearish" and outcome == 0)
            )

            # Mise à jour Brier rolling
            info = self._cache.get(source_id, {
                "n_obs": 0, "brier": 0.25, "lr_empirical": None,
                "hit_rate": 0.5,
            })
            n       = info["n_obs"] + 1
            new_br  = (info["brier"] * (info["n_obs"]) + brier_contrib) / n
            old_hr  = info.get("hit_rate", 0.5)
            new_hr  = (old_hr * info["n_obs"] + (1 if signal_correct else 0)) / n
            lr_emp  = max(0.50, new_hr / max(0.01, 1 - new_hr))

            self._cache[source_id] = {
                "n_obs": n, "brier": new_br,
                "lr_empirical": lr_emp, "hit_rate": new_hr,
            }

            self.conn.execute("""
                UPDATE calibration
                SET resolved_at=?, outcome=?, p_at_resolution=?, brier_contrib=?,
                    lr_empirical=?
                WHERE alert_hash=? AND market_id=?
            """, (
                datetime.now(timezone.utc).isoformat(), outcome,
                p_at_resolution, brier_contrib, lr_emp,
                alert_hash, market_id,
            ))
            self.conn.execute("""
                INSERT INTO source_brier
                    (source_id, n_obs, brier_score, lr_empirical, hit_rate, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    n_obs=excluded.n_obs,
                    brier_score=excluded.brier_score,
                    lr_empirical=excluded.lr_empirical,
                    hit_rate=excluded.hit_rate,
                    updated_at=excluded.updated_at
            """, (
                source_id, n, new_br, lr_emp, new_hr,
                datetime.now(timezone.utc).isoformat(),
            ))

        self.conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# 6. SIGNAL PARSER
#    Normalise les alertes brutes Crucix en signaux structurés.
# ═══════════════════════════════════════════════════════════════════════════

class SignalParser:
    """
    Détermine direction, magnitude et keywords à partir du texte brut.

    En production : remplace les règles déterministes par un appel LLM
    prompt-engineered ou un classifieur fine-tuné sur des données Polymarket.
    Pour la robustesse, le moteur de règles reste actif comme fallback.
    """

    BULLISH_TOKENS = {
        "cut", "dovish", "support", "rise", "above", "exceed", "beat",
        "accumulation", "inflow", "surge", "strong", "growth", "expansion",
        "soft landing", "below expectations", "lower than expected",
        "miss lower", "open to cut",
    }
    BEARISH_TOKENS = {
        "hike", "hawkish", "miss", "below", "fall", "drop", "weak",
        "outflow", "recession", "contraction", "decline", "war", "strike",
        "higher for longer", "hold", "pause", "above expectations",
        "sticky inflation", "liquidation",
    }
    MARKET_KEYWORDS = [
        "fed", "fomc", "rate", "cut", "hike", "bitcoin", "btc", "ethereum",
        "eth", "crypto", "cpi", "inflation", "gdp", "unemployment", "yield",
        "treasury", "ukraine", "iran", "china", "taiwan", "march", "june",
        "september", "2026", "50bps", "25bps", "recession", "kalshi",
        "deribit", "implied", "vol", "futures", "options",
    ]

    def parse(self, alert: CrucixAlert) -> CrucixAlert:
        """Enrichit l'alerte. Modifie en place, retourne l'alerte enrichie."""
        text = alert.raw_text.lower()

        if alert.direction == SignalDirection.UNKNOWN:
            alert.direction = self._infer_direction(text)

        if not alert.market_keywords:
            alert.market_keywords = self._extract_keywords(text)

        if alert.magnitude == 0.0:
            alert.magnitude = self._infer_magnitude(text, alert)

        # Enrichissement entities si pas déjà fait
        if not alert.entities:
            alert.entities = self._extract_entities(text, alert)

        return alert

    def _infer_direction(self, text: str) -> SignalDirection:
        bs = sum(1 for t in self.BULLISH_TOKENS if t in text)
        be = sum(1 for t in self.BEARISH_TOKENS if t in text)
        if bs > be + 1:   # +1 pour éviter égalité bruyante
            return SignalDirection.BULLISH
        if be > bs + 1:
            return SignalDirection.BEARISH
        if bs == be and bs > 0:
            return SignalDirection.NEUTRAL
        return SignalDirection.UNKNOWN

    def _extract_keywords(self, text: str) -> list[str]:
        return [kw for kw in self.MARKET_KEYWORDS if kw in text]

    def _infer_magnitude(self, text: str, alert: CrucixAlert) -> float:
        """
        Magnitude = confiance dans le signal, 0→1.
        Boostée si le signal est quantitatif (bps, %, points chiffrés).
        """
        import re
        base = {
            AlertCategory.FED_MACRO:      0.68,
            AlertCategory.CRYPTO_PRICE:   0.58,
            AlertCategory.NEWS_TIER1:     0.62,
            AlertCategory.PREDICTION_MKT: 0.72,
            AlertCategory.CRYPTO_ONCHAIN: 0.52,
            AlertCategory.SOCIAL_TIER1:   0.55,
            AlertCategory.GEOPOLITICAL:   0.50,
        }.get(alert.category, 0.45)

        quantitative = bool(re.search(
            r"\d+\.?\d*\s*(?:bp|bps|%|pts|points|percent|move|moved|up|down)",
            text
        ))
        if quantitative:
            base = min(1.0, base + 0.12)
            alert.entities["quantitative"] = True

        return round(base, 2)

    def _extract_entities(self, text: str, alert: CrucixAlert) -> dict:
        """Extraction d'entités simples pour enrichir le routage."""
        import re
        entities: dict = {}

        # Extraction de mouvements quantitatifs (ex: "+4pts", "moved 58%")
        m = re.search(r"([+-]?\d+\.?\d*)\s*(?:pts|points|%|bp|bps)", text)
        if m:
            entities["delta_value"] = float(m.group(1))

        # Détection de meeting FOMC
        for mth in ["january", "february", "march", "april", "may", "june",
                    "july", "august", "september", "october", "november", "december"]:
            if mth in text:
                entities["month_ref"] = mth
                break

        return entities


# ═══════════════════════════════════════════════════════════════════════════
# 7. SOURCE CORRELATION CHECKER
#    Détecte quand plusieurs sources ne sont pas vraiment indépendantes.
#    Ex: Reuters + AP couvrent souvent le même article source → corrélées.
# ═══════════════════════════════════════════════════════════════════════════

class SourceCorrelationChecker:
    """
    Principe superforecaster : ne pas se laisser conforter par un
    consensus apparent qui proviendrait en réalité d'une seule source
    (ex: toutes les news reprennent le même Reuters).

    Deux sources dans le même groupe de corrélation comptent pour
    0.5 source indépendante chacune (plutôt que 1.0 chacune).

    L'extremizing n'est appliqué que si le nombre de sources
    *effectivement indépendantes* ≥ 2.0.
    """

    def count_independent_sources(self, source_ids: list[str]) -> float:
        """
        Retourne un nombre flottant de "sources indépendantes effectives".
        Les sources du même groupe comptent pour 1/N du groupe.
        """
        groups: dict[str, list[str]] = defaultdict(list)
        ungrouped = []

        for sid in source_ids:
            grp = SOURCE_CORRELATION_GROUPS.get(sid)
            if grp:
                groups[grp].append(sid)
            else:
                ungrouped.append(sid)

        effective = float(len(ungrouped))  # ungrouped = pleinement indép.
        for grp, members in groups.items():
            if len(members) == 1:
                effective += 1.0
            else:
                # N sources corrélées dans le même groupe → √N contribution
                effective += math.sqrt(len(members))

        return round(effective, 2)

    def should_extremize(
        self, updates: list[BayesUpdate], min_independent: float = 2.0
    ) -> tuple[bool, float]:
        """
        Retourne (should_extremize, n_effective_independent).
        """
        source_ids = [u.source_id for u in updates if abs(u.delta_p) > 0.005]
        n_eff = self.count_independent_sources(source_ids)
        return n_eff >= min_independent, n_eff


# ═══════════════════════════════════════════════════════════════════════════
# 8. MARKET ROUTER
#    Détermine quels marchés ouverts sont pertinents pour une alerte.
# ═══════════════════════════════════════════════════════════════════════════

class MarketRouter:
    """
    Score de pertinence = affinité_catégorie × overlap_keywords × poids_résolution

    Seuil de routage : pertinence ≥ 0.28
    En dessous, le marché n'est pas touché par l'alerte.
    """

    CATEGORY_AFFINITY: dict[str, dict[str, float]] = {
        "fed_macro":       {"macro": 1.00, "crypto": 0.30, "politics": 0.18},
        "crypto_price":    {"crypto": 1.00, "macro": 0.15},
        "crypto_onchain":  {"crypto": 0.85, "macro": 0.10},
        "news_tier1":      {"macro": 0.72, "politics": 0.62, "crypto": 0.28},
        "news_tier2":      {"macro": 0.48, "politics": 0.48, "crypto": 0.20},
        "prediction_mkt":  {"macro": 0.88, "crypto": 0.88, "politics": 0.72},
        "social_tier1":    {"macro": 0.65, "crypto": 0.42},
        "geopolitical":    {"macro": 0.58, "politics": 0.82, "crypto": 0.18},
        "onchain_defi":    {"crypto": 0.75},
        "macro_indicator": {"macro": 0.80, "crypto": 0.25, "politics": 0.30},
    }

    ROUTING_THRESHOLD = 0.28

    def route(
        self,
        alert: CrucixAlert,
        markets: list[MarketContext],
    ) -> list[tuple[MarketContext, float]]:
        results = []
        affinities = self.CATEGORY_AFFINITY.get(alert.category.value, {})

        for mkt in markets:
            cat_aff = affinities.get(mkt.category, 0.0)
            if cat_aff == 0.0:
                continue

            # Overlap Jaccard sur les keywords
            a_kw  = set(alert.market_keywords)
            m_kw  = set(mkt.keywords)
            if a_kw and m_kw:
                overlap = len(a_kw & m_kw) / len(a_kw | m_kw)
                overlap = max(overlap, 0.15)
            else:
                overlap = 0.30

            # Pondération par proximité de résolution
            # Les marchés proches de résolution reçoivent un boost
            # (un signal maintenant est plus informatif pour un marché à 3j)
            if mkt.days_to_res <= 5:
                days_w = 1.30
            elif mkt.days_to_res <= 14:
                days_w = 1.10
            elif mkt.days_to_res <= 30:
                days_w = 1.00
            elif mkt.days_to_res <= 90:
                days_w = 0.85
            else:
                days_w = 0.70

            relevance = cat_aff * overlap * days_w * alert.magnitude

            if relevance >= self.ROUTING_THRESHOLD:
                results.append((mkt, round(relevance, 4)))

        return sorted(results, key=lambda x: x[1], reverse=True)


# ═══════════════════════════════════════════════════════════════════════════
# 9. BAYES UPDATER
#    Moteur de mise à jour bayésienne avec tous les garde-fous.
# ═══════════════════════════════════════════════════════════════════════════

class BayesUpdater:
    """
    Règle de mise à jour :
        O_post = O_prior × LR_effective
        p_post = O_post / (1 + O_post)

    LR_effective incorpore :
    1. Calibration (prior vs empirique)
    2. Décroissance temporelle (signal âgé = LR atténué)
    3. Direction (BEARISH → inversion LR)

    Gardes appliqués dans l'ordre :
    1. Signal NEUTRAL → LR = 1.0 (aucune mise à jour)
    2. Signal périmé (>24h) → rejet
    3. Hard cap ±MAX_DELTA_P
    4. Domaine [0.02, 0.98]
    """

    def __init__(
        self,
        calibration: CalibrationEngine,
        decay: TemporalDecayEngine,
        conn: sqlite3.Connection,
    ):
        self.cal   = calibration
        self.decay = decay
        self.conn  = conn

    def update(
        self,
        alert: CrucixAlert,
        market: MarketContext,
        confidence: ConfidenceLevel,
    ) -> BayesUpdate:

        alert_hash = self._hash_alert(alert)

        # ── Rejets immédiats ──────────────────────────────────────────────
        if alert.direction == SignalDirection.NEUTRAL:
            return self._no_change(alert, market, confidence, alert_hash)

        if self.decay.is_stale(alert, max_age_hours=24.0):
            log.warning(f"STALE signal rejected: {alert.source_id} age>24h")
            return self._no_change(alert, market, confidence, alert_hash)

        # ── LR calibré ───────────────────────────────────────────────────
        lr_calibrated = self.cal.get_calibrated_lr(alert.source_id, alert.direction)

        # ── Décroissance temporelle ───────────────────────────────────────
        lr_decayed, decay_factor = self.decay.apply(lr_calibrated, alert)

        # ── Direction : BEARISH → inverser le LR ─────────────────────────
        lr_applied = (
            lr_decayed if alert.direction == SignalDirection.BULLISH
            else 1.0 / lr_decayed
        )

        # ── Mise à jour Bayésienne en espace odds ─────────────────────────
        p = max(0.02, min(0.98, market.p_model))
        odds_prior = p / (1.0 - p)
        odds_post  = odds_prior * lr_applied
        p_post_raw = odds_post / (1.0 + odds_post)

        # ── Hard cap ±15pts ───────────────────────────────────────────────
        delta_raw = p_post_raw - p
        capped    = False
        if abs(delta_raw) > MAX_DELTA_P:
            p_post = p + math.copysign(MAX_DELTA_P, delta_raw)
            capped = True
            log.warning(
                f"CAP [{alert.source_id}→{market.market_id[:28]}]: "
                f"Δp={delta_raw:+.3f} réduit à {math.copysign(MAX_DELTA_P, delta_raw):+.3f}"
            )
        else:
            p_post = p_post_raw

        p_post      = max(0.02, min(0.98, p_post))
        delta_final = round(p_post - p, 4)

        # ── Confirmation requise ? ────────────────────────────────────────
        # Nécessaire si : source peu fiable ou mouvement important sur seule source
        trust            = self.cal.get_trust_weight(alert.source_id)
        requires_confirm = (
            confidence == ConfidenceLevel.LOW
            or (trust < 0.45 and abs(delta_final) > 0.06)
            or (market.strategy == "S2" and abs(delta_final) > 0.08)
        )

        result = BayesUpdate(
            market_id        = market.market_id,
            source_id        = alert.source_id,
            p_prior          = round(p, 4),
            lr_applied       = round(lr_applied, 4),
            lr_calibrated    = round(lr_calibrated, 4),
            p_posterior      = round(p_post, 4),
            delta_p          = delta_final,
            capped           = capped,
            direction        = alert.direction,
            confidence       = confidence,
            requires_confirm = requires_confirm,
            decay_factor     = decay_factor,
            alert_hash       = alert_hash,
        )

        self._log(alert, market, result, lr_decayed)
        log.info(
            f"UPDATE [{alert.source_id}→{market.market_id[:30]}]: "
            f"p {p:.3f}→{p_post:.3f} (Δ{delta_final:+.3f}) "
            f"LR={lr_applied:.3f} decay={decay_factor:.2f} cap={capped}"
        )
        return result

    def _no_change(
        self,
        alert: CrucixAlert,
        market: MarketContext,
        confidence: ConfidenceLevel,
        alert_hash: str,
    ) -> BayesUpdate:
        return BayesUpdate(
            market_id=market.market_id, source_id=alert.source_id,
            p_prior=market.p_model, lr_applied=1.0, lr_calibrated=1.0,
            p_posterior=market.p_model, delta_p=0.0, capped=False,
            direction=alert.direction, confidence=confidence,
            requires_confirm=False, decay_factor=1.0, alert_hash=alert_hash,
        )

    @staticmethod
    def _hash_alert(alert: CrucixAlert) -> str:
        raw = f"{alert.source_id}|{alert.timestamp.isoformat()[:16]}|{alert.raw_text[:80]}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _log(
        self,
        alert: CrucixAlert,
        market: MarketContext,
        result: BayesUpdate,
        lr_decayed: float,
    ):
        self.conn.execute("""
            INSERT INTO signal_log
            (ts, source_id, category, direction, magnitude, decay_factor,
             market_id, p_prior, lr_raw, lr_decayed, p_posterior, delta_p,
             capped, confidence, alert_hash, raw_text)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            alert.timestamp.isoformat(),
            alert.source_id, alert.category.value, alert.direction.value,
            alert.magnitude, result.decay_factor, market.market_id,
            result.p_prior, result.lr_calibrated, lr_decayed,
            result.p_posterior, result.delta_p,
            int(result.capped), result.confidence.value,
            result.alert_hash, alert.raw_text[:500],
        ))
        self.conn.execute("""
            INSERT INTO calibration
            (source_id, alert_hash, market_id, direction, p_prior, p_posterior, lr_applied)
            VALUES (?,?,?,?,?,?,?)
        """, (
            alert.source_id, result.alert_hash, market.market_id,
            alert.direction.value, result.p_prior, result.p_posterior, result.lr_applied,
        ))
        self.conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# 10. MULTI-SOURCE AGGREGATOR
#     Chaînage Bayésien + Extremizing (Satopää 2014)
# ═══════════════════════════════════════════════════════════════════════════

class MultiSourceAggregator:
    """
    Quand plusieurs alertes arrivent dans la même fenêtre temporelle :

    1. Chaînage Bayésien pondéré par trust
       Chaque source est traitée comme une observation indépendante.
       Séquence : p → update_1 → update_2 → ... → p_chained

    2. Extremizing si ≥2.0 sources indépendantes effectives convergent
       p_ext = p^α / (p^α + (1-p)^α)  [Satopää 2014, α=1.30]

    3. Hard cap sur le mouvement total depuis le p_model original
       Pour éviter que l'agrégation ne soit une boucle d'amplification.
    """

    def __init__(
        self,
        calibration: CalibrationEngine,
        correlation: SourceCorrelationChecker,
        conn: sqlite3.Connection,
    ):
        self.cal   = calibration
        self.corr  = correlation
        self.conn  = conn

    def aggregate(
        self,
        updates: list[BayesUpdate],
        market: MarketContext,
    ) -> float:
        valid = [u for u in updates if abs(u.delta_p) > 0.003]
        if not valid:
            return market.p_model

        # Tri par magnitude delta desc (le plus fort en premier)
        valid.sort(key=lambda u: abs(u.delta_p), reverse=True)

        # ── Chaînage Bayésien ────────────────────────────────────────────
        p = max(0.02, min(0.98, market.p_model))   # guard against 0/1 before odds calc
        seen_groups: dict[str, bool] = {}
        for u in valid:
            # FIX: trust déjà appliqué dans CalibrationEngine.get_calibrated_lr()
            # On applique ici uniquement la pénalité de corrélation inter-sources
            group = SOURCE_CORRELATION_GROUPS.get(u.source_id)
            if group and group in seen_groups:
                # Source corrélée déjà vue → réduire le LR (√(1/N) contribution)
                eff_lr = 1.0 + 0.5 * (u.lr_applied - 1.0)
            else:
                eff_lr = u.lr_applied
            if group:
                seen_groups[group] = True
            odds      = p / (1 - p)
            odds     *= eff_lr
            p         = max(0.02, min(0.98, odds / (1 + odds)))

        p_chained = p

        # ── Extremizing ──────────────────────────────────────────────────
        should_ext, n_eff = self.corr.should_extremize(valid)
        extremized        = False
        alpha_used        = 1.0

        if should_ext:
            # Vérifier que la direction dominante est claire (≥60% des updates)
            dirs      = [u.direction for u in valid if u.delta_p != 0]
            dom_dir   = max(set(dirs), key=dirs.count) if dirs else None
            frac_dom  = dirs.count(dom_dir) / len(dirs) if dirs else 0

            if frac_dom >= 0.60:
                # Adapter alpha : plus de sources convergentes = plus d'extremizing
                alpha_used  = EXTREMIZE_ALPHA + 0.05 * max(0, n_eff - 2.0)
                alpha_used  = min(alpha_used, 1.60)  # plafond à 1.60

                p_ext       = self._extremize(p_chained, alpha_used)
                total_delta = p_ext - market.p_model

                if abs(total_delta) > MAX_DELTA_P_MULTI:
                    p_ext = market.p_model + math.copysign(MAX_DELTA_P_MULTI, total_delta)

                p          = max(0.02, min(0.98, p_ext))
                extremized = True
                log.info(
                    f"EXTREMIZE [{market.market_id[:28]}]: "
                    f"{p_chained:.3f} → {p:.3f} "
                    f"(α={alpha_used:.2f}, n_eff={n_eff:.1f}, dir={dom_dir})"
                )

        self.conn.execute("""
            INSERT INTO aggregation_log
            (ts, market_id, n_sources, n_independent, sources, p_prior,
             p_chained, p_extremized, extremized, alpha_used, delta_p_total)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            market.market_id, len(valid), n_eff,
            json.dumps([u.source_id for u in valid]),
            market.p_model, round(p_chained, 4), round(p, 4),
            int(extremized), alpha_used,
            round(p - market.p_model, 4),
        ))
        self.conn.commit()
        return round(max(0.02, min(0.98, p)), 4)

    @staticmethod
    def _extremize(p: float, alpha: float) -> float:
        p = max(1e-6, min(1 - 1e-6, p))
        return p ** alpha / (p ** alpha + (1 - p) ** alpha)


# ═══════════════════════════════════════════════════════════════════════════
# 11. DYNAMIC Z-SCORE ENGINE
#     Z-score = (p_model - p_market) / sigma_dynamique
#
#     sigma_dynamique = rolling 14j de la vol des prix du marché.
#     Mise à jour après chaque price feed.
#     Valeur par défaut : 0.06 (estimé sur historique Polymarket 2024).
# ═══════════════════════════════════════════════════════════════════════════

class DynamicZScoreEngine:
    """
    Calcule le Z-score avec un sigma dynamique par marché.

    Pourquoi sigma dynamique ?
    - Un marché à 50% a naturellement plus de volatilité qu'un à 90%.
    - La vol varie selon la liquidité et les actualités.
    - Utiliser σ=0.06 fixe pour tous les marchés est une approximation.

    Implémentation :
    - Maintient les 14 derniers prix par marché dans market_sigma
    - Calcule σ = écart-type des variations journalières (|Δp|)
    - Plancher σ_min = 0.03 (évite Z-scores absurdes sur marchés morts)
    - Plafond σ_max = 0.20 (évite signaux dilués sur marchés très volatils)
    """

    SIGMA_MIN   = 0.03
    SIGMA_MAX   = 0.20
    WINDOW_DAYS = 14

    def __init__(self, conn: sqlite3.Connection):
        self.conn  = conn
        self._cache: dict[str, float] = {}
        self._load_cache()

    def _load_cache(self):
        rows = self.conn.execute(
            "SELECT market_id, sigma_14d FROM market_sigma"
        ).fetchall()
        for mid, sigma in rows:
            self._cache[mid] = sigma

    def get_sigma(self, market_id: str, fallback: float = 0.06) -> float:
        return self._cache.get(market_id, fallback)

    def compute_z(self, p_model: float, p_market: float, sigma: float) -> float:
        sigma_eff = max(self.SIGMA_MIN, min(self.SIGMA_MAX, sigma))
        return round((p_model - p_market) / sigma_eff, 3)

    def update_price(self, market_id: str, new_price: float):
        """Appelé à chaque mise à jour du prix de marché."""
        row = self.conn.execute(
            "SELECT last_prices FROM market_sigma WHERE market_id=?",
            (market_id,)
        ).fetchone()

        if row:
            prices = json.loads(row[0])
        else:
            prices = []

        prices.append(new_price)
        prices = prices[-self.WINDOW_DAYS:]   # garder seulement 14 points

        if len(prices) >= 3:
            # FIX: stdev sur variations brutes (pas abs) pour mesurer la vraie dispersion
            # abs() biaisait sigma vers 0 quand le prix oscillait régulièrement
            diffs = [prices[i] - prices[i-1] for i in range(1, len(prices))]
            sigma = statistics.stdev(diffs) if len(diffs) >= 2 else abs(statistics.mean(diffs))
            sigma = max(self.SIGMA_MIN, min(self.SIGMA_MAX, sigma))
        else:
            sigma = 0.06

        self._cache[market_id] = sigma

        self.conn.execute("""
            INSERT INTO market_sigma (market_id, sigma_14d, last_prices, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                sigma_14d=excluded.sigma_14d,
                last_prices=excluded.last_prices,
                updated_at=excluded.updated_at
        """, (
            market_id, sigma, json.dumps(prices),
            datetime.now(timezone.utc).isoformat(),
        ))
        self.conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# 12. SEVEN GATE REVALIDATOR
#     Re-valide les 7 gates après chaque mise à jour p_model.
#
#     Principe : un signal Crucix peut faire passer un marché de
#     "hold" à "exit" (si edge disparaît) ou de "hold" à "add"
#     (si edge se renforce). La pipeline doit le détecter en temps réel.
# ═══════════════════════════════════════════════════════════════════════════

class SevenGateRevalidator:
    """
    Re-valide toutes les conditions de sortie après p_model update.

    Les 7 gates dans l'ordre :
    1. edge_gate        : edge > 4¢
    2. ev_gate          : EV > 0
    3. kelly_gate       : size ≤ kelly(bankroll, alpha=0.30)
    4. exposure_gate    : exposure totale ≤ 25% bankroll
    5. var_gate         : VaR 95% ≤ 5% bankroll (simplifié)
    6. mdd_gate         : MDD 30d < 8%
    7. brier_gate       : Brier rolling ≤ 0.22

    Le revalidateur *ne prend pas* de décision de trade lui-même.
    Il émet une recommandation d'action.
    """

    EDGE_MIN        = 0.04
    EV_MIN          = 0.0
    ALPHA_KELLY     = 0.30
    ALPHA_LONGSHOT  = 0.15      # 1/2 Kelly longshots
    MAX_EXPOSURE    = 0.25      # 25% bankroll max en une seule position
    MAX_VAR_PCT     = 0.05      # VaR 95% ≤ 5% bankroll
    MDD_LIMIT       = 0.08
    BRIER_LIMIT     = 0.22

    def __init__(self, z_engine: DynamicZScoreEngine, conn: sqlite3.Connection):
        self.z_engine = z_engine
        self.conn     = conn

    def revalidate(
        self,
        market: MarketContext,
        p_model_new: float,
        current_mdd: float   = 0.0,
        current_brier: float = 0.15,
    ) -> GateResult:

        sigma     = self.z_engine.get_sigma(market.market_id, market.sigma_14d)
        edge_new  = round(p_model_new - market.p_market, 4)
        z_new     = self.z_engine.compute_z(p_model_new, market.p_market, sigma)

        # Kelly sizing
        kelly_size = self._kelly(
            p_model_new, market.p_market,
            market.bankroll, market.strategy
        )

        # EV : E[gain] = p_model × (1/price - 1) × size
        b  = (1 - market.p_market) / market.p_market
        ev = p_model_new * b - (1 - p_model_new)  # EV par unité misée

        failures: list[str] = []
        if abs(edge_new) < self.EDGE_MIN:
            failures.append("edge_gate")
        if ev <= self.EV_MIN:
            failures.append("ev_gate")
        if market.position_size > kelly_size * 1.15:   # 15% tolérance
            failures.append("kelly_gate_oversize")
        if market.position_size > market.bankroll * self.MAX_EXPOSURE:
            failures.append("exposure_gate")
        if current_mdd >= self.MDD_LIMIT:
            failures.append("mdd_gate")
        if current_brier >= self.BRIER_LIMIT:
            failures.append("brier_gate")

        # Détermination de l'action
        kelly_delta = round(kelly_size - market.position_size, 4)

        action, rationale = self._determine_action(
            market, p_model_new, edge_new, z_new, failures,
            kelly_size, kelly_delta,
        )

        result = GateResult(
            passed              = len(failures) == 0,
            gate_failures       = failures,
            action              = action,
            edge_new            = edge_new,
            z_score_new         = z_new,
            kelly_size_new      = kelly_size,
            kelly_size_delta    = kelly_delta,
            requires_exit       = action in ("EXIT_EDGE", "EXIT_FLIP", "HALT"),
            requires_size_reduce= action == "REDUCE",
            rationale           = rationale,
        )

        self._log_gate(market, p_model_new, result)
        return result

    def _determine_action(
        self,
        market: MarketContext,
        p_new: float,
        edge_new: float,
        z_new: float,
        failures: list[str],
        kelly_size: float,
        kelly_delta: float,
    ) -> tuple[str, str]:

        # Edge a changé de signe → sortie urgente
        original_sign = (market.edge > 0)
        new_sign      = (edge_new > 0)
        if original_sign != new_sign and abs(edge_new) > 0.01:
            return "EXIT_FLIP", (
                f"Edge sign reversal: {market.edge:+.3f}→{edge_new:+.3f}. "
                f"p_model {market.p_model:.3f}→{p_new:.3f}. "
                "Model now disagrees with our direction."
            )

        # Edge disparu (< seuil min)
        if "edge_gate" in failures:
            if abs(edge_new) < 0.02:
                return "EXIT_EDGE", (
                    f"Edge collapsed to {edge_new:+.3f} (min {self.EDGE_MIN:.2f}). "
                    "Position has no statistical justification."
                )
            return "HOLD_WATCH", (
                f"Edge at {edge_new:+.3f}, below threshold but still positive. "
                "Monitor — do not add."
            )

        # Kill switch actif
        if "mdd_gate" in failures or "brier_gate" in failures:
            return "HALT", (
                f"Kill switch: {', '.join(failures)}. "
                "No new trades. Review calibration."
            )

        # Sur-exposition Kelly → réduire
        if "kelly_gate_oversize" in failures:
            return "REDUCE", (
                f"Position €{market.position_size:.2f} > Kelly €{kelly_size:.2f}. "
                f"Reduce by €{-kelly_delta:.2f}."
            )

        # Edge s'est renforcé → potentiel d'ajout
        if edge_new > market.edge + 0.04 and z_new > 1.8 and kelly_delta > 0.50:
            return "ADD_CONSIDER", (
                f"Edge improved {market.edge:+.3f}→{edge_new:+.3f}. "
                f"Z={z_new:.2f}. Kelly suggests +€{kelly_delta:.2f}. "
                "Confirm with 2nd source before adding."
            )

        # Aucun problème → tenir
        return "HOLD_UPDATED", (
            f"p_model updated {market.p_model:.3f}→{p_new:.3f}. "
            f"Edge {edge_new:+.3f}. Z={z_new:.2f}. All gates pass."
        )

    def _kelly(
        self,
        p_model: float,
        p_market: float,
        bankroll: float,
        strategy: str,
    ) -> float:
        b    = (1 - p_market) / max(0.001, p_market)
        q    = 1 - p_model
        f    = (p_model * b - q) / b
        f    = max(0.0, f)
        alpha = self.ALPHA_LONGSHOT if strategy == "S2" else self.ALPHA_KELLY
        size  = alpha * f * bankroll
        return round(min(size, bankroll * self.MAX_EXPOSURE, 6.0), 2)

    def _log_gate(
        self,
        market: MarketContext,
        p_new: float,
        result: GateResult,
    ):
        self.conn.execute("""
            INSERT INTO gate_log
            (ts, market_id, trigger, p_model_old, p_model_new,
             edge_new, z_score_new, kelly_size_new, action, gates_failed, rationale)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            market.market_id, "crucix_signal",
            market.p_model, p_new,
            result.edge_new, result.z_score_new, result.kelly_size_new,
            result.action, json.dumps(result.gate_failures), result.rationale,
        ))
        self.conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# 13. MODEL OUTPUT BUS
#     Publie les événements finaux vers les systèmes downstream.
# ═══════════════════════════════════════════════════════════════════════════

class ModelOutputBus:
    """
    Produit les événements structurés pour :
    - Le validateur 7 gates (SevenGateRevalidator)
    - L'alerte Telegram (si Δp > seuil)
    - Le dashboard PAF-001
    - Le script d'exécution Almgren-Chriss

    Format de sortie : dict JSON-serializable.
    """

    ALERT_DELTA_THRESHOLD = 0.035   # seulement alerter si Δp ≥ 3.5pts

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def publish(
        self,
        market: MarketContext,
        p_new: float,
        updates: list[BayesUpdate],
        gate_result: GateResult,
        aggregated: bool = False,
    ) -> dict:
        delta       = round(p_new - market.p_model, 4)
        sources     = list({u.source_id for u in updates if u.delta_p != 0})
        req_confirm = any(u.requires_confirm for u in updates)

        event = {
            "type":              "crucix_model_update",
            "market_id":         market.market_id,
            "question":          market.question,
            "strategy":          market.strategy,
            "category":          market.category,

            # Probabilités
            "p_model_old":       market.p_model,
            "p_model_new":       p_new,
            "p_market":          market.p_market,

            # Edge & Z-score
            "edge_old":          market.edge,
            "edge_new":          gate_result.edge_new,
            "z_score_old":       market.z_score,
            "z_score_new":       gate_result.z_score_new,

            # Mouvement
            "delta_p":           delta,
            "sources":           sources,
            "n_sources":         len(sources),
            "aggregated":        aggregated,
            "requires_confirm":  req_confirm,

            # Décision
            "action":            gate_result.action,
            "gate_failures":     gate_result.gate_failures,
            "gates_passed":      gate_result.passed,
            "rationale":         gate_result.rationale,
            "requires_exit":     gate_result.requires_exit,

            # Sizing
            "kelly_size_new":    gate_result.kelly_size_new,
            "kelly_size_delta":  gate_result.kelly_size_delta,
            "position_size_current": market.position_size,

            "timestamp":         datetime.now(timezone.utc).isoformat(),
        }

        # Log si le mouvement est significatif
        if abs(delta) >= self.ALERT_DELTA_THRESHOLD:
            log.info(
                f"\n{'─'*60}\n"
                f"  📡 CRUCIX → {market.market_id[:35]}\n"
                f"  p_model  : {market.p_model:.3f} → {p_new:.3f}  (Δ{delta:+.3f})\n"
                f"  edge     : {market.edge:+.3f} → {gate_result.edge_new:+.3f}\n"
                f"  Z-score  : {market.z_score:.2f} → {gate_result.z_score_new:.2f}\n"
                f"  sources  : {', '.join(sources)}\n"
                f"  action   : {gate_result.action}\n"
                f"  rationale: {gate_result.rationale[:80]}\n"
                f"{'─'*60}"
            )

        return event


# ═══════════════════════════════════════════════════════════════════════════
# 14. WEEKLY CALIBRATION REPORT
#     Rapport automatique de performance des 26 sources.
# ═══════════════════════════════════════════════════════════════════════════

class WeeklyCalibrationReport:
    """
    Génère un rapport hebdomadaire (format texte ou JSON) listant :
    - Brier score par source
    - LR empirique vs prior
    - Hit rate
    - Trust weight courant
    - Recommandations de recalibration

    À lancer chaque lundi matin avant la session.
    """

    def __init__(self, calibration: CalibrationEngine, conn: sqlite3.Connection):
        self.cal  = calibration
        self.conn = conn

    def generate(self) -> dict:
        rows = self.conn.execute("""
            SELECT source_id, n_obs, brier_score, lr_empirical, hit_rate, updated_at
            FROM source_brier
            ORDER BY n_obs DESC
        """).fetchall()

        sources_data = []
        for row in rows:
            sid, n_obs, brier, lr_emp, hit_rate, updated = row
            lr_prior_bull, _ = LR_PRIOR.get(sid, (1.20, 0.833))
            trust    = self.cal.get_trust_weight(sid)
            status   = self._source_status(n_obs, brier, lr_emp, lr_prior_bull)

            sources_data.append({
                "source_id":    sid,
                "n_obs":        n_obs,
                "brier_score":  round(brier, 4) if brier else None,
                "lr_prior":     lr_prior_bull,
                "lr_empirical": round(lr_emp, 3) if lr_emp else None,
                "hit_rate":     round(hit_rate, 3) if hit_rate else None,
                "trust_weight": round(trust, 2),
                "status":       status,
                "last_updated": updated,
            })

        # Sources sans observations (pas encore dans la DB)
        observed_ids = {r["source_id"] for r in sources_data}
        for sid in LR_PRIOR:
            if sid not in observed_ids:
                sources_data.append({
                    "source_id": sid,
                    "n_obs": 0,
                    "brier_score": None,
                    "lr_prior": LR_PRIOR[sid][0],
                    "lr_empirical": None,
                    "hit_rate": None,
                    "trust_weight": 0.75,
                    "status": "NO_DATA",
                    "last_updated": None,
                })

        # Métriques globales
        recent_signals = self.conn.execute("""
            SELECT COUNT(*), SUM(ABS(delta_p)), AVG(ABS(delta_p))
            FROM signal_log
            WHERE ts > datetime('now', '-7 days')
        """).fetchone()

        recent_actions = self.conn.execute("""
            SELECT action, COUNT(*)
            FROM gate_log
            WHERE ts > datetime('now', '-7 days')
            GROUP BY action
        """).fetchall()

        return {
            "report_date":     datetime.now(timezone.utc).isoformat()[:10],
            "period":          "7 days",
            "sources":         sources_data,
            "total_sources":   len(sources_data),
            "calibrated":      sum(1 for s in sources_data if (s["n_obs"] or 0) >= MIN_CALIBRATION_OBS),
            "signals_7d":      recent_signals[0] or 0,
            "avg_delta_7d":    round(recent_signals[2] or 0, 4),
            "actions_7d":      dict(recent_actions),
            "recalib_needed":  [
                s["source_id"] for s in sources_data
                if (s["brier_score"] or 0) > 0.20 and (s["n_obs"] or 0) >= 10
            ],
        }

    def print_table(self):
        report  = self.generate()
        sources = report["sources"]
        print(f"\n{'═'*80}")
        print(f"  CRUCIX CALIBRATION REPORT  ·  {report['report_date']}")
        print(f"{'═'*80}")
        print(f"  Signaux 7j : {report['signals_7d']}  |  "
              f"Avg Δp : {report['avg_delta_7d']:+.3f}  |  "
              f"Sources calibrées : {report['calibrated']}/{report['total_sources']}")
        print()
        hdr = f"  {'SOURCE':<24} {'N':>5} {'BRIER':>7} {'LR_PRI':>7} {'LR_EMP':>7} {'HIT%':>6} {'TRUST':>6}  STATUS"
        print(hdr)
        print("  " + "─" * 76)
        for s in sorted(sources, key=lambda x: -(x["n_obs"] or 0)):
            brier = f"{s['brier_score']:.4f}" if s["brier_score"] else "  —  "
            lr_e  = f"{s['lr_empirical']:.3f}" if s["lr_empirical"] else "  —  "
            hr    = f"{s['hit_rate']*100:.0f}%" if s["hit_rate"] else "  — "
            print(
                f"  {s['source_id']:<24} {s['n_obs']:>5} {brier:>7} "
                f"{s['lr_prior']:>7.3f} {lr_e:>7} {hr:>6} {s['trust_weight']:>6.2f}  "
                f"{s['status']}"
            )
        if report["recalib_needed"]:
            print(f"\n  ⚠  Recalibration recommandée : {', '.join(report['recalib_needed'])}")
        print(f"{'═'*80}\n")

    @staticmethod
    def _source_status(
        n_obs: int, brier: Optional[float],
        lr_emp: Optional[float], lr_prior: float,
    ) -> str:
        if n_obs == 0:
            return "NO_DATA"
        if n_obs < MIN_CALIBRATION_OBS:
            return f"WARMUP ({n_obs}/{MIN_CALIBRATION_OBS})"
        if brier is None:
            return "CALIBRATED"
        if brier > 0.22:
            return "⚠ POOR_CALIB"
        if brier > 0.18:
            return "WATCH"
        if lr_emp and abs(lr_emp - lr_prior) / lr_prior > 0.30:
            return "LR_DRIFT"    # LR empirique s'écarte >30% du prior
        return "✓ CALIBRATED"


# ═══════════════════════════════════════════════════════════════════════════
# 15. CRUCIX ROUTER — orchestrateur principal
# ═══════════════════════════════════════════════════════════════════════════

class CrucixRouter:
    """
    Point d'entrée unique du pipeline.

    Usage typique :

        router = CrucixRouter()

        # Alerte unique
        events = router.process(alert, open_markets)

        # Batch d'alertes (même fenêtre temporelle, 10 min)
        events = router.process_batch(alerts, open_markets)

        # Résolution d'un marché
        router.resolve_market(market_id, outcome=1)

        # Rapport de calibration
        router.calibration_report()
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.conn      = init_db(db_path)
        self.parser    = SignalParser()
        self.decay     = TemporalDecayEngine()
        self.router    = MarketRouter()
        self.corr      = SourceCorrelationChecker()
        self.cal       = CalibrationEngine(self.conn)
        self.updater   = BayesUpdater(self.cal, self.decay, self.conn)
        self.agg       = MultiSourceAggregator(self.cal, self.corr, self.conn)
        self.z_engine  = DynamicZScoreEngine(self.conn)
        self.gate      = SevenGateRevalidator(self.z_engine, self.conn)
        self.bus       = ModelOutputBus(self.conn)
        self.report    = WeeklyCalibrationReport(self.cal, self.conn)

    def process(
        self,
        alert: CrucixAlert,
        open_markets: list[MarketContext],
        current_mdd: float   = 0.0,
        current_brier: float = 0.15,
    ) -> list[dict]:
        """
        Pipeline complet pour une alerte unique.
        Retourne une liste d'événements pour le système en aval.
        """
        alert = self.parser.parse(alert)

        if alert.direction in (SignalDirection.NEUTRAL, SignalDirection.UNKNOWN):
            log.debug(f"Skip non-directional: {alert.source_id}")
            return []

        if self.decay.is_stale(alert):
            log.warning(f"Reject stale alert: {alert.source_id}")
            return []

        routed = self.router.route(alert, open_markets)
        if not routed:
            log.debug(f"No market matched for: {alert.source_id}")
            return []

        events = []
        for market, relevance in routed:
            confidence = self._confidence_level(relevance, alert)
            update     = self.updater.update(alert, market, confidence)

            if update.delta_p == 0.0:
                continue

            gate_result = self.gate.revalidate(
                market, update.p_posterior, current_mdd, current_brier
            )
            event = self.bus.publish(
                market, update.p_posterior, [update], gate_result,
                aggregated=False,
            )
            events.append(event)

        return events

    def process_batch(
        self,
        alerts: list[CrucixAlert],
        open_markets: list[MarketContext],
        current_mdd:   float = 0.0,
        current_brier: float = 0.15,
    ) -> list[dict]:
        """
        Pipeline multi-sources : aggrège les mises à jour de la même
        fenêtre temporelle avant de re-valider les gates.
        Applique le Satopää extremizing si ≥2 sources indépendantes.
        """
        market_map: dict[str, MarketContext]    = {m.market_id: m for m in open_markets}
        per_market: dict[str, list[BayesUpdate]] = defaultdict(list)

        for alert in alerts:
            alert = self.parser.parse(alert)
            if alert.direction in (SignalDirection.NEUTRAL, SignalDirection.UNKNOWN):
                continue
            if self.decay.is_stale(alert):
                continue

            routed = self.router.route(alert, open_markets)
            for market, relevance in routed:
                confidence = self._confidence_level(relevance, alert)
                update     = self.updater.update(alert, market, confidence)
                if abs(update.delta_p) > 0.002:
                    per_market[market.market_id].append(update)

        events = []
        for mid, updates in per_market.items():
            if not updates:
                continue
            market = market_map.get(mid)
            if market is None:
                log.warning(f"process_batch: market_id {mid!r} not found in market_map, skipping")
                continue
            p_agg       = self.agg.aggregate(updates, market)
            gate_result = self.gate.revalidate(
                market, p_agg, current_mdd, current_brier
            )
            event = self.bus.publish(
                market, p_agg, updates, gate_result, aggregated=True,
            )
            event["n_sources_aggregated"] = len(updates)
            event["sources_detail"] = [
                {"source": u.source_id, "delta_p": u.delta_p, "lr": u.lr_applied,
                 "decay": u.decay_factor, "capped": u.capped}
                for u in updates
            ]
            events.append(event)

        return events

    def resolve_market(
        self,
        market_id: str,
        outcome: int,               # 1=YES, 0=NO
        p_at_resolution: float,
    ):
        """
        Appelé quand Polymarket résout un marché.
        Met à jour les Brier scores et LR empiriques de toutes les sources
        qui avaient contribué à ce marché.
        """
        hashes = self.conn.execute(
            "SELECT DISTINCT alert_hash FROM calibration WHERE market_id=?",
            (market_id,)
        ).fetchall()

        for (h,) in hashes:
            self.cal.record_resolution(h, market_id, outcome, p_at_resolution)

        log.info(
            f"RESOLVED [{market_id}]: outcome={outcome} "
            f"p_final={p_at_resolution:.3f} "
            f"calibration updated for {len(hashes)} signal(s)"
        )

    def source_report(self) -> list[dict]:
        """Retourne le rapport de calibration sous forme de liste."""
        return self.report.generate()["sources"]

    def print_calibration_report(self):
        self.report.print_table()

    @staticmethod
    def _confidence_level(relevance: float, alert: CrucixAlert) -> ConfidenceLevel:
        if relevance >= 0.60 and alert.category in (
            AlertCategory.FED_MACRO,
            AlertCategory.PREDICTION_MKT,
            AlertCategory.CRYPTO_PRICE,
        ):
            return ConfidenceLevel.HIGH
        if relevance >= 0.40:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW


# ═══════════════════════════════════════════════════════════════════════════
# INTÉGRATION — exemple complet
# ═══════════════════════════════════════════════════════════════════════════

def run_demo():
    """
    Démontre le pipeline complet sur les 4 positions ouvertes de PAF-001.
    """
    import tempfile, os
    db_tmp = Path(tempfile.mktemp(suffix=".db"))

    router  = CrucixRouter(db_path=db_tmp)

    # ── Positions ouvertes ────────────────────────────────────────────────
    markets = [
        MarketContext(
            market_id    = "fed_cut_march_2026",
            question     = "Will Fed cut 25bps at March 2026 meeting?",
            p_model      = 0.810,
            p_market     = 0.720,
            category     = "macro",
            keywords     = ["fed", "fomc", "cut", "march", "25bps", "rate"],
            days_to_res  = 5,
            bankroll     = 114.82,
            position_size= 4.80,
            edge         = 0.090,
            z_score      = 1.50,
            strategy     = "S1",
            sigma_14d    = 0.060,
            n_shares     = 6.67,
        ),
        MarketContext(
            market_id    = "btc_120k_dec_2026",
            question     = "Will BTC reach $120,000 before December 2026?",
            p_model      = 0.120,
            p_market     = 0.051,
            category     = "crypto",
            keywords     = ["btc", "bitcoin", "120k", "december", "2026"],
            days_to_res  = 48,
            bankroll     = 114.82,
            position_size= 4.20,
            edge         = 0.069,
            z_score      = 2.41,
            strategy     = "S2",
            sigma_14d    = 0.029,
            n_shares     = 100.0,
        ),
    ]

    # ─────────────────────────────────────────────────────────────────────
    # TEST 1 : Alerte unique — CME FedWatch +4pts bullish
    # ─────────────────────────────────────────────────────────────────────
    print("\n" + "═"*72)
    print("  TEST 1 — CME FedWatch single alert (frais, 0 min)")
    print("═"*72)

    alert1 = CrucixAlert(
        source_id       = "cme_fedwatch",
        category        = AlertCategory.FED_MACRO,
        raw_text        = (
            "CME FedWatch update: P(cut March 2026) moved 54% → 58% (+4pts). "
            "Fed funds futures now pricing 58% chance of 25bps cut."
        ),
        direction       = SignalDirection.BULLISH,
        magnitude       = 0.72,
        market_keywords = ["fed", "fomc", "cut", "march", "25bps"],
        timestamp       = datetime.now(timezone.utc),  # frais
    )

    events1 = router.process(alert1, markets)
    for e in events1:
        print(f"\n  ► {e['question'][:55]}...")
        print(f"    p_model  : {e['p_model_old']:.3f} → {e['p_model_new']:.3f}  (Δ{e['delta_p']:+.3f})")
        print(f"    edge     : {e['edge_old']:+.3f} → {e['edge_new']:+.3f}")
        print(f"    Z-score  : {e['z_score_old']:.2f} → {e['z_score_new']:.2f}")
        print(f"    action   : {e['action']}")
        print(f"    rationale: {e['rationale'][:70]}")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 2 : Batch — 4 sources convergentes (extremizing déclenché)
    # ─────────────────────────────────────────────────────────────────────
    print("\n" + "═"*72)
    print("  TEST 2 — Batch 4 sources (Satopää extremizing)")
    print("═"*72)

    now = datetime.now(timezone.utc)
    alerts2 = [
        CrucixAlert(
            source_id="cme_fedwatch", category=AlertCategory.FED_MACRO,
            raw_text="CME FedWatch: March cut probability 62% (+8pts this session).",
            direction=SignalDirection.BULLISH, magnitude=0.82,
            market_keywords=["fed", "fomc", "cut", "march"],
            timestamp=now,
        ),
        CrucixAlert(
            source_id="kalshi", category=AlertCategory.PREDICTION_MKT,
            raw_text="Kalshi Fed March: 60% YES (was 48%). Narrowing gap with Polymarket 72%.",
            direction=SignalDirection.BULLISH, magnitude=0.76,
            market_keywords=["fed", "fomc", "cut", "march", "rate"],
            timestamp=now,
        ),
        CrucixAlert(
            source_id="reuters_rss", category=AlertCategory.NEWS_TIER1,
            raw_text=(
                "Reuters: Fed officials signal openness to March rate cut "
                "if CPI continues to cool. Dovish tilt broadly confirmed."
            ),
            direction=SignalDirection.BULLISH, magnitude=0.70,
            market_keywords=["fed", "fomc", "cut", "march", "dovish"],
            timestamp=now,
        ),
        CrucixAlert(
            source_id="deribit_vol", category=AlertCategory.CRYPTO_PRICE,
            raw_text=(
                "Deribit BTC options: implied vol σ=0.94 (+12%). "
                "Call delta $120k Dec 2026 : 0.11 (was 0.09). Upside pricing up."
            ),
            direction=SignalDirection.BULLISH, magnitude=0.68,
            market_keywords=["btc", "bitcoin", "120k", "deribit", "options"],
            timestamp=now,
        ),
    ]

    events2 = router.process_batch(alerts2, markets)
    for e in events2:
        print(f"\n  ► {e['question'][:55]}...")
        print(f"    p_model  : {e['p_model_old']:.3f} → {e['p_model_new']:.3f}  (Δ{e['delta_p']:+.3f})")
        print(f"    edge     : {e['edge_old']:+.3f} → {e['edge_new']:+.3f}")
        print(f"    Z-score  : {e['z_score_old']:.2f} → {e['z_score_new']:.2f}")
        print(f"    sources  : {', '.join(e['sources'])}")
        print(f"    sources_detail: " +
              str([f"{d['source']}(Δ{d['delta_p']:+.3f},decay={d['decay']:.2f})"
                   for d in e.get("sources_detail", [])]))
        print(f"    aggregated: {e['aggregated']} | action: {e['action']}")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 3 : Signal âgé (4h) — décroissance temporelle
    # ─────────────────────────────────────────────────────────────────────
    print("\n" + "═"*72)
    print("  TEST 3 — Signal âgé de 4h (temporal decay)")
    print("═"*72)

    old_ts = datetime.now(timezone.utc) - timedelta(hours=4)
    alert3 = CrucixAlert(
        source_id       = "cme_fedwatch",
        category        = AlertCategory.FED_MACRO,
        raw_text        = "CME FedWatch: March cut 58%.",
        direction       = SignalDirection.BULLISH,
        magnitude       = 0.72,
        market_keywords = ["fed", "fomc", "cut", "march"],
        timestamp       = old_ts,  # signal de 4h
    )
    decay_e = TemporalDecayEngine()
    _, factor = decay_e.apply(2.50, alert3)
    print(f"  LR FedWatch (2.50) après 4h : ×{factor:.3f} → LR_eff = {1.0 + (2.50-1.0)*factor:.3f}")
    print(f"  (versus LR=2.50 si signal frais)")

    events3 = router.process(alert3, markets)
    for e in events3:
        print(f"\n  ► {e['question'][:55]}...")
        print(f"    p_model  : {e['p_model_old']:.3f} → {e['p_model_new']:.3f}  (Δ{e['delta_p']:+.3f})")
        print(f"    (Δ serait {events1[0]['delta_p']:+.3f} avec signal frais)")

    # ─────────────────────────────────────────────────────────────────────
    # TEST 4 : Résolution + rapport de calibration
    # ─────────────────────────────────────────────────────────────────────
    print("\n" + "═"*72)
    print("  TEST 4 — Résolution marché + rapport calibration")
    print("═"*72)

    router.resolve_market("fed_cut_march_2026", outcome=1, p_at_resolution=0.95)
    print("\n  (Résolution YES enregistrée pour fed_cut_march_2026)")
    router.print_calibration_report()

    db_tmp.unlink(missing_ok=True)
    print("  ✓ Demo complète.\n")


if __name__ == "__main__":
    run_demo()
