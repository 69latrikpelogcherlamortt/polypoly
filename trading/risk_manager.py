"""
risk_manager.py  ·  Polymarket Trading Bot
────────────────────────────────────────────
Gestion du risque complète :
  KillSwitchMonitor   — 4 niveaux d'arrêt
  ExitRuleEvaluator   — 5 règles de sortie anticipée
  SizingEngine        — Kelly fractionnel + Monte Carlo VaR
  GateValidator       — 7 gates avant chaque trade

Règle absolue : aucun LLM prompt ne peut override un gate failure.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np

from core.config import (
    INITIAL_BANKROLL,
    MAX_TRADE_EUR, MAX_TRADE_PCT_BANKROLL,
    MAX_OPEN_POSITIONS, MAX_TOTAL_EXPOSURE_PCT,
    ALPHA_KELLY_FAVORI, ALPHA_KELLY_LONGSHOT,
    EDGE_MIN, EV_MIN, Z_SCORE_MIN,
    BRIER_LIMIT, MDD_LIMIT, MAX_EXPOSURE_PER_POS, MAX_VAR_PCT,
    BANKROLL_STOP_LEVEL,
    CONSECUTIVE_LOSSES_PAUSE, SHARPE_MIN, PROFIT_FACTOR_MIN,
    COOLDOWN_HOURS_MDD, COOLDOWN_HOURS_LOSSES,
    EXIT_EDGE_MIN, EXIT_PROFIT_CAPTURE_PCT, EXIT_ADVERSE_MOVE_PCT,
    MC_PATHS, MC_HORIZON, MC_CONF,
    S2_EDGE_RATIO_MIN, S2_EDGE_ABS_MIN,
)

log = logging.getLogger("risk")


# ═══════════════════════════════════════════════════════════════════════════
# 1. SIZING ENGINE
# ═══════════════════════════════════════════════════════════════════════════

class SizingEngine:
    """
    Kelly fractionnel + Monte Carlo VaR.

    Kelly fractionnaire :
      f* = (p×b - q) / b
      alpha = 0.30 favoris / 0.15 longshots
      size = min(alpha×f*×bankroll, 5% bankroll, 5€)

    Monte Carlo VaR :
      Bootstrap sur l'historique des returns réels (pas gaussien)
      10,000 chemins × 30 jours
    """

    def kelly_size(
        self,
        p_model: float,
        market_price: float,
        bankroll: float,
        is_longshot: bool = False,
    ) -> float:
        """Calcule la taille optimale par Kelly fractionnel."""
        if market_price <= 0.0 or market_price >= 1.0:
            return 0.0

        b = (1.0 - market_price) / market_price   # cote implicite
        if b < 1e-6:
            return 0.0
        q      = 1 - p_model
        f_star = (p_model * b - q) / b

        if f_star <= 0:
            return 0.0

        alpha    = ALPHA_KELLY_LONGSHOT if is_longshot else ALPHA_KELLY_FAVORI
        raw_size = alpha * f_star * bankroll

        max_size = min(
            raw_size,
            bankroll * MAX_TRADE_PCT_BANKROLL,
            MAX_TRADE_EUR,
        )
        return round(max(0.0, max_size), 2)

    def expected_value(self, p_model: float, market_price: float) -> float:
        """EV par unité misée."""
        if market_price <= 0 or market_price >= 1:
            return -1.0
        b  = (1 - market_price) / market_price
        ev = p_model * b - (1 - p_model)
        return round(ev, 4)

    def monte_carlo_var(
        self,
        returns_history: list[float],
        n_paths: int = MC_PATHS,
        horizon: int = MC_HORIZON,
        confidence: float = MC_CONF,
    ) -> tuple[float, float]:
        """
        VaR Monte Carlo Bootstrap — jamais gaussien (kurtosis BTC > 4).

        Returns:
            (var_95, expected_shortfall)
        """
        if len(returns_history) < 5:
            return 0.0, 0.0

        arr = np.array(returns_history)
        # Bootstrap : tirer horizon jours avec remise, n_paths fois
        rng      = np.random.default_rng()
        idx      = rng.choice(len(arr), size=(n_paths, horizon), replace=True)
        sim      = arr[idx].sum(axis=1)
        threshold = np.percentile(sim, (1 - confidence) * 100)
        es        = float(sim[sim < threshold].mean()) if (sim < threshold).any() else threshold

        return round(float(threshold), 4), round(es, 4)

    def n_shares(self, size_eur: float, price: float) -> float:
        """Nombre de shares achetées pour un montant donné au prix donné."""
        if price <= 0:
            return 0.0
        return round(size_eur / price, 4)


# ═══════════════════════════════════════════════════════════════════════════
# 2. GATE VALIDATOR — 7 gates avant chaque trade
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class GateCheckInput:
    """Input pour la validation des 7 gates."""
    edge:               float
    ev:                 float
    size_requested:     float
    kelly_size:         float
    bankroll:           float
    total_exposure:     float
    open_positions:     int
    var_95:             float
    mdd_30d:            float
    brier_15:           float
    strategy:           str        # "S1" ou "S2"
    is_longshot:        bool = False
    z_score:            float = 0.0
    p_model:            float = 0.0
    market_price:       float = 0.0


@dataclass
class GateCheckResult:
    """Résultat de la validation des 7 gates."""
    passed:         bool
    failures:       list[str]
    action:         str        # "TRADE" | "REDUCE" | "BLOCK" | "HALT"
    size_approved:  float      # taille approuvée (peut être réduite)
    rationale:      str


class GateValidator:
    """
    Valide les 7 conditions avant d'ouvrir un trade.
    Ordre d'exécution obligatoire.
    """

    def validate(self, inp: GateCheckInput) -> GateCheckResult:
        failures: list[str] = []
        size_approved = inp.size_requested

        # ── Gate 1 : Edge ─────────────────────────────────────────────────
        # No abs() — a negative edge (p_model < price) must be rejected
        if inp.edge < EDGE_MIN:
            failures.append("edge_gate")

        # ── Gate 2 : Expected Value ────────────────────────────────────────
        if inp.ev <= EV_MIN:
            failures.append("ev_gate")

        # ── Gate 3 : Kelly size ────────────────────────────────────────────
        if inp.size_requested > inp.kelly_size * 1.05:
            size_approved = inp.kelly_size
            failures.append("kelly_gate_reduce")

        # ── Gate 4 : Exposure ─────────────────────────────────────────────
        max_exposure = inp.bankroll * MAX_TOTAL_EXPOSURE_PCT
        if inp.total_exposure + size_approved > max_exposure:
            failures.append("exposure_gate")

        # ── Gate 4b : Max positions ────────────────────────────────────────
        if inp.open_positions >= MAX_OPEN_POSITIONS:
            failures.append("max_positions_gate")

        # ── Gate 5 : VaR ──────────────────────────────────────────────────
        var_limit = inp.bankroll * MAX_VAR_PCT
        if abs(inp.var_95) > var_limit:
            failures.append("var_gate")

        # ── Gate 6 : MDD ──────────────────────────────────────────────────
        if inp.mdd_30d >= MDD_LIMIT:
            failures.append("mdd_gate")

        # ── Gate 7 : Brier ────────────────────────────────────────────────
        if inp.brier_15 >= BRIER_LIMIT:
            failures.append("brier_gate")

        # ── Gate spécifique S2 : Z-score ───────────────────────────────────
        if inp.is_longshot and inp.z_score < Z_SCORE_MIN:
            failures.append("z_score_gate_longshot")

        # ── Gate spécifique S2 : Edge ratio ───────────────────────────────
        if inp.strategy == "S2":
            if inp.market_price <= 0:
                failures.append("s2_edge_ratio_gate")
            else:
                edge_ratio = inp.p_model / inp.market_price
                edge_abs   = inp.p_model - inp.market_price
                if edge_ratio < S2_EDGE_RATIO_MIN:
                    failures.append("s2_edge_ratio_gate")
                if edge_abs < S2_EDGE_ABS_MIN:
                    failures.append("s2_edge_abs_gate")

        # ── Bankroll minimum ──────────────────────────────────────────────
        if inp.bankroll < BANKROLL_STOP_LEVEL:
            failures.append("bankroll_stop")

        # ── Détermination action ──────────────────────────────────────────
        blocker_gates = {
            "edge_gate", "ev_gate", "exposure_gate", "max_positions_gate",
            "var_gate", "mdd_gate", "brier_gate", "bankroll_stop",
            "z_score_gate_longshot", "s2_edge_ratio_gate", "s2_edge_abs_gate",
        }
        reducing_gates = {"kelly_gate_reduce"}

        has_blocker = any(f in blocker_gates for f in failures)
        has_reducer = any(f in reducing_gates for f in failures)

        if "bankroll_stop" in failures or "mdd_gate" in failures or "brier_gate" in failures:
            action = "HALT"
        elif has_blocker:
            action = "BLOCK"
        elif has_reducer:
            action = "REDUCE"
        else:
            action = "TRADE"

        rationale = self._build_rationale(inp, failures, size_approved)

        return GateCheckResult(
            passed        = len(failures) == 0,
            failures      = failures,
            action        = action,
            size_approved = round(max(0.0, size_approved), 2),
            rationale     = rationale,
        )

    def _build_rationale(
        self,
        inp: GateCheckInput,
        failures: list[str],
        size: float,
    ) -> str:
        if not failures:
            return (
                f"All gates passed. edge={inp.edge:+.3f} ev={inp.ev:+.3f} "
                f"kelly={inp.kelly_size:.2f} size={size:.2f} "
                f"mdd={inp.mdd_30d:.1%} brier={inp.brier_15:.3f}"
            )
        return (
            f"Gate failures: {', '.join(failures)}. "
            f"edge={inp.edge:+.3f} ev={inp.ev:+.3f} "
            f"brier={inp.brier_15:.3f} mdd={inp.mdd_30d:.1%}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3. EXIT RULE EVALUATOR
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ExitSignal:
    should_exit:    bool
    reason:         str
    urgency:        str   # "immediate" | "monitor" | "hold"


class ExitRuleEvaluator:
    """
    Évalue les 5 règles de sortie anticipée.

    VENDRE si UNE condition vraie :
    1. Edge actuel < 4 cents
    2. P_true a changé de signe (thèse invalidée)
    3. 65% du potentiel capturé
    4. J-3 avant résolution ET prix entre 0.40-0.80€
    5. Marché bouge >30% contre toi sans nouvelle info

    CONSERVER si :
    1. Longshot encore < 0.30€ mais P_true > 0.50
    2. Prix > 0.85€ et P_true > 0.90€
    3. Moins de 24h et conviction forte
    4. Prix baisse temporairement sans nouvelle info
    """

    def evaluate(
        self,
        p_model_current: float,
        current_price: float,
        entry_price: float,
        days_to_res: float,
        is_longshot: bool = False,
        strategy: str = "S1",
    ) -> ExitSignal:

        edge_current = p_model_current - current_price

        # ── Règle 1 : Edge mort ────────────────────────────────────────────
        if edge_current < EXIT_EDGE_MIN:
            return ExitSignal(
                should_exit = True,
                reason      = f"edge_mort: edge={edge_current:+.3f} < {EXIT_EDGE_MIN}",
                urgency     = "immediate",
            )

        # ── Règle 2 : P_true a changé de signe ────────────────────────────
        if p_model_current < entry_price - 0.05:
            return ExitSignal(
                should_exit = True,
                reason      = (
                    f"thesis_flip: p_model={p_model_current:.3f} "
                    f"vs entry={entry_price:.3f}"
                ),
                urgency     = "immediate",
            )

        # ── Règle 3 : 65% du potentiel capturé ────────────────────────────
        max_gain = 1.0 - entry_price   # max = 1€ (résolution YES)
        if max_gain < 1e-6:
            pass
        elif not is_longshot:
            captured = (current_price - entry_price) / max_gain
            if captured >= EXIT_PROFIT_CAPTURE_PCT:
                return ExitSignal(
                    should_exit = True,
                    reason      = f"profit_capture: {captured:.0%} >= {EXIT_PROFIT_CAPTURE_PCT:.0%}",
                    urgency     = "monitor",
                )

        # ── Règle 4 : J-3 + zone binaire ──────────────────────────────────
        if days_to_res <= 3 and 0.40 <= current_price <= 0.80:
            return ExitSignal(
                should_exit = True,
                reason      = (
                    f"binary_risk_zone: {days_to_res:.1f}j avant résolution, "
                    f"prix={current_price:.2f} (zone 0.40-0.80)"
                ),
                urgency     = "monitor",
            )

        # ── Règle 5 : Mouvement adverse > 30% sans news ────────────────────
        if entry_price >= 1e-6:
            adverse_move = (entry_price - current_price) / entry_price
            if adverse_move >= EXIT_ADVERSE_MOVE_PCT:
                return ExitSignal(
                    should_exit = True,
                    reason      = f"adverse_move: {adverse_move:.0%} contre toi",
                    urgency     = "monitor",
                )

        # ── Règles de conservation ─────────────────────────────────────────
        if is_longshot and current_price < 0.30 and p_model_current > 0.50:
            return ExitSignal(
                should_exit = False,
                reason      = "hold: longshot < 0.30 mais p_model > 50%",
                urgency     = "hold",
            )

        if current_price > 0.85 and p_model_current > 0.90:
            return ExitSignal(
                should_exit = False,
                reason      = "hold: prix > 85% et p_model > 90%",
                urgency     = "hold",
            )

        return ExitSignal(
            should_exit = False,
            reason      = "all_clear",
            urgency     = "hold",
        )


# ═══════════════════════════════════════════════════════════════════════════
# 4. KILL SWITCH MONITOR
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class KillSwitchStatus:
    level:              int       # 0=ok, 1=stop, 2=cooldown, 3=pause, 4=review
    active:             bool
    reason:             str
    cooldown_until:     Optional[datetime]
    allow_new_trades:   bool
    allow_exit_only:    bool


class KillSwitchMonitor:
    """
    Hiérarchie des stops :
    NIVEAU 1 — Stop immédiat      : bankroll < 60€
    NIVEAU 2 — Cooldown 72h       : MDD > 8% sur 30j
    NIVEAU 3 — Pause + recalib.   : Brier > 0.22 / 5 pertes consécutives
    NIVEAU 4 — Revue stratégie    : Sharpe < 1.0 / PF < 1.2
    """

    def __init__(self, db_repo):
        self.repo = db_repo   # TradeRepository

    def check(
        self,
        bankroll: float,
        mdd_30d: float,
        brier_15: float,
        consecutive_losses: int,
        sharpe_30d: float,
        profit_factor: float,
    ) -> KillSwitchStatus:

        now = datetime.now(timezone.utc)

        # ── NIVEAU 1 : Stop total ─────────────────────────────────────────
        if bankroll < BANKROLL_STOP_LEVEL:
            return KillSwitchStatus(
                level           = 1,
                active          = True,
                reason          = f"Bankroll {bankroll:.2f}€ < {BANKROLL_STOP_LEVEL}€ — STOP TOTAL",
                cooldown_until  = None,
                allow_new_trades= False,
                allow_exit_only = True,
            )

        # ── NIVEAU 2 : Cooldown 72h (MDD) ────────────────────────────────
        if mdd_30d >= MDD_LIMIT:
            cooldown_until = now + timedelta(hours=COOLDOWN_HOURS_MDD)
            # Vérifier si le cooldown est déjà en cours
            stored = self.repo.get_kill_switch("mdd_cooldown_until")
            if stored:
                try:
                    cd_end = datetime.fromisoformat(stored)
                    if cd_end > now:
                        return KillSwitchStatus(
                            level           = 2,
                            active          = True,
                            reason          = f"MDD cooldown jusqu'à {cd_end.strftime('%Y-%m-%d %H:%M')} UTC",
                            cooldown_until  = cd_end,
                            allow_new_trades= False,
                            allow_exit_only = True,
                        )
                except ValueError:
                    log.warning(f"Invalid mdd_cooldown_until value in DB: {stored!r}, resetting cooldown")
                    self.repo.set_kill_switch("mdd_cooldown_until", "")

            # Déclencher cooldown
            self.repo.set_kill_switch(
                "mdd_cooldown_until", cooldown_until.isoformat()
            )
            log.warning(f"KILL SWITCH NIVEAU 2: MDD {mdd_30d:.1%} >= {MDD_LIMIT:.0%}")
            return KillSwitchStatus(
                level           = 2,
                active          = True,
                reason          = f"MDD {mdd_30d:.1%} ≥ {MDD_LIMIT:.0%} — cooldown 72h",
                cooldown_until  = cooldown_until,
                allow_new_trades= False,
                allow_exit_only = True,
            )

        # ── NIVEAU 3A : Brier trop élevé ─────────────────────────────────
        if brier_15 >= BRIER_LIMIT:
            return KillSwitchStatus(
                level           = 3,
                active          = True,
                reason          = f"Brier {brier_15:.3f} ≥ {BRIER_LIMIT} — recalibration requise",
                cooldown_until  = None,
                allow_new_trades= False,
                allow_exit_only = True,
            )

        # ── NIVEAU 3B : 5 pertes consécutives ────────────────────────────
        if consecutive_losses >= CONSECUTIVE_LOSSES_PAUSE:
            cooldown_until = now + timedelta(hours=COOLDOWN_HOURS_LOSSES)
            stored = self.repo.get_kill_switch("loss_streak_cooldown_until")
            if stored:
                try:
                    cd_end = datetime.fromisoformat(stored)
                    if cd_end > now:
                        return KillSwitchStatus(
                            level           = 3,
                            active          = True,
                            reason          = f"{consecutive_losses} pertes consécutives — pause 48h",
                            cooldown_until  = cd_end,
                            allow_new_trades= False,
                            allow_exit_only = True,
                        )
                except ValueError:
                    log.warning(f"Invalid loss_streak_cooldown_until value in DB: {stored!r}, resetting cooldown")
                    self.repo.set_kill_switch("loss_streak_cooldown_until", "")
            self.repo.set_kill_switch(
                "loss_streak_cooldown_until", cooldown_until.isoformat()
            )
            log.warning(f"KILL SWITCH NIVEAU 3: {consecutive_losses} pertes consécutives")
            return KillSwitchStatus(
                level           = 3,
                active          = True,
                reason          = f"{consecutive_losses} pertes consécutives — pause 48h",
                cooldown_until  = cooldown_until,
                allow_new_trades= False,
                allow_exit_only = True,
            )

        # ── NIVEAU 4 : Revue stratégie ────────────────────────────────────
        if sharpe_30d < SHARPE_MIN and sharpe_30d != 0.0:
            log.warning(f"KILL SWITCH NIVEAU 4: Sharpe {sharpe_30d:.2f} < {SHARPE_MIN}")
            return KillSwitchStatus(
                level           = 4,
                active          = True,
                reason          = f"Sharpe {sharpe_30d:.2f} < {SHARPE_MIN} — revue stratégie",
                cooldown_until  = None,
                allow_new_trades= False,
                allow_exit_only = False,
            )

        if profit_factor < PROFIT_FACTOR_MIN and profit_factor > 0:
            log.warning(f"KILL SWITCH NIVEAU 4: PF {profit_factor:.2f} < {PROFIT_FACTOR_MIN}")
            return KillSwitchStatus(
                level           = 4,
                active          = True,
                reason          = f"Profit Factor {profit_factor:.2f} < {PROFIT_FACTOR_MIN} — recalibration",
                cooldown_until  = None,
                allow_new_trades= False,
                allow_exit_only = False,
            )

        return KillSwitchStatus(
            level           = 0,
            active          = False,
            reason          = "ok",
            cooldown_until  = None,
            allow_new_trades= True,
            allow_exit_only = False,
        )


# ═══════════════════════════════════════════════════════════════════════════
# 5. RISK MANAGER — interface principale
# ═══════════════════════════════════════════════════════════════════════════

class RiskManager:
    """
    Orchestre toutes les vérifications de risque.
    Point d'entrée unique pour le main loop.
    """

    def __init__(self, db_repo, metrics):
        self.sizing     = SizingEngine()
        self.gate       = GateValidator()
        self.exit_eval  = ExitRuleEvaluator()
        self.kill_sw    = KillSwitchMonitor(db_repo)
        self.metrics    = metrics

    def check_kill_switches(
        self,
        bankroll: float,
        positions: list,
    ) -> KillSwitchStatus:
        state = self.metrics.build_portfolio_state(bankroll, positions)
        return self.kill_sw.check(
            bankroll            = bankroll,
            mdd_30d             = state.mdd_30d,
            brier_15            = state.brier_15,
            consecutive_losses  = state.consecutive_losses,
            sharpe_30d          = state.sharpe_30d,
            profit_factor       = state.profit_factor,
        )

    def validate_new_trade(
        self,
        p_model: float,
        market_price: float,
        bankroll: float,
        positions: list,
        strategy: str,
        is_longshot: bool,
        z_score: float,
        returns_history: list[float],
    ) -> GateCheckResult:
        """
        Valide qu'un nouveau trade peut être ouvert.
        """
        edge           = p_model - market_price
        ev             = self.sizing.expected_value(p_model, market_price)
        kelly          = self.sizing.kelly_size(p_model, market_price, bankroll, is_longshot)
        total_exposure = sum(p.cost_basis for p in positions)
        open_count     = len(positions)
        var_95, _      = self.sizing.monte_carlo_var(returns_history)

        state = self.metrics.build_portfolio_state(bankroll, positions)

        inp = GateCheckInput(
            edge            = edge,
            ev              = ev,
            size_requested  = kelly,
            kelly_size      = kelly,
            bankroll        = bankroll,
            total_exposure  = total_exposure,
            open_positions  = open_count,
            var_95          = var_95,
            mdd_30d         = state.mdd_30d,
            brier_15        = state.brier_15,
            strategy        = strategy,
            is_longshot     = is_longshot,
            z_score         = z_score,
            p_model         = p_model,
            market_price    = market_price,
        )
        result = self.gate.validate(inp)

        if result.passed:
            log.info(
                f"Gates OK: edge={edge:+.3f} ev={ev:+.3f} kelly={kelly:.2f}€ "
                f"strategy={strategy}"
            )
        else:
            log.warning(
                f"Gate failures: {result.failures} — {result.rationale}"
            )
        return result

    def check_exit(
        self,
        p_model: float,
        current_price: float,
        entry_price: float,
        days_to_res: float,
        is_longshot: bool,
        strategy: str,
    ) -> ExitSignal:
        return self.exit_eval.evaluate(
            p_model_current = p_model,
            current_price   = current_price,
            entry_price     = entry_price,
            days_to_res     = days_to_res,
            is_longshot     = is_longshot,
            strategy        = strategy,
        )
