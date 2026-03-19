"""
portfolio_risk.py  ·  Polymarket Trading Bot
──────────────────────────────────────────────
Gestion du risque au niveau portefeuille :
  PortfolioRiskMetrics   -- dataclass des metriques de risque
  PortfolioRiskEngine    -- VaR, CVaR, correlation, concentration
  kelly_portfolio_size   -- Kelly fractionnel avec penalite de concentration

Utilise une matrice de correlation estimee entre positions
(par categorie quand les donnees de prix sont insuffisantes).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import norm

from trading.risk_manager import get_market_category

log = logging.getLogger("portfolio_risk")


# ═══════════════════════════════════════════════════════════════════════════
# 1. DATACLASS
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PortfolioRiskMetrics:
    """Metriques de risque au niveau portefeuille."""
    var_95: float                # Value-at-Risk 95%
    var_99: float                # Value-at-Risk 99%
    expected_shortfall: float    # CVaR (Expected Shortfall) au-dela du VaR 95%
    effective_positions: float   # 1 / HHI — nombre effectif de positions
    concentration_hhi: float     # Herfindahl-Hirschman Index
    max_correlated_loss: float   # perte max si positions correlees bougent ensemble


# ═══════════════════════════════════════════════════════════════════════════
# 2. PORTFOLIO RISK ENGINE
# ═══════════════════════════════════════════════════════════════════════════

class PortfolioRiskEngine:
    """
    Moteur de risque portefeuille pour marches binaires Polymarket.

    Estime les correlations entre positions, calcule VaR / CVaR via
    approximation gaussienne sur la variance ajustee par correlation,
    et applique des gates de risque portefeuille.
    """

    # ── Constantes ────────────────────────────────────────────────────────
    MAX_PORTFOLIO_VAR95: float    = 0.15
    MAX_CORRELATED_LOSS: float    = 0.20
    MIN_EFFECTIVE_POSITIONS: float = 2

    # Correlations intra-categorie par defaut
    _INTRA_CATEGORY_CORR: dict[str, float] = {
        "macro_fed":   0.80,
        "crypto":      0.70,
        "politics":    0.40,
        "sports":      0.20,
        "geopolitics": 0.50,
        "other":       0.15,
    }

    # Correlations inter-categories par defaut
    _CROSS_CATEGORY_CORR: dict[tuple[str, str], float] = {
        ("macro_fed", "crypto"):      0.20,
        ("macro_fed", "politics"):    0.15,
        ("macro_fed", "geopolitics"): 0.15,
        ("crypto",    "politics"):    0.10,
        ("crypto",    "geopolitics"): 0.10,
        ("politics",  "geopolitics"): 0.20,
    }
    _DEFAULT_CROSS_CORR: float = 0.05

    def __init__(self, db_conn):
        self.db_conn = db_conn

    # ── Correlation matrix ────────────────────────────────────────────────

    def _category_correlation(self, cat_a: str, cat_b: str) -> float:
        """Correlation par defaut entre deux categories."""
        if cat_a == cat_b:
            return self._INTRA_CATEGORY_CORR.get(cat_a, 0.15)

        key = tuple(sorted((cat_a, cat_b)))
        return self._CROSS_CATEGORY_CORR.get(key, self._DEFAULT_CROSS_CORR)

    def _estimate_correlation_matrix(
        self,
        market_ids: list[str],
        positions: list[dict],
    ) -> np.ndarray:
        """
        Estime la matrice de correlation entre positions.

        Tente d'abord d'utiliser l'historique de prix en base.
        Retombe sur _category_correlation() quand les donnees sont
        insuffisantes (< 10 observations communes).
        """
        n = len(market_ids)
        if n == 0:
            return np.array([[]])

        corr = np.eye(n)

        # Construire un mapping market_id -> category
        categories: list[str] = []
        pos_by_id = {p["market_id"]: p for p in positions}
        for mid in market_ids:
            question = pos_by_id.get(mid, {}).get("question", "")
            categories.append(get_market_category(question))

        # Tenter de charger l'historique de prix depuis la base
        price_series: dict[str, list[float]] = {}
        try:
            for mid in market_ids:
                rows = self.db_conn.execute(
                    "SELECT price FROM price_history "
                    "WHERE market_id = ? ORDER BY ts ASC",
                    (mid,),
                ).fetchall()
                if rows:
                    price_series[mid] = [float(r[0]) for r in rows]
        except Exception:
            log.debug("price_history table unavailable, using category fallback")

        for i in range(n):
            for j in range(i + 1, n):
                rho = self._compute_pairwise_corr(
                    market_ids[i], market_ids[j],
                    price_series, categories[i], categories[j],
                )
                corr[i, j] = rho
                corr[j, i] = rho

        return corr

    def _compute_pairwise_corr(
        self,
        mid_a: str,
        mid_b: str,
        price_series: dict[str, list[float]],
        cat_a: str,
        cat_b: str,
    ) -> float:
        """Correlation entre deux positions — prix si possible, sinon categorie."""
        sa = price_series.get(mid_a)
        sb = price_series.get(mid_b)

        if sa and sb:
            # Aligner sur la longueur minimale (dernieres observations)
            min_len = min(len(sa), len(sb))
            if min_len >= 10:
                a = np.diff(np.array(sa[-min_len:]))
                b = np.diff(np.array(sb[-min_len:]))
                if a.std() > 1e-9 and b.std() > 1e-9:
                    rho = float(np.corrcoef(a, b)[0, 1])
                    # Clamp to [-1, 1] pour robustesse numerique
                    return max(-1.0, min(1.0, rho))

        # Fallback : correlation par categorie
        return self._category_correlation(cat_a, cat_b)

    # ── Portfolio metrics ─────────────────────────────────────────────────

    def compute_portfolio_metrics(
        self,
        positions: list[dict],
        bankroll: float,
    ) -> PortfolioRiskMetrics:
        """
        Calcule les metriques de risque portefeuille.

        Chaque position dict doit contenir au minimum :
          market_id, question, size (montant en EUR), p_market (prix courant)

        VaR via approximation gaussienne :
          sigma_i = sqrt(p_i * (1 - p_i))   (ecart-type binaire)
          portfolio_var = w^T * Sigma * w    (variance avec correlation)
          VaR_alpha = -z_alpha * sqrt(portfolio_var) * bankroll
        """
        if not positions or bankroll <= 0:
            return PortfolioRiskMetrics(
                var_95=0.0, var_99=0.0, expected_shortfall=0.0,
                effective_positions=0.0, concentration_hhi=1.0,
                max_correlated_loss=0.0,
            )

        n = len(positions)
        market_ids = [p["market_id"] for p in positions]
        total_exposure = sum(p.get("size", 0.0) for p in positions)

        if total_exposure <= 0:
            return PortfolioRiskMetrics(
                var_95=0.0, var_99=0.0, expected_shortfall=0.0,
                effective_positions=0.0, concentration_hhi=1.0,
                max_correlated_loss=0.0,
            )

        # Poids normalises par le bankroll
        weights = np.array([p.get("size", 0.0) / bankroll for p in positions])

        # Ecart-type binaire par position
        sigmas = np.array([
            math.sqrt(max(p.get("p_market", 0.5) * (1 - p.get("p_market", 0.5)), 1e-9))
            for p in positions
        ])

        # Matrice de correlation -> matrice de covariance
        corr_matrix = self._estimate_correlation_matrix(market_ids, positions)
        # Cov = diag(sigma) @ Corr @ diag(sigma)
        D = np.diag(sigmas)
        cov_matrix = D @ corr_matrix @ D

        # Variance du portefeuille (ponderation par poids)
        portfolio_variance = float(weights @ cov_matrix @ weights)
        portfolio_std = math.sqrt(max(portfolio_variance, 0.0))

        # VaR gaussien
        z_95 = norm.ppf(0.95)   # ~1.645
        z_99 = norm.ppf(0.99)   # ~2.326
        var_95 = z_95 * portfolio_std
        var_99 = z_99 * portfolio_std

        # Expected Shortfall (CVaR) gaussien : ES = sigma * phi(z) / (1 - alpha)
        es_95 = portfolio_std * norm.pdf(z_95) / 0.05

        # ── HHI & effective positions ─────────────────────────────────────
        weight_fractions = np.array([
            p.get("size", 0.0) / total_exposure for p in positions
        ])
        hhi = float(np.sum(weight_fractions ** 2))
        effective_n = 1.0 / hhi if hhi > 1e-9 else float(n)

        # ── Max correlated loss ───────────────────────────────────────────
        # Scenario : toutes les positions correlees perdent simultanement
        # Somme des pertes ponderees par les correlations moyennes
        max_corr_loss = self._compute_max_correlated_loss(
            weights, sigmas, corr_matrix, bankroll,
        )

        return PortfolioRiskMetrics(
            var_95=round(var_95, 6),
            var_99=round(var_99, 6),
            expected_shortfall=round(es_95, 6),
            effective_positions=round(effective_n, 4),
            concentration_hhi=round(hhi, 6),
            max_correlated_loss=round(max_corr_loss, 6),
        )

    def _compute_max_correlated_loss(
        self,
        weights: np.ndarray,
        sigmas: np.ndarray,
        corr_matrix: np.ndarray,
        bankroll: float,
    ) -> float:
        """
        Perte maximale en scenario de stress correle.

        Pour chaque position, calcule la perte potentielle (2-sigma)
        ponderee par la correlation moyenne avec les autres positions.
        Retourne la somme normalisee par le bankroll.
        """
        n = len(weights)
        if n <= 1:
            # Position unique : perte max = poids * 2 * sigma
            if n == 1:
                return float(weights[0] * 2.0 * sigmas[0])
            return 0.0

        total_stress_loss = 0.0
        for i in range(n):
            # Correlation moyenne de cette position avec les autres
            avg_corr = float(np.mean([
                corr_matrix[i, j] for j in range(n) if j != i
            ]))
            # Perte stress = poids * 2-sigma * (1 + avg_corr) / 2
            stress_factor = (1.0 + max(avg_corr, 0.0)) / 2.0
            total_stress_loss += weights[i] * 2.0 * sigmas[i] * stress_factor

        return total_stress_loss

    # ── Risk gates ────────────────────────────────────────────────────────

    def check_portfolio_risk_gates(
        self,
        metrics: PortfolioRiskMetrics,
        new_position_size: float,
        bankroll: float,
    ) -> tuple[bool, str]:
        """
        Verifie les gates de risque portefeuille avant un nouveau trade.

        Returns:
            (passed, reason) — passed=True si le trade est autorise.
        """
        failures: list[str] = []

        # Gate 1 : VaR 95% du portefeuille
        if metrics.var_95 > self.MAX_PORTFOLIO_VAR95:
            failures.append(
                f"portfolio_var95={metrics.var_95:.4f} > "
                f"limit={self.MAX_PORTFOLIO_VAR95}"
            )

        # Gate 2 : Perte correlee maximale
        if metrics.max_correlated_loss > self.MAX_CORRELATED_LOSS:
            failures.append(
                f"max_correlated_loss={metrics.max_correlated_loss:.4f} > "
                f"limit={self.MAX_CORRELATED_LOSS}"
            )

        # Gate 3 : Nombre effectif de positions
        if metrics.effective_positions < self.MIN_EFFECTIVE_POSITIONS:
            # Seulement si on a deja des positions significatives
            if metrics.concentration_hhi < 1.0:
                failures.append(
                    f"effective_positions={metrics.effective_positions:.2f} < "
                    f"min={self.MIN_EFFECTIVE_POSITIONS}"
                )

        if failures:
            reason = "PORTFOLIO RISK BLOCK: " + "; ".join(failures)
            log.warning(reason)
            return False, reason

        return True, "portfolio_risk_gates_passed"


# ═══════════════════════════════════════════════════════════════════════════
# 3. KELLY PORTFOLIO SIZE
# ═══════════════════════════════════════════════════════════════════════════

def kelly_portfolio_size(
    p_model: float,
    p_market: float,
    portfolio_metrics: PortfolioRiskMetrics,
    bankroll: float,
    max_trade_eur: float,
    max_trade_pct: float,
    is_longshot: bool = False,
) -> float:
    """
    Kelly fractionnel ajuste par la concentration du portefeuille.

    Formule :
      b = (1 - p_market) / p_market       (cote implicite)
      f* = (p_model * b - (1 - p_model)) / b
      alpha = 0.15 (longshot) | 0.30 (favori)
      kelly_base = alpha * f* * bankroll

    Penalite de concentration :
      size = kelly_base * (1 - min(hhi * 2.0, 0.5))

    Capped par max_trade_eur et max_trade_pct * bankroll.
    """
    if p_market <= 0.0 or p_market >= 1.0:
        return 0.0
    if bankroll <= 0.0:
        return 0.0

    # Cote implicite
    b = (1.0 - p_market) / p_market
    if b < 1e-6:
        return 0.0

    q = 1.0 - p_model
    f_star = (p_model * b - q) / b

    if f_star <= 0.0:
        return 0.0

    # Kelly fractionnel
    alpha = 0.15 if is_longshot else 0.30
    kelly_base = alpha * f_star * bankroll

    # Penalite de concentration via HHI
    hhi = portfolio_metrics.concentration_hhi
    concentration_penalty = min(hhi * 2.0, 0.5)
    size = kelly_base * (1.0 - concentration_penalty)

    # Caps
    size = min(size, max_trade_eur, bankroll * max_trade_pct)

    return round(max(0.0, size), 2)
