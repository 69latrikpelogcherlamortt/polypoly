"""
prob_model.py  ·  Polymarket Trading Bot
─────────────────────────────────────────
Pipeline probabiliste complet — 5 étapes :

  ÉTAPE 1 → Reference Class Engine (outside view + Beta distribution)
  ÉTAPE 2 → Modèle quantitatif spécialisé (Crypto / Macro / Événement)
  ÉTAPE 3 → Bayes Update continu (signaux contextuels)
  ÉTAPE 4 → Ensemble pondéré par Brier
  ÉTAPE 5 → Extremizing adaptatif (Satopää 2014) + décision finale

Implémentation exacte de la spécification polymarket_strategies.md.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit
from scipy.stats import beta as beta_dist, norm

from core.config import (
    RCE_N_SIMILAR, RCE_MODEL_NAME,
    MERTON_LAMBDA_J, MERTON_MU_J, MERTON_SIGMA_J, MERTON_N_TERMS,
    MACRO_W_FEDWATCH, MACRO_W_LOGIT, MACRO_W_NS,
    RISK_FREE_RATE, EXTREMIZE_ALPHA, UNCERTAINTY_MAX,
)
from signals.crucix_router import LR_PRIOR

log = logging.getLogger("prob_model")


# ═══════════════════════════════════════════════════════════════════════════
# 1. HISTORICAL DATABASE (Reference Class Engine)
# ═══════════════════════════════════════════════════════════════════════════

class HistoricalDB:
    """
    Base de données des marchés historiques Polymarket résolus.
    Alimente le Reference Class Engine avec des données réelles.
    """

    def __init__(self, db_path: Path = Path("paf_trading.db")):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS historical_markets (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id    TEXT    UNIQUE NOT NULL,
                question     TEXT    NOT NULL,
                resolved_yes INTEGER NOT NULL,   -- 1 = YES, 0 = NO
                volume       REAL    NOT NULL DEFAULT 0,
                category     TEXT    NOT NULL DEFAULT '',
                resolution_date TEXT,
                price_at_entry  REAL,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def add_market(self, market_id: str, question: str,
                    resolved_yes: int, volume: float,
                    category: str = "", resolution_date: str = ""):
        try:
            self.conn.execute("""
                INSERT OR IGNORE INTO historical_markets
                (market_id, question, resolved_yes, volume, category, resolution_date)
                VALUES (?,?,?,?,?,?)
            """, (market_id, question, resolved_yes, volume, category, resolution_date))
            self.conn.commit()
        except Exception as e:
            log.debug(f"HistoricalDB.add_market: {e}")

    def get_all_questions(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT question FROM historical_markets"
        ).fetchall()
        return [r[0] for r in rows]

    def get_all_entries(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT market_id, question, resolved_yes, volume FROM historical_markets"
        ).fetchall()
        return [
            {"market_id": r[0], "question": r[1],
             "resolved_yes": r[2], "volume": r[3]}
            for r in rows
        ]

    def count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM historical_markets"
        ).fetchone()
        return row[0] if row else 0


# ═══════════════════════════════════════════════════════════════════════════
# 2. REFERENCE CLASS ENGINE — ÉTAPE 1
# ═══════════════════════════════════════════════════════════════════════════

class ReferenceClassEngine:
    """
    Trouve les marchés historiques les plus similaires
    et calcule une base rate pondérée.

    Principe superforecaster :
    "Quelle classe de situations ressemble à celle-ci ?
     Quel % se résout YES dans cette classe ?"
    """

    def __init__(self, historical_db: HistoricalDB):
        self.db      = historical_db
        self._model  = None   # chargement lazy pour économiser la mémoire

    def _get_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(RCE_MODEL_NAME)
                log.info(f"SentenceTransformer '{RCE_MODEL_NAME}' chargé")
            except Exception as e:
                log.warning(f"sentence-transformers non disponible: {e}")
                self._model = None
        return self._model

    def find_similar_markets(self, question: str, n: int = None) -> list[dict]:
        n = n or RCE_N_SIMILAR
        entries = self.db.get_all_entries()
        if not entries:
            return []

        model = self._get_model()
        if model is None:
            # Fallback : similarité basée sur les mots-clés
            return self._keyword_similarity(question, entries, n)

        try:
            from sklearn.metrics.pairwise import cosine_similarity
            all_questions  = [e["question"] for e in entries]
            q_emb          = model.encode([question])
            all_emb        = model.encode(all_questions)
            sims           = cosine_similarity(q_emb, all_emb)[0]
            top_idx        = np.argsort(sims)[-n:][::-1]
            return [
                {
                    "question":   entries[i]["question"],
                    "outcome":    entries[i]["resolved_yes"],
                    "similarity": float(sims[i]),
                    "volume":     entries[i]["volume"],
                }
                for i in top_idx
            ]
        except Exception as e:
            log.warning(f"RCE embedding error: {e}")
            return self._keyword_similarity(question, entries, n)

    def _keyword_similarity(self, question: str, entries: list[dict],
                             n: int) -> list[dict]:
        """Fallback : Jaccard sur les mots."""
        q_words = set(question.lower().split())
        scored  = []
        for e in entries:
            e_words = set(e["question"].lower().split())
            if not q_words or not e_words:
                sim = 0.0
            else:
                sim = len(q_words & e_words) / len(q_words | e_words)
            scored.append({
                "question":   e["question"],
                "outcome":    e["resolved_yes"],
                "similarity": sim,
                "volume":     e["volume"],
            })
        return sorted(scored, key=lambda x: -x["similarity"])[:n]

    def compute_base_rate(
        self, similar_markets: list[dict]
    ) -> tuple[float, float, float]:
        """
        Base rate pondérée par similarité ET volume.
        Beta(α, β) pour quantifier l'incertitude.
        Prior de Laplace : α=1, β=1 (évite les extrêmes).
        """
        if not similar_markets:
            return 0.5, 0.4, 0.6

        weights = np.array([
            m["similarity"] * np.log1p(max(m["volume"], 1))
            for m in similar_markets
        ])
        total = weights.sum()
        if total == 0:
            return 0.5, 0.35, 0.65
        weights /= total

        outcomes = np.array([m["outcome"] for m in similar_markets])
        p_base   = float(np.average(outcomes, weights=weights))

        n = len(similar_markets)
        alpha_b = np.sum(weights * outcomes)   * n + 1
        beta_b  = np.sum(weights * (1 - outcomes)) * n + 1

        low  = float(beta_dist.ppf(0.10, alpha_b, beta_b))
        high = float(beta_dist.ppf(0.90, alpha_b, beta_b))

        return round(p_base, 4), round(low, 4), round(high, 4)

    def get_base_rate(self, question: str) -> dict:
        n_hist = self.db.count()
        if n_hist < 5:
            log.debug(f"HistoricalDB trop petite ({n_hist} entrées), prior uniforme")
            return {
                "p_base": 0.5, "interval": (0.35, 0.65),
                "uncertainty": 0.30, "n_similar": 0, "signal": "faible",
            }
        similar = self.find_similar_markets(question, n=RCE_N_SIMILAR)
        p_base, low, high = self.compute_base_rate(similar)
        uncertainty = high - low
        return {
            "p_base":     p_base,
            "interval":   (low, high),
            "uncertainty": uncertainty,
            "n_similar":  len(similar),
            "signal":     "fort" if uncertainty < 0.20 else "faible",
        }


# ═══════════════════════════════════════════════════════════════════════════
# 3A. MODÈLE CRYPTO — Black-Scholes + Merton Jump Diffusion
# ═══════════════════════════════════════════════════════════════════════════

class CryptoModel:
    """
    Black-Scholes + Jump Diffusion (Merton 1976)
    P(S_T > K) = probabilité que BTC dépasse le prix cible K à date T

    Merton supérieur à BS sur BTC car :
    - Kurtosis BTC > 4 (queues épaisses)
    - Sauts fréquents (+/-20% en 24h)
    """

    def black_scholes_prob(
        self, S: float, K: float, T: float, r: float, sigma: float
    ) -> float:
        """P(S_T > K) = N(d2)"""
        if T <= 0 or sigma <= 0:
            return 1.0 if S > K else 0.0
        d2 = (np.log(S / K) + (r - 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        return float(norm.cdf(d2))

    def merton_jump_prob(
        self,
        S: float, K: float, T: float, r: float, sigma: float,
        lambda_j: float = MERTON_LAMBDA_J,
        mu_j: float = MERTON_MU_J,
        sigma_j: float = MERTON_SIGMA_J,
        n_terms: int = MERTON_N_TERMS,
    ) -> float:
        """
        Merton (1976) : somme de BS conditionnels sur n sauts
        Poisson(λT) sauts sur la période [0, T]
        """
        prob           = 0.0
        poisson_weight = np.exp(-lambda_j * T)

        for n in range(n_terms):
            factorial_n = math.factorial(n)
            w_n         = poisson_weight * (lambda_j * T) ** n / factorial_n
            # FIX: ajout du terme 0.5*sigma_j^2 manquant dans le drift par saut (Merton 1976)
            r_n         = r - lambda_j * (np.exp(mu_j + 0.5 * sigma_j ** 2) - 1) + n * (mu_j + 0.5 * sigma_j ** 2) / max(T, 1e-6)
            r_n         = np.clip(r_n, -10, 10)
            sigma_n     = np.sqrt(sigma ** 2 + n * sigma_j ** 2 / max(T, 1e-6))
            prob       += w_n * self.black_scholes_prob(S, K, T, r_n, sigma_n)

        return min(0.99, max(0.01, prob))

    def get_probability(
        self, S: float, K: float, T: float, sigma: float,
        r: float = RISK_FREE_RATE
    ) -> dict:
        """
        S = prix spot actuel
        K = prix cible (strike)
        T = années jusqu'à expiration
        sigma = volatilité implicite (Deribit IV)
        """
        p_bs     = self.black_scholes_prob(S, K, T, r, sigma)
        p_merton = self.merton_jump_prob(S, K, T, r, sigma)

        # Plus loin du spot → Merton plus fiable
        distance_ratio = abs(K - S) / max(S, 1)
        w_merton = min(0.80, distance_ratio * 2)
        w_bs     = 1 - w_merton
        p_final  = w_bs * p_bs + w_merton * p_merton

        # Intervalle : sensibilité à ±10% sur la vol
        p_low  = self.black_scholes_prob(S, K, T, r, sigma * 0.9)
        p_high = self.black_scholes_prob(S, K, T, r, sigma * 1.1)

        return {
            "p_model":   round(p_final, 4),
            "p_bs":      round(p_bs, 4),
            "p_merton":  round(p_merton, 4),
            "interval":  (round(p_low, 4), round(p_high, 4)),
            "sigma_used": sigma,
            "model":     "merton_jump_diffusion",
        }


# ═══════════════════════════════════════════════════════════════════════════
# 3B. MODÈLE MACRO/FED — Logit + Nelson-Siegel + FedWatch
# ═══════════════════════════════════════════════════════════════════════════

class MacroFedModel:
    """
    Combine 3 modèles indépendants :
    1. Logit calibré sur Fed 1990-2025
    2. Nelson-Siegel (courbe des taux)
    3. CME FedWatch (source principale)

    Coefficients Logit calibrés OLS (1990-2025) :
    intercept -2.10 / cpi_yoy -0.82 / unemployment +0.63 /
    gdp_growth -0.41 / fed_funds_rate -0.38 / yield_curve +0.95
    """

    LOGIT_COEFFS = {
        "intercept":     -2.10,
        "cpi_yoy":       -0.82,
        "unemployment":  +0.63,
        "gdp_growth":    -0.41,
        "fed_funds_rate":-0.38,
        "yield_curve":   +0.95,
    }

    def logit_probability(self, macro_data: dict) -> float:
        z = self.LOGIT_COEFFS["intercept"]
        for key, coeff in self.LOGIT_COEFFS.items():
            if key != "intercept" and key in macro_data:
                z += coeff * macro_data[key]
        return float(expit(z))

    def nelson_siegel_implied(
        self, maturities: np.ndarray, yields: np.ndarray
    ) -> float:
        """
        f(t) = β₀ + β₁×e^(-t/τ) + β₂×(t/τ)×e^(-t/τ)
        Pente de la courbe → signal politique monétaire.
        """
        def ns_curve(params, t):
            b0, b1, b2, tau = params
            tau = max(tau, 0.1)
            return b0 + b1 * np.exp(-t / tau) + b2 * (t / tau) * np.exp(-t / tau)

        try:
            result = minimize(
                lambda p: np.sum((ns_curve(p, maturities) - yields) ** 2),
                [0.03, -0.01, 0.01, 1.5],
                method="Nelder-Mead",
            )
            b0, b1, b2, tau = result.x
            if tau <= 0:
                return 0.5
            # FIX: utiliser la pente réelle de la courbe fittée (10Y - 3M)
            # au lieu de slope = -b1 qui perdait l'info de courbure β₂
            rate_short = ns_curve(result.x, 0.25)   # 3 mois
            rate_long  = ns_curve(result.x, 10.0)   # 10 ans
            slope = rate_long - rate_short
            # Courbure (β₂) : signal additionnel sur attentes de taux futurs
            curvature_signal = b2 * 5.0
            combined = slope * 15.0 + curvature_signal
            return float(expit(combined))
        except Exception:
            return 0.5

    def get_probability(
        self,
        macro_data: dict,
        p_fedwatch: float,
    ) -> dict:
        p_logit = self.logit_probability(macro_data)

        # Nelson-Siegel (optionnel si données disponibles)
        p_ns = 0.5
        if "maturities" in macro_data and "yields" in macro_data:
            p_ns = self.nelson_siegel_implied(
                np.array(macro_data["maturities"]),
                np.array(macro_data["yields"]),
            )

        # FedWatch dominant
        p_ensemble = (
            MACRO_W_FEDWATCH * p_fedwatch
            + MACRO_W_LOGIT  * p_logit
            + MACRO_W_NS     * p_ns
        )

        return {
            "p_model":   round(p_ensemble, 4),
            "p_logit":   round(p_logit, 4),
            "p_fedwatch": round(p_fedwatch, 4),
            "p_ns":      round(p_ns, 4),
            "interval":  (
                round(min(p_logit, p_fedwatch, p_ns), 4),
                round(max(p_logit, p_fedwatch, p_ns), 4),
            ),
            "model":     "macro_ensemble",
        }


# ═══════════════════════════════════════════════════════════════════════════
# 3C. MODÈLE ÉVÉNEMENTS/POLITIQUE — Fermi + Kalshi + Extremizing
# ═══════════════════════════════════════════════════════════════════════════

class EventModel:
    """
    Pour les marchés sans signal quantitatif direct.
    Combine : Base rate, Fermi, Kalshi, Extremizing.
    """

    def fermi_decomposition(
        self, sub_questions: list[tuple[str, float]]
    ) -> Optional[float]:
        """
        Décomposer en sous-questions indépendantes.
        P_final = ∏ P(sous-question i)
        """
        if not sub_questions:
            return None
        p = 1.0
        for _, p_sub in sub_questions:
            p *= max(0.01, min(0.99, p_sub))
        return round(p, 4)

    def extremize(self, p: float, alpha: float = EXTREMIZE_ALPHA) -> float:
        """
        Satopää et al. (2014) — corriger le biais de prudence.
        P_extremized = P^α / (P^α + (1-P)^α)
        """
        p = max(1e-6, min(1 - 1e-6, p))
        return p ** alpha / (p ** alpha + (1 - p) ** alpha)

    def get_probability(
        self,
        ref_class_result: dict,
        p_kalshi: Optional[float] = None,
        sub_questions: Optional[list[tuple[str, float]]] = None,
    ) -> dict:
        sources = [("base_rate", ref_class_result["p_base"], 1.0)]

        if p_kalshi is not None:
            sources.append(("kalshi", p_kalshi, 1.5))

        if sub_questions:
            p_fermi = self.fermi_decomposition(sub_questions)
            if p_fermi:
                sources.append(("fermi", p_fermi, 1.2))

        total_w    = sum(w for _, _, w in sources)
        p_ensemble = sum(p * w for _, p, w in sources) / total_w

        n       = len(sources)
        # NOTE: extremizing géré uniquement par final_decision() pour éviter
        # le double extremizing (EventModel + final_decision = biais ~+5-7¢)
        p_final = p_ensemble

        return {
            "p_model":   round(p_final, 4),
            "p_raw":     round(p_ensemble, 4),
            "sources":   sources,
            "extremized": n >= 2,
            "interval":  ref_class_result["interval"],
            "model":     "event_ensemble",
        }


# ═══════════════════════════════════════════════════════════════════════════
# 4. BRIER-WEIGHTED ENSEMBLE — ÉTAPE 4
# ═══════════════════════════════════════════════════════════════════════════

class BrierWeightedEnsemble:
    """
    w_i = exp(-Brier_i) / Σ exp(-Brier_j)

    Les modèles les mieux calibrés reçoivent plus de poids.
    Minimum 5 résolutions pour calculer les poids — sinon poids égaux.
    """

    def __init__(self, db_conn: sqlite3.Connection):
        self.conn   = db_conn
        self.models = ["base_rate", "quant", "bayes_updated"]

    def _get_model_predictions(self, model_tag: str, n: int = 20) -> list[float]:
        rows = self.conn.execute("""
            SELECT p_model FROM trades
            WHERE status='closed' AND outcome IS NOT NULL
            ORDER BY exit_ts DESC LIMIT ?
        """, (n,)).fetchall()
        return [r[0] for r in rows]

    def _get_outcomes(self, n: int = 20) -> list[int]:
        rows = self.conn.execute("""
            SELECT outcome FROM trades
            WHERE status='closed' AND outcome IS NOT NULL
            ORDER BY exit_ts DESC LIMIT ?
        """, (n,)).fetchall()
        return [r[0] for r in rows]

    def compute_weights(self, last_n: int = 20) -> dict[str, float]:
        preds   = self._get_model_predictions("all", last_n)
        outcomes = self._get_outcomes(last_n)

        if len(preds) < 5:
            # Poids égaux si pas assez d'historique
            return {m: 1.0 / len(self.models) for m in self.models}

        brier = np.mean([(p - o) ** 2 for p, o in zip(preds, outcomes)])
        # Un seul Brier global → distribuer aux 3 modèles (à affiner avec tracking par modèle)
        exp_val = np.exp(-brier)
        total   = exp_val * len(self.models)
        return {m: exp_val / total for m in self.models}

    def combine(self, predictions_dict: dict[str, float]) -> dict:
        weights = self.compute_weights()
        p_ensemble = sum(
            weights.get(m, 0) * p
            for m, p in predictions_dict.items()
        )
        probs  = list(predictions_dict.values())
        spread = max(probs) - min(probs) if len(probs) > 1 else 0.0
        return {
            "p_final":      round(p_ensemble, 4),
            "weights_used": weights,
            "model_spread": round(spread, 4),
            "signal":       (
                "fort"  if spread < 0.10 else
                "moyen" if spread < 0.20 else "faible"
            ),
        }


# ═══════════════════════════════════════════════════════════════════════════
# 5. DÉCISION FINALE + EXTREMIZING ADAPTATIF — ÉTAPE 5
# ═══════════════════════════════════════════════════════════════════════════

def final_decision(
    base_rate_result: Optional[dict],
    quant_result: Optional[dict],
    bayes_result: Optional[float],
    ensemble: dict,
) -> tuple[Optional[dict], str]:
    """
    Synthèse finale + extremizing adaptatif.

    Règles d'extremizing :
    - 3 sources convergentes (spread < 0.12) → α = 1.4
    - 2 sources                              → α = 1.2
    - 1 source                               → α = 1.0 (off)
    """
    if ensemble["signal"] == "faible":
        return None, "signal_trop_faible"

    p_raw     = ensemble["p_final"]
    n_sources = sum(1 for r in [base_rate_result, quant_result, bayes_result]
                    if r is not None)

    if n_sources >= 3 and ensemble["model_spread"] < 0.12:
        p_final = p_raw ** 1.4 / (p_raw ** 1.4 + (1 - p_raw) ** 1.4)
    elif n_sources >= 2:
        p_final = p_raw ** 1.2 / (p_raw ** 1.2 + (1 - p_raw) ** 1.2)
    else:
        p_final = p_raw

    p_final = max(0.01, min(0.99, p_final))

    # Intervalles
    intervals = [r["interval"] for r in [base_rate_result, quant_result]
                 if r and "interval" in r]
    if intervals:
        low  = np.mean([i[0] for i in intervals])
        high = np.mean([i[1] for i in intervals])
    else:
        low  = p_final - 0.15
        high = p_final + 0.15

    low  = max(0.0, float(low))
    high = min(1.0, float(high))
    uncertainty = high - low

    return {
        "p_final":        round(p_final, 4),
        "p_raw":          round(p_raw, 4),
        "interval":       (round(low, 4), round(high, 4)),
        "uncertainty":    round(uncertainty, 4),
        "n_sources":      n_sources,
        "extremized":     n_sources >= 2,
        "signal_strength": (
            "fort"  if uncertainty < 0.15 else
            "moyen" if uncertainty < 0.25 else "faible"
        ),
        "tradeable": uncertainty < UNCERTAINTY_MAX and n_sources >= 2,
    }, "ok"


# ═══════════════════════════════════════════════════════════════════════════
# 6. SÉLECTION AUTOMATIQUE DU MODÈLE
# ═══════════════════════════════════════════════════════════════════════════

def route_to_model(question: str) -> str:
    """Sélectionne automatiquement le modèle adapté selon la catégorie."""
    q = question.lower()
    crypto_kw = ["bitcoin", "btc", "eth", "ethereum", "solana", "sol",
                 "crypto", "price", "100k", "120k", "150k", "200k"]
    fed_kw    = ["fed", "federal reserve", "interest rate", "fomc",
                 "rate cut", "rate hike", "bps", "25bps", "50bps"]

    if any(kw in q for kw in crypto_kw):
        return "crypto"
    elif any(kw in q for kw in fed_kw):
        return "macro"
    else:
        return "event"


# ═══════════════════════════════════════════════════════════════════════════
# 7. PROBABILISTIC SCORER — interface principale
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ScoringContext:
    """Contexte pour scorer un marché candidat."""
    question:       str
    market_price:   float
    days_to_res:    float
    category:       str
    volume_24h:     float
    # Données quantitatives optionnelles
    btc_spot:       Optional[float] = None
    btc_target:     Optional[float] = None
    btc_sigma:      Optional[float] = None    # vol implicite Deribit
    p_fedwatch:     Optional[float] = None    # prob CME FedWatch
    macro_data:     Optional[dict]  = None    # {cpi_yoy, unemployment, gdp_growth, ...}
    p_kalshi:       Optional[float] = None
    sub_questions:  Optional[list]  = None
    news_signals:   Optional[list]  = None    # liste de CrucixAlert


class ProbabilisticScorer:
    """
    Pipeline probabiliste complet.
    Retourne P_final avec intervalle de confiance et score de tradabilité.
    """

    def __init__(self, db_conn: sqlite3.Connection, historical_db: HistoricalDB):
        self.rce       = ReferenceClassEngine(historical_db)
        self.crypto    = CryptoModel()
        self.macro     = MacroFedModel()
        self.event     = EventModel()
        self.ensemble  = BrierWeightedEnsemble(db_conn)

    def score(self, ctx: ScoringContext) -> dict:
        """
        Pipeline complet en 5 étapes.
        Retourne None si signal trop faible.
        """
        log.info(f"Scoring: {ctx.question[:60]}...")

        # ── ÉTAPE 1 : Reference Class Engine ──────────────────────────────
        base_rate_result = self.rce.get_base_rate(ctx.question)

        # ── ÉTAPE 2 : Modèle quantitatif ──────────────────────────────────
        model_type   = route_to_model(ctx.question)
        quant_result = None

        if model_type == "crypto" and ctx.btc_spot and ctx.btc_target:
            T = ctx.days_to_res / 365.0
            sigma = ctx.btc_sigma if ctx.btc_sigma is not None else 0.80
            quant_result = self.crypto.get_probability(
                S=ctx.btc_spot, K=ctx.btc_target, T=T, sigma=sigma
            )

        elif model_type == "macro" and ctx.p_fedwatch is not None:
            macro = {**(ctx.macro_data or {})}   # shallow copy — never mutate the shared context
            # Fallback statiques — avertissement si données live non fournies
            missing = [k for k in ("cpi_yoy", "unemployment", "gdp_growth", "fed_funds_rate", "yield_curve")
                       if k not in macro]
            if missing:
                log.warning(
                    f"MacroFedModel: données live manquantes {missing}, "
                    "utilisation des fallbacks statiques — configurer BLS_API_KEY pour données live"
                )
            macro.setdefault("cpi_yoy", 3.2)
            macro.setdefault("unemployment", 4.1)
            macro.setdefault("gdp_growth", 2.8)
            macro.setdefault("fed_funds_rate", 5.25)
            macro.setdefault("yield_curve", -0.20)
            quant_result = self.macro.get_probability(macro, ctx.p_fedwatch)

        else:
            # EventModel — toujours disponible
            quant_result = self.event.get_probability(
                ref_class_result=base_rate_result,
                p_kalshi=ctx.p_kalshi,
                sub_questions=ctx.sub_questions,
            )

        # ── ÉTAPE 3 : Bayes update sur signaux news ────────────────────────
        # LRs issus de LR_PRIOR (table calibrée CrucixRouter) — pas de valeurs hardcodées
        bayes_result = None
        if ctx.news_signals:
            p = base_rate_result["p_base"]
            for signal in ctx.news_signals:
                lr_bull, _ = LR_PRIOR.get(signal.source_id, (1.20, 0.833))
                lr = lr_bull  # excès de LR par rapport à 1, direction gérée séparément
                try:
                    dir_value = signal.direction.value if signal.direction else None
                except AttributeError:
                    dir_value = None
                if dir_value not in ("bullish", "bearish"):
                    continue
                if dir_value == "bullish":
                    odds = p / (1 - p) * lr
                else:
                    odds = p / (1 - p) / lr
                p = max(0.02, min(0.98, odds / (1 + odds)))
            bayes_result = round(p, 4)

        # ── ÉTAPE 4 : Ensemble pondéré par Brier ──────────────────────────
        preds: dict[str, float] = {}
        if base_rate_result:
            preds["base_rate"] = base_rate_result["p_base"]
        if quant_result:
            preds["quant"] = quant_result["p_model"]
        if bayes_result is not None:
            preds["bayes_updated"] = bayes_result

        if not preds:
            return {"tradeable": False, "reason": "no_models_available"}

        ens = self.ensemble.combine(preds)

        # ── ÉTAPE 5 : Décision finale + extremizing ────────────────────────
        result, reason = final_decision(
            base_rate_result, quant_result, bayes_result, ens
        )

        if result is None:
            return {"tradeable": False, "reason": reason}

        # Edge calcul
        p_final = result["p_final"]
        edge    = p_final - ctx.market_price

        result.update({
            "model_type":         model_type,
            "p_market":           ctx.market_price,
            "edge":               round(edge, 4),
            "base_rate":          base_rate_result["p_base"],
            "quant_model":        quant_result["p_model"] if quant_result else None,
            "bayes_updated":      bayes_result,
            "n_historical":       self.rce.db.count(),
        })

        log.info(
            f"Score: p_final={p_final:.3f} market={ctx.market_price:.3f} "
            f"edge={edge:+.3f} uncertainty={result['uncertainty']:.3f} "
            f"tradeable={result['tradeable']}"
        )
        return result
