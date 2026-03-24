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

    FIX #7 : Le logit modélise P(rate cut). Si le marché Polymarket demande
    P(rate hike) ou P(rate hold), il faut inverser ou ajuster la sortie.
    detect_macro_direction() analyse la question pour déterminer quoi mapper.
    """

    # FIX #4 : fed_funds_rate → signe positif (+0.38)
    # Économiquement : taux élevé → plus de marge pour couper → P(cut) augmente
    # L'ancien -0.38 était inversé (calibration artefact, pas causalité)
    # Shrinkage global de 20% sur tous les coefficients pour réduire l'overfit
    LOGIT_COEFFS = {
        "intercept":      -2.10 * 0.8,
        "cpi_yoy":        -0.82 * 0.8,
        "unemployment":   +0.63 * 0.8,
        "gdp_growth":     -0.41 * 0.8,
        "fed_funds_rate": +0.38 * 0.8,   # FIX: signe corrigé + shrinkage
        "yield_curve":    +0.95 * 0.8,
    }

    @staticmethod
    def detect_macro_direction(question: str) -> str:
        """
        Analyse la question Polymarket pour déterminer ce qu'elle demande.
        Returns: "cut", "hike", "hold", ou "unknown"
        """
        q = question.lower()
        if any(kw in q for kw in ["rate cut", "cut rate", "lower rate", "reduce rate",
                                    "decrease rate", "25bps cut", "50bps cut"]):
            return "cut"
        if any(kw in q for kw in ["rate hike", "hike rate", "raise rate", "increase rate",
                                    "25bps hike", "50bps hike", "higher rate"]):
            return "hike"
        if any(kw in q for kw in ["hold", "unchanged", "no change", "pause",
                                    "maintain rate", "keep rate"]):
            return "hold"
        return "unknown"

    def logit_probability(self, macro_data: dict, question: str = "") -> float:
        """
        Calcule P(rate cut) via logit, puis ajuste selon ce que le marché demande.

        Le logit est un modèle BINAIRE calibré sur P(cut). Il n'est fiable
        que pour les questions sur les rate cuts. Pour les questions sur hike
        ou hold, on applique un shrinkage fort vers 0.5 et on laisse
        FedWatch (60% du poids) dominer l'ensemble macro.

        - Si le marché demande P(cut) → retourne P(cut) directement
        - Si le marché demande P(hike) → retourne 1 - P(cut) avec shrinkage 30%
        - Si le marché demande P(hold) → shrinkage 70% vers 0.5
        - Si unknown → shrinkage 80% vers 0.5
        """
        z = self.LOGIT_COEFFS["intercept"]
        for key, coeff in self.LOGIT_COEFFS.items():
            if key != "intercept" and key in macro_data:
                z += coeff * macro_data[key]
        p_cut = float(expit(z))

        direction = self.detect_macro_direction(question)
        if direction == "cut":
            return p_cut
        elif direction == "hike":
            # Approximation : P(hike) ≈ 1 - P(cut), mais avec shrinkage
            # car le logit ne modélise pas directement les hikes
            p_hike_raw = 1.0 - p_cut
            return 0.5 + (p_hike_raw - 0.5) * 0.7  # 30% shrinkage
        elif direction == "hold":
            # Le logit binaire n'est pas conçu pour le 3-way cut/hike/hold
            # Shrinkage fort : on laisse FedWatch décider
            p_hold_raw = 1.0 - abs(2 * p_cut - 1.0)  # max quand p_cut ≈ 0.5
            return 0.5 + (p_hold_raw - 0.5) * 0.3  # 70% shrinkage
        else:
            log.warning("MacroFedModel: direction inconnue pour '%s', shrinkage vers 0.5", question[:60])
            return 0.5 + (p_cut - 0.5) * 0.2  # 80% shrinkage

    # Statistiques historiques de la pente 10Y-3M (FRED 1982-2025)
    # Moyenne ~ +1.50%, σ ~ 1.20%
    # Permet de convertir la pente en z-score au lieu d'un scaling arbitraire
    SLOPE_HIST_MEAN = 0.015   # 1.50% = moyenne historique
    SLOPE_HIST_STD  = 0.012   # 1.20% = écart-type historique

    def nelson_siegel_implied(
        self, maturities: np.ndarray, yields: np.ndarray
    ) -> float:
        """
        f(t) = β₀ + β₁×e^(-t/τ) + β₂×(t/τ)×e^(-t/τ)
        Pente de la courbe → signal politique monétaire.

        FIX #5 : remplace le scaling arbitraire ×15/×5 par un z-score
        normalisé sur la distribution historique de la pente 10Y-3M.
        z > 0 → courbe plus pentue que la moyenne → dove → rate cut probable
        z < 0 → courbe inversée → hawkish → rate cut improbable
        sigmoid(z) donne une probabilité calibrée dans [0, 1].
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

            rate_short = ns_curve(result.x, 0.25)   # 3 mois
            rate_long  = ns_curve(result.x, 10.0)   # 10 ans
            slope = rate_long - rate_short

            # Z-score de la pente par rapport à la distribution historique
            z_slope = (slope - self.SLOPE_HIST_MEAN) / max(self.SLOPE_HIST_STD, 1e-6)

            # Courbure (β₂) : z-score aussi, centré sur 0
            # β₂ > 0 → "hump" → marché attend un changement de régime
            # Contribution modeste : pondérée à 25% du signal total
            z_curvature = b2 / max(abs(b2) + 0.01, 0.01) * 0.5  # normalisé [-0.5, +0.5]

            z_combined = z_slope + 0.25 * z_curvature
            return float(expit(z_combined))
        except Exception:
            return 0.5

    def get_probability(
        self,
        macro_data: dict,
        p_fedwatch: float,
        question: str = "",
    ) -> dict:
        p_logit = self.logit_probability(macro_data, question)

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
        p_market: Optional[float] = None,
        days_to_res: Optional[float] = None,
    ) -> dict:
        """
        FIX #5 : EventModel enrichi
        - Market price anchor : le prix Polymarket EST de l'information
          (efficience partielle). Ancrage léger w=0.3 pour ne pas être circulaire.
        - Temps restant : si < 3j, augmenter le poids des sources live
        """
        sources = [("base_rate", ref_class_result["p_base"], 1.0)]

        if p_kalshi is not None:
            sources.append(("kalshi", p_kalshi, 1.5))

        if sub_questions:
            p_fermi = self.fermi_decomposition(sub_questions)
            if p_fermi:
                sources.append(("fermi", p_fermi, 1.2))

        # FIX #5 : market price anchor (léger, pour ne pas être circulaire)
        # Le marché Polymarket contient de l'information agrégée — l'ignorer
        # complètement est aussi une erreur. Poids faible = 0.3
        if p_market is not None and 0.05 < p_market < 0.95:
            sources.append(("market_anchor", p_market, 0.3))

        # FIX #5 : si proche de l'expiration, les sources live (Kalshi, Fermi)
        # sont plus fiables que le base_rate historique
        if days_to_res is not None and days_to_res < 3:
            sources = [
                (name, p, w * (2.0 if name != "base_rate" else 0.5))
                for name, p, w in sources
            ]

        total_w    = sum(w for _, _, w in sources)
        p_ensemble = sum(p * w for _, p, w in sources) / total_w

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
    Minimum 5 résolutions par modèle pour calculer son poids — sinon poids égaux.

    FIX Bug #2 : utilise la table model_predictions pour calculer le Brier
    individuel de chaque modèle (base_rate, quant, bayes_updated) au lieu
    d'un seul Brier global réparti 1/3 - 1/3 - 1/3.
    """

    MIN_SAMPLES = 5   # résolutions minimum pour pondérer un modèle
    MIN_FOR_EXTREMIZE = 20  # résolutions minimum avant d'activer l'extremizing

    def __init__(self, db_conn: sqlite3.Connection):
        self.conn   = db_conn
        self.models = ["quant_adjusted", "llm", "base_rate"]

    def _get_per_model_brier(self, model_name: str, last_n: int = 20) -> tuple[float | None, int]:
        """
        Calcule le Brier score d'un modèle sur ses last_n dernières prédictions résolues.
        Retourne (brier, n_samples) ou (None, 0) si pas assez de données.
        """
        rows = self.conn.execute("""
            SELECT mp.p_predicted, mp.outcome
            FROM model_predictions mp
            JOIN trades t ON mp.market_id = t.market_id
            WHERE mp.model_name = ?
              AND mp.outcome IS NOT NULL
              AND t.status = 'closed'
            ORDER BY t.exit_ts DESC
            LIMIT ?
        """, (model_name, last_n)).fetchall()

        n = len(rows)
        if n < self.MIN_SAMPLES:
            return None, n

        brier = sum((p - o) ** 2 for p, o in rows) / n
        return brier, n

    def compute_weights(self, last_n: int = 20) -> dict[str, float]:
        """
        Calcule les poids Brier-weighted pour chaque modèle.
        Modèles sans assez d'historique reçoivent le poids moyen des autres.
        """
        brier_scores: dict[str, float | None] = {}
        n_samples: dict[str, int] = {}

        for model in self.models:
            b, n = self._get_per_model_brier(model, last_n)
            brier_scores[model] = b
            n_samples[model] = n

        # Modèles avec assez de données
        calibrated = {m: b for m, b in brier_scores.items() if b is not None}

        if not calibrated:
            # Aucun modèle n'a assez d'historique → poids égaux
            equal_w = 1.0 / len(self.models)
            log.debug("BrierEnsemble: pas assez d'historique, poids égaux (1/%d)", len(self.models))
            return {m: equal_w for m in self.models}

        # exp(-Brier) pour chaque modèle calibré
        exp_weights = {m: float(np.exp(-b)) for m, b in calibrated.items()}
        total_exp = sum(exp_weights.values())

        # Poids normalisés pour les modèles calibrés
        norm_weights = {m: w / total_exp for m, w in exp_weights.items()}

        # Poids moyen pour les modèles non calibrés
        avg_weight = sum(norm_weights.values()) / len(norm_weights)

        weights: dict[str, float] = {}
        for m in self.models:
            if m in norm_weights:
                weights[m] = norm_weights[m]
            else:
                weights[m] = avg_weight

        # Re-normaliser pour que Σ = 1
        total = sum(weights.values())
        weights = {m: w / total for m, w in weights.items()}

        log.info(
            "BrierEnsemble weights: %s  (brier: %s, samples: %s)",
            {m: f"{w:.3f}" for m, w in weights.items()},
            {m: f"{b:.4f}" if b is not None else "N/A" for m, b in brier_scores.items()},
            n_samples,
        )
        return weights

    def is_calibrated(self) -> bool:
        """True si au moins 2 modèles ont >= MIN_FOR_EXTREMIZE résolutions."""
        calibrated_count = 0
        for model in self.models:
            _, n = self._get_per_model_brier(model, self.MIN_FOR_EXTREMIZE)
            if n >= self.MIN_FOR_EXTREMIZE:
                calibrated_count += 1
        return calibrated_count >= 2

    def combine(self, predictions_dict: dict[str, float]) -> dict:
        weights = self.compute_weights()
        # Filtrer : ne pondérer que les modèles présents dans predictions_dict
        active_weights = {m: weights.get(m, 0) for m in predictions_dict}
        total_w = sum(active_weights.values())
        if total_w > 0:
            active_weights = {m: w / total_w for m, w in active_weights.items()}

        p_ensemble = sum(
            active_weights.get(m, 0) * p
            for m, p in predictions_dict.items()
        )
        probs  = list(predictions_dict.values())
        spread = max(probs) - min(probs) if len(probs) > 1 else 0.0
        return {
            "p_final":      round(p_ensemble, 4),
            "weights_used": active_weights,
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
    ensemble: dict,
    preds: dict,
    days_to_res: float = 30.0,
    brier_calibrated: bool = False,
) -> tuple[Optional[dict], str]:
    """
    Synthèse finale + extremizing adaptatif.

    FIX #1 : supprime bayes_result (plus de variable séparée)
    FIX #6 : uncertainty pondérée par temps restant
    FIX #7 : ne pas rejeter les signaux "faibles" si l'edge est fort
    """
    p_raw     = ensemble["p_final"]
    n_sources = len(preds)
    spread    = ensemble["model_spread"]

    # FIX #7 : ne rejeter que si spread > 0.25 ET < 2 sources
    # Un spread élevé avec 2+ sources = désaccord informatif, pas "faible"
    if spread > 0.25 and n_sources < 2:
        return None, "signal_trop_faible"

    extremized = False
    p_final = p_raw

    if brier_calibrated and n_sources >= 2:
        if spread < 0.12:
            alpha = 1.3
        else:
            alpha = 1.15
        p_final = p_raw ** alpha / (p_raw ** alpha + (1 - p_raw) ** alpha)
        extremized = True
        log.debug(f"Extremizing: α={alpha}, p_raw={p_raw:.4f} → p_final={p_final:.4f}")
    elif not brier_calibrated:
        log.debug("Extremizing OFF: Brier non calibré, p_final = p_raw")

    p_final = max(0.01, min(0.99, p_final))

    # Intervalles — enveloppe (min/max)
    all_lows = []
    all_highs = []
    for r in [base_rate_result, quant_result]:
        if r and "interval" in r:
            all_lows.append(r["interval"][0])
            all_highs.append(r["interval"][1])

    if all_lows and all_highs:
        low  = min(all_lows)
        high = max(all_highs)
    else:
        low  = p_final - 0.15
        high = p_final + 0.15

    low  = max(0.0, float(low))
    high = min(1.0, float(high))
    raw_uncertainty = high - low

    # FIX #6 : uncertainty scaling par temps restant
    # Proche de l'expiration → plus de confiance dans le modèle
    # Loin de l'expiration → uncertainty augmente (plus de temps pour surprises)
    if days_to_res <= 1:
        time_factor = 0.7   # très proche → réduire uncertainty
    elif days_to_res <= 7:
        time_factor = 0.85
    elif days_to_res <= 30:
        time_factor = 1.0   # baseline
    elif days_to_res <= 90:
        time_factor = 1.15
    else:
        time_factor = 1.3   # long terme → plus d'incertitude

    uncertainty = min(0.95, raw_uncertainty * time_factor)

    return {
        "p_final":        round(p_final, 4),
        "p_raw":          round(p_raw, 4),
        "interval":       (round(low, 4), round(high, 4)),
        "uncertainty":    round(uncertainty, 4),
        "n_sources":      n_sources,
        "extremized":     extremized,
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
    """
    Sélectionne le modèle adapté selon la NATURE du marché.

    FIX #6 : classification en 2 passes —
      1. Détecte si c'est un marché de PRIX crypto (Black-Scholes pertinent)
         vs un marché ÉVÉNEMENTIEL lié à la crypto (ETF, régulation, etc.)
      2. Détecte les marchés de politique monétaire (macro)
      3. Fallback → event

    Principe : Black-Scholes n'a de sens que pour "Will X reach price Y by date Z?"
    Pas pour "Will X ETF be approved?" même si X = Bitcoin.
    """
    q = question.lower()

    # ── Marchés événementiels (priorité haute — override crypto keywords) ──
    event_overrides = [
        "approve", "approval", "ban", "regulation", "congress", "senate",
        "law", "bill", "vote", "elect", "resign", "impeach", "indict",
        "etf", "sec ", "cftc", "executive order", "strategic reserve",
        "war", "ceasefire", "invasion", "sanction", "tariff", "treaty",
    ]
    if any(kw in q for kw in event_overrides):
        # Sauf si c'est un vrai prix target caché dedans
        price_patterns = ["above", "below", "reach", "hit", "exceed", "price"]
        if not any(pp in q for pp in price_patterns):
            return "event"

    # ── Marchés crypto PRIX (Black-Scholes a du sens) ──
    # FIX #9 : keywords étendus pour meilleure couverture
    crypto_price_kw = [
        "bitcoin", "btc", "eth", "ethereum", "solana", "sol", "crypto",
        "xrp", "ripple", "cardano", "ada", "dogecoin", "doge", "bnb",
        "avalanche", "avax", "polkadot", "dot", "matic", "polygon",
    ]
    price_kw = [
        "price", "above", "below", "reach", "hit", "exceed", "trading at",
        "100k", "120k", "150k", "200k", "50k", "75k", "250k",
        "1000", "2000", "3000", "4000", "5000", "10000",
        "$", "usd", "worth",
    ]
    has_crypto = any(kw in q for kw in crypto_price_kw)
    has_price  = any(kw in q for kw in price_kw)
    if has_crypto and has_price:
        return "crypto"

    # ── Marchés macro/Fed ──
    # FIX #9 : keywords macro étendus
    fed_kw = [
        "fed", "federal reserve", "interest rate", "fomc",
        "rate cut", "rate hike", "bps", "25bps", "50bps",
        "monetary policy", "inflation target", "quantitative",
        "jerome powell", "powell", "jay powell", "dot plot",
        "treasury yield", "yield curve", "cpi ", "pce ",
        "nonfarm", "non-farm", "payroll", "unemployment rate",
        "gdp growth", "recession", "soft landing", "hard landing",
        "tightening", "easing", "hawkish", "dovish",
    ]
    if any(kw in q for kw in fed_kw):
        return "macro"

    # ── Fallback ──
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
    # ── Superforce live feeds ────────────────────────────────────────────
    llm_estimate:   Optional[dict]  = None    # {p_estimate, confidence, reasoning, ...}
    news_analysis:  Optional[dict]  = None    # {direction, magnitude, key_signals}
    kalshi_divergence: Optional[dict] = None  # {divergence, direction, signal_strength}


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
            # FIX #2 : 0.80 était un fallback trop élevé (80% annualisée = mouvement extrême)
            # BTC IV typique Deribit : 45-65% en régime normal, 70-90% en crise
            # Fallback 0.60 si pas de données Deribit live
            sigma = ctx.btc_sigma if ctx.btc_sigma is not None else 0.60
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
            quant_result = self.macro.get_probability(macro, ctx.p_fedwatch, ctx.question)

        else:
            # EventModel — toujours disponible
            # Superforce : si Kalshi divergence détectée, utiliser son prix
            p_kalshi = ctx.p_kalshi
            if p_kalshi is None and ctx.kalshi_divergence:
                p_kalshi = ctx.kalshi_divergence.get("kalshi_price")
                log.info(f"Superforce Kalshi divergence: {ctx.kalshi_divergence.get('divergence', 0):+.3f}")

            # Superforce : si LLM a fourni des sub_questions (Fermi), les utiliser
            sub_q = ctx.sub_questions
            if not sub_q and ctx.llm_estimate:
                llm_subs = ctx.llm_estimate.get("sub_probabilities", [])
                if llm_subs:
                    sub_q = [(s.get("factor", ""), s.get("p", 0.5)) for s in llm_subs]
                    log.info(f"Superforce LLM Fermi: {len(sub_q)} sub-questions")

            quant_result = self.event.get_probability(
                ref_class_result=base_rate_result,
                p_kalshi=p_kalshi,
                sub_questions=sub_q,
                p_market=ctx.market_price,
                days_to_res=ctx.days_to_res,
            )

        # ── ÉTAPE 3 : Bayes update sur signaux news ────────────────────────
        # Le Bayes update MODIFIE le quant_result, il ne crée pas un modèle
        # séparé. bayes_updated remplace quant dans l'ensemble (pas les deux).
        # Temporal decay + cap ±15% par source.
        p_quant = quant_result["p_model"] if quant_result else base_rate_result["p_base"]
        p_news_adjusted = p_quant  # point de départ

        has_news_update = False
        if ctx.news_signals:
            now = datetime.now(timezone.utc)
            for signal in ctx.news_signals:
                lr_bull, lr_bear = LR_PRIOR.get(signal.source_id, (1.20, 0.833))
                try:
                    dir_value = signal.direction.value if signal.direction else None
                except AttributeError:
                    dir_value = None
                if dir_value not in ("bullish", "bearish"):
                    continue
                age_min = max(0.0, (now - signal.timestamp).total_seconds() / 60.0)
                decay = math.exp(-0.0025 * age_min)
                if dir_value == "bullish":
                    lr_eff = 1.0 + (lr_bull - 1.0) * decay
                else:
                    lr_eff = 1.0 + (lr_bear - 1.0) * decay
                odds = p_news_adjusted / max(1 - p_news_adjusted, 1e-9) * lr_eff
                p_new = max(0.02, min(0.98, odds / (1 + odds)))
                delta = p_new - p_news_adjusted
                if abs(delta) > 0.15:
                    p_new = p_news_adjusted + math.copysign(0.15, delta)
                p_news_adjusted = p_new
                has_news_update = True

        # ── ÉTAPE 3b : Superforce news_analysis (vrai Bayes, pas additif) ─
        # FIX #3 critique : convertir magnitude en likelihood ratio au lieu
        # d'ajouter directement. Magnitude 0.05 → LR ≈ 1.2, 0.10 → LR ≈ 1.5
        if ctx.news_analysis:
            direction = ctx.news_analysis.get("direction", "neutral")
            magnitude = min(0.15, abs(ctx.news_analysis.get("magnitude", 0)))
            if direction in ("bullish", "bearish") and magnitude > 0.01:
                # Convertir magnitude en LR : LR = exp(3 × magnitude)
                # 0.03→1.09, 0.05→1.16, 0.10→1.35, 0.15→1.57
                lr = math.exp(3.0 * magnitude)
                if direction == "bearish":
                    lr = 1.0 / lr
                odds = p_news_adjusted / max(1 - p_news_adjusted, 1e-9) * lr
                p_news_adjusted = max(0.02, min(0.98, odds / (1 + odds)))
                has_news_update = True
                log.info(f"Superforce news: {direction} mag={magnitude:.3f} LR={lr:.3f} → p={p_news_adjusted:.4f}")

        # ── ÉTAPE 4 : Ensemble de modèles INDÉPENDANTS ───────────────────
        # Architecture : on ne met dans l'ensemble que des sources qui ne
        # partagent PAS le même signal sous-jacent.
        #
        # - "quant_or_bayes" : le meilleur signal quantitatif (quant enrichi
        #   par news si disponible). UN seul slot, pas deux.
        # - "llm" : estimation LLM SANS les données quant en contexte
        #   (FIX #8 : le LLM reçoit la question + news, mais PAS les données
        #   FRED/macro qui alimentent déjà le quant).
        # - "base_rate" : seulement si aucune autre source secondaire.
        preds: dict[str, float] = {}

        # Slot 1 : meilleur signal quantitatif (news-adjusted si dispo)
        if has_news_update:
            preds["quant_adjusted"] = round(p_news_adjusted, 4)
        elif quant_result:
            preds["quant_adjusted"] = quant_result["p_model"]

        # Slot 2 : LLM (source indépendante — raisonnement, pas données)
        if ctx.llm_estimate and "p_estimate" in ctx.llm_estimate:
            p_llm = ctx.llm_estimate["p_estimate"]
            if 0.01 <= p_llm <= 0.99:
                confidence = ctx.llm_estimate.get("confidence", "medium")
                # Shrinkage adaptatif : LLM peu fiable → fort shrinkage
                shrinkage = {"high": 0.20, "medium": 0.35, "low": 0.55}.get(confidence, 0.35)
                p_llm_shrunk = 0.5 + (p_llm - 0.5) * (1 - shrinkage)
                preds["llm"] = round(p_llm_shrunk, 4)
                log.info(f"Superforce LLM: p_raw={p_llm:.3f} conf={confidence} → shrunk={p_llm_shrunk:.3f}")

        # Slot 3 : base_rate — uniquement comme fallback si < 2 sources
        if len(preds) < 2 and base_rate_result:
            preds["base_rate"] = base_rate_result["p_base"]

        if not preds:
            return {"tradeable": False, "reason": "no_models_available"}

        # Sauvegarder les prédictions per-model pour le Brier tracking
        # (sera enregistré en DB par main.py après ouverture du trade)
        model_predictions_snapshot = dict(preds)

        ens = self.ensemble.combine(preds)

        # ── ÉTAPE 5 : Décision finale + extremizing ────────────────────────
        brier_ok = self.ensemble.is_calibrated()
        result, reason = final_decision(
            base_rate_result, quant_result, ens, preds,
            days_to_res=ctx.days_to_res,
            brier_calibrated=brier_ok,
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
            "news_adjusted":      preds.get("quant_adjusted"),
            "n_historical":       self.rce.db.count(),
            "model_predictions":  model_predictions_snapshot,
        })

        log.info(
            f"Score: p_final={p_final:.3f} market={ctx.market_price:.3f} "
            f"edge={edge:+.3f} uncertainty={result['uncertainty']:.3f} "
            f"tradeable={result['tradeable']}"
        )
        return result
