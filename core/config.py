"""
config.py  ·  Polymarket Trading Bot
─────────────────────────────────────
Centralise toutes les constantes, endpoints et paramètres de configuration.
Charger via variables d'environnement (fichier .env).
"""

from __future__ import annotations
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

_cfg_log = logging.getLogger("config")

# ── Fix SSL_CERT_FILE si le fichier référencé n'existe pas ─────────────────
# Corrige le conflit avec d'autres environnements Python (conda, etc.)
_ssl_cert = os.environ.get("SSL_CERT_FILE", "")
if _ssl_cert and not Path(_ssl_cert).exists():
    try:
        import certifi as _certifi
        os.environ["SSL_CERT_FILE"] = _certifi.where()
        _cfg_log.debug(f"SSL_CERT_FILE corrigé: {_ssl_cert!r} → {_certifi.where()!r}")
    except ImportError:
        os.environ.pop("SSL_CERT_FILE", None)
        _cfg_log.warning("SSL_CERT_FILE invalide et certifi absent — variable supprimée")

# ═══════════════════════════════════════════════════════════════════════════
# 1. POLYMARKET CREDENTIALS
# ═══════════════════════════════════════════════════════════════════════════

POLY_API_KEY        = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET     = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")
def _load_private_key() -> str:
    key_file = Path.home() / ".paf" / "poly_key"
    if key_file.exists():
        return key_file.read_text().strip()
    val = os.getenv("POLY_PRIVATE_KEY", "")
    if val:
        _cfg_log.warning("POLY_PRIVATE_KEY loaded from environment variable — prefer ~/.paf/poly_key")
    return val

POLY_PRIVATE_KEY    = _load_private_key()        # clé privée wallet (hex)
POLY_WALLET_ADDRESS = os.getenv("POLY_WALLET_ADDRESS", "")

# ═══════════════════════════════════════════════════════════════════════════
# 2. API EXTERNES
# ═══════════════════════════════════════════════════════════════════════════

BLS_API_KEY         = os.getenv("BLS_API_KEY", "")             # bls.gov (gratuit)
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN", "")          # alertes Telegram
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")

# ═══════════════════════════════════════════════════════════════════════════
# 3. POLYMARKET ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

GAMMA_API       = "https://gamma-api.polymarket.com"
CLOB_API        = "https://clob.polymarket.com"
DATA_API        = "https://data-api.polymarket.com"
WS_URL          = "wss://ws-subscriptions-clob.polymarket.com"

# ═══════════════════════════════════════════════════════════════════════════
# 4. SOURCES DE DONNÉES — ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

# CME FedWatch
CME_FEDWATCH_URL = "https://www.cmegroup.com/CmeWS/mvc/GetFedWatch/ProbHistoricalData"

# Deribit API v2
DERIBIT_BASE    = "https://www.deribit.com/api/v2/public"

# Reuters / AP News RSS
REUTERS_FEEDS   = [
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/politicsNews",
]
AP_FEEDS = [
    "https://apnews.com/apf-topnews",
]

# Google News RSS
GOOGLE_NEWS_BASE = "https://news.google.com/rss/search"

# Kalshi
KALSHI_API      = "https://trading.kalshi.com/trade-api/v2/markets/"

# Binance WebSocket
BINANCE_WS      = "wss://stream.binance.com:9443/ws/btcusdt@ticker"

# BLS / Fed
BLS_API_BASE    = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
FED_PRESS_RSS   = "https://www.federalreserve.gov/feeds/press_all.xml"

# Polymarket Activity
POLY_ACTIVITY   = f"{DATA_API}/activity"
POLY_PROFILE    = f"{DATA_API}/profile"

# Nitter RSS (Twitter alternatif)
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

# Comptes Twitter haute valeur à surveiller
TWITTER_ACCOUNTS_TIER1 = [
    "federalreserve", "nick_timiraos", "elerianmohamed",
]
TWITTER_ACCOUNTS_CRYPTO = [
    "woonomic", "glassnode", "ki_young_ju", "lawmaster",
]
TWITTER_ACCOUNTS_POLY = [
    "Polymarket", "PolymarketWhales", "Domahhhh",
]

# Keywords Twitter par catégorie
TWITTER_KEYWORDS = {
    "fed": ["Federal Reserve decision", "FOMC meeting",
            "interest rate cut", "Jerome Powell statement"],
    "btc_price": ["Bitcoin ATH", "BTC resistance",
                  "Bitcoin institutional", "spot Bitcoin ETF flows"],
}

# Blacklist tweets spam
TWEET_BLACKLIST = [
    "copytrade", "copy trade", "t.me/", "made $", "profit today",
    "bot made", "ref=", "?code=", "join my", "my bot", "automated",
    "passive income", "DM me", "link in bio",
]

# ═══════════════════════════════════════════════════════════════════════════
# 5. WALLET DE RÉFÉRENCE (whale tracking)
# ═══════════════════════════════════════════════════════════════════════════

REFERENCE_WALLET = os.getenv("REFERENCE_WALLET", "")   # wallet2 à surveiller

# ═══════════════════════════════════════════════════════════════════════════
# 6. PARAMÈTRES DE TRADING
# ═══════════════════════════════════════════════════════════════════════════

try:
    INITIAL_BANKROLL    = float(os.getenv("INITIAL_BANKROLL", "100.0"))
except ValueError as _e:
    raise ValueError(f"INITIAL_BANKROLL must be a valid float: {_e}") from _e
MAX_TRADE_EUR           = 5.0               # maximum absolu par trade
MAX_TRADE_PCT_BANKROLL  = 0.05              # 5% du bankroll max
MAX_OPEN_POSITIONS      = 8
MAX_TOTAL_EXPOSURE_PCT  = 0.40              # 40% max exposition totale

ALPHA_KELLY_FAVORI      = 0.30              # 1/4 Kelly — favoris
ALPHA_KELLY_LONGSHOT    = 0.15              # 1/8 Kelly — longshots

# Seuils 7 gates
EDGE_MIN                = 0.04             # edge minimum (4 cents)
EV_MIN                  = 0.0
Z_SCORE_MIN             = 1.5
BRIER_LIMIT             = 0.22
MDD_LIMIT               = 0.08             # 8% max drawdown sur 30j
MAX_EXPOSURE_PER_POS    = 0.25             # 25% bankroll max par position
MAX_VAR_PCT             = 0.05             # VaR 95% ≤ 5% bankroll

# Kill switches
BANKROLL_STOP_LEVEL     = 60.0             # stop total si bankroll < 60€
CONSECUTIVE_LOSSES_PAUSE = 5               # pause 48h si 5 pertes consécutives
SHARPE_MIN              = 1.0              # revue si Sharpe < 1.0 sur 30j
PROFIT_FACTOR_MIN       = 1.2             # recalibration si PF < 1.2 sur 50 trades
COOLDOWN_HOURS_MDD      = 72              # cooldown après MDD gate
COOLDOWN_HOURS_LOSSES   = 48              # cooldown après 5 pertes consécutives

# Daily loss limits
MAX_DAILY_LOSS_EUR      = 15.0            # arrêt si perte > 15€ en un jour
MAX_DAILY_LOSS_PCT      = 0.15            # arrêt si perte > 15% du bankroll en un jour

# Concentration risk
MAX_POSITIONS_PER_CATEGORY = 3            # max positions dans une même catégorie

# Sortie anticipée
EXIT_EDGE_MIN           = 0.04            # sortir si edge < 4¢
EXIT_PROFIT_CAPTURE_PCT = 0.65            # sortir si 65% du potentiel capturé
EXIT_ADVERSE_MOVE_PCT   = 0.30            # sortir si marché bouge >30% contre

# Stratégie 1 filtres
S1_VOL_MIN  = 5_000
S1_VOL_MAX  = 80_000
S1_DAYS_MIN = 5
S1_DAYS_MAX = 21
S1_PRICE_FAV_MIN  = 0.70
S1_PRICE_FAV_MAX  = 0.92
S1_PRICE_LONG_MIN = 0.01
S1_PRICE_LONG_MAX = 0.10

# Stratégie 2 filtres
S2_VOL_MIN   = 10_000
S2_DAYS_MIN  = 14
S2_DAYS_MAX  = 90
S2_PRICE_MIN = 0.01
S2_PRICE_MAX = 0.08
S2_EDGE_RATIO_MIN  = 2.0
S2_EDGE_ABS_MIN    = 0.06

S2_CATEGORIES = {"crypto", "economics", "politics", "finance", "sports"}

# ═══════════════════════════════════════════════════════════════════════════
# 7. POLLING INTERVALS (secondes)
# ═══════════════════════════════════════════════════════════════════════════

POLL_SIGNAL_CYCLE   = 600    # 10 minutes — cycle principal signaux
POLL_CME_FEDWATCH   = 3600   # 1 heure
POLL_DERIBIT        = 900    # 15 minutes
POLL_RSS_NEWS       = 300    # 5 minutes
POLL_MARKET_SCAN    = 3600   # 1 heure (scan nouveaux marchés)
POLL_TWITTER        = 600    # 10 minutes
POLL_POSITION_CHECK = 120    # 2 minutes — vérifier exit rules

# ═══════════════════════════════════════════════════════════════════════════
# 8. FICHIERS DE PERSISTANCE
# ═══════════════════════════════════════════════════════════════════════════

DB_PATH         = Path(os.getenv("DB_PATH", "paf_trading.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
SIGNAL_DB_PATH  = Path(os.getenv("SIGNAL_DB_PATH", "paf_signals.db"))
SIGNAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
LOG_DIR         = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════
# 9. EXECUTION PARAMETERS (Almgren-Chriss)
# ═══════════════════════════════════════════════════════════════════════════

# Profils par défaut (voir execution.py pour les profils complets)
AC_GAMMA_DEFAULT    = 0.001
AC_ETA_DEFAULT      = 0.010
AC_SIGMA_DEFAULT    = 0.05
AC_LAMBDA_DEFAULT   = 0.30
AC_T_DEFAULT        = 300.0    # 5 minutes horizon
AC_N_DEFAULT        = 10       # 10 slices
AC_MAX_REPRICE      = 8

# Repricing
REPRICE_SPREAD_BPS_MAX  = 400   # spread > 400bps → illiquide, ne pas repricer
REPRICE_MID_DELTA_MIN   = -0.02 # mouvement -2¢ → repricer
REPRICE_IMBALANCE_WAIT  = 0.65  # imbalance > 0.65 → attendre
REPRICE_OFFSET          = 0.002 # mid + 0.2¢

# ═══════════════════════════════════════════════════════════════════════════
# 10. MONTE CARLO VAR
# ═══════════════════════════════════════════════════════════════════════════

MC_PATHS    = 10_000
MC_HORIZON  = 30    # jours
MC_CONF     = 0.95

# ═══════════════════════════════════════════════════════════════════════════
# 11. MODÈLE PROBABILISTE
# ═══════════════════════════════════════════════════════════════════════════

# Reference Class Engine
RCE_N_SIMILAR   = 20
RCE_MODEL_NAME  = "all-MiniLM-L6-v2"

# Merton Jump Diffusion (BTC calibré 2020-2025)
MERTON_LAMBDA_J = 0.8
MERTON_MU_J     = 0.05
MERTON_SIGMA_J  = 0.15
MERTON_N_TERMS  = 20

# MacroFed Ensemble weights
MACRO_W_FEDWATCH = 0.60
MACRO_W_LOGIT    = 0.25
MACRO_W_NS       = 0.15

# Taux sans risque
RISK_FREE_RATE = 0.049     # 4.9% (T-bills mars 2026)

# Extremizing (Satopää 2014)
EXTREMIZE_ALPHA = 1.30
UNCERTAINTY_MAX = 0.25     # skip si intervalle de confiance > 25%

# ═══════════════════════════════════════════════════════════════════════════
# 12. BLACKLIST MARCHÉS (scanner)
# ═══════════════════════════════════════════════════════════════════════════

MARKET_BLACKLIST_KEYWORDS = [
    "most", "best", "worst", "favorite", "popular",
    "who will win the most", "predict", "guess", "elon",
    "trump says", "viral",
]
MARKET_BLACKLIST_SOURCES = [
    "admin", "polymarket", "discretion", "panel",
]

# ═══════════════════════════════════════════════════════════════════════════
# 13. TELEGRAM ALERTES
# ═══════════════════════════════════════════════════════════════════════════

TELEGRAM_ALERT_DELTA_MIN = 0.035    # alerter si Δp ≥ 3.5pts
TELEGRAM_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)

# ═══════════════════════════════════════════════════════════════════════════
# 14. DRY RUN / PAPER TRADING
# ═══════════════════════════════════════════════════════════════════════════

DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
# DRY_RUN=true  → simule les trades sans appel CLOB réel
# DRY_RUN=false → exécution réelle (ATTENTION : argent réel)

if DRY_RUN:
    logging.getLogger("config").warning(
        "⚠  MODE PAPER TRADING ACTIF — aucun ordre réel ne sera passé"
    )
else:
    # Mode LIVE : vérifier que les credentials critiques sont présents
    _missing_live: list[str] = []
    if not POLY_API_KEY:
        _missing_live.append("POLY_API_KEY")
    if not POLY_API_SECRET:
        _missing_live.append("POLY_API_SECRET")
    if not POLY_API_PASSPHRASE:
        _missing_live.append("POLY_API_PASSPHRASE")
    if not POLY_PRIVATE_KEY:
        _missing_live.append("POLY_PRIVATE_KEY / ~/.paf/poly_key")
    if not POLY_WALLET_ADDRESS:
        _missing_live.append("POLY_WALLET_ADDRESS")
    if _missing_live:
        _cfg_log.error(
            f"🔴 MODE LIVE — credentials manquants : {', '.join(_missing_live)}. "
            "Le bot ne pourra pas passer d'ordres réels."
        )
    else:
        _cfg_log.info("✅ MODE LIVE — credentials Polymarket chargés")

if not TELEGRAM_ENABLED:
    _cfg_log.info(
        "Telegram désactivé (TELEGRAM_TOKEN/TELEGRAM_CHAT_ID non configurés) — "
        "configurez ces variables pour recevoir les alertes live"
    )

if not BLS_API_KEY:
    _cfg_log.info(
        "BLS_API_KEY absent — MacroFedModel utilisera des fallbacks statiques "
        "(CPI/chômage non live). Obtenez une clé gratuite sur bls.gov"
    )
