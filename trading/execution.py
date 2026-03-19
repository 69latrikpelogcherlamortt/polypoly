"""
execution.py  ·  Polymarket Trading Bot
─────────────────────────────────────────
Exécution optimale via Almgren-Chriss (2000) + microstructure Kyle (1985).

Composants :
  ExecutionParams      — paramètres calibrés par profil
  AlmgrenChrissExecutor— trajectoire optimale + limit orders + repricing
  PartialFillHandler   — 3 règles réalistes pour fills partiels
  RepricingEngine      — décision de repricing intelligente
  EXECUTION_PROFILES   — 3 profils selon le type de signal

Règle absolue : JAMAIS de market order. Limit orders uniquement.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from core.config import (
    DRY_RUN,
    AC_GAMMA_DEFAULT, AC_ETA_DEFAULT, AC_SIGMA_DEFAULT,
    AC_LAMBDA_DEFAULT, AC_T_DEFAULT, AC_N_DEFAULT, AC_MAX_REPRICE,
    REPRICE_SPREAD_BPS_MAX, REPRICE_MID_DELTA_MIN,
    REPRICE_IMBALANCE_WAIT, REPRICE_OFFSET,
    EDGE_MIN,
)

FILL_RATE_THRESHOLD = 0.80   # minimum fill rate to consider execution successful

log = logging.getLogger("execution")


# ═══════════════════════════════════════════════════════════════════════════
# 1. EXECUTION PARAMS
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ExecutionParams:
    """
    Paramètres calibrés pour Polymarket.

    γ (permanent impact) : faible — marchés binaires
    η (temporary impact) : dépend liquidité du marché
    σ (volatilité)       : rolling 14 jours
    λ (risk aversion)    : 0.30 (= α Kelly)
    """
    gamma: float = AC_GAMMA_DEFAULT
    eta:   float = AC_ETA_DEFAULT
    sigma: float = AC_SIGMA_DEFAULT
    lam:   float = AC_LAMBDA_DEFAULT
    T:     float = AC_T_DEFAULT     # horizon exécution (secondes)
    N:     int   = AC_N_DEFAULT     # nombre de slices
    max_reprice: int = AC_MAX_REPRICE


# 3 profils d'exécution
EXECUTION_PROFILES = {
    "favori_patient": ExecutionParams(
        # Favori (0.70-0.92€), liquidité correcte
        gamma=0.001, eta=0.008, sigma=0.03,
        lam=0.20,   # patient → TWAP quasi-pur
        T=480.0,    # 8 minutes
        N=8, max_reprice=6,
    ),
    "longshot_patient": ExecutionParams(
        # Longshot (0.01-0.08€), marché peu liquide
        gamma=0.003, eta=0.015, sigma=0.08,
        lam=0.10,   # très patient → minimise impact
        T=600.0,    # 10 minutes
        N=5, max_reprice=4,
    ),
    "signal_urgent": ExecutionParams(
        # Edge éphémère (news intraday)
        gamma=0.001, eta=0.010, sigma=0.05,
        lam=0.80,   # risquophobe → trade vite au début
        T=120.0,    # 2 minutes
        N=4, max_reprice=3,
    ),
}


def select_profile(market_type: str, signal_decay: float = 0.3) -> ExecutionParams:
    """
    signal_decay [0-1] :
    0.0 = edge stable (longshot macro, semaines)
    1.0 = edge éphémère (news intraday, minutes)
    """
    if signal_decay > 0.7:
        return EXECUTION_PROFILES["signal_urgent"]
    elif market_type == "longshot":
        return EXECUTION_PROFILES["longshot_patient"]
    else:
        return EXECUTION_PROFILES["favori_patient"]


# ═══════════════════════════════════════════════════════════════════════════
# 2. ORDERBOOK ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════

def analyze_orderbook(clob, token_id: str) -> dict:
    """
    Métriques clés de microstructure (Kyle 1985) :
    - Imbalance : pression achat vs vente
    - Depth     : liquidité disponible à ±2% du mid
    - Spread    : coût de crossing en bps
    - VWAP bid  : prix moyen pondéré côté achat
    """
    try:
        book = clob.get_order_book(token_id)
    except Exception as e:
        log.warning(f"get_order_book({token_id[:20]}): {e}")
        return {"status": "error"}

    if not book or not hasattr(book, "bids") or not book.bids or not book.asks:
        return {"status": "illiquide"}

    bids = [(float(b.price), float(b.size)) for b in book.bids[:5]]
    asks = [(float(a.price), float(a.size)) for a in book.asks[:5]]

    best_bid, best_ask = bids[0][0], asks[0][0]
    mid    = (best_bid + best_ask) / 2
    spread = best_ask - best_bid

    bid_vol  = sum(s for _, s in bids)
    ask_vol  = sum(s for _, s in asks)
    total_v  = bid_vol + ask_vol
    if total_v <= 0:
        log.warning(f"analyze_orderbook({token_id[:20]}): orderbook vide, imbalance neutralisé à 0.5")
    imbalance = bid_vol / total_v if total_v > 0 else 0.5

    vwap_bid = sum(p * s for p, s in bids) / bid_vol if bid_vol > 0 else best_bid

    depth_2pct = (
        sum(s for p, s in bids if p >= mid * 0.98)
        + sum(s for p, s in asks if p <= mid * 1.02)
    )

    return {
        "best_bid":   best_bid,
        "best_ask":   best_ask,
        "mid":        mid,
        "spread":     spread,
        "spread_bps": spread / mid * 10_000 if mid > 0 else 9999,
        "imbalance":  imbalance,
        "vwap_bid":   vwap_bid,
        "depth_2pct": depth_2pct,
        "status":     "ok",
    }


def optimal_limit_price(ob: dict, side: str = "BUY",
                         urgency: float = 0.5) -> float:
    """
    urgency ∈ [0,1] :
      0.0 = patient (bid, attendre rebate maker)
      0.5 = neutre  (mid + offset)
      1.0 = urgent  (ask, fill immédiat)

    Ajustement imbalance (Kyle 1985 microstructure) :
      Forte pression acheteuse → poster plus haut pour priorité.
    """
    bid, ask = ob["best_bid"], ob["best_ask"]
    spread   = ob["spread"]
    imb_adj  = (ob["imbalance"] - 0.5) * spread * 0.3

    price = bid + urgency * spread + imb_adj

    if side == "BUY":
        price = min(price, ask - 0.001)
        price = max(price, bid)

    return round(price, 4)


# ═══════════════════════════════════════════════════════════════════════════
# 3. ALMGREN-CHRISS EXECUTOR
# ═══════════════════════════════════════════════════════════════════════════

class AlmgrenChrissExecutor:
    """
    Exécution optimale Almgren-Chriss (2000).

    Pipeline :
    1. Trajectoire AC optimale (cosh-shaped ou TWAP si κ→0)
    2. Pour chaque slice :
       a. Analyser orderbook (imbalance, depth, spread)
       b. Calculer prix optimal (Kyle microstructure)
       c. Placer limit order (maker rebate)
       d. Monitor avec repricing intelligent (max N fois)
    3. Retourner E(IS) espéré vs réalisé
    """

    def __init__(self, clob, params: ExecutionParams):
        self.clob = clob
        self.p    = params

    # ── 1. TRAJECTOIRE ────────────────────────────────────────────────────

    def optimal_trajectory(self, X: float) -> np.ndarray:
        """
        x*(t) = X × sinh[κ(T-t)] / sinh[κT]
        Retourne les tailles des N slices.
        """
        kappa = np.sqrt(max(0, self.p.lam * self.p.sigma ** 2 / max(self.p.eta, 1e-6)))
        kappa = max(kappa, 1e-6)
        times = np.linspace(0, self.p.T, self.p.N + 1)

        if kappa * self.p.T < 1e-6:
            inventory = X * (1 - times / self.p.T)  # TWAP pur
        else:
            inventory = X * np.sinh(kappa * (self.p.T - times)) / np.sinh(kappa * self.p.T)

        slices = np.diff(-inventory)
        # Corriger les artefacts numériques
        slices = np.maximum(slices, 0)
        if slices.sum() > 0:
            slices = slices / slices.sum() * X
        return slices

    def implementation_shortfall(self, X: float) -> tuple[float, float]:
        """
        E(IS) = ε×X + (η - 0.5×γ×τ) × Σn²_k / τ
        V(IS) = σ² × τ × Σx²_k
        """
        kappa  = np.sqrt(max(0, self.p.lam * self.p.sigma ** 2 / max(self.p.eta, 1e-6)))
        kappa  = max(kappa, 1e-6)
        tau    = self.p.T / max(self.p.N, 1)
        slices = self.optimal_trajectory(X)

        epsilon = 0.001  # half bid-ask spread estimé
        e_is = (
            epsilon * X
            + (self.p.eta - 0.5 * self.p.gamma * tau)
            * np.sum(slices ** 2) / max(tau, 1e-6)
        )

        times   = np.linspace(0, self.p.T, self.p.N + 1)
        denom   = np.sinh(kappa * self.p.T) + 1e-10
        inventory = X * np.sinh(kappa * (self.p.T - times)) / denom
        v_is    = self.p.sigma ** 2 * tau * np.sum(inventory ** 2)

        return float(e_is), float(v_is)

    # ── 2. EXÉCUTION PRINCIPALE ───────────────────────────────────────────

    async def execute(
        self,
        token_id: str,
        total_size: float,
        max_price: float,
        strategy_id: str,
        urgency: float = 0.4,
    ) -> dict:
        """
        Exécution complète d'un ordre.

        Args:
            token_id   : token_id YES du marché
            total_size : montant à acheter (EUR / USDC)
            max_price  : prix maximum acceptable (p_model - EDGE_MIN)
            strategy_id: "S1" ou "S2" (pour le log)
            urgency    : 0=patient, 1=urgent

        Returns:
            dict avec fill_rate, avg_fill_price, realized_is, ...
        """
        if DRY_RUN:
            return await self._dry_run_execute(token_id, total_size, max_price, urgency)

        start_time = time.time()
        slices     = self.optimal_trajectory(total_size)
        e_is, _    = self.implementation_shortfall(total_size)

        fills, total_filled, total_cost, reprice_count = [], 0.0, 0.0, 0

        for i, slice_size in enumerate(slices):
            if slice_size < 0.005:
                continue

            remaining     = slice_size
            slice_start   = time.time()
            slice_timeout = self.p.T / self.p.N * 2.5

            ob = None
            for _retry in range(3):
                ob = analyze_orderbook(self.clob, token_id)
                if ob["status"] == "ok":
                    break
                await asyncio.sleep(1)
            if ob is None or ob["status"] != "ok":
                log.debug(f"Slice {i}: orderbook illiquide après 3 tentatives, skip")
                continue
            if ob["spread_bps"] > 300:
                log.debug(f"Slice {i}: spread trop large ({ob['spread_bps']:.0f}bps), skip")
                continue

            limit_price = optimal_limit_price(ob, "BUY", urgency)
            if limit_price > max_price:
                log.info(f"Slice {i}: limit_price {limit_price:.4f} > max {max_price:.4f}, stop")
                break

            order = self._place_limit(token_id, limit_price, remaining)
            if not order:
                continue

            order_id     = order.id
            last_mid     = ob["mid"]
            slice_reprice = 0

            while remaining > 0.005:
                await asyncio.sleep(3)

                if time.time() - slice_start > slice_timeout:
                    self._cancel(order_id)
                    break

                try:
                    status = self.clob.get_order(order_id)
                except Exception:
                    break

                if getattr(status, "status", "") == "FILLED":
                    fp = float(getattr(status, "avg_price", limit_price))
                    fs = float(getattr(status, "size_matched", slice_size))
                    fills.append((fp, fs))
                    total_filled += fs
                    total_cost   += fp * fs
                    remaining     = 0
                    break

                # Fill partiel
                fs = float(getattr(status, "size_matched", 0) or 0)
                if fs > 0 and fs < remaining:
                    fp = float(getattr(status, "avg_price", limit_price))
                    fills.append((fp, fs))
                    total_filled += fs
                    total_cost   += fp * fs
                    remaining    = slice_size - fs

                # Repricing si drift > 0.5¢
                new_ob = analyze_orderbook(self.clob, token_id)
                if new_ob["status"] != "ok":
                    continue

                drift = abs(new_ob["mid"] - last_mid)
                if drift > 0.005 and slice_reprice < self.p.max_reprice:
                    new_price = optimal_limit_price(new_ob, "BUY", urgency)
                    if new_price > max_price:
                        self._cancel(order_id)
                        remaining = 0
                        break
                    self._cancel(order_id)
                    order = self._place_limit(token_id, new_price, remaining)
                    if order:
                        order_id      = order.id
                        last_mid      = new_ob["mid"]
                        slice_reprice += 1
                        reprice_count += 1
                        limit_price   = new_price

            interval = self.p.T / max(self.p.N, 1)
            await asyncio.sleep(max(0, interval - (time.time() - slice_start)))

        elapsed   = time.time() - start_time
        avg_price = total_cost / total_filled if total_filled > 0 else 0.0
        fill_rate = total_filled / total_size if total_size > 0 else 0.0

        ob_final = analyze_orderbook(self.clob, token_id)
        arrival  = ob_final.get("mid", avg_price) if ob_final["status"] == "ok" else avg_price
        real_is  = arrival - avg_price if total_filled > 0 else 0.0

        result = {
            "status":          "ok" if fill_rate > FILL_RATE_THRESHOLD else "partial" if fill_rate > 0 else "failed",
            "fill_rate":       round(fill_rate, 4),
            "avg_fill_price":  round(avg_price, 4),
            "total_filled":    round(total_filled, 4),
            "total_cost":      round(total_cost, 4),
            "realized_is":     round(real_is, 6),
            "expected_is":     round(e_is, 6),
            "reprice_count":   reprice_count,
            "elapsed":         round(elapsed, 1),
            "n_fills":         len(fills),
        }
        log.info(
            f"Execute {strategy_id}: fill={fill_rate:.0%} "
            f"avg={avg_price:.4f} IS={real_is:+.4f} reprice={reprice_count}"
        )
        return result

    async def _dry_run_execute(
        self, token_id: str, total_size: float, max_price: float, urgency: float = 0.4
    ) -> dict:
        """
        Simulation de l'exécution (pas d'ordres réels).
        Slippage proportionnel à l'urgence : 0.002 (patient) → 0.008 (urgent).
        """
        slippage   = 0.002 + urgency * 0.006   # 0.2¢ à 0.8¢ selon urgence
        avg_price  = max(0.01, max_price - slippage)
        e_is, _    = self.implementation_shortfall(total_size)
        log.info(
            f"DRY RUN: simulate buy {total_size:.2f} EUR @ max {max_price:.4f} "
            f"urgency={urgency:.1f} slippage={slippage*100:.2f}¢ avg={avg_price:.4f}"
        )
        await asyncio.sleep(0.1)   # simuler latence
        return {
            "status":         "ok",
            "fill_rate":       1.0,
            "avg_fill_price":  avg_price,
            "total_filled":    total_size,
            "total_cost":      total_size * avg_price,
            "realized_is":     avg_price - max_price,   # IS simulé
            "expected_is":     e_is,
            "reprice_count":   0,
            "elapsed":         0.1,
            "n_fills":         self.p.N,
            "dry_run":         True,
        }

    def _place_limit(self, token_id: str, price: float, size: float,
                     _max_retries: int = 3) -> dict | None:
        """
        Place un limit order BUY via py-clob-client.
        Retry avec backoff exponentiel sur erreurs réseau transitoires (429, 5xx).
        Erreurs métier (400, 403) ne sont pas retentées.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        delay = 1.0  # secondes, double à chaque retry

        for attempt in range(1, _max_retries + 1):
            try:
                signed = self.clob.create_order(args)
                result = self.clob.post_order(signed, OrderType.GTC)
                if attempt > 1:
                    log.info(f"_place_limit: succès à la tentative {attempt}")
                return result
            except Exception as e:
                err_str = str(e).lower()
                # Erreurs non-retriables : 400 Bad Request, 401 Unauthorized, 403 Forbidden
                if any(code in err_str for code in ("400", "401", "403", "invalid", "insufficient")):
                    log.error(f"_place_limit({price:.4f}, {size:.2f}) erreur métier (non-retriable): {e}")
                    return None
                if attempt < _max_retries:
                    log.warning(
                        f"_place_limit tentative {attempt}/{_max_retries} échouée: {e} "
                        f"— retry dans {delay:.0f}s"
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)  # backoff exponentiel, max 30s
                else:
                    log.error(
                        f"_place_limit({price:.4f}, {size:.2f}) échec après "
                        f"{_max_retries} tentatives: {e}"
                    )
        return None

    def _cancel(self, order_id: str):
        try:
            self.clob.cancel_order(order_id)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# 4. PARTIAL FILL HANDLER
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PartialFillState:
    order_id:             str
    token_id:             str
    market_id:            str
    fill_price:           float
    fill_rate:            float     # 0.0 → 1.0
    remaining:            float     # EUR restant à acheter
    p_model_current:      float
    days_to_resolution:   float
    edge_at_entry:        float


async def handle_partial_fill(
    state: PartialFillState,
    clob,
    executor: AlmgrenChrissExecutor,
) -> dict:
    """
    Gestion réaliste des fills partiels sur Polymarket.
    3 règles ancrées dans la réalité du marché.
    """
    ob = analyze_orderbook(clob, state.token_id)

    # ── RÈGLE 1 : Edge disparu → annuler immédiatement ────────────────────
    if ob["status"] == "ok":
        edge_current = state.p_model_current - ob["mid"]
        if edge_current < EDGE_MIN:
            try:
                clob.cancel_order(state.order_id)
            except Exception:
                pass
            return {
                "action":       "cancel",
                "reason":       "edge_mort",
                "edge_current": edge_current,
            }
    else:
        edge_current = state.edge_at_entry  # fallback

    # ── RÈGLE 2 : Marché long (>7 jours) → attendre ──────────────────────
    if state.days_to_resolution > 7:
        return {
            "action":          "wait",
            "reason":          "marche_long_patience",
            "days_remaining":  state.days_to_resolution,
        }

    # ── RÈGLE 3 : Marché court + fill > 50% → ajustement ─────────────────
    if (
        state.fill_rate > 0.50
        and ob["status"] == "ok"
        and edge_current > 0.06
    ):
        max_acceptable = state.p_model_current - EDGE_MIN
        new_price      = round(ob["best_ask"] - 0.001, 4)

        if new_price > max_acceptable:
            return {
                "action":         "wait",
                "reason":         "ajustement_annulerait_edge",
                "max_acceptable": max_acceptable,
                "current_ask":    ob["best_ask"],
            }

        try:
            clob.cancel_order(state.order_id)
        except Exception:
            pass

        new_order = executor._place_limit(
            state.token_id, new_price, state.remaining
        )
        if new_order:
            return {
                "action":         "adjusted",
                "new_price":      new_price,
                "delta_cents":    round((new_price - state.fill_price) * 100, 2),
                "cost_of_adjust": (new_price - state.fill_price) * state.remaining,
            }

    return {"action": "wait", "reason": "default"}


# ═══════════════════════════════════════════════════════════════════════════
# 5. REPRICING ENGINE
# ═══════════════════════════════════════════════════════════════════════════

def should_reprice(
    limit_price: float,
    p_model: float,
    ob: dict,
    reprice_count: int,
    days_to_resolution: float,
    elapsed_seconds: float,
    max_wait_seconds: float,
) -> tuple[str, float]:
    """
    Décide s'il faut repricer un ordre non rempli.

    Returns:
        (decision, new_price)
        decision : "reprice" | "wait" | "cancel"
        new_price : prix calculé si reprice, 0.0 sinon
    """
    if ob["status"] != "ok":
        return "wait", 0.0

    edge = p_model - ob["mid"]

    # 1. Edge mort → annuler
    if edge < EDGE_MIN:
        return "cancel", 0.0

    # 2. Timeout → annuler
    if elapsed_seconds >= max_wait_seconds:
        return "cancel", 0.0

    # 3. Trop de repricings → annuler
    max_r = 2 if days_to_resolution > 7 else 3
    if reprice_count >= max_r:
        return "cancel", 0.0

    # 4. Pression acheteuse → attendre
    if ob["imbalance"] > REPRICE_IMBALANCE_WAIT:
        return "wait", 0.0

    # 5. Direction du mid
    mid_delta = ob["mid"] - limit_price
    if mid_delta > REPRICE_MID_DELTA_MIN:   # -0.02 → attendre si mid pas trop bas
        return "wait", 0.0

    # 6. Marché illiquide → attendre
    if ob["spread_bps"] > REPRICE_SPREAD_BPS_MAX:
        return "wait", 0.0

    # 7. Calcul nouveau prix
    new_price = round(
        min(ob["mid"] + REPRICE_OFFSET, ob["best_ask"] - 0.001), 4
    )

    # 8. Reprice annulerait edge → annuler
    if p_model - new_price < EDGE_MIN:
        return "cancel", 0.0

    return "reprice", new_price


# ═══════════════════════════════════════════════════════════════════════════
# 6. SELL (clôture de position)
# ═══════════════════════════════════════════════════════════════════════════

async def close_position(
    clob,
    token_id_yes: str,
    n_shares: float,
    min_price: float = 0.01,
    dry_run: bool = True,
) -> dict:
    """
    Vend les shares YES pour clôturer une position.
    Utilise un limit order SELL au meilleur bid disponible.
    """
    if dry_run:
        log.info(f"DRY RUN: simulate sell {n_shares:.2f} shares @ min {min_price:.4f}")
        return {
            "status":          "ok",
            "fill_rate":       1.0,
            "avg_fill_price":  min_price + 0.005,
            "total_received":  n_shares * (min_price + 0.005),
            "dry_run":         True,
        }

    try:
        ob = analyze_orderbook(clob, token_id_yes)
        if ob["status"] != "ok":
            return {"status": "error", "reason": "illiquide"}

        sell_price = max(ob["best_bid"], min_price)

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL
        args   = OrderArgs(token_id=token_id_yes, price=sell_price,
                           size=n_shares, side=SELL)
        signed = clob.create_order(args)
        order  = clob.post_order(signed, OrderType.GTC)

        # Attendre le fill (max 60s)
        for _ in range(20):
            await asyncio.sleep(3)
            status = clob.get_order(order.id)
            if getattr(status, "status", "") == "FILLED":
                fp = float(getattr(status, "avg_price", sell_price))
                fs = float(getattr(status, "size_matched", n_shares))
                return {
                    "status":         "ok",
                    "fill_rate":      fs / n_shares if n_shares > 0 else 0,
                    "avg_fill_price": fp,
                    "total_received": fp * fs,
                }

        return {"status": "timeout", "fill_rate": 0}

    except Exception as e:
        log.error(f"close_position: {e}")
        return {"status": "error", "reason": str(e)}
