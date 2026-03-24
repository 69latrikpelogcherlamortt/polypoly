"""
seed_historical_db.py — Polymarket Trading Bot
───────────────────────────────────────────────
Peuple la base de données historique avec les marchés Polymarket résolus.

POURQUOI : le Reference Class Engine (Étape 1 du p_model) retourne 0.5
par défaut tant que la DB est vide. Avec 2000+ marchés résolus, on obtient
de vraies base rates par catégorie → l'Étape 1 devient utile.

USAGE :
    python scripts/seed_historical_db.py --limit 2000
    python scripts/seed_historical_db.py --limit 500 --db /chemin/vers/db.sqlite

API Polymarket Gamma :
    GET https://gamma-api.polymarket.com/markets
    Params : resolved=true, closed=true, limit=100, offset=N

Chaque marché retourne :
    id, question, outcomes, outcomePrices, volume, category,
    endDate, resolutionSource, closed, resolved, ...
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import requests

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seed_db")

# ── Config ───────────────────────────────────────────────────────────────────
GAMMA_API  = "https://gamma-api.polymarket.com/markets"
BATCH_SIZE = 100        # max par requête
SLEEP_MS   = 300        # ms entre requêtes pour respecter le rate limit
MIN_VOLUME = 500        # ignorer les marchés fantômes < 500$

# ── Catégories Polymarket → mapping interne ──────────────────────────────────
CATEGORY_MAP = {
    "crypto":       "crypto",
    "politics":     "politics",
    "economics":    "macro",
    "sports":       "sports",
    "science":      "science",
    "business":     "business",
    "entertainment":"entertainment",
    "weather":      "weather",
    "world":        "geopolitics",
    "usa":          "politics",
    "elections":    "politics",
}

def normalize_category(raw: str) -> str:
    if not raw:
        return "event"
    r = raw.lower().strip()
    for key, val in CATEGORY_MAP.items():
        if key in r:
            return val
    return "event"


def fetch_resolved_markets(limit: int, sleep_ms: int) -> list[dict]:
    """
    Récupère les marchés Polymarket résolus via l'API Gamma.
    Pagine automatiquement jusqu'à atteindre `limit` marchés valides.
    """
    markets: list[dict] = []
    offset  = 0
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (PolyBot/1.0)"

    log.info(f"Démarrage du scraping — objectif: {limit} marchés résolus")

    while len(markets) < limit:
        params = {
            "resolved": "true",
            "closed":   "true",
            "limit":    BATCH_SIZE,
            "offset":   offset,
        }
        try:
            resp = session.get(GAMMA_API, params=params, timeout=15)
            resp.raise_for_status()
            batch = resp.json()
        except requests.RequestException as e:
            log.error(f"Erreur API offset={offset}: {e}")
            time.sleep(2.0)
            continue
        except json.JSONDecodeError as e:
            log.error(f"JSON invalide offset={offset}: {e}")
            break

        if not batch:
            log.info(f"Plus de données API après {len(markets)} marchés")
            break

        for m in batch:
            # Ignorer si pas vraiment résolu
            if not m.get("resolved") and not m.get("closed"):
                continue

            volume = float(m.get("volume", 0) or 0)
            if volume < MIN_VOLUME:
                continue

            question = (m.get("question") or "").strip()
            if not question:
                continue

            # Déterminer l'outcome : quel token a gagné ?
            # outcomePrices = ["1", "0"] ou ["0", "1"] selon YES/NO winner
            # outcomes = ["Yes", "No"] généralement
            outcome_prices = m.get("outcomePrices") or []
            outcomes       = m.get("outcomes") or []
            resolved_yes   = None

            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except Exception:
                    outcome_prices = []
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = []

            # Chercher quel outcome a le prix = 1.0 (le winner)
            if outcome_prices and outcomes:
                for i, price_str in enumerate(outcome_prices):
                    try:
                        p = float(price_str)
                        if p >= 0.99:   # le winner token vaut $1
                            label = (outcomes[i] or "").lower() if i < len(outcomes) else ""
                            resolved_yes = 1 if "yes" in label or i == 0 else 0
                            break
                    except (ValueError, TypeError):
                        continue

            # Fallback via resolutionSource ou market metadata
            if resolved_yes is None:
                resolved = m.get("resolvedBy") or m.get("resolutionSource") or ""
                if "yes" in str(resolved).lower():
                    resolved_yes = 1
                elif "no" in str(resolved).lower():
                    resolved_yes = 0
                else:
                    continue  # impossible de déterminer l'outcome → skip

            category = normalize_category(
                m.get("category") or m.get("tags", [{}])[0].get("label", "") if m.get("tags") else ""
            )

            markets.append({
                "market_id":       str(m.get("id") or m.get("conditionId") or ""),
                "question":        question,
                "resolved_yes":    resolved_yes,
                "volume":          volume,
                "category":        category,
                "resolution_date": m.get("endDate") or m.get("resolutionDate") or "",
                "price_at_entry":  None,
            })

        log.info(f"  Collecté {len(markets)}/{limit} marchés valides (offset={offset})")

        offset += BATCH_SIZE
        time.sleep(sleep_ms / 1000.0)

        # Sécurité : max 50 pages (5000 marchés)
        if offset > 50 * BATCH_SIZE:
            log.warning("Limite de pages atteinte")
            break

    return markets[:limit]


def seed_database(markets: list[dict], db_path: Path) -> int:
    """
    Insère les marchés dans la table historical_markets.
    Retourne le nombre de nouveaux marchés insérés.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_markets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id       TEXT    UNIQUE NOT NULL,
            question        TEXT    NOT NULL,
            resolved_yes    INTEGER NOT NULL,
            volume          REAL    NOT NULL DEFAULT 0,
            category        TEXT    NOT NULL DEFAULT '',
            resolution_date TEXT,
            price_at_entry  REAL,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    inserted = 0
    for m in markets:
        mid = m["market_id"]
        if not mid:
            continue
        try:
            conn.execute("""
                INSERT OR IGNORE INTO historical_markets
                (market_id, question, resolved_yes, volume, category,
                 resolution_date, price_at_entry)
                VALUES (?,?,?,?,?,?,?)
            """, (
                mid, m["question"], m["resolved_yes"], m["volume"],
                m["category"], m["resolution_date"], m["price_at_entry"],
            ))
            if conn.execute("SELECT changes()").fetchone()[0] > 0:
                inserted += 1
        except sqlite3.Error as e:
            log.debug(f"Insert error {mid}: {e}")

    conn.commit()
    conn.close()
    return inserted


def print_stats(db_path: Path):
    """Affiche les statistiques de la DB après population."""
    conn = sqlite3.connect(str(db_path))

    total = conn.execute("SELECT COUNT(*) FROM historical_markets").fetchone()[0]
    yes_rate = conn.execute(
        "SELECT AVG(resolved_yes) FROM historical_markets"
    ).fetchone()[0] or 0

    log.info("─" * 50)
    log.info(f"✓ Total marchés en DB : {total}")
    log.info(f"  Taux YES global    : {yes_rate:.1%}")

    rows = conn.execute("""
        SELECT category, COUNT(*) as n, AVG(resolved_yes) as yes_rate
        FROM historical_markets
        GROUP BY category
        ORDER BY n DESC
    """).fetchall()

    log.info("\n  Par catégorie :")
    for cat, n, rate in rows:
        log.info(f"    {cat:15s}  {n:5d} marchés   YES={rate:.1%}")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Seed historical Polymarket DB")
    parser.add_argument("--limit",  type=int, default=2000,
                        help="Nombre de marchés à scraper (défaut: 2000)")
    parser.add_argument("--db",     type=str, default="paf_trading.db",
                        help="Chemin vers la DB SQLite (défaut: paf_trading.db)")
    parser.add_argument("--sleep",  type=int, default=300,
                        help="Délai entre requêtes en ms (défaut: 300)")
    args = parser.parse_args()

    db_path = Path(args.db)
    log.info(f"DB cible : {db_path.resolve()}")

    # Scraping
    markets = fetch_resolved_markets(limit=args.limit, sleep_ms=args.sleep)
    if not markets:
        log.error("Aucun marché récupéré. Vérifier la connectivité réseau.")
        sys.exit(1)

    log.info(f"Scraped {len(markets)} marchés valides")

    # Insertion
    inserted = seed_database(markets, db_path)
    log.info(f"Insérés (nouveaux) : {inserted} / {len(markets)}")

    # Stats
    print_stats(db_path)


if __name__ == "__main__":
    main()
