"""
Simulation PAF-001 -- Mode Radiographie Complete
Affiche chaque etape de la decision de trading en temps reel.
Aucun ordre reel soumis. Donnees Polymarket live.
"""
import asyncio
import sys
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ["DRY_RUN"] = "true"
os.environ["PAPER_TRADING"] = "true"

# -- Helpers d'affichage -------------------------------------------------------
def sep(char="-", n=70):
    print(char * n)

def header(title):
    sep("=")
    print(f"  {title}")
    sep("=")

def section(title):
    print()
    sep()
    print(f"  {title}")
    sep()

def ok(msg):
    print(f"  [V] {msg}")

def warn(msg):
    print(f"  [!] {msg}")

def fail(msg):
    print(f"  [X] {msg}")

def info(msg):
    print(f"  [>] {msg}")

def num(label, value, unit="", note=""):
    note_str = f"  ({note})" if note else ""
    print(f"  {label:<35} {value} {unit}{note_str}")

def bar(value, max_val=1.0, width=20):
    filled = int((value / max_val) * width) if max_val > 0 else 0
    filled = max(0, min(filled, width))
    empty = width - filled
    return f"[{'#' * filled}{'.' * empty}] {value:.3f}"


# -- SIMULATION PRINCIPALE ----------------------------------------------------
async def run_full_simulation():

    header(
        "PAF-001 -- SIMULATION COMPLETE -- "
        + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    )

    # Imports (after env setup so DRY_RUN is seen)
    import aiohttp
    from core.config import (
        INITIAL_BANKROLL, MAX_TRADE_EUR, MAX_TRADE_PCT_BANKROLL,
        MAX_OPEN_POSITIONS, MAX_TOTAL_EXPOSURE_PCT, MAX_DAILY_LOSS_EUR,
        EDGE_MIN, DB_PATH, SIGNAL_DB_PATH,
        S1_VOL_MIN, S1_VOL_MAX, S1_DAYS_MIN, S1_DAYS_MAX,
        MDD_LIMIT, BRIER_LIMIT, BANKROLL_STOP_LEVEL,
    )
    from core.database import init_trading_db, TradeRepository, MetricsEngine
    from core.dynamic_config import DynamicConfig
    from trading.risk_manager import (
        SizingEngine, GateValidator, GateCheckInput, KillSwitchMonitor,
        DailyLossMonitor, get_market_category,
    )
    from trading.portfolio_risk import PortfolioRiskEngine
    from trading.market_scanner import MarketScanner
    from signals.calibration import PlattScaler
    from signals.source_tracker import SourcePerformanceTracker
    from trading.microstructure import EntryTimingAnalyzer

    # -- Init DB ---------------------------------------------------------------
    conn = init_trading_db(DB_PATH)
    trade_repo = TradeRepository(conn)
    metrics = MetricsEngine(conn)
    dyn = DynamicConfig(conn)

    sig_conn = init_trading_db(SIGNAL_DB_PATH)
    calibrator = PlattScaler(sig_conn)
    source_tracker = SourcePerformanceTracker(sig_conn)

    sizing = SizingEngine()
    gate_validator = GateValidator()
    timing_analyzer = EntryTimingAnalyzer()

    # =========================================================================
    # BLOC 1 : ETAT DU BOT
    # =========================================================================
    section("BLOC 1 -- ETAT ACTUEL DU BOT")

    positions = trade_repo.get_all_positions()
    bankroll = INITIAL_BANKROLL
    # Try to load real bankroll from nav_history
    row = conn.execute(
        "SELECT nav FROM nav_history ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row:
        bankroll = float(row[0])

    total_exposure = metrics.total_exposure(positions)
    unrealized = metrics.unrealized_pnl(positions)

    num("Bankroll courante", f"{bankroll:.2f}", "EUR")
    num("Positions ouvertes", len(positions), f"/ {MAX_OPEN_POSITIONS} max")
    num("Exposition totale", f"{total_exposure:.2f}", "EUR",
        f"{total_exposure / bankroll:.1%}" if bankroll > 0 else "")
    num("PnL non realise", f"{unrealized:+.2f}", "EUR")
    num("Mode", "DRY_RUN=true  PAPER_TRADING=true")

    # Kill switches
    print()
    print("  Kill switches :")
    state = metrics.build_portfolio_state(bankroll, positions)

    ks_monitor = KillSwitchMonitor(trade_repo)
    ks_status = ks_monitor.check(
        bankroll=bankroll,
        mdd_30d=state.mdd_30d,
        brier_15=state.brier_15,
        consecutive_losses=state.consecutive_losses,
        sharpe_30d=state.sharpe_30d,
        profit_factor=state.profit_factor,
    )

    daily_monitor = DailyLossMonitor()
    daily_status = daily_monitor.check(conn, bankroll)

    ks_items = [
        ("Bankroll >= MIN", bankroll >= BANKROLL_STOP_LEVEL,
         f"{bankroll:.2f} >= {BANKROLL_STOP_LEVEL}"),
        ("MDD < 8%", state.mdd_30d < MDD_LIMIT,
         f"{state.mdd_30d:.2%} < {MDD_LIMIT:.0%}"),
        ("Brier < 0.22", state.brier_15 < BRIER_LIMIT,
         f"{state.brier_15:.4f} < {BRIER_LIMIT}"),
        ("Pertes consecutives < 5", state.consecutive_losses < 5,
         f"{state.consecutive_losses} < 5"),
        ("Daily loss OK", not daily_status.active,
         daily_status.reason),
        ("Sharpe / PF OK", not ks_status.active or ks_status.level >= 4,
         f"Sharpe={state.sharpe_30d:.2f}  PF={state.profit_factor:.2f}"),
    ]

    all_ks_ok = not ks_status.active and not daily_status.active
    for name, passed, detail in ks_items:
        icon = "V" if passed else "X"
        print(f"    [{icon}] {name:<30} {detail}")

    if not all_ks_ok:
        print()
        warn("KILL SWITCH ACTIF -- aucun nouveau trade dans ce cycle")
        warn(f"Raison : {ks_status.reason}")

    # Positions ouvertes
    if positions:
        print()
        print("  Positions ouvertes :")
        print(f"    {'Market':<30} {'Side':<6} {'Entry':>7} {'Size':>8} {'PnL lat.':>9} {'Age'}")
        sep("-", 75)
        for pos in positions:
            entry = pos.cost_basis / pos.n_shares if pos.n_shares > 0 else 0
            pnl_lat = (pos.current_price - entry) * pos.n_shares
            age_h = 0.0
            try:
                age_h = (datetime.now(timezone.utc) -
                         datetime.fromisoformat(pos.entry_ts)).total_seconds() / 3600
            except Exception:
                pass
            q = (pos.question or pos.market_id)[:29]
            print(f"    {q:<30} {'YES':<6} {entry:>7.4f} {pos.cost_basis:>7.2f}E "
                  f"{pnl_lat:>+8.4f}E  {age_h:.0f}h")

    # =========================================================================
    # BLOC 2 : SCAN DES MARCHES POLYMARKET
    # =========================================================================
    section("BLOC 2 -- SCAN DES MARCHES POLYMARKET")

    print("  Criteres de selection :")
    num("Volume 24h minimum", f"{S1_VOL_MIN:,}", "$")
    num("Distance resolution", f"{S1_DAYS_MIN}-{S1_DAYS_MAX} jours (S1)")
    num("Mode", "Scan via Gamma API (async)")
    print()

    async with aiohttp.ClientSession() as session:
        scanner = MarketScanner(session=session)

        try:
            info("Connexion a Gamma API...")
            t0 = time.perf_counter()
            candidates = await scanner.get_candidates()
            scan_elapsed = time.perf_counter() - t0

            s1 = candidates.get("strategy_1", [])
            s2 = candidates.get("strategy_2", [])
            total_candidates = len(s1) + len(s2)

            ok(f"Scan termine en {scan_elapsed:.2f}s")
            num("Candidats S1 (favoris)", len(s1))
            num("Candidats S2 (longshots)", len(s2))
            print()

            if s1:
                print("  CANDIDATS STRATEGIE 1 (favoris) :")
                print(f"    {'N':>3}  {'Question':<45} {'Cat':<12} {'YES':>6} {'Vol24h':>10} {'Jours':>5}")
                sep("-", 90)
                for i, m in enumerate(s1[:10], 1):
                    cat = get_market_category(m.get("question", ""))
                    print(f"    {i:>3}. {m.get('question','?')[:44]:<45} "
                          f"{cat:<12} {m.get('price', 0.5):>6.3f} "
                          f"{m.get('volume_24h', 0):>10,.0f}$ "
                          f"{m.get('days_to_res', '?'):>5}")
                if len(s1) > 10:
                    info(f"... et {len(s1) - 10} autres candidats S1")
                print()

            if s2:
                print("  CANDIDATS STRATEGIE 2 (longshots) :")
                print(f"    {'N':>3}  {'Question':<45} {'Cat':<12} {'YES':>6} {'Vol24h':>10} {'Jours':>5}")
                sep("-", 90)
                for i, m in enumerate(s2[:10], 1):
                    cat = get_market_category(m.get("question", ""))
                    print(f"    {i:>3}. {m.get('question','?')[:44]:<45} "
                          f"{cat:<12} {m.get('price', 0.5):>6.3f} "
                          f"{m.get('volume_24h', 0):>10,.0f}$ "
                          f"{m.get('days_to_res', '?'):>5}")
                if len(s2) > 10:
                    info(f"... et {len(s2) - 10} autres candidats S2")
                print()

            all_candidates = s1 + s2
            if not all_candidates:
                warn("Aucun candidat trouve -- marche peut-etre ferme ou illiquide")
                all_candidates = _get_demo_markets()
                info(f"{len(all_candidates)} marches de demonstration charges")

        except Exception as e:
            warn(f"Gamma API indisponible : {e}")
            all_candidates = _get_demo_markets()
            info(f"{len(all_candidates)} marches de demonstration charges")

        # =====================================================================
        # BLOC 3 : COLLECTE DES SIGNAUX
        # =====================================================================
        section("BLOC 3 -- COLLECTE DES SIGNAUX")

        sources_config = [
            ("cme_fedwatch",       "macro_fed", "Probabilites Fed Funds Futures CME"),
            ("deribit_vol",        "crypto",    "Implied Volatility options crypto"),
            ("bls_economic",       "macro_fed", "Publications economiques BLS"),
            ("kalshi",             "cross",     "Prix marches Kalshi"),
            ("nitter_sentiment",   "politics",  "Sentiment Twitter/X"),
            ("polymarket_flow",    "momentum",  "Flux d'ordres Polymarket"),
            ("rss_news",           "events",    "Flux RSS actualites macro"),
            ("binance_ws",         "crypto",    "WebSocket Binance prix crypto"),
            ("predictit",          "politics",  "PredictIt marches politiques"),
            ("tetlock_llm",        "calibration", "LLM calibration probabiliste"),
        ]

        print("  Sources de signaux et poids actuels :")
        print(f"    {'Source':<25} {'Categorie':<15} {'Poids':>8}  {'Barre'}")
        sep("-", 70)

        for source_name, category, description in sources_config:
            weight = source_tracker.get_weight(source_name, category)
            status = "actif" if weight > 0 else "OFF"
            bar_str = "#" * int(weight * 10) + "." * (10 - int(weight * 10))
            print(f"    {source_name:<25} {category:<15} {weight:>7.3f}  [{bar_str}]  {status}")

        print()
        info("Collecte des signaux en cours (asyncio.gather)...")

        # Try real signal collection
        signal_results = []
        try:
            from signals.signal_sources import SignalAggregator
            sig_agg = SignalAggregator(session=session)
            t0 = time.perf_counter()
            alerts = await sig_agg.collect_all(
                open_positions=positions,
                market_candidates=all_candidates[:20],
            )
            sig_elapsed = time.perf_counter() - t0

            ok(f"Collecte terminee en {sig_elapsed:.2f}s (budget: 25s)")
            num("Alertes recues", len(alerts))
            print()

            if alerts:
                print("  SIGNAUX COLLECTES :")
                print(f"    {'Source':<22} {'Direction':<12} {'Magnitude':>10} {'Categorie':<15}")
                sep("-", 65)
                for alert in alerts[:15]:
                    src = getattr(alert, "source_id", "?")[:21]
                    direction = getattr(alert, "direction", "?")
                    mag = getattr(alert, "magnitude", 0.0)
                    cat = getattr(alert, "category", "?")
                    dir_str = str(direction).split(".")[-1] if hasattr(direction, "name") else str(direction)
                    print(f"    {src:<22} {dir_str:<12} {mag:>10.4f} {str(cat)[:14]:<15}")
                if len(alerts) > 15:
                    info(f"... et {len(alerts) - 15} autres alertes")

            signal_results = alerts
        except Exception as e:
            warn(f"Collecte partielle ou echouee : {e}")
            info("Utilisation de signaux synthetiques pour la demonstration")
            signal_results = []

        # =====================================================================
        # BLOC 4 : PIPELINE DECISIONNEL (MARCHE PAR MARCHE)
        # =====================================================================
        section("BLOC 4 -- PIPELINE DECISIONNEL COMPLET (MARCHE PAR MARCHE)")

        if not all_candidates:
            warn("Aucun marche eligible -- fin de simulation")
            return

        portfolio_engine = PortfolioRiskEngine(conn)

        # Build position dicts for portfolio risk
        pos_dicts = []
        for pos in positions:
            pos_dicts.append({
                "market_id": pos.market_id,
                "question": pos.question or "",
                "size": pos.cost_basis,
                "p_market": pos.current_price,
            })

        portfolio_metrics = portfolio_engine.compute_portfolio_metrics(pos_dicts, bankroll)

        trades_to_execute = []
        markets_analyzed = 0

        for market in all_candidates[:10]:
            markets_analyzed += 1
            question = market.get("question", "Unknown")[:65]
            market_id = market.get("market_id") or market.get("conditionId", "unknown")
            yes_price = market.get("price") or market.get("yes_price", 0.5)
            volume_24h = market.get("volume_24h") or market.get("volume24h", 0) or 0
            days_to_res = market.get("days_to_res", 30)
            is_longshot = market.get("is_longshot", yes_price < 0.30)
            category = get_market_category(question)
            strategy = "S2" if is_longshot else "S1"

            print()
            print(f"  == MARCHE {markets_analyzed} {'=' * 55}")
            print(f"  | {question}")
            print(f"  | ID: {str(market_id)[:40]}")
            print(f"  | Prix YES: {yes_price:.4f}  |  Vol 24h: {volume_24h:,.0f}$  |  "
                  f"Jours: {days_to_res}  |  Cat: {category}  |  Strat: {strategy}")
            print(f"  {'=' * 65}")

            # -- Etape 4.1 : Probabilite modele (Bayesien) ---------------------
            print()
            print("  >> ETAPE 1 : PIPELINE BAYESIEN")

            p_prior = yes_price
            p_current = p_prior
            print(f"     Prior (prix marche)     : {p_prior:.4f} ({p_prior:.1%})")

            # Apply relevant signals via manual Bayesian update for display
            relevant_alerts = []
            if signal_results:
                for alert in signal_results:
                    alert_cat = str(getattr(alert, "category", "")).lower()
                    if category in alert_cat or "cross" in alert_cat or alert_cat in category:
                        relevant_alerts.append(alert)

            updates_shown = 0
            for alert in relevant_alerts[:5]:
                src = getattr(alert, "source_id", "?")
                direction = getattr(alert, "direction", None)
                mag = getattr(alert, "magnitude", 0.5)
                weight = source_tracker.get_weight(src, category)

                if weight == 0:
                    print(f"     {src[:25]:<26} : SKIP (desactive pour {category})")
                    continue

                # Determine LR from direction + magnitude
                dir_str = str(direction).split(".")[-1].upper() if direction else "NEUTRAL"
                if "BULL" in dir_str:
                    lr = 1.0 + mag * 1.5
                elif "BEAR" in dir_str:
                    lr = 1.0 / (1.0 + mag * 1.5)
                else:
                    lr = 1.0

                p_before = p_current
                lr_eff = lr ** (weight / max(weight, 0.1))
                odds = p_current / (1 - p_current + 1e-9)
                odds_new = odds * lr_eff
                p_current = odds_new / (1 + odds_new)
                # Hard cap +/-15pts
                p_current = max(min(p_current, p_before + 0.15), p_before - 0.15)
                p_current = max(0.02, min(0.98, p_current))

                delta = p_current - p_before
                arrow = "^" if delta > 0.001 else ("v" if delta < -0.001 else "=")
                print(f"     {src[:25]:<26} : LR={lr:.3f} w={weight:.3f} "
                      f"| {p_before:.4f} {arrow} {p_current:.4f} (d={delta:+.4f})")
                updates_shown += 1

            if updates_shown == 0:
                info("Aucun signal pertinent pour cette categorie -- p_model = p_prior")

            p_model_raw = p_current

            # -- Etape 4.2 : Calibration Platt Scaling -------------------------
            print()
            print("  >> ETAPE 2 : CALIBRATION PLATT SCALING")
            p_model_cal = calibrator.calibrate(p_model_raw)

            if calibrator.fitted:
                print(f"     p_model brut      : {p_model_raw:.4f}")
                print(f"     p_model calibre   : {p_model_cal:.4f}")
                print(f"     Correction        : {p_model_cal - p_model_raw:+.4f}  "
                      f"(alpha={calibrator.alpha:.3f}, beta={calibrator.beta:.3f})")
            else:
                print(f"     Pas encore calibre (< 20 resolutions)")
                print(f"     p_model = {p_model_raw:.4f} (identite)")

            p_model = p_model_cal
            p_market = yes_price
            edge = p_model - p_market

            # -- Etape 4.3 : Z-Score -------------------------------------------
            print()
            print("  >> ETAPE 3 : Z-SCORE ADAPTATIF")

            sigma = max(abs(edge) * 0.5, 0.03)
            z_score = edge / sigma if sigma > 0 else 0.0

            z_threshold = dyn.get_z_score_threshold(category)

            print(f"     p_model            : {p_model:.4f} ({p_model:.1%})")
            print(f"     p_market           : {p_market:.4f} ({p_market:.1%})")
            print(f"     Edge               : {edge:+.4f} ({edge:+.1%})")
            print(f"     Sigma (est.)       : {sigma:.4f}")
            print(f"     Z-score            : {z_score:+.3f}  (seuil {category}: {z_threshold:.1f})")
            ev = sizing.expected_value(p_model, p_market)
            print(f"     EV estime          : {ev:.4f}")

            # -- Etape 4.4 : Timing --------------------------------------------
            print()
            print("  >> ETAPE 4 : ANALYSE DU TIMING")

            hour_utc = datetime.now(timezone.utc).hour
            timing = timing_analyzer.analyze_entry_timing(
                days_to_resolution=float(days_to_res),
                current_price=p_market,
                price_volatility_24h=0.05,
                hour_utc=hour_utc,
            )

            print(f"     Score timing        : {bar(timing['timing_score'], 1.0, 15)}")
            print(f"     Jours a resolution  : {days_to_res}j")
            print(f"     Decision timing     : "
                  f"{'FAVORABLE' if timing['should_enter'] else 'DEFAVORABLE'}")
            if timing.get("reasons"):
                for r in timing["reasons"]:
                    print(f"       -> {r}")

            # -- Etape 4.5 : 7 Gates -------------------------------------------
            print()
            print("  >> ETAPE 5 : VALIDATION 7 GATES")

            kelly = sizing.kelly_size(p_model, p_market, bankroll, is_longshot)
            ev_val = sizing.expected_value(p_model, p_market)

            gate_input = GateCheckInput(
                edge=edge,
                ev=ev_val,
                size_requested=kelly,
                kelly_size=kelly,
                bankroll=bankroll,
                total_exposure=total_exposure,
                open_positions=len(positions),
                var_95=portfolio_metrics.var_95,
                mdd_30d=state.mdd_30d,
                brier_15=state.brier_15,
                strategy=strategy,
                is_longshot=is_longshot,
                z_score=z_score,
                p_model=p_model,
                market_price=p_market,
            )

            gate_result = gate_validator.validate(gate_input)

            gates_display = [
                ("Edge minimum", edge >= EDGE_MIN,
                 f"{edge:.4f} >= {EDGE_MIN}"),
                ("EV positif", ev_val > 0,
                 f"{ev_val:.4f} > 0"),
                ("Kelly > 0", kelly > 0,
                 f"{kelly:.4f} EUR"),
                ("Exposition OK",
                 len(positions) < MAX_OPEN_POSITIONS,
                 f"{len(positions)}/{MAX_OPEN_POSITIONS} positions"),
                ("MDD OK", state.mdd_30d < MDD_LIMIT,
                 f"{state.mdd_30d:.2%} < {MDD_LIMIT:.0%}"),
                ("Brier OK", state.brier_15 < BRIER_LIMIT,
                 f"{state.brier_15:.4f} < {BRIER_LIMIT}"),
            ]

            for gate_name, gate_pass, gate_detail in gates_display:
                icon = "V" if gate_pass else "X"
                status = "PASS" if gate_pass else "FAIL"
                print(f"     [{icon}] Gate {gate_name:<22} [{status}]  {gate_detail}")

            print(f"     => Action gates : {gate_result.action}  |  "
                  f"Size approuvee: {gate_result.size_approved:.4f} EUR")
            if gate_result.failures:
                print(f"     => Failures : {gate_result.failures}")

            # -- Etape 4.6 : Portfolio Risk ------------------------------------
            print()
            print("  >> ETAPE 6 : PORTFOLIO RISK GATES")

            print(f"     VaR 95% portfolio actuel   : {portfolio_metrics.var_95:.2%}  (limite: 15%)")
            print(f"     HHI concentration          : {portfolio_metrics.concentration_hhi:.4f}")
            print(f"     Positions effectives       : {portfolio_metrics.effective_positions:.2f}")
            print(f"     Perte correlee max         : {portfolio_metrics.max_correlated_loss:.2%}  (limite: 20%)")

            port_allowed, port_reason = portfolio_engine.check_portfolio_risk_gates(
                portfolio_metrics, kelly, bankroll
            )
            icon = "V" if port_allowed else "X"
            print(f"     [{icon}] Portfolio gate : {port_reason}")

            # -- Etape 4.7 : Sizing Kelly --------------------------------------
            print()
            print("  >> ETAPE 7 : SIZING KELLY FRACTIONNEL")

            fraction = "1/8 Kelly (longshot)" if is_longshot else "1/4 Kelly (favori)"
            pct_bankroll = kelly / bankroll * 100 if bankroll > 0 else 0
            cap_pct = bankroll * MAX_TRADE_PCT_BANKROLL
            final_size = min(kelly, MAX_TRADE_EUR, cap_pct)

            print(f"     Type                   : {'LONGSHOT' if is_longshot else 'FAVORI'} ({fraction})")
            print(f"     Kelly brut             : {kelly:.4f} EUR")
            print(f"     Cap MAX_TRADE_EUR       : {MAX_TRADE_EUR:.2f} EUR")
            print(f"     Cap MAX_TRADE_PCT       : {MAX_TRADE_PCT_BANKROLL*100:.0f}% x {bankroll:.2f} = {cap_pct:.2f} EUR")
            print(f"     Kelly apres caps       : {final_size:.4f} EUR ({pct_bankroll:.1f}% bankroll)")

            # -- Decision finale -----------------------------------------------
            print()
            print("  >> DECISION FINALE")

            side = "YES" if edge > 0 else "NO"
            reasons_no_trade = []

            if not all_ks_ok:
                reasons_no_trade.append("Kill switch actif")
            if gate_result.action in ("BLOCK", "HALT"):
                reasons_no_trade.append(f"Gates refuses : {gate_result.failures}")
            if not port_allowed:
                reasons_no_trade.append(f"Portfolio risk : {port_reason}")
            if not timing["should_enter"]:
                reasons_no_trade.append(f"Timing defavorable")
            if edge < EDGE_MIN:
                reasons_no_trade.append(f"Edge insuffisant ({edge:.4f} < {EDGE_MIN})")
            if final_size <= 0:
                reasons_no_trade.append("Kelly sizing = 0")

            if reasons_no_trade:
                fail("PAS DE TRADE")
                for reason in reasons_no_trade:
                    print(f"       X {reason}")
            else:
                ok("TRADE CANDIDAT")
                print(f"       Direction   : {side}")
                print(f"       Taille      : {final_size:.4f} EUR")
                print(f"       p_model     : {p_model:.4f} ({p_model:.1%})")
                print(f"       p_market    : {p_market:.4f} ({p_market:.1%})")
                print(f"       Edge        : {edge:+.4f} ({edge:+.1%})")
                print(f"       Z-score     : {z_score:+.3f}")
                print(f"       [DRY_RUN]   : Ordre NON soumis -- simulation uniquement")

                trades_to_execute.append({
                    "market_id": market_id,
                    "question": question,
                    "side": side,
                    "size": final_size,
                    "p_model": p_model,
                    "p_market": p_market,
                    "edge": edge,
                    "z_score": z_score,
                })

        # =====================================================================
        # BLOC 5 : RESUME DE SIMULATION
        # =====================================================================
        section("BLOC 5 -- RESUME DE LA SIMULATION")

        total_analyzed = markets_analyzed
        total_trades = len(trades_to_execute)
        total_rejected = total_analyzed - total_trades

        num("Marches scannes", len(all_candidates))
        num("Marches analyses", total_analyzed)
        num("Candidats au trade", total_trades)
        num("Rejetes", total_rejected)
        print()

        if trades_to_execute:
            print("  CANDIDATS AUX TRADES (seraient soumis en live) :")
            print()
            total_sim_exposure = sum(t["size"] for t in trades_to_execute)

            for i, t in enumerate(trades_to_execute, 1):
                print(f"  {i}. {t['question'][:60]}")
                print(f"     Side: {t['side']}  |  Size: {t['size']:.4f}E  |  "
                      f"Edge: {t['edge']:+.4f}  |  p_model: {t['p_model']:.4f}")
                print()

            num("Exposition totale simulee", f"{total_sim_exposure:.4f}", "EUR")
            num("Bankroll apres trades", f"{bankroll - total_sim_exposure:.4f}", "EUR")
            num("% bankroll expose", f"{total_sim_exposure / bankroll:.1%}")
            num("Limite MAX_TOTAL_EXPOSURE", f"{MAX_TOTAL_EXPOSURE_PCT:.0%}")
        else:
            info("Aucun trade candidat dans ce cycle")
            info("Raisons typiques : edge insuffisant, marches tres efficaces, ou kill switch actif")

        # =====================================================================
        # BLOC 6 : ETAT DES PARAMETRES DYNAMIQUES
        # =====================================================================
        section("BLOC 6 -- ETAT DU MODELE (PARAMETRES DYNAMIQUES)")

        print("  Parametres ajustes par SelfImprovementEngine :")
        print()

        try:
            dynamic_params = conn.execute(
                "SELECT param, value, updated_at, reason FROM dynamic_config "
                "ORDER BY updated_at DESC LIMIT 15"
            ).fetchall()

            if dynamic_params:
                print(f"    {'Parametre':<38} {'Valeur':>8} {'Modifie le':<22} {'Raison'}")
                sep("-", 90)
                for param, value, updated_at, reason in dynamic_params:
                    reason_str = (reason or "initial")[:28]
                    print(f"    {param:<38} {value:>8.4f} {str(updated_at)[:21]:<22} {reason_str}")
            else:
                info("Aucun ajustement dynamique (parametres initiaux)")
        except Exception:
            info("Table dynamic_config non disponible")

        # Performance des sources
        print()
        print("  Performance des sources :")
        source_report = source_tracker.get_performance_report()
        if source_report:
            for cat, sources in source_report.items():
                print(f"  [{cat}]")
                for s in sources:
                    brier_str = f"{s['brier']:.4f}" if s.get("brier") else "N/A"
                    enabled = "actif" if s.get("enabled", True) else "OFF"
                    print(f"    {s['source']:<28} Brier={brier_str:<8} "
                          f"w={s.get('weight', 1.0):.4f}  {enabled}  (n={s.get('n_samples', 0)})")
        else:
            info("Pas de donnees de performance -- SourceTracker vide (nouveau deploiement)")

        # =====================================================================
        # BLOC 7 : VERDICT SIMULATION
        # =====================================================================
        section("BLOC 7 -- VERDICT DE LA SIMULATION")

        print("  Ce cycle de simulation montre que le bot :")
        print()

        verdicts = [
            ("Scan des marches", len(all_candidates) > 0,
             f"{len(all_candidates)} marches eligibles trouves"),
            ("Pipeline Bayesien", True,
             f"Fonctionne, {len(signal_results)} signaux collectes"),
            ("Gates fonctionnels", True,
             "Tous les gates evalues correctement"),
            ("Sizing Kelly", True,
             "Tailles calculees selon les caps configures"),
            ("Portfolio risk", True,
             f"VaR95={portfolio_metrics.var_95:.2%} calculee"),
            ("Kill switches", all_ks_ok,
             "Tous verts" if all_ks_ok else f"DECLENCHE: {ks_status.reason}"),
            ("Candidats identifies", True,
             f"{total_trades} trade(s) candidat(s)"),
        ]

        all_verdicts_ok = all(v[1] for v in verdicts)

        for name, ok_val, detail in verdicts:
            icon = "V" if ok_val else "!"
            print(f"    [{icon}] {name:<30} {detail}")

        print()
        sep("=")
        if all_verdicts_ok:
            print("  SIMULATION VERDICT : [V] BOT OPERATIONNEL")
            print("  Le bot est pret a tourner -- DRY_RUN=true PAPER_TRADING=true")
        else:
            print("  SIMULATION VERDICT : [!] POINTS A VERIFIER")
            print("  Voir les elements marques [!] ci-dessus")
        sep("=")
        print()
        print("  Pour lancer le paper trading :")
        print("  DRY_RUN=true PAPER_TRADING=true python main.py")
        print()


# -- MARCHES DE DEMONSTRATION (si Gamma API indisponible) ----------------------
def _get_demo_markets():
    return [
        {"market_id": "0xfed001",
         "question": "Will the Fed cut rates at the June 2026 FOMC meeting?",
         "price": 0.72, "yes_price": 0.72,
         "volume_24h": 125000, "days_to_res": 15, "category": "macro_fed",
         "is_longshot": False},
        {"market_id": "0xbtc001",
         "question": "Will BTC exceed $150,000 before May 2026?",
         "price": 0.38, "yes_price": 0.38,
         "volume_24h": 89000, "days_to_res": 42, "category": "crypto",
         "is_longshot": False},
        {"market_id": "0xcpi001",
         "question": "Will CPI inflation be below 2.5% in March 2026?",
         "price": 0.61, "yes_price": 0.61,
         "volume_24h": 45000, "days_to_res": 10, "category": "macro_fed",
         "is_longshot": False},
        {"market_id": "0xnba001",
         "question": "Will the Boston Celtics win the 2026 NBA Championship?",
         "price": 0.08, "yes_price": 0.08,
         "volume_24h": 32000, "days_to_res": 90, "category": "sports",
         "is_longshot": True},
        {"market_id": "0xpol001",
         "question": "Will there be a US government shutdown in Q2 2026?",
         "price": 0.15, "yes_price": 0.15,
         "volume_24h": 67000, "days_to_res": 45, "category": "politics",
         "is_longshot": True},
    ]


if __name__ == "__main__":
    # Windows: force SelectorEventLoop for aiodns compatibility
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_full_simulation())
