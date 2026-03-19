"""
main.py  ·  Polymarket Trading Bot — PAF-001
──────────────────────────────────────────────
Orchestrateur principal du bot.

Architecture des boucles asynchrones :
  main_loop()          → toutes les 10min : cycle signaux complet
  position_monitor()   → toutes les 2min  : vérif exits + prix
  market_scan_loop()   → toutes les 60min : scanner nouveaux candidats
  telegram_heartbeat() → toutes les 30min : rapport santé (si Telegram configuré)

Flow complet par cycle :
  1. Vérifier kill switches
  2. Collecter signaux (CME, Deribit, RSS, Nitter, BLS...)
  3. Router signaux via CrucixRouter → events p_model mis à jour
  4. Pour chaque event ADD_CONSIDER / signal fort :
     → Valider 7 gates
     → Exécuter via AlmgrenChriss (si gates passés)
  5. Vérifier exits des positions ouvertes
  6. Logger tout en SQLite
  7. Alertes Telegram si ΔP ≥ 3.5pts
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Logging ────────────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s  %(levelname)-7s  %(name)-20s  %(message)s"
LOG_DATE   = "%Y-%m-%d %H:%M:%S"

Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level    = logging.INFO,
    format   = LOG_FORMAT,
    datefmt  = LOG_DATE,
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("main")

# ── Imports internes ───────────────────────────────────────────────────────
from core.config import (
    DRY_RUN, POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE,
    POLY_PRIVATE_KEY, POLY_WALLET_ADDRESS,
    DB_PATH, SIGNAL_DB_PATH, INITIAL_BANKROLL,
    POLL_SIGNAL_CYCLE, POLL_POSITION_CHECK, POLL_MARKET_SCAN,
    TELEGRAM_ENABLED, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    TELEGRAM_ALERT_DELTA_MIN, EDGE_MIN, Z_SCORE_MIN,
)
from core.database import init_trading_db, TradeRepository, MetricsEngine, TradeRecord, OpenPosition
from trading.market_scanner import MarketScanner
from signals.signal_sources import SignalAggregator
from signals.prob_model import (
    ProbabilisticScorer, HistoricalDB, ScoringContext, route_to_model
)
from trading.execution import (
    AlmgrenChrissExecutor, select_profile, close_position, ExecutionParams
)
from trading.risk_manager import RiskManager
from signals.crucix_router import (
    CrucixRouter, MarketContext, CrucixAlert, AlertCategory, SignalDirection
)


# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM NOTIFIER
# ═══════════════════════════════════════════════════════════════════════════

async def send_telegram(text: str):
    """Envoie un message Telegram si configuré."""
    if not TELEGRAM_ENABLED:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            await session.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                      "parse_mode": "Markdown"},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception as e:
        log.debug(f"Telegram: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# CLOB CLIENT FACTORY
# ═══════════════════════════════════════════════════════════════════════════

def build_clob_client():
    """Initialise le client CLOB Polymarket."""
    if DRY_RUN:
        log.info("DRY RUN: pas de connexion CLOB réelle")
        return None

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        creds = ApiCreds(
            api_key        = POLY_API_KEY,
            api_secret     = POLY_API_SECRET,
            api_passphrase = POLY_API_PASSPHRASE,
        )
        client = ClobClient(
            host        = "https://clob.polymarket.com",
            chain_id    = 137,
            key         = POLY_PRIVATE_KEY,
            creds       = creds,
            signature_type = 0,
            funder      = POLY_WALLET_ADDRESS,
        )
        log.info("CLOB client initialisé")
        return client
    except ImportError:
        log.error("py-clob-client non installé. pip install py-clob-client")
        return None
    except Exception as e:
        log.error(f"CLOB client init failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# POSITION CONTEXT → MarketContext pour CrucixRouter
# ═══════════════════════════════════════════════════════════════════════════

def position_to_market_context(
    pos: OpenPosition, bankroll: float
) -> MarketContext:
    return MarketContext(
        market_id     = pos.market_id,
        question      = pos.question,
        p_model       = pos.p_model,
        p_market      = pos.p_market,
        category      = pos.category,
        keywords      = pos.keywords,
        days_to_res   = int(pos.days_to_res),
        bankroll      = bankroll,
        position_size = pos.cost_basis,
        edge          = pos.edge,
        z_score       = pos.z_score,
        strategy      = pos.strategy,
        sigma_14d     = pos.sigma_14d,
        n_shares      = pos.n_shares,
    )


# ═══════════════════════════════════════════════════════════════════════════
# TRADING BOT
# ═══════════════════════════════════════════════════════════════════════════

class TradingBot:
    """
    Orchestrateur principal du bot PAF-001.
    """

    def __init__(self):
        log.info("=" * 60)
        log.info("  PAF-001 TRADING BOT — Démarrage")
        log.info(f"  Mode: {'PAPER TRADING (DRY RUN)' if DRY_RUN else '⚠  LIVE TRADING'}")
        log.info("=" * 60)

        # ── Persistance ───────────────────────────────────────────────────
        self.trading_conn  = init_trading_db(DB_PATH)
        self.trade_repo    = TradeRepository(self.trading_conn)
        self.metrics       = MetricsEngine(self.trading_conn)

        # ── Modèles ───────────────────────────────────────────────────────
        self.hist_db       = HistoricalDB(DB_PATH)
        self.scorer        = ProbabilisticScorer(self.trading_conn, self.hist_db)
        self.scanner       = MarketScanner()
        self.signals       = SignalAggregator()
        self.risk          = RiskManager(self.trade_repo, self.metrics)

        # ── Signal router ─────────────────────────────────────────────────
        self.crucix        = CrucixRouter(db_path=SIGNAL_DB_PATH)

        # ── Exécution ─────────────────────────────────────────────────────
        self.clob          = build_clob_client()
        self._executor: Optional[AlmgrenChrissExecutor] = None

        # ── État ──────────────────────────────────────────────────────────
        self.bankroll      = self._load_bankroll()
        self.candidates: dict = {"strategy_1": [], "strategy_2": []}
        self._returns_history: list[float] = self._load_returns_history()
        self._last_scan    = datetime.min.replace(tzinfo=timezone.utc)
        self._running      = False

        log.info(f"Bankroll: {self.bankroll:.2f}€")
        log.info(f"Historique: {self.hist_db.count()} marchés résolus")
        log.info(f"Positions ouvertes: {len(self.trade_repo.get_all_positions())}")

    def _load_bankroll(self) -> float:
        """Charge le bankroll depuis la DB (dernier snapshot NAV) ou utilise la config."""
        rows = self.trading_conn.execute(
            "SELECT nav FROM nav_history ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if rows:
            return float(rows[0])
        return INITIAL_BANKROLL

    def _save_bankroll(self):
        positions = self.trade_repo.get_all_positions()
        unrealized = self.metrics.unrealized_pnl(positions)
        nav_now = self.bankroll + unrealized
        last = self.trading_conn.execute(
            "SELECT nav FROM nav_history ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        daily_pnl = round(nav_now - float(last[0]), 4) if last else 0.0
        self.trade_repo.record_nav(nav=nav_now, daily_pnl=daily_pnl)

    def _load_returns_history(self) -> list[float]:
        rows = self.trading_conn.execute(
            "SELECT pnl FROM trades WHERE status='closed' AND pnl IS NOT NULL "
            "ORDER BY exit_ts DESC LIMIT 200"
        ).fetchall()
        return [r[0] for r in rows]

    def _get_executor(self, profile: ExecutionParams) -> AlmgrenChrissExecutor:
        return AlmgrenChrissExecutor(self.clob, profile)

    # ── CYCLE PRINCIPAL ────────────────────────────────────────────────────

    async def signal_cycle(self):
        """
        Cycle principal toutes les 10 minutes :
        1. Vérifier kill switches
        2. Collecter signaux
        3. Router via CrucixRouter
        4. Traiter les events → décisions de trade
        """
        log.info("─" * 50 + " SIGNAL CYCLE " + "─" * 50)

        positions = self.trade_repo.get_all_positions()

        # ── Kill switches ─────────────────────────────────────────────────
        ks = self.risk.check_kill_switches(self.bankroll, positions)
        if ks.active and not ks.allow_new_trades:
            log.warning(f"KILL SWITCH actif: {ks.reason}")
            await send_telegram(f"⚠ Kill Switch: {ks.reason}")
            return

        # ── Collecter signaux ─────────────────────────────────────────────
        alerts = self.signals.collect_all(
            open_positions  = positions,
            market_candidates = self.candidates,
        )

        if not alerts:
            log.info("Aucun signal ce cycle")
            return

        # ── Construire les MarketContext des positions ouvertes ────────────
        market_contexts = [
            position_to_market_context(pos, self.bankroll)
            for pos in positions
        ]

        # ── Router via CrucixRouter ────────────────────────────────────────
        brier = self.metrics.brier_score_last_n(15)
        mdd   = self.metrics.mdd_last_n_days(30)

        events = self.crucix.process_batch(
            alerts        = alerts,
            open_markets  = market_contexts,
            current_mdd   = mdd,
            current_brier = brier,
        )

        log.info(f"Crucix: {len(alerts)} signaux → {len(events)} événements")

        # ── Traiter les événements ─────────────────────────────────────────
        for event in events:
            await self._handle_crucix_event(event, positions)

    async def _handle_crucix_event(self, event: dict, positions: list[OpenPosition]):
        """Traite un événement CrucixRouter — met à jour p_model et évalue action."""
        action        = event.get("action", "HOLD")
        market_id     = event.get("market_id")
        p_model_new   = event.get("p_model_new")
        p_market      = event.get("p_market")
        delta_p       = event.get("delta_p", 0.0)

        if not market_id or not p_model_new:
            return

        # Alerter Telegram si Δp significatif
        if abs(delta_p) >= TELEGRAM_ALERT_DELTA_MIN:
            await send_telegram(
                f"📡 *Crucix Update*\n"
                f"Marché: {event.get('question', '')[:50]}\n"
                f"p\_model: {event.get('p_model_old', 0):.3f} → {p_model_new:.3f} "
                f"(Δ{delta_p:+.3f})\n"
                f"Action: `{action}`\n"
                f"Sources: {', '.join(event.get('sources', []))}"
            )

        # Mise à jour de la position en DB
        pos = next((p for p in positions if p.market_id == market_id), None)
        if pos:
            cur_price  = p_market if p_market is not None else pos.current_price
            new_edge   = p_model_new - cur_price
            new_z      = new_edge / max(pos.sigma_14d or 0.06, 0.03)
            self.trade_repo.update_position_price(
                market_id  = market_id,
                new_price  = cur_price,
                new_p_model= p_model_new,
                new_edge   = new_edge,
                new_z      = new_z,
                days_to_res= pos.days_to_res,
            )

        # Si l'action suggère d'ajouter ou de rentrer
        if action in ("ADD_CONSIDER",):
            log.info(f"ADD_CONSIDER pour {market_id} — nécessite confirmation 2e source")
            # Pas d'action immédiate — attendre 2e source (principe superforecaster)

        elif action in ("EXIT_FLIP", "EXIT_EDGE", "HALT"):
            if pos:
                log.warning(f"EXIT signalé pour {market_id}: {event.get('rationale', '')[:60]}")
                await self._exit_position(pos, reason=action)

    # ── MARKET SCAN + SCORING ──────────────────────────────────────────────

    async def market_scan_loop(self):
        """Scan horaire des nouveaux marchés candidats + scoring."""
        now = datetime.now(timezone.utc)
        if (now - self._last_scan).total_seconds() < POLL_MARKET_SCAN:
            return

        log.info("─" * 40 + " MARKET SCAN " + "─" * 40)
        self._last_scan = now

        try:
            self.candidates = self.scanner.get_candidates()
        except Exception as e:
            log.error(f"Market scan error: {e}")
            return

        positions     = self.trade_repo.get_all_positions()
        open_ids      = {p.market_id for p in positions}

        ks = self.risk.check_kill_switches(self.bankroll, positions)
        if ks.active and not ks.allow_new_trades:
            log.warning(f"Scan: kill switch actif, pas de nouveaux trades")
            return

        all_candidates = (
            [("S1", c) for c in self.candidates.get("strategy_1", [])]
            + [("S2", c) for c in self.candidates.get("strategy_2", [])]
        )

        for strategy, candidate in all_candidates:
            mid = candidate.get("market_id")
            if mid in open_ids:
                continue  # déjà en position

            await self._evaluate_candidate(strategy, candidate, positions)
            await asyncio.sleep(0.5)  # rate limiting

    async def _evaluate_candidate(
        self, strategy: str, candidate: dict, positions: list[OpenPosition]
    ):
        """Score un marché candidat et ouvre un trade si les conditions sont réunies."""
        question   = candidate.get("question", "")
        price      = candidate.get("price", 0.5)
        days       = candidate.get("days_to_res", 14.0)
        category   = candidate.get("category", "")
        vol24      = candidate.get("volume_24h", 0.0)
        is_longshot= candidate.get("is_longshot", False)

        log.debug(f"Évaluation: {question[:50]}... (S={strategy})")

        # Collecter signaux contextuels
        context_alerts = self.signals.collect_for_market(
            question = question,
            price    = price,
            keywords = question.lower().split()[:8],
        )

        # Scoring probabiliste
        btc_spot   = self.signals.btc_tracker.current_price
        btc_target = None
        if "btc" in question.lower() or "bitcoin" in question.lower():
            # Extraire le target depuis la question
            m = re.search(r"\$(\d[\d,]+)", question)
            if m:
                val = float(m.group(1).replace(",", ""))
                btc_target = val if 10_000 <= val <= 500_000 else None

        ctx = ScoringContext(
            question     = question,
            market_price = price,
            days_to_res  = days,
            category     = category,
            volume_24h   = vol24,
            btc_spot     = btc_spot,
            btc_target   = btc_target,
            btc_sigma    = 0.80,  # vol BTC par défaut
            p_fedwatch   = self._get_latest_fedwatch_prob(question),
            news_signals = context_alerts,
            macro_data   = self.signals.get_latest_macro_data() or None,
        )

        score = self.scorer.score(ctx)

        if not score.get("tradeable"):
            log.debug(f"Non tradeable: {score.get('reason', '')} — {question[:40]}...")
            return

        p_final     = score["p_final"]
        edge        = score["edge"]
        uncertainty = score["uncertainty"]
        sigma_14d   = self.crucix.z_engine.get_sigma(candidate["market_id"], 0.06)
        z_score     = edge / max(sigma_14d, 0.03)

        log.info(
            f"Candidat: {question[:50]}... "
            f"p_final={p_final:.3f} price={price:.3f} "
            f"edge={edge:+.3f} z={z_score:.2f} unc={uncertainty:.2f}"
        )

        # Valider les 7 gates
        gate = self.risk.validate_new_trade(
            p_model         = p_final,
            market_price    = price,
            bankroll        = self.bankroll,
            positions       = positions,
            strategy        = strategy,
            is_longshot     = is_longshot,
            z_score         = z_score,
            returns_history = self._returns_history,
        )

        if not gate.passed or gate.action != "TRADE":
            log.debug(f"Gates failed: {gate.failures}")
            return

        # Ouvrir le trade
        size = gate.size_approved
        if size < 0.10:
            log.debug(f"Size trop petite: {size:.2f}€")
            return

        await self._open_trade(
            candidate   = candidate,
            strategy    = strategy,
            p_model     = p_final,
            size_eur    = size,
            z_score     = z_score,
            gate        = gate,
            score       = score,
        )

    def _get_latest_fedwatch_prob(self, question: str) -> Optional[float]:
        """Retourne la dernière prob CME FedWatch depuis la DB des signaux."""
        if not any(kw in question.lower() for kw in ["fed", "fomc", "rate", "cut"]):
            return None
        try:
            # Lire le signal CME FedWatch le plus récent dans la DB signaux Crucix
            row = self.crucix.conn.execute(
                """SELECT p_posterior FROM signal_log
                   WHERE source_id='cme_fedwatch'
                   ORDER BY ts DESC LIMIT 1"""
            ).fetchone()
            if row:
                return float(row[0])
        except Exception as e:
            log.debug(f"_get_latest_fedwatch_prob: {e}")
        # Fallback neutre si aucun signal CME en DB
        return None

    # ── OPEN TRADE ─────────────────────────────────────────────────────────

    async def _open_trade(
        self,
        candidate: dict,
        strategy: str,
        p_model: float,
        size_eur: float,
        z_score: float,
        gate,
        score: dict,
    ):
        """Ouvre un trade via AlmgrenChriss."""
        token_id    = candidate["token_id"]
        market_id   = candidate["market_id"]
        question    = candidate["question"]
        price       = candidate["price"]
        is_longshot = candidate.get("is_longshot", False)
        days        = candidate.get("days_to_res", 14.0)
        category    = candidate.get("category", "")

        log.info(
            f"TRADE: {question[:50]}... "
            f"S={strategy} size={size_eur:.2f}€ "
            f"p_model={p_model:.3f} price={price:.3f} "
            f"edge={score['edge']:+.3f}"
        )

        # Profil d'exécution
        market_type  = "longshot" if is_longshot else "favori"
        signal_decay = 0.3  # signal de moyen terme
        profile      = select_profile(market_type, signal_decay)
        executor     = self._get_executor(profile)

        # Prix maximum = p_model - EDGE_MIN (ne jamais payer plus)
        max_price = round(p_model - EDGE_MIN, 4)

        exec_result = await executor.execute(
            token_id   = token_id,
            total_size = size_eur,
            max_price  = max_price,
            strategy_id= strategy,
            urgency    = 0.4,
        )

        if exec_result["status"] == "failed":
            log.warning(f"Exécution échouée: {market_id}")
            return

        fill_rate  = exec_result["fill_rate"]
        fill_price = exec_result["avg_fill_price"]
        filled_eur = exec_result["total_cost"]
        n_shares   = filled_eur / max(fill_price, 0.001)

        if fill_rate < 0.10:
            log.warning(f"Fill trop faible ({fill_rate:.0%}): {market_id}")
            return

        # Logger le trade
        trade = TradeRecord(
            market_id      = market_id,
            question       = question,
            strategy       = strategy,
            side           = "BUY",
            token_id       = token_id,
            size_eur       = round(filled_eur, 4),
            n_shares       = round(n_shares, 4),
            fill_price     = fill_price,
            p_model        = p_model,
            p_market       = price,
            edge           = score["edge"],
            z_score        = z_score,
            kelly_fraction = size_eur / max(self.bankroll, 1),
            category       = category,
            gates_passed   = gate.failures,   # stores non-blocking gate notes (e.g. kelly_gate_reduce)
            fill_rate      = fill_rate,
        )
        self.trade_repo.insert_trade(trade)

        # Créer position
        sigma_pos = self.crucix.z_engine.get_sigma(market_id, 0.06)
        pos = OpenPosition(
            market_id     = market_id,
            question      = question,
            token_id      = token_id,
            strategy      = strategy,
            p_model       = p_model,
            p_market      = price,
            edge          = score["edge"],
            z_score       = z_score,
            n_shares      = round(n_shares, 4),
            cost_basis    = round(filled_eur, 4),
            current_price = fill_price,
            days_to_res   = days,
            entry_ts      = datetime.now(timezone.utc).isoformat(),
            last_updated  = datetime.now(timezone.utc).isoformat(),
            category      = category,
            keywords      = question.lower().split()[:8],
            sigma_14d     = sigma_pos,
        )
        self.trade_repo.upsert_position(pos)

        # Mettre à jour bankroll (réel et DRY_RUN pour Kelly sizing cohérent)
        self.bankroll -= filled_eur

        self._returns_history.append(-filled_eur)  # outflow

        log.info(
            f"✓ TRADE OUVERT: {market_id} "
            f"fill={fill_rate:.0%} price={fill_price:.4f} "
            f"eur={filled_eur:.2f} shares={n_shares:.2f}"
        )
        await send_telegram(
            f"✅ *Nouveau Trade*\n"
            f"{question[:60]}\n"
            f"Stratégie: {strategy} | Prix: {fill_price:.3f}\n"
            f"Montant: {filled_eur:.2f}€ | Shares: {n_shares:.0f}\n"
            f"p\_model={p_model:.3f} edge={score['edge']:+.3f}"
        )

    # ── POSITION MONITOR ───────────────────────────────────────────────────

    async def position_monitor(self):
        """
        Vérifie toutes les 2 minutes :
        - Résolution des marchés
        - Règles de sortie anticipée
        - Mise à jour des prix
        """
        positions = self.trade_repo.get_all_positions()
        if not positions:
            return

        for pos in positions:
            try:
                await self._check_position(pos)
            except Exception as e:
                log.error(f"position_monitor({pos.market_id}): {e}")
            await asyncio.sleep(0.3)

    async def _check_position(self, pos: OpenPosition):
        """Vérifie l'état d'une position."""
        # Vérifier résolution
        is_resolved, outcome = self.scanner.is_market_resolved(pos.market_id)

        if is_resolved and outcome is not None:
            await self._close_position_resolved(pos, outcome)
            return

        # Mettre à jour le prix actuel
        current_price = self.scanner.get_market_price(pos.market_id)
        if current_price is None:
            return

        current_days = self.scanner.get_current_days(pos.market_id)
        days = current_days or pos.days_to_res

        self.trade_repo.update_position_price(
            market_id   = pos.market_id,
            new_price   = current_price,
            new_p_model = pos.p_model,
            new_edge    = pos.p_model - current_price,
            new_z       = (pos.p_model - current_price) / max(pos.sigma_14d, 0.03),
            days_to_res = days,
        )

        # Vérifier exit rules
        exit_signal = self.risk.check_exit(
            p_model       = pos.p_model,
            current_price = current_price,
            entry_price   = pos.p_market,
            days_to_res   = days,
            is_longshot   = pos.strategy == "S2",
            strategy      = pos.strategy,
        )

        if exit_signal.should_exit and exit_signal.urgency in ("immediate", "monitor"):
            log.info(f"EXIT: {pos.market_id} — {exit_signal.reason}")
            await self._exit_position(pos, reason=exit_signal.reason)

    async def _close_position_resolved(self, pos: OpenPosition, outcome: int):
        """Ferme une position après résolution du marché."""
        p_resolution = 1.0 if outcome == 1 else 0.0

        if outcome == 1:
            # YES résolu → les shares valent 1€ chacune
            pnl = pos.n_shares * 1.0 - pos.cost_basis
        else:
            # NO résolu → les shares valent 0€
            pnl = -pos.cost_basis

        pnl = round(pnl, 4)

        # Mettre à jour DB
        self.trade_repo.close_trade(
            market_id        = pos.market_id,
            outcome          = outcome,
            pnl              = pnl,
            p_at_resolution  = p_resolution,
            exit_reason      = "resolution",
        )
        self.trade_repo.remove_position(pos.market_id)

        # Mettre à jour bankroll (réel et DRY_RUN pour Kelly sizing cohérent)
        if outcome == 1:
            self.bankroll += pos.n_shares * 1.0
        # Si NO, la mise est perdue (déjà déduite à l'entrée)

        # Calibration Crucix
        try:
            self.crucix.resolve_market(
                market_id       = pos.market_id,
                outcome         = outcome,
                p_at_resolution = p_resolution,
            )
        except Exception as e:
            log.debug(f"crucix.resolve_market: {e}")

        self._returns_history.append(pnl)

        result_emoji = "✅" if outcome == 1 else "❌"
        log.info(
            f"{result_emoji} RÉSOLUTION: {pos.market_id} "
            f"outcome={'YES' if outcome else 'NO'} PnL={pnl:+.2f}€"
        )
        await send_telegram(
            f"{result_emoji} *Résolution*\n"
            f"{pos.question[:60]}\n"
            f"Outcome: {'YES' if outcome else 'NO'}\n"
            f"PnL: {pnl:+.2f}€ | Bankroll: {self.bankroll:.2f}€"
        )
        self._save_bankroll()

        # Ajouter à l'historique pour la prochaine calibration RCE
        self.hist_db.add_market(
            market_id     = pos.market_id,
            question      = pos.question,
            resolved_yes  = outcome,
            volume        = 0.0,
            category      = pos.category,
        )

    async def _exit_position(self, pos: OpenPosition, reason: str = "exit_rule"):
        """Clôture anticipée d'une position (vente avant résolution)."""
        log.info(f"EXIT ANTICIPÉ: {pos.market_id} reason={reason}")

        result = await close_position(
            clob         = self.clob,
            token_id_yes = pos.token_id,
            n_shares     = pos.n_shares,
            min_price    = 0.01,
            dry_run      = DRY_RUN,
        )

        if result.get("status") == "ok":
            received    = result.get("total_received", 0.0)
            pnl         = round(received - pos.cost_basis, 4)
            fill_price  = result.get("avg_fill_price", 0.0)
            p_market_now = fill_price

            # Sortie anticipée : outcome inconnu (résolution pas encore survenue).
            # On passe outcome=None pour ne pas polluer le Brier score avec un proxy PnL.
            self.trade_repo.close_trade(
                market_id       = pos.market_id,
                outcome         = None,
                pnl             = pnl,
                p_at_resolution = p_market_now,
                exit_reason     = reason,
            )
            self.trade_repo.remove_position(pos.market_id)

            self.bankroll += received

            self._returns_history.append(pnl)

            log.info(
                f"Exit: {pos.market_id} sell={received:.2f}€ "
                f"PnL={pnl:+.2f}€ reason={reason}"
            )
            await send_telegram(
                f"🔴 *Exit Anticipé*\n"
                f"{pos.question[:60]}\n"
                f"Raison: {reason}\n"
                f"PnL: {pnl:+.2f}€"
            )
            self._save_bankroll()

    # ── RAPPORT PÉRIODIQUE ─────────────────────────────────────────────────

    async def telegram_heartbeat(self):
        """Rapport de santé toutes les 30 minutes."""
        positions = self.trade_repo.get_all_positions()
        state = self.metrics.build_portfolio_state(self.bankroll, positions)

        msg = (
            f"🤖 *PAF-001 Status* — {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
            f"Bankroll: *{state.bankroll:.2f}€*\n"
            f"Positions: {state.open_positions} ouvertes\n"
            f"PnL unrealized: {state.unrealized_pnl:+.2f}€\n"
            f"PnL realized: {state.realized_pnl:+.2f}€\n"
            f"Brier 15: {state.brier_15:.3f} | MDD 30j: {state.mdd_30d:.1%}\n"
            f"Win Rate: {state.win_rate:.0%} | PF: {state.profit_factor:.2f}\n"
            f"Mode: {'🟡 PAPER' if DRY_RUN else '🟢 LIVE'}"
        )
        await send_telegram(msg)

    # ── MAIN LOOP ──────────────────────────────────────────────────────────

    async def _start_websockets(self):
        """Démarre les WebSockets en arrière-plan."""
        await self.signals.btc_tracker.start()

    async def run(self):
        """Boucle principale asynchrone."""
        self._running = True
        log.info("Bot démarré. Ctrl+C pour arrêter.")

        # Démarrer Binance WS en arrière-plan
        await self._start_websockets()

        # Rapport de démarrage
        if TELEGRAM_ENABLED:
            await send_telegram(
                f"🚀 *PAF-001 démarré*\n"
                f"Mode: {'PAPER' if DRY_RUN else 'LIVE'}\n"
                f"Bankroll: {self.bankroll:.2f}€"
            )

        # Scan initial
        await self.market_scan_loop()

        # Compteurs pour les tâches périodiques
        cycle_count    = 0
        last_heartbeat = datetime.now(timezone.utc)

        while self._running:
            try:
                cycle_start = datetime.now(timezone.utc)

                # ── 1. Signal cycle (toutes les 10 min) ───────────────────
                await self.signal_cycle()

                # ── 2. Position monitor (toutes les 2 min) ────────────────
                await self.position_monitor()

                # ── 3. Market scan (toutes les 60 min) ────────────────────
                await self.market_scan_loop()

                # ── 4. Heartbeat Telegram (toutes les 30 min) ─────────────
                if (datetime.now(timezone.utc) - last_heartbeat).total_seconds() >= 1800:
                    await self.telegram_heartbeat()
                    last_heartbeat = datetime.now(timezone.utc)

                # ── 5. Save bankroll snapshot ─────────────────────────────
                cycle_count += 1
                if cycle_count % 6 == 0:   # toutes les heures
                    self._save_bankroll()

                # Attendre jusqu'au prochain cycle (10 min)
                elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
                sleep_s = max(0, POLL_SIGNAL_CYCLE - elapsed)
                log.info(f"Cycle #{cycle_count} terminé en {elapsed:.0f}s. Pause {sleep_s:.0f}s")
                await asyncio.sleep(sleep_s)

            except asyncio.CancelledError:
                break
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"Erreur cycle principale: {e}", exc_info=True)
                await asyncio.sleep(30)  # pause avant retry

        log.info("Bot arrêté.")
        if TELEGRAM_ENABLED:
            await send_telegram("🛑 PAF-001 arrêté")


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    bot = TradingBot()
    try:
        await bot.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Interruption reçue. Arrêt propre.")
    finally:
        bot.signals.btc_tracker.stop()


if __name__ == "__main__":
    asyncio.run(main())
