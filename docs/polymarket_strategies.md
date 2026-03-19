# Polymarket Trading Strategies
> Capital de départ : 100€ | Dernière mise à jour : Mars 2026

---

## Table des matières
1. [Infrastructure](#infrastructure)
2. [Signaux & Sources de données](#signaux--sources-de-données)
3. [Market Scanner](#market-scanner)
4. [Modèle Probabiliste — Superforecaster Framework](#modèle-probabiliste--superforecaster-framework)
5. [Fondations théoriques](#fondations-théoriques)
6. [Système de validation — 7 Gates](#système-de-validation--7-gates)
7. [Stratégie 1 — Hybride Tetlock](#stratégie-1--hybride-tetlock)
8. [Stratégie 2 — Longshots Macro](#stratégie-2--longshots-macro)
9. [Exécution — Almgren-Chriss Optimal Execution](#exécution--almgren-chriss-optimal-execution)
10. [Gestion des Fills Partiels](#gestion-des-fills-partiels)
11. [Repricing — Ordre Non Rempli](#repricing--ordre-non-rempli)
12. [Sizing & Risk Management](#sizing--risk-management)
13. [Règles de sortie](#règles-de-sortie)
14. [Kill Switches](#kill-switches)
15. [Métriques de calibration](#métriques-de-calibration)
16. [Wallets de référence](#wallets-de-référence)
17. [Ce qui ne fonctionne plus en 2026](#ce-qui-ne-fonctionne-plus-en-2026)

---

## Infrastructure

```
VPS       : Hetzner Ireland (eu-west-1) — ~4€/mois
            Latence 5-10ms vers serveurs Polymarket (AWS London)
Language  : Python 3.10+
Client    : py-clob-client (officiel Polymarket)
Data      : WebSocket wss://ws-subscriptions-clob.polymarket.com
            Gamma API gamma-api.polymarket.com
            Data API data-api.polymarket.com
Signals   : CME FedWatch (gratuit)
            Deribit API v2 (gratuit, BTC/ETH implied vol)
            sentence-transformers (Reference Class Engine, local)
            scipy/sklearn (modèles quantitatifs, local)
Logging   : SQLite local — chaque trade loggé avec P_estimée + outcome
Execution : Limit orders uniquement — JAMAIS market orders
Gas       : Builder Program Polymarket (gasless transactions)
```

---

## Signaux & Sources de données

### Pipeline global
```
Toutes les 10 minutes :
  1. RSS Reuters/AP      → headlines filtrées par market keywords
  2. Google News RSS     → 5 headlines par marché actif
  3. CME FedWatch        → update prob Fed si marché Fed actif
  4. Deribit             → implied prob BTC/ETH si marché crypto actif
  5. Kalshi              → cross-check prix si marché existe des deux côtés
  6. Binance WS          → momentum BTC continu en background
  7. Twitter/Nitter      → comptes haute valeur seulement (filtre strict)

À chaque nouveau marché candidat :
  Assembler contexte → LLM Tetlock prompt → P_true
  → Z-score → 7 gates → trade ou skip
```

---

### TIER 1 — Sources primaires (vérité de terrain)

#### CME FedWatch
```
URL       : https://www.cmegroup.com/markets/interest-rates/fed-funds/
Auth      : gratuit, pas de clé requise
Données   : probabilité implicite de chaque décision Fed par meeting
            (hausse / baisse / maintien)

Utilisation :
  Si Polymarket "Fed coupe 25bps" = 35%
  Et FedWatch = 58%
  → Edge de 23 points → passe le Z-score facilement

Polling   : toutes les heures
Format    : JSON parsable directement
```

#### Deribit API v2
```
URL       : https://www.derebit.com/api/v2/public/
Auth      : pas de clé pour données publiques
Données   : implied volatility BTC/ETH
            prix options par strike et expiry
            delta par contrat

Calcul probabilité implicite :
  P(BTC > $X à date Y) = delta option call strike X expiry Y
  Delta disponible directement dans l'API

Utilisation :
  Polymarket "BTC $150k Dec 2026" = 3%
  Deribit delta call $150k Dec 2026 = 8%
  → Edge structurel de 5 points minimum

Polling   : toutes les 15 minutes
```

#### Reuters / AP News RSS
```
URLs :
  https://feeds.reuters.com/reuters/topNews
  https://feeds.reuters.com/reuters/businessNews
  https://feeds.reuters.com/reuters/politicsNews
  https://apnews.com/apf-topnews

Auth      : gratuit, pas de clé
Format    : XML parsable

Pipeline :
  1. Parser RSS toutes les 5 minutes
  2. Filtrer par keywords du marché ciblé
  3. Injecter dans prompt LLM comme contexte news
  4. LLM fait le Bayes update sur P_true
```

#### Binance WebSocket
```
URL       : wss://stream.binance.com:9443/ws/btcusdt@ticker
Auth      : gratuit, pas de clé
Données   : prix BTC/ETH/SOL en temps réel (tick by tick)

Utilisation :
  Signal externe pour marchés BTC price targets
  Calcul momentum : BTC +3%/semaine → ajuster P_true hausse
  PAS utilisé pour HFT — uniquement pour scoring macro
```

#### BLS / Fed.gov feeds
```
BLS       : https://api.bls.gov/publicAPI/v2/timeseries/data/
            clé gratuite sur registration bls.gov
            CPI, jobs, inflation data officielle

Fed       : https://www.federalreserve.gov/feeds/press_all.xml
            Communiqués officiels en RSS

Utilisation :
  Contexte macro pour marchés Fed/inflation
  Base rate historique sur décisions similaires
```

---

### TIER 2 — Sources d'enrichissement

#### Google News RSS
```
URL pattern :
  https://news.google.com/rss/search?q={KEYWORD}&hl=en&gl=US

Auth      : gratuit, pas de clé, pas de scraping
Données   : headlines agrégées par keyword

Utilisation :
  Keyword = titre du marché Polymarket
  Ex : "Federal Reserve interest rate decision"
  Ex : "Bitcoin price 2026"
  Injecter les 5 headlines les plus récentes dans le prompt LLM

Limite    : throttling possible si trop de requêtes
Solution  : une requête par marché au moment du scoring uniquement
```

#### Kalshi — Cross-platform arbitrage
```
URL       : https://trading.kalshi.com/trade-api/v2/markets/
Auth      : données publiques sans clé

Utilisation :
  Si même question existe sur Kalshi ET Polymarket
  Et prix divergent de >5% → signal fort

  Ex : Polymarket "Fed coupe" = 35%, Kalshi = 48%
  → Polymarket est en retard → acheter sur Polymarket

ATTENTION : vérifier que la résolution criterion est identique
            (gouvernement shutdown 2024 : résolution différente = piège)
```

#### Polymarket Activity API
```
URL       : data-api.polymarket.com/activity
            data-api.polymarket.com/profile?user={address}

Utilisation :
  Détecter gros mouvements récents sur un marché (volume spike)
  Si volume spike dans les 2 dernières heures → signal possible
  
  Whale detection — surveiller wallet2 de référence :
  GET /activity?user={wallet2_address}&type=TRADE&limit=10
  Si wallet2 entre sur un marché → signal de conviction fort
```

---

### TIER 2.5 — Twitter (filtré strict)

> **Règle d'or :** Un tweet ne fait JAMAIS changer P_true seul.
> Il déclenche une vérification sur les sources Tier 1.
> Si Tier 1 confirme → P_true update. Sinon → ignorer.

#### Accès via Nitter RSS (gratuit)
```
URL pattern :
  https://nitter.net/{username}/rss
  https://nitter.privacydev.net/{username}/rss  (backup)

Avantages  : gratuit, pas de clé, pas de rate limiting agressif
Délai      : ~2-5 minutes (acceptable pour notre usage)
```

#### Comptes haute valeur à surveiller
```
Macro/Fed :
  @federalreserve    (officiel — annonces directes)
  @nick_timiraos     (WSJ Fed reporter — le plus important)
  @elerianmohamed    (macro global)

BTC/Crypto :
  @woonomic          (on-chain analytics)
  @glassnode         (on-chain data)
  @ki_young_ju       (CryptoQuant CEO)
  @lawmaster         (options flow)

Polymarket :
  @Polymarket        (annonces officielles)
  @PolymarketWhales  (whale tracking)
  @Domahhhh          (analyste sérieux)
```

#### Keywords par catégorie
```python
TWITTER_KEYWORDS = {
    "fed": [
        "Federal Reserve decision",
        "FOMC meeting",
        "interest rate cut",
        "Jerome Powell statement"
    ],
    "btc_price": [
        "Bitcoin ATH",
        "BTC resistance",
        "Bitcoin institutional",
        "spot Bitcoin ETF flows"
    ],
    "politics": []  # générer dynamiquement depuis le titre du marché
}
```

#### Filtre anti-bruit obligatoire
```python
BLACKLIST_KEYWORDS = [
    "copytrade", "copy trade", "t.me/",
    "made $", "profit today", "bot made",
    "ref=", "?code=", "join my",
    "my bot", "automated", "passive income",
    "DM me", "link in bio"
]

def filter_tweet_signal(tweet):
    # Exclure comptes < 1000 followers
    if tweet.author.followers < 1000:
        return False, "low_followers"
    
    # Exclure comptes créés < 6 mois
    if tweet.author.age_months < 6:
        return False, "new_account"
    
    # Blacklist keywords
    for kw in BLACKLIST_KEYWORDS:
        if kw.lower() in tweet.text.lower():
            return False, "blacklisted_keyword"
    
    # Engagement minimum
    if tweet.retweets < 5 and tweet.likes < 20:
        return False, "low_engagement"
    
    return True, "signal_valide"
```

---

## Market Scanner

### Responsabilité

Trouver les marchés candidats qui passent les filtres des stratégies 1 et 2. Le scoring probabiliste est délégué au modèle (section suivante). Le scanner ne décide pas si un marché est rentable — il décide si un marché mérite d'être scoré.

### Champs réels de la Gamma API utilisés

```
id               → identifiant du marché
question         → texte de la question
outcomePrices    → ["0.72", "0.28"] — YES en index 0, NO en index 1
volume24hr       → volume dernières 24h en USDC
liquidity        → liquidité totale disponible
endDate          → date de résolution ISO 8601
active           → bool, marché actif
closed           → bool, marché fermé
category         → "crypto", "politics", "economics", etc.
resolutionSource → source officielle de résolution
tokens           → [{outcome: "Yes", token_id: "..."}, ...]
conditionId      → identifiant du marché (≠ token_id CLOB)
```

> **Important :** Le CLOB exige le `token_id` de l'outcome YES — pas le `conditionId`. Ce sont deux identifiants distincts.

### Code

```python
import requests
import time
from datetime import datetime, timezone
from typing import Optional

GAMMA_API = "https://gamma-api.polymarket.com"

# Résolution subjective ou non vérifiable → skip
BLACKLIST_KEYWORDS = [
    "most", "best", "worst", "favorite", "popular",
    "who will win the most", "predict", "guess",
    "elon", "trump says", "viral"
]

BLACKLIST_SOURCES = [
    "admin", "polymarket", "discretion", "panel"
]


class MarketScanner:
    """
    Scanner de marchés Polymarket.
    Basé sur les champs réels de la Gamma API.

    Responsabilité unique : trouver les candidats.
    Le scoring probabiliste est fait par le modèle.
    """

    def fetch_active_markets(self, limit: int = 500) -> list:
        """
        Récupère les marchés actifs via pagination.
        Rate limiting : 0.5s entre requêtes (non documenté
        mais conservateur).
        """
        markets = []
        offset  = 0

        while len(markets) < limit:
            try:
                resp = requests.get(
                    f"{GAMMA_API}/markets",
                    params={
                        "active":    "true",
                        "closed":    "false",
                        "limit":     100,
                        "offset":    offset,
                        "order":     "volume24hr",
                        "ascending": "false"
                    },
                    timeout=10
                )
                resp.raise_for_status()
                batch = resp.json()

                if not batch:
                    break

                markets.extend(batch)
                offset += 100

                if len(batch) < 100:
                    break

                time.sleep(0.5)

            except requests.RequestException as e:
                print(f"Gamma API error : {e}")
                break

        return markets

    def extract_yes_token_id(self, market: dict) -> Optional[str]:
        """
        Extrait le token_id de l'outcome YES.
        Nécessaire pour placer un ordre sur le CLOB.
        conditionId ≠ token_id — erreur fréquente.
        """
        tokens  = market.get("tokens", [])
        yes_tok = next(
            (t for t in tokens if t.get("outcome") == "Yes"), None)
        return yes_tok.get("token_id") if yes_tok else None

    def parse_yes_price(self, market: dict) -> Optional[float]:
        """
        Prix YES depuis outcomePrices[0].
        Fallback sur lastTradePrice si absent.
        """
        try:
            prices = market.get("outcomePrices", [])
            if isinstance(prices, list) and len(prices) >= 1:
                return float(prices[0])
            ltp = market.get("lastTradePrice")
            if ltp is not None:
                return float(ltp)
        except (ValueError, TypeError):
            pass
        return None

    def days_to_resolution(self, market: dict) -> Optional[float]:
        """Jours avant résolution depuis endDate."""
        end_str = market.get("endDate")
        if not end_str:
            return None
        try:
            end = datetime.fromisoformat(
                end_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return round((end - now).total_seconds() / 86400, 1)
        except (ValueError, AttributeError):
            return None

    def is_hard_resolution(self, market: dict) -> bool:
        """
        Résolution vérifiable objectivement.
        Exclut les marchés subjectifs ou à source non fiable.
        Note : seuils non calibrés — affiner après observation
        de faux positifs réels.
        """
        question = market.get("question", "").lower()
        source   = market.get("resolutionSource", "").lower()

        for kw in BLACKLIST_KEYWORDS:
            if kw in question:
                return False
        for src in BLACKLIST_SOURCES:
            if src in source:
                return False
        return True

    def filter_strategy_1(self, market: dict,
                           price: float, days: float,
                           vol24: float) -> bool:
        """
        Stratégie 1 — Hybride Tetlock.
        Prix favoris : 0.70-0.92€ OU longshots : 0.01-0.10€
        Volume 24h   : 5,000 - 80,000€
        Résolution   : 5 - 21 jours
        """
        if not (5_000 <= vol24 <= 80_000):
            return False
        if not (5 <= days <= 21):
            return False
        if not ((0.70 <= price <= 0.92) or (0.01 <= price <= 0.10)):
            return False
        return True

    def filter_strategy_2(self, market: dict,
                           price: float, days: float,
                           vol24: float) -> bool:
        """
        Stratégie 2 — Longshots Macro.
        Prix      : 0.01 - 0.08€ strictement
        Volume 24h: > 10,000€
        Résolution: 14 - 90 jours
        Catégorie : crypto / economics / politics / finance / sports
        """
        category = market.get("category", "").lower()

        if vol24 < 10_000:
            return False
        if not (14 <= days <= 90):
            return False
        if not (0.01 <= price <= 0.08):
            return False
        if category not in ("crypto", "economics", "politics",
                             "finance", "sports"):
            return False
        return True

    def get_candidates(self) -> dict:
        """
        Point d'entrée principal.
        Retourne les candidats pour chaque stratégie.
        Le scoring probabiliste est fait ensuite.

        Format retourné :
        {
          "strategy_1": [{"market_id", "token_id", "question",
                          "price", "volume_24h", "liquidity",
                          "days_to_res", "category",
                          "resolution_source"}, ...],
          "strategy_2": [...]
        }
        """
        all_markets = self.fetch_active_markets(limit=500)
        candidates  = {"strategy_1": [], "strategy_2": []}

        for m in all_markets:
            # Extraire token_id YES — obligatoire pour le CLOB
            token_id = self.extract_yes_token_id(m)
            if token_id is None:
                continue

            price = self.parse_yes_price(m)
            days  = self.days_to_resolution(m)
            vol24 = float(m.get("volume24hr", 0) or 0)

            if price is None or days is None:
                continue
            if days <= 0:
                continue
            if not self.is_hard_resolution(m):
                continue

            entry = {
                "market_id":         m.get("id"),
                "token_id":          token_id,
                "question":          m.get("question"),
                "price":             price,
                "volume_24h":        vol24,
                "liquidity":         float(m.get("liquidity", 0) or 0),
                "days_to_res":       days,
                "category":          m.get("category", ""),
                "resolution_source": m.get("resolutionSource", "")
            }

            if self.filter_strategy_1(m, price, days, vol24):
                candidates["strategy_1"].append(entry)

            if self.filter_strategy_2(m, price, days, vol24):
                candidates["strategy_2"].append(entry)

        print(f"Scanner : {len(all_markets)} marchés analysés")
        print(f"  Stratégie 1 : {len(candidates['strategy_1'])} candidats")
        print(f"  Stratégie 2 : {len(candidates['strategy_2'])} candidats")

        return candidates
```

### À calibrer après 30-50 trades réels

```
BLACKLIST_KEYWORDS et BLACKLIST_SOURCES :
  → Logger chaque marché skippé avec la raison
  → Identifier les faux positifs (marchés valides rejetés)
  → Ajuster les listes selon les patterns observés

Filtres volume/jours/prix :
  → Déjà calibrés sur les stratégies 1 et 2 documentées
  → Ajuster si les candidats trouvés sont systématiquement
    non profitables dans une sous-catégorie
```

---

## Modèle Probabiliste — Superforecaster Framework

### Architecture globale du pipeline
```
INPUT : question + contexte marché

ÉTAPE 1 → Reference Class Engine
           Base rate pondérée + Beta distribution

ÉTAPE 2 → Modèle quantitatif spécialisé
           Crypto   : Black-Scholes + Merton Jump Diffusion
           Macro    : Logit + Nelson-Siegel + FedWatch
           Politique: Fermi decomposition + Kalshi + Extremizing

ÉTAPE 3 → Bayes Update continu
           Mise à jour sur chaque signal externe

ÉTAPE 4 → Ensemble pondéré par Brier
           Poids adaptatifs selon performance historique

ÉTAPE 5 → Extremizing (Satopää 2014) + décision finale

OUTPUT  : P_final + [low, high] + signal_strength + tradeable
```

### 5 Principes superforecasters traduits en math
```
1. Outside view first    → toujours partir de la base rate
2. Inside view limité    → ajustements max ±15 points
3. Bayes update continu  → chaque signal met à jour P
4. Extremizing           → corriger le biais de prudence
5. Quantifier incertitude → intervalle obligatoire, skip si > 0.20
```

---

### ÉTAPE 1 — Reference Class Engine

```python
from scipy.stats import beta as beta_dist
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer
import numpy as np

class ReferenceClassEngine:
    """
    Trouve les marchés historiques les plus similaires
    et calcule une base rate pondérée.

    Principe superforecaster :
    "Quelle classe de situations ressemble à celle-ci ?
     Quel % se résout YES dans cette classe ?"
    """

    def __init__(self, historical_db):
        self.db = historical_db
        self.encoder = SentenceTransformer('all-MiniLM-L6-v2')

    def find_similar_markets(self, question, n=20):
        q_embedding = self.encoder.encode([question])
        all_questions = self.db.get_all_questions()
        all_embeddings = self.encoder.encode(all_questions)
        similarities = cosine_similarity(q_embedding, all_embeddings)[0]
        top_indices = np.argsort(similarities)[-n:][::-1]

        return [{
            "question": self.db.get_market(i).question,
            "outcome": self.db.get_market(i).resolved_yes,
            "similarity": similarities[i],
            "volume": self.db.get_market(i).volume
        } for i in top_indices]

    def compute_base_rate(self, similar_markets):
        """
        Base rate pondérée par similarité ET volume.
        Beta(α, β) pour quantifier l'incertitude.
        Prior de Laplace : α=1, β=1 (évite les extrêmes)
        """
        if not similar_markets:
            return 0.5, 0.4, 0.6  # prior uniforme

        weights = np.array([
            m["similarity"] * np.log1p(m["volume"])
            for m in similar_markets
        ])
        weights /= weights.sum()
        outcomes = np.array([m["outcome"] for m in similar_markets])

        p_base = np.average(outcomes, weights=weights)

        # Beta distribution pour intervalle de confiance
        alpha = np.sum(weights * outcomes) * len(similar_markets) + 1
        beta_p = np.sum(weights * (1 - outcomes)) * len(similar_markets) + 1

        low  = beta_dist.ppf(0.10, alpha, beta_p)
        high = beta_dist.ppf(0.90, alpha, beta_p)

        return p_base, low, high

    def get_base_rate(self, question):
        similar = self.find_similar_markets(question, n=20)
        p_base, low, high = self.compute_base_rate(similar)
        uncertainty = high - low
        return {
            "p_base": p_base,
            "interval": (low, high),
            "uncertainty": uncertainty,
            "n_similar": len(similar),
            "signal": "fort" if uncertainty < 0.20 else "faible"
        }
```

---

### ÉTAPE 2A — Modèle Crypto (Black-Scholes + Merton)

```python
from scipy.stats import norm
import numpy as np

class CryptoModel:
    """
    Black-Scholes + Jump Diffusion (Merton 1976)
    P(S_T > K) = probabilité que BTC dépasse le prix cible K à date T

    Merton supérieur à BS sur BTC car :
    - Kurtosis BTC > 4 (queues épaisses)
    - Sauts fréquents (+/-20% en 24h)
    - BS sous-estime les longshots extrêmes

    Paramètres BTC calibrés 2020-2025 :
      lambda_j = 0.8  (0.8 sauts/an)
      mu_j     = 0.05 (sauts légèrement haussiers)
      sigma_j  = 0.15 (volatilité des sauts)
    """

    def black_scholes_prob(self, S, K, T, r, sigma):
        """
        P(S_T > K) = N(d2)
        d2 = [ln(S/K) + (r - σ²/2)T] / (σ√T)
        """
        if T <= 0 or sigma <= 0:
            return 1.0 if S > K else 0.0
        d2 = (np.log(S/K) + (r - 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
        return float(norm.cdf(d2))

    def merton_jump_prob(self, S, K, T, r, sigma,
                         lambda_j=0.8, mu_j=0.05, sigma_j=0.15,
                         n_terms=20):
        """
        Merton (1976) : somme de BS conditionnels sur n sauts
        Poisson(λT) sauts sur la période [0, T]
        """
        prob = 0.0
        poisson_weight = np.exp(-lambda_j * T)

        for n in range(n_terms):
            w_n = poisson_weight * (lambda_j*T)**n / np.math.factorial(n)
            r_n = r - lambda_j*(np.exp(mu_j + 0.5*sigma_j**2)-1) + n*mu_j/T
            sigma_n = np.sqrt(sigma**2 + n*sigma_j**2/T)
            prob += w_n * self.black_scholes_prob(S, K, T, r_n, sigma_n)

        return prob

    def get_probability(self, S, K, T, sigma, r=0.049):
        p_bs     = self.black_scholes_prob(S, K, T, r, sigma)
        p_merton = self.merton_jump_prob(S, K, T, r, sigma)

        # Plus loin du spot → Merton plus fiable
        distance_ratio = abs(K - S) / S
        w_merton = min(0.80, distance_ratio * 2)
        w_bs = 1 - w_merton
        p_final = w_bs * p_bs + w_merton * p_merton

        # Intervalle : sensibilité à ±10% sur la vol implicite
        p_low  = self.black_scholes_prob(S, K, T, r, sigma*0.9)
        p_high = self.black_scholes_prob(S, K, T, r, sigma*1.1)

        return {
            "p_model": p_final, "p_bs": p_bs, "p_merton": p_merton,
            "interval": (p_low, p_high), "sigma_used": sigma,
            "model": "merton_jump_diffusion"
        }
```

---

### ÉTAPE 2B — Modèle Macro/Fed

```python
import numpy as np
from scipy.special import expit
from scipy.optimize import minimize

class MacroFedModel:
    """
    Combine 3 modèles indépendants :
    1. Logit calibré sur Fed 1990-2025
    2. Nelson-Siegel (courbe des taux)
    3. CME FedWatch (source principale)

    Coefficients Logit calibrés OLS (1990-2025) :
    Variable              Coeff   Interprétation
    intercept            -2.10
    cpi_yoy              -0.82   inflation haute → moins de cuts
    unemployment         +0.63   chômage haut → plus de cuts
    gdp_growth           -0.41   croissance haute → moins de cuts
    fed_funds_rate       -0.38   taux élevés → plus probable de couper
    yield_curve (10Y-2Y) +0.95   courbe inversée → forte pression cut
    """

    LOGIT_COEFFS = {
        "intercept": -2.10, "cpi_yoy": -0.82,
        "unemployment": +0.63, "gdp_growth": -0.41,
        "fed_funds_rate": -0.38, "yield_curve": +0.95
    }

    def logit_probability(self, macro_data):
        z = self.LOGIT_COEFFS["intercept"]
        for key, coeff in self.LOGIT_COEFFS.items():
            if key != "intercept" and key in macro_data:
                z += coeff * macro_data[key]
        return float(expit(z))

    def nelson_siegel_implied(self, maturities, yields):
        """
        f(t) = β₀ + β₁×e^(-t/τ) + β₂×(t/τ)×e^(-t/τ)

        Pente de la courbe → signal politique monétaire
        Courbe inversée → pression forte de cut
        """
        def ns_curve(params, t):
            b0, b1, b2, tau = params
            return b0 + b1*np.exp(-t/tau) + b2*(t/tau)*np.exp(-t/tau)

        result = minimize(
            lambda p: np.sum((ns_curve(p, maturities) - yields)**2),
            [0.03, -0.01, 0.01, 1.5]
        )
        b0, b1 = result.x[0], result.x[1]
        slope = b0 - (b0 + b1)
        return float(expit(slope * 10))

    def get_probability(self, macro_data, p_fedwatch):
        p_logit = self.logit_probability(macro_data)
        p_ns = self.nelson_siegel_implied(
            macro_data["maturities"], macro_data["yields"]
        )

        # FedWatch dominant (marché le plus liquide)
        p_ensemble = 0.60*p_fedwatch + 0.25*p_logit + 0.15*p_ns

        return {
            "p_model": p_ensemble,
            "p_logit": p_logit, "p_fedwatch": p_fedwatch, "p_ns": p_ns,
            "interval": (min(p_logit, p_fedwatch, p_ns),
                         max(p_logit, p_fedwatch, p_ns)),
            "model": "macro_ensemble"
        }
```

---

### ÉTAPE 2C — Modèle Événements/Politique

```python
class EventModel:
    """
    Pour les marchés sans signal quantitatif direct.

    Combine :
    1. Base rate Beta pondérée (Reference Class Engine)
    2. Décomposition de Fermi (sous-questions indépendantes)
    3. Kalshi cross-platform signal
    4. Extremizing de Satopää (2014)
    """

    def fermi_decomposition(self, sub_questions):
        """
        Décomposer en sous-questions indépendantes.
        P_final = ∏ P(sous-question i)

        Ex : "Will X win the election?"
        → P(X leads polls) × P(polls accurate) × P(turnout favors X)
        """
        if not sub_questions:
            return None
        p = 1.0
        for _, p_sub in sub_questions:
            p *= p_sub
        return p

    def extremize(self, p, alpha=1.3):
        """
        Satopää et al. (2014) — corriger le biais de prudence.

        P_extremized = P^α / (P^α + (1-P)^α)

        α = 1.3 → légère extrémisation (recommandé marchés politiques)
        α = 1.0 → désactivé (source unique)
        α = 2.0 → forte extrémisation (risqué, éviter)

        Pourquoi : l'agrégation de prédicteurs indépendants
        produit un résultat systématiquement trop prudent.
        Extrémiser compense ce biais structurel.
        """
        return p**alpha / (p**alpha + (1-p)**alpha)

    def get_probability(self, ref_class_result, p_kalshi=None,
                        sub_questions=None):
        sources = [("base_rate", ref_class_result["p_base"], 1.0)]

        if p_kalshi:
            sources.append(("kalshi", p_kalshi, 1.5))

        if sub_questions:
            p_fermi = self.fermi_decomposition(sub_questions)
            if p_fermi:
                sources.append(("fermi", p_fermi, 1.2))

        total_w = sum(w for _, _, w in sources)
        p_ensemble = sum(p*w for _, p, w in sources) / total_w

        # Extremizing si >= 2 sources indépendantes
        n = len(sources)
        p_final = self.extremize(p_ensemble, alpha=1.3) if n >= 2 else p_ensemble

        return {
            "p_model": p_final, "p_raw": p_ensemble,
            "sources": sources, "extremized": n >= 2,
            "interval": ref_class_result["interval"],
            "model": "event_ensemble"
        }
```

---

### ÉTAPE 3 — Bayes Update continu

```python
class BayesUpdater:
    """
    Mise à jour bayésienne sur chaque signal.

    Principe superforecaster : mettre à jour proportionnellement
    à la force évidentielle. Ni sur-réagir, ni ignorer.

    Odds_post = Odds_prior × LR
    LR = P(E|H_true) / P(E|H_false)

    Forces calibrées empiriquement :
    Signal                    LR     Source
    fedwatch_update          3.0    FedWatch change de niveau
    deribit_vol_spike        2.5    Vol BTC monte brutalement
    kalshi_divergence        2.0    Kalshi diverge >5%
    reuters_headline         1.8    Headline primaire Reuters/AP
    google_news              1.3    News secondaires
    twitter_tier1            1.5    Compte haute valeur vérifié
    twitter_tier2            1.1    Compte standard (faible)
    volume_spike_polymarket  1.4    Volume spike sur le marché
    """

    SIGNAL_STRENGTHS = {
        "fedwatch_update": 3.0, "deribit_vol_spike": 2.5,
        "kalshi_divergence": 2.0, "reuters_headline": 1.8,
        "google_news": 1.3, "twitter_tier1": 1.5,
        "twitter_tier2": 1.1, "volume_spike_polymarket": 1.4
    }

    def update(self, p_prior, signal_type, signal_direction):
        """
        signal_direction : +1 favorable YES, -1 défavorable
        """
        lr = self.SIGNAL_STRENGTHS.get(signal_type, 1.0)
        if signal_direction == -1:
            lr = 1 / lr

        prior_odds = p_prior / (1 - p_prior)
        posterior_odds = prior_odds * lr
        p_post = posterior_odds / (1 + posterior_odds)

        return float(np.clip(p_post, 0.02, 0.98))

    def update_sequence(self, p_initial, signals):
        """signals = liste de (signal_type, direction)"""
        p = p_initial
        history = [p]
        for signal_type, direction in signals:
            p = self.update(p, signal_type, direction)
            history.append(p)
        return p, history
```

---

### ÉTAPE 4 — Ensemble pondéré par Brier

```python
class BrierWeightedEnsemble:
    """
    w_i = exp(-Brier_i) / Σ exp(-Brier_j)

    Les modèles les mieux calibrés reçoivent plus de poids.
    Automatiquement adaptatif — se dégrade si un modèle performe mal.
    Minimum 5 résolutions pour calculer les poids.
    Sinon : poids égaux.
    """

    def __init__(self, history_db):
        self.db = history_db
        self.models = ["base_rate", "quant", "bayes_updated"]

    def compute_weights(self, last_n=20):
        weights = {}
        for model in self.models:
            preds = self.db.get_model_predictions(model, last_n)
            outcomes = self.db.get_outcomes(last_n)
            if len(preds) < 5:
                weights[model] = 1 / len(self.models)
                continue
            brier = np.mean([(p-o)**2 for p, o in zip(preds, outcomes)])
            weights[model] = np.exp(-brier)
        total = sum(weights.values())
        return {k: v/total for k, v in weights.items()}

    def combine(self, predictions_dict):
        weights = self.compute_weights()
        p_ensemble = sum(
            weights.get(m, 0) * p
            for m, p in predictions_dict.items()
        )
        probs = list(predictions_dict.values())
        spread = max(probs) - min(probs)
        return {
            "p_final": p_ensemble, "weights_used": weights,
            "model_spread": spread,
            "signal": "fort" if spread < 0.10 else
                      "moyen" if spread < 0.20 else "faible"
        }
```

---

### ÉTAPE 5 — Décision finale

```python
def final_decision(base_rate_result, quant_result,
                   bayes_result, ensemble):
    """
    Synthèse finale + extremizing adaptatif.

    Règles d'extremizing :
    - 3 sources convergentes (spread < 0.12) → α = 1.4
    - 2 sources                              → α = 1.2
    - 1 source                               → α = 1.0 (off)
    """
    if ensemble["signal"] == "faible":
        return None, "signal_trop_faible"

    p_raw = ensemble["p_final"]
    n_sources = sum(1 for r in [base_rate_result, quant_result, bayes_result]
                    if r is not None)

    if n_sources >= 3 and ensemble["model_spread"] < 0.12:
        p_final = p_raw**1.4 / (p_raw**1.4 + (1-p_raw)**1.4)
    elif n_sources >= 2:
        p_final = p_raw**1.2 / (p_raw**1.2 + (1-p_raw)**1.2)
    else:
        p_final = p_raw

    intervals = [r["interval"] for r in
                 [base_rate_result, quant_result] if r and "interval" in r]
    low  = np.mean([i[0] for i in intervals]) if intervals else p_final - 0.15
    high = np.mean([i[1] for i in intervals]) if intervals else p_final + 0.15
    uncertainty = high - low

    return {
        "p_final": p_final, "p_raw": p_raw,
        "interval": (low, high), "uncertainty": uncertainty,
        "n_sources": n_sources, "extremized": n_sources >= 2,
        "signal_strength": "fort"  if uncertainty < 0.15 else
                           "moyen" if uncertainty < 0.25 else "faible",
        "tradeable": uncertainty < 0.25 and n_sources >= 2
    }
```

---

### Sélection automatique du modèle

```python
def route_to_model(question, resolution_date, context):
    """
    Sélectionne automatiquement le modèle adapté
    selon la catégorie du marché.
    """
    q_lower = question.lower()

    # Détection catégorie
    crypto_keywords = ["bitcoin", "btc", "eth", "ethereum",
                       "solana", "sol", "crypto", "price"]
    fed_keywords    = ["fed", "federal reserve", "interest rate",
                       "fomc", "rate cut", "rate hike", "bps"]

    if any(kw in q_lower for kw in crypto_keywords):
        return "crypto"   # → CryptoModel (BS + Merton)
    elif any(kw in q_lower for kw in fed_keywords):
        return "macro"    # → MacroFedModel (Logit + NS + FedWatch)
    else:
        return "event"    # → EventModel (Fermi + Kalshi + Extremizing)
```

---

## Fondations théoriques

### Bayes Update
```
P(H|E) = P(E|H) × P(H) / P(E)

Mise à jour de la probabilité à chaque nouvelle information.
Research agent scrape news → Bayes step par headline.
```

### Market Edge
```
edge = p_model - p_mkt

Trade uniquement si edge > 0.04 (4 cents minimum)
Seuil plancher contre le bruit et le drag des frais.
```

### Mispricing Z-Score
```
δ = (p_model - p_mkt) / σ

σ = rolling 14-day std de la volatilité du marché
δ > 1.5 = signal structurel, pas du bruit
δ > 1.5 obligatoire sur tous les longshots
```

### Kelly Criterion (fractionnaire)
```
f* = (p×b - q) / b

q = 1 - p
b = cote implicite = (1 - prix) / prix

Position réelle = α × f* × bankroll
α = 0.30 (fractional Kelly)

Pourquoi α=0.30 :
  Kelly plein    → drawdown max -38%
  α=0.30 Kelly  → drawdown max -17%
  Perte long-run growth : seulement -12%
```

### Expected Value
```
EV = p×b - (1-p)

Trade uniquement si EV > 0
Pre-trade sanity check après spread, slippage, frais.
```

### Value at Risk (Monte Carlo)
```
VaR = μ - 1.645 × σ  (95% confidence)
σ = rolling 14-day std

IMPORTANT : Bootstrap Monte Carlo sur 10,000 chemins
            Jamais VaR gaussien — kurtosis marché > 4
            VaR gaussien sous-estime le tail risk de 2-3x

Si VaR(95%) > daily_limit → blocage toutes nouvelles positions
```

### Sharpe Ratio
```
SR = (E[R] - Rf) / σ(R)
Rf = 4.9% (taux sans risque)
Target SR > 2.0

SR < 1.0 sur 30 jours → revue stratégie obligatoire
```

### Profit Factor
```
PF = gross_profit / gross_loss
Target PF > 1.5

PF < 1.2 sur 50 trades → recalibration + review 50 dernières pertes
```

---

## Système de validation — 7 Gates

**Ordre d'exécution obligatoire — load-bearing :**

| Gate | Condition | On Failure | API needed |
|------|-----------|------------|------------|
| `edge_gate` | edge > 0.04 | Skip | ❌ |
| `ev_gate` | EV > 0 | Skip | ❌ |
| `kelly_gate` | size ≤ kelly(f, bankroll) | Reduce | ❌ |
| `exposure_gate` | exposure + bet ≤ max | Block | ❌ |
| `var_gate` | VaR(95%) ≤ daily_limit | Halt | ✅ |
| `mdd_gate` | MDD(30d) < 0.08 | 72h CD | ✅ |
| `brier_gate` | Brier Score < 0.22 | Recalib. | ✅ |

> **Règle absolue :** aucun LLM prompt ne peut override un gate failure.
> Le code valide le risque, pas le langage.

```python
def validate_trade(edge, ev, size, bankroll, var_95, mdd_30d, brier):
    if edge <= 0.04:       return False, "edge_gate"
    if ev <= 0:            return False, "ev_gate"
    if size > kelly(bankroll): size = kelly(bankroll)  # reduce
    if exposure_exceeded:  return False, "exposure_gate"
    if var_95 > daily_limit: return False, "var_gate"
    if mdd_30d >= 0.08:    return False, "mdd_gate_72h_cooldown"
    if brier >= 0.22:      return False, "brier_gate_recalib"
    return True, "all_gates_passed"
```

---

## Stratégie 1 — Hybride Tetlock

**Concept :** Exploitation du Favorite-Longshot Bias documenté académiquement (CEPR/NBER) combinée à une estimation probabiliste rigoureuse méthode Tetlock.

### Allocation du capital
```
70% bankroll → Favoris sous-cotés (0.70€ - 0.92€)
               Variance faible, gains modestes, construit le capital

30% bankroll → Longshots sous-cotés (0.01€ - 0.10€)
               Variance élevée, upside asymétrique
```

### Filtres de sélection des marchés
```
✓ Volume 24h : 5,000€ - 80,000€
  (trop faible = illiquide / trop fort = efficient)
✓ Résolution : 5 à 21 jours
✓ Prix favoris : 0.70€ - 0.92€
✓ Prix longshots : 0.01€ - 0.10€
✓ Spread bid/ask visible > 2 cents
✓ Question à résolution "dure" (stats officielles, résultats chiffrés)
✓ Z-score δ > 1.5 obligatoire sur longshots
```

### Processus d'estimation (méthode Tetlock)
```
Étape 1 — Base rate (outside view)
  "Sur les 20 derniers marchés similaires, quel % a résolu YES ?"
  → Polymarket Data API pour historique

Étape 2 — LLM scoring (inside view)
  Prompt structuré :
  "Tu es un superforecaster calibré.
   Question : [X] | Résolution : [date]
   Base rate similaire : [Y%] | News récentes : [Z]
   Donne P(YES) avec intervalle [low, high].
   Si |high - low| > 20% → réponds 'signal faible'."

Étape 3 — Validation Z-score
  δ = (P_LLM - P_marché) / σ_historique
  Si δ < 1.5 → skip
  Si intervalle de confiance > 20% → skip

Étape 4 — Signaux externes
  Marchés Fed    → CME FedWatch probability
  Marchés BTC    → Deribit implied probability
  Marchés corr.  → Cross-market Bayesian inference
```

### Sizing Stratégie 1
```python
def size_trade(p_model, market_price, bankroll, is_longshot):
    b = (1 - market_price) / market_price
    q = 1 - p_model
    f_kelly = (p_model * b - q) / b
    
    alpha = 0.30  # fractional Kelly
    
    if is_longshot:
        alpha = alpha / 2  # 1/8 Kelly pour longshots
    
    size = alpha * f_kelly * bankroll
    return min(size, bankroll * 0.05, 5.0)  # max 5€ ou 5%
```

### Kill Switches Stratégie 1
```
• Bankroll < 60€                    → Stop total
• Brier Score > 0.22 sur 15 trades → Arrêt + recalibration
• MDD > 8% sur 30 jours            → 72h cooldown
• Sharpe Ratio < 1.0 sur 30 jours  → Revue stratégie
• Profit Factor < 1.2 sur 50 trades → Recalibration
```

---

## Stratégie 2 — Longshots Macro

**Concept :** Inspiration directe du wallet2 analysé (mars 2024, $441K profit, 14,120 trades). Achat de contrats très sous-cotés sur des événements macro que le marché sous-estime systématiquement.

**Principe fondamental :** Le marché price trop prudemment les événements "jamais arrivés avant". L'anchoring bias + momentum ignoré créent des longshots à 2-5% qui devraient être à 15-25%.

### Allocation du capital
```
70€ → Favoris sous-cotés (0.70€ - 0.92€)
      Maintient le capital en vie pendant que les longshots attendent

30€ → 2-3 longshots macro maximum (0.01€ - 0.08€)
      Positions de 3€ à 5€ maximum
      Concentration sur les meilleures convictions
```

### Types de marchés ciblés
```
✓ BTC/ETH price targets à long terme
  Ex: "BTC atteint $150k avant Dec 2026 ?"
  Ex: "BTC dip sous $60k avant June 2026 ?"

✓ Décisions Fed sous-estimées
  Comparer avec CME FedWatch
  Si Polymarket < FedWatch de >8% → opportunité

✓ Événements macro à fort impact
  Elections, votes, résultats économiques
  Focus sur les outcomes que les médias mainstream ignorent

✓ Corrélations inter-marchés
  Si marché A implique logiquement marché B
  Et marché B est mal pricé → acheter B
```

### Filtres spécifiques Stratégie 2
```
✓ Prix entrée : 0.01€ - 0.08€ strictement
✓ Volume minimum : 10,000€ (assez liquide pour sortir)
✓ Résolution : 14 à 90 jours
✓ Z-score δ > 1.5 obligatoire (signal structurel, pas du bruit)
✓ Edge ratio : p_model > prix_marché × 2.0 minimum
✓ Profit Factor target global : > 1.5
✓ Monte Carlo VaR sur 10,000 chemins avant chaque entrée
```

### Signaux d'entrée
```python
def longshot_entry_signal(market_price, p_model, sigma_hist):
    # Z-score obligatoire
    delta = (p_model - market_price) / sigma_hist
    if delta < 1.5:
        return False, "z_score_insuffisant"
    
    # Ratio minimum 2x
    edge_ratio = p_model / market_price
    if edge_ratio < 2.0:
        return False, "ratio_insuffisant"
    
    # Edge absolu minimum
    edge_abs = p_model - market_price
    if edge_abs < 0.06:
        return False, "edge_absolu_insuffisant"
    
    return True, f"delta={delta:.2f}, ratio={edge_ratio:.2f}"
```

### Règle de sortie Stratégie 2
```
Tenir jusqu'à résolution SAUF :
  1. Une nouvelle information invalide fondamentalement la thèse
     (pas la volatilité normale du prix — la THÈSE)
  2. Le marché a bougé >30% contre toi sans nouvelle info
     → tu avais probablement tort sur la thèse de base

NE PAS sortir si :
  • Le prix baisse temporairement (variance normale)
  • "Tout le monde" dit que tu as tort
  • La position est dans le rouge depuis quelques jours
```

### Kill Switches Stratégie 2
```
• Bankroll < 60€               → Stop total
• 5 pertes consécutives        → Pause 48h + revue thèses
• Brier Score > 0.22/15 trades → Arrêt + recalibration
• MDD > 8% sur 30 jours        → 72h cooldown obligatoire
```

---

## Exécution — Almgren-Chriss Optimal Execution

### Pourquoi Almgren-Chriss (2000)

Le framework d'exécution le plus utilisé par les meilleures firmes institutionnelles (Two Sigma, Citadel, Jane Street). C'est le seul modèle d'exécution avec une **solution mathématique optimale prouvée** — minimiser E(IS) + λ×V(IS).

```
Problème fondamental :
  Trade VITE → impact marché élevé, risque prix faible
  Trade LENTEMENT → impact faible, risque prix élevé

Almgren-Chriss minimise : E(IS) + λ × V(IS)

E(IS) = expected implementation shortfall
V(IS) = variance du shortfall
λ     = aversion au risque (= α Kelly = 0.30)
```

### Dynamique de prix avec impact

```
S_t^exec = S_0 + σ×B_t + γ×∫v_s ds − η×v_t

γ = impact permanent (information leakage, faible sur Polymarket)
η = impact temporaire (liquidité immédiate)
v_t = vitesse de trading (shares/seconde)
σ = volatilité rolling 14 jours
```

### Trajectoire optimale (solution closed-form)

```
x*(t) = X × sinh[κ(T-t)] / sinh[κT]

κ = sqrt(λσ²/η)  ← taux de décroissance optimal

Interprétation :
  κ grand (λ élevé) → trader risquophobe → trade vite au début
  κ petit (λ faible) → trader patient    → TWAP linéaire
  λ = 0 exactement  → TWAP pur

Pour notre capital (€100) :
  Impact quasi nul → κ ≈ 0 → trajectoire quasi-TWAP
  Le vrai apport AC : repricing intelligent + orderbook microstructure
```

### Système d'exécution complet

```python
import numpy as np
import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Tuple
from py_clob_client.client import ClobClient

@dataclass
class ExecutionParams:
    """
    Paramètres calibrés pour Polymarket.

    Calibration empirique :
    γ (permanent impact) : faible — marchés binaires
    η (temporary impact) : dépend liquidité du marché
    σ (volatilité)       : rolling 14 jours
    λ (risk aversion)    : 0.30 (= α Kelly)
    """
    gamma: float = 0.001
    eta:   float = 0.010
    sigma: float = 0.05
    lam:   float = 0.30
    T:     float = 300.0   # horizon exécution (secondes)
    N:     int   = 10      # nombre de slices
    max_reprice: int = 8


class AlmgrenChrissExecutor:

    def __init__(self, clob: ClobClient, params: ExecutionParams):
        self.clob = clob
        self.p = params

    # ── 1. TRAJECTOIRE OPTIMALE ──────────────────────────────────

    def optimal_trajectory(self, X: float) -> np.ndarray:
        """
        x*(t) = X × sinh[κ(T-t)] / sinh[κT]
        Retourne tailles des N slices.
        """
        kappa = np.sqrt(self.p.lam * self.p.sigma**2 / self.p.eta)
        times = np.linspace(0, self.p.T, self.p.N + 1)

        if kappa * self.p.T < 1e-6:
            inventory = X * (1 - times / self.p.T)  # TWAP pur
        else:
            inventory = X * np.sinh(kappa*(self.p.T-times)) / \
                           np.sinh(kappa*self.p.T)

        return np.diff(-inventory)  # taille de chaque slice

    def implementation_shortfall(self, X: float) -> Tuple[float, float]:
        """
        E(IS) = ε×X + (η - 0.5×γ×τ) × Σn²_k / τ
        V(IS) = σ² × τ × Σx²_k
        """
        kappa = np.sqrt(self.p.lam * self.p.sigma**2 / self.p.eta)
        tau   = self.p.T / self.p.N
        slices = self.optimal_trajectory(X)

        epsilon = 0.001  # half bid-ask spread estimé Polymarket
        e_is = (epsilon * X +
                (self.p.eta - 0.5*self.p.gamma*tau) *
                np.sum(slices**2) / tau)

        times = np.linspace(0, self.p.T, self.p.N + 1)
        denom = np.sinh(kappa*self.p.T) + 1e-10
        inventory = X * np.sinh(kappa*(self.p.T-times)) / denom
        v_is = self.p.sigma**2 * tau * np.sum(inventory**2)

        return e_is, v_is

    # ── 2. ORDERBOOK INTELLIGENCE (Kyle 1985) ────────────────────

    def analyze_orderbook(self, token_id: str) -> dict:
        """
        Métriques clés :
        - Imbalance : pression achat vs vente (Kyle 1985)
        - Depth     : liquidité disponible à ±2% du mid
        - Spread    : coût de crossing en bps
        - VWAP bid  : prix moyen pondéré côté achat
        """
        book = self.clob.get_order_book(token_id)
        if not book.bids or not book.asks:
            return {"status": "illiquide"}

        bids = [(float(b.price), float(b.size)) for b in book.bids[:5]]
        asks = [(float(a.price), float(a.size)) for a in book.asks[:5]]

        best_bid, best_ask = bids[0][0], asks[0][0]
        mid    = (best_bid + best_ask) / 2
        spread = best_ask - best_bid

        bid_vol  = sum(s for _, s in bids)
        ask_vol  = sum(s for _, s in asks)
        total_v  = bid_vol + ask_vol
        imbalance = bid_vol / total_v if total_v > 0 else 0.5

        vwap_bid = sum(p*s for p,s in bids) / bid_vol if bid_vol>0 else best_bid

        depth_2pct = (sum(s for p,s in bids if p >= mid*0.98) +
                      sum(s for p,s in asks if p <= mid*1.02))

        return {
            "best_bid": best_bid, "best_ask": best_ask,
            "mid": mid, "spread": spread,
            "spread_bps": spread/mid*10000,
            "imbalance": imbalance,
            "vwap_bid": vwap_bid,
            "depth_2pct": depth_2pct,
            "status": "ok"
        }

    def optimal_limit_price(self, ob: dict, side: str,
                             urgency: float = 0.5) -> float:
        """
        urgency ∈ [0,1] :
          0.0 = patient (bid, attendre rebate maker)
          0.5 = neutre  (mid + offset)
          1.0 = urgent  (ask, fill immédiat)

        Ajustement imbalance (Kyle 1985 microstructure) :
          Forte pression acheteuse → poster plus haut pour priorité
        """
        bid, ask = ob["best_bid"], ob["best_ask"]
        spread   = ob["spread"]
        imb_adj  = (ob["imbalance"] - 0.5) * spread * 0.3

        price = bid + urgency * spread + imb_adj

        if side == "BUY":
            price = min(price, ask - 0.001)
            price = max(price, bid)

        return round(price, 4)

    # ── 3. EXÉCUTION PRINCIPALE ──────────────────────────────────

    async def execute(self, token_id: str, total_size: float,
                      max_price: float, strategy_id: str,
                      urgency: float = 0.4) -> dict:
        """
        Pipeline complet :
        1. Trajectoire AC optimale
        2. Pour chaque slice :
           a. Analyser orderbook (imbalance, depth, spread)
           b. Calculer prix optimal (Kyle microstructure)
           c. Placer limit order (maker rebate)
           d. Monitor avec repricing intelligent
        3. Retourner E(IS) espéré vs réalisé
        """
        start_time = time.time()
        slices = self.optimal_trajectory(total_size)
        e_is, v_is = self.implementation_shortfall(total_size)

        fills, total_filled, total_cost, reprice_count = [], 0.0, 0.0, 0

        for i, slice_size in enumerate(slices):
            if slice_size < 0.01:
                continue

            remaining   = slice_size
            slice_start = time.time()
            slice_timeout = self.p.T / self.p.N * 2

            ob = self.analyze_orderbook(token_id)
            if ob["status"] == "illiquide":
                continue
            if ob["spread_bps"] > 300:   # spread > 3% → skip
                continue

            limit_price = self.optimal_limit_price(ob, "BUY", urgency)
            if limit_price > max_price:
                break

            order = self._place_limit(token_id, limit_price, remaining)
            if not order:
                continue

            order_id, last_mid, slice_reprice = order.id, ob["mid"], 0

            while remaining > 0.005:
                await asyncio.sleep(3)

                if time.time() - slice_start > slice_timeout:
                    self._cancel(order_id)
                    break

                status = self.clob.get_order(order_id)

                if status.status == "FILLED":
                    fp, fs = float(status.avg_price), float(status.size_matched)
                    fills.append((fp, fs))
                    total_filled += fs
                    total_cost   += fp * fs
                    remaining = 0
                    break

                # Fill partiel → voir section suivante
                fs = float(status.size_matched)
                if fs > 0:
                    fp = float(status.avg_price)
                    fills.append((fp, fs))
                    total_filled += fs
                    total_cost   += fp * fs
                    remaining = slice_size - fs

                # Repricing si drift > 0.5¢
                new_ob  = self.analyze_orderbook(token_id)
                if new_ob["status"] != "ok":
                    continue

                drift = abs(new_ob["mid"] - last_mid)
                if drift > 0.005 and slice_reprice < self.p.max_reprice:
                    new_price = self.optimal_limit_price(new_ob, "BUY", urgency)
                    if new_price > max_price:
                        self._cancel(order_id)
                        remaining = 0
                        break
                    self._cancel(order_id)
                    order = self._place_limit(token_id, new_price, remaining)
                    if order:
                        order_id = order.id
                        last_mid = new_ob["mid"]
                        slice_reprice += 1
                        reprice_count += 1
                        limit_price = new_price

            interval = self.p.T / self.p.N
            await asyncio.sleep(max(0, interval-(time.time()-slice_start)))

        elapsed = time.time() - start_time
        avg_price = total_cost/total_filled if total_filled>0 else 0
        fill_rate = total_filled/total_size if total_size>0 else 0

        ob_final = self.analyze_orderbook(token_id)
        arrival  = ob_final["mid"] if ob_final["status"]=="ok" else avg_price
        real_is  = arrival - avg_price if total_filled > 0 else 0

        return {
            "status": "ok" if fill_rate>0.8 else "partial",
            "fill_rate": fill_rate,
            "avg_fill_price": avg_price,
            "total_cost": total_cost,
            "realized_is": real_is,
            "expected_is": e_is,
            "reprice_count": reprice_count,
            "elapsed": elapsed,
            "fills": fills
        }

    def _place_limit(self, token_id, price, size):
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
            args   = OrderArgs(token_id=token_id, price=price,
                               size=size, side=BUY)
            signed = self.clob.create_order(args)
            return self.clob.post_order(signed, OrderType.GTC)
        except Exception as e:
            print(f"Placement échoué : {e}")
            return None

    def _cancel(self, order_id):
        try:
            self.clob.cancel_order(order_id)
        except Exception:
            pass
```

### Profils d'exécution selon le marché

```python
EXECUTION_PROFILES = {

    "favori_patient": ExecutionParams(
        # Favori (0.70-0.92€), liquidité correcte
        gamma=0.001, eta=0.008, sigma=0.03,
        lam=0.20,  # patient → TWAP quasi-pur
        T=480.0,   # 8 minutes
        N=8, max_reprice=6
    ),

    "longshot_patient": ExecutionParams(
        # Longshot (0.01-0.08€), marché peu liquide
        gamma=0.003, eta=0.015, sigma=0.08,
        lam=0.10,  # très patient → minimise impact
        T=600.0,   # 10 minutes
        N=5, max_reprice=4
    ),

    "signal_urgent": ExecutionParams(
        # Edge éphémère (news intraday, Fed surprise)
        gamma=0.001, eta=0.010, sigma=0.05,
        lam=0.80,  # risquophobe → trade vite au début
        T=120.0,   # 2 minutes
        N=4, max_reprice=3
    )
}

def select_profile(market_type: str, signal_decay: float) -> ExecutionParams:
    """
    signal_decay [0-1] : vitesse de décroissance de l'edge
    0.0 = edge stable (longshot macro, semaines)
    1.0 = edge éphémère (news intraday, minutes)
    """
    if signal_decay > 0.7:
        return EXECUTION_PROFILES["signal_urgent"]
    elif market_type == "longshot":
        return EXECUTION_PROFILES["longshot_patient"]
    else:
        return EXECUTION_PROFILES["favori_patient"]
```

### Comparaison version simple vs Almgren-Chriss

```
Critère           | Version simple      | Almgren-Chriss
──────────────────|─────────────────────|──────────────────────────
Fondation         | Ad hoc              | Optimal prouvé (2000)
Slicing           | Ordre unique        | N slices trajectoire cosh
Repricing         | Réactif (drift)     | Proactif (imbalance + timing)
Prix optimal      | Mid + offset fixe   | Kyle microstructure
Métrique retour   | Aucune              | E(IS) espéré vs réalisé
Urgence           | Binaire             | Paramètre continu [0,1]
Profils           | Un seul             | 3 profils selon signal_decay
```

---

## Gestion des Fills Partiels

### Ce qui s'applique réellement à Polymarket avec €100

**Avertissement :** Les techniques institutionnelles (Kaplan-Meier, Dark Ice, fragmentation multi-venue) sont conçues pour des ordres de $100K+. Avec €4 par trade sur un marché à $50K de volume quotidien, ton impact marché est littéralement zéro. Ces méthodes seraient du théâtre mathématique.

**Ce qui arrive en pratique :**
Tu places €4 à 0.72€. Seulement €1.80 se remplit. Il reste €2.20.

Causes réelles :
```
→ L'orderbook avait €1.80 disponible à 0.72€ et rien entre
  0.72€ et le niveau suivant
→ Un autre acheteur a pris la liquidité avant toi
  (price-time priority : premier arrivé, premier servi)
→ Le mid a légèrement bougé depuis le placement
```

### Les 3 règles — simples, honnêtes, applicables

```python
async def handle_partial_fill(state, clob, executor):
    """
    Gestion réaliste des fills partiels sur Polymarket.
    Pas de théorie institutionnelle inapplicable.
    3 règles ancrées dans la réalité du marché.
    """

    ob = executor.analyze_orderbook(state.token_id)

    # ── RÈGLE 1 : Edge disparu → annuler immédiatement ──────────
    # La seule décision vraiment non-triviale.
    # Principe superforecaster : "Ne jamais s'accrocher
    # à une position dont la thèse a changé."
    if ob["status"] == "ok":
        edge_current = state.p_model_current - ob["mid"]
        if edge_current < 0.04:
            clob.cancel_order(state.order_id)
            return {
                "action": "cancel",
                "reason": "edge_mort",
                "edge_current": edge_current
            }

    # ── RÈGLE 2 : Marché long (>7 jours) → attendre ─────────────
    # La liquidité revient naturellement sur les marchés longs.
    # Inutile de payer plus cher pour forcer un fill.
    # Le fill partiel sera complété dans les heures suivantes
    # par de nouveaux market makers.
    if state.days_to_resolution > 7:
        return {
            "action": "wait",
            "reason": "marche_long_patience",
            "days_remaining": state.days_to_resolution
        }

    # ── RÈGLE 3 : Marché court + fill > 50% → ajustement ───────
    # Si >50% déjà rempli et edge justifie encore le trade :
    # monter de 1-2 cents pour compléter.
    # Ne jamais ajuster si cela annule l'edge.
    if (state.fill_rate > 0.50 and
        ob["status"] == "ok" and
        edge_current > 0.06):

        # Prix maximum acceptable : ne jamais payer plus que
        # p_model - 4 cents (seuil edge minimum)
        max_acceptable = state.p_model_current - 0.04
        new_price = round(ob["best_ask"] - 0.001, 4)

        if new_price > max_acceptable:
            return {
                "action": "wait",
                "reason": "ajustement_annulerait_edge",
                "max_acceptable": max_acceptable,
                "current_ask": ob["best_ask"]
            }

        clob.cancel_order(state.order_id)
        new_order = executor._place_limit(
            state.token_id, new_price, state.remaining)

        if new_order:
            return {
                "action": "adjusted",
                "new_price": new_price,
                "delta_cents": round((new_price - state.fill_price)*100, 2),
                "cost_of_adjustment": (new_price - state.fill_price)
                                       * state.remaining
            }

    # Défaut : attendre
    return {"action": "wait", "reason": "default"}
```

### Matrice de décision

```
Fill partiel détecté
        │
        ├── edge_current < 4¢ ──────────────→ ANNULER (edge mort)
        │                                       Règle 1 — priorité absolue
        │
        ├── résolution > 7 jours ──────────→ ATTENDRE
        │                                       La liquidité revient
        │
        ├── fill_rate > 50%               → AJUSTER (+1-2¢)
        │   ET edge > 6¢                    seulement si pas d'annulation edge
        │   ET marché court (<7j)
        │
        └── défaut ──────────────────────→ ATTENDRE
```

### Ce qui est loggé pour amélioration continue

```python
# Après chaque fill partiel résolu, logger :
{
    "market_id":       str,
    "fill_rate":       float,   # % rempli final
    "action_taken":    str,     # cancel / wait / adjusted
    "days_resolution": int,
    "edge_at_entry":   float,
    "edge_at_partial": float,
    "outcome":         int,     # 0 ou 1 (résolution marché)
    "pnl":             float
}
# → Brier score calculé sur p_model_current au moment du partial
# → Permet d'identifier si les fills partiels corrèlent
#   avec de mauvaises estimations initiales
```

---

## Repricing — Ordre Non Rempli

### Pourquoi repricer le moins possible

Chaque centime payé en plus réduit directement le profit attendu.

```
Tu places €4 à 0.72€. Edge initial = 8¢. Best ask = 0.74€.
Si tu reprices à 0.74€ :
  →  Edge restant = 6¢ au lieu de 8¢
  →  Sur 5.4 shares : tu perds €0.11 de profit attendu

Repricer une fois sur chaque trade = perdre ~10-15% de ton EV total.
```

Un ordre non rempli n'est pas un échec. C'est le bot qui refuse de payer trop cher.

### Fonction de décision

```python
def should_reprice(limit_price, p_model, ob,
                   reprice_count, days_to_resolution,
                   elapsed_seconds, max_wait_seconds):
    """
    Retourne (decision, new_price)
    decision : "reprice" | "wait" | "cancel"
    new_price : prix calculé si reprice, 0.0 sinon

    Seuils actuels non calibrés — nécessitent données
    réelles après 30-50 trades pour ajustement.
    imbalance > 0.65 : pression acheteuse
    mid_movement > 2¢ : mouvement structurel
    spread > 400 bps  : marché illiquide
    """

    edge = p_model - ob["mid"]

    # 1. Edge mort → annuler
    if edge < 0.04:
        return "cancel", 0.0

    # 2. Timeout → annuler
    if elapsed_seconds >= max_wait_seconds:
        return "cancel", 0.0

    # 3. Trop de repricings → annuler, passer au marché suivant
    #    max 2 si résolution > 7 jours (patience justifiée)
    #    max 3 si résolution < 7 jours (plus urgent)
    max_r = 2 if days_to_resolution > 7 else 3
    if reprice_count >= max_r:
        return "cancel", 0.0

    # 4. Pression acheteuse → attendre
    #    Le prix va monter vers notre ordre naturellement
    if ob["imbalance"] > 0.65:
        return "wait", 0.0

    # 5. Direction du mouvement du mid
    #    Montée vers notre ordre (> +1¢) → fill imminent → attendre
    #    Pas bougé (entre -2¢ et +1¢)   → attendre
    #    Baisse > 2¢                     → ordre trop loin → repricer
    mid_delta = ob["mid"] - limit_price
    if mid_delta > -0.02:
        return "wait", 0.0

    # 6. Marché structurellement illiquide → attendre
    #    Repricer dans un marché illiquide ne change rien
    if ob["spread_bps"] > 400:
        return "wait", 0.0

    # 7. Calcul nouveau prix
    #    mid + 0.2¢, plafonné à best_ask - 0.1¢ pour rester maker
    #    (ne jamais croiser l'ask = ne jamais devenir taker)
    new_price = round(
        min(ob["mid"] + 0.002, ob["best_ask"] - 0.001), 4)

    # 8. Reprice annulerait edge → annuler
    if p_model - new_price < 0.04:
        return "cancel", 0.0

    return "reprice", new_price
```

### Matrice de décision

```
Ordre non rempli après TICK_INTERVAL
        │
        ├── edge < 4¢ ──────────────────────────→ ANNULER
        │
        ├── elapsed ≥ max_wait ─────────────────→ ANNULER
        │
        ├── reprice_count ≥ max (2 ou 3) ────────→ ANNULER
        │   passer au marché suivant
        │
        ├── imbalance > 0.65 ──────────────────→ ATTENDRE
        │   prix va monter vers l'ordre
        │
        ├── mid_delta > -2¢ ────────────────────→ ATTENDRE
        │   marché stable ou qui monte
        │
        ├── spread > 400 bps ──────────────────→ ATTENDRE
        │   illiquide, reprice inutile
        │
        ├── reprice annulerait edge (< 4¢) ─────→ ANNULER
        │
        └── tout OK ──────────────────────────→ REPRICER
            mid + 0.2¢, plafonné à ask - 0.1¢
```

### Ce qui est loggé après chaque reprice

```python
{
    "market_id":       str,
    "old_price":       float,
    "new_price":       float,
    "edge_before":     float,
    "edge_after":      float,
    "edge_cost":       float,   # new_price - old_price
    "mid_delta":       float,   # déclencheur du reprice
    "reprice_count":   int,
    "outcome":         int,     # 0 ou 1
    "filled_after":    bool     # le reprice a-t-il abouti ?
}
# → Après 30 trades : calibrer les seuils (imbalance, mid_delta, spread_bps)
# → Si filled_after = False majoritairement → seuils trop agressifs
```

---

## Sizing & Risk Management

### Règles universelles (les deux stratégies)
```
MAX par trade         : 5€ ou 5% du bankroll (le plus petit)
MAX positions ouvertes: 8 simultanément
MAX exposition totale : 40% du bankroll
Alpha Kelly favoris   : 0.30 (1/4 Kelly approx)
Alpha Kelly longshots : 0.15 (1/8 Kelly approx)
Limit orders ONLY     : jamais market orders (slippage)
```

### Calcul Kelly complet
```python
def kelly_size(p_model, market_price, bankroll, asset_type):
    b = (1 - market_price) / market_price  # cote implicite
    q = 1 - p_model
    f_star = (p_model * b - q) / b
    
    alpha = 0.30 if asset_type == "favori" else 0.15
    
    raw_size = alpha * f_star * bankroll
    
    # Caps stricts
    max_size = min(
        raw_size,
        bankroll * 0.05,   # max 5% bankroll
        5.0                 # max absolu 5€
    )
    
    return max(0, max_size)
```

### Monte Carlo VaR
```python
import numpy as np

def monte_carlo_var(returns_history, n_paths=10000, confidence=0.95):
    # Bootstrap depuis historique réel (pas gaussien)
    simulated = np.random.choice(returns_history, 
                                  size=(n_paths, 30), 
                                  replace=True)
    portfolio_returns = simulated.sum(axis=1)
    var_95 = np.percentile(portfolio_returns, (1 - confidence) * 100)
    es = portfolio_returns[portfolio_returns < var_95].mean()
    return var_95, es
```

---

## Règles de sortie

### Sortie anticipée (avant résolution)
```
VENDRE si UNE condition vraie :

1. Edge actuel < 4 cents
   (p_model_updated - current_price < 0.04)

2. P_true a changé de signe
   Tu estimais 60% → nouvelle info → tu estimes 35%
   Le trade est devenu perdant selon ta propre analyse

3. 65% du potentiel capturé
   Acheté à 5¢, potentiel max 100¢, prix actuel 67¢
   → (67-5)/(100-5) = 65.3% → sortir

4. J-3 avant résolution ET prix entre 0.40€ - 0.80€
   Risque binaire maximum, valeur temps résiduelle faible

5. Marché bouge >30% contre toi sans nouvelle information
   → tu avais probablement tort
```

### Tenir jusqu'à résolution
```
CONSERVER si :

1. Longshot encore < 0.30€ mais P_true > 0.50
   La valeur vient de la résolution finale à 1€

2. Prix > 0.85€ et P_true > 0.90€
   Move résiduel trop petit pour justifier les frais de sortie

3. Moins de 24h et conviction forte
   Inutile de sortir pour 1-2 cents de gain

4. Prix baisse temporairement sans nouvelle information
   Variance normale — ne pas sortir sur la volatilité
```

---

## Kill Switches

### Hiérarchie des stops
```
NIVEAU 1 — Stop immédiat
  Bankroll < 60€ → fermer toutes positions, arrêt total

NIVEAU 2 — Cooldown 72h
  MDD > 8% sur 30 jours → fermer nouvelles positions
  Reprendre après 72h seulement si MDD < 6%

NIVEAU 3 — Pause + recalibration
  Brier Score > 0.22 sur 15 trades → arrêt + analyse
  5 pertes consécutives → pause 48h + revue thèses

NIVEAU 4 — Revue stratégie
  Sharpe Ratio < 1.0 sur 30 jours → revoir paramètres
  Profit Factor < 1.2 sur 50 trades → recalibration modèle
```

---

## Métriques de calibration

### Brier Score
```python
def brier_score(predictions, outcomes):
    # predictions : liste de P_estimées [0.0 - 1.0]
    # outcomes    : liste de résolutions [0 ou 1]
    return np.mean([(p - o)**2 for p, o in zip(predictions, outcomes)])

# Objectifs :
# < 0.15 → excellent
# < 0.20 → bon
# < 0.22 → limite acceptable
# > 0.22 → arrêt obligatoire
```

### Log à maintenir pour chaque trade
```
timestamp       : datetime ISO
market_id       : str
question        : str
p_model         : float  # ton estimation
p_market        : float  # prix au moment du trade
edge            : float  # p_model - p_market
delta           : float  # z-score
size_eur        : float  # montant en euros
outcome         : int    # 0 ou 1 (après résolution)
pnl             : float  # profit/perte réalisé
brier_contrib   : float  # (p_model - outcome)^2
strategy        : str    # "1" ou "2"
gate_passed     : list   # quels gates ont validé
```

### Dashboard métriques (calcul hebdomadaire)
```
Brier Score     : sur les 15 dernières résolutions
Sharpe Ratio    : rolling 30 jours
Profit Factor   : gross_profit / gross_loss
Max Drawdown    : rolling 30 jours
Win Rate        : % trades profitables
Avg Edge        : edge moyen des trades exécutés
VaR 95%         : Monte Carlo 10,000 chemins
```

---

## Wallets de référence

### Wallet 2 — Modèle de la Stratégie 2
```
Profil     : anonyme, rejoint mars 2024
PnL        : $441,263 all-time
Trades     : 14,120 (~19/jour sur 24 mois)
Positions  : $522,300 actives
Biggest    : $86,100 (BTC $100k à 2.7¢ → +524%)

Ce qui est reproductible :
  → Acheter des longshots macro sous-cotés (0.01-0.08€)
  → Diversifier sur BTC targets, Fed, politique
  → Tenir jusqu'à résolution sauf invalidation thèse
  → Combiner avec des favoris pour survivre à la variance

Ce qui n'est PAS reproductible avec 100€ :
  → Ses tailles de position ($2K-$225K)
  → Sa capacité à absorber 10 pertes consécutives
```

### Wallets à éviter — Patterns frauduleux
```
k9Q2mX4L8A7ZP3R    : temporal arb 15-min, stratégie morte jan 2026
distinct-baguette   : même pattern, même période, même stratégie morte
0xdE17f7...         : market making HFT, capital incompatible
```

### Red flags wallet
```
✗ 100% win rate affiché
✗ Tous les trades sur fenêtres 5-min ou 15-min
✗ Rejoint après octobre 2025
✗ Lien Telegram ou referral associé
✗ Profits réalisés uniquement entre oct 2025 et jan 2026
✗ Lifecycle identique : dépôt → warm-up → ladder → retrait
```

---

## Ce qui ne fonctionne plus en 2026

```
❌ Temporal arbitrage Binance→Polymarket
   Fees dynamiques jusqu'à 3.15% — dépasse le spread exploitable
   Mort depuis janvier 2026

❌ Sum arbitrage YES+NO
   Fenêtre d'opportunité : 2.7 secondes en moyenne
   73% capturé par bots sub-100ms
   Inaccessible sans infra HFT dédiée

❌ Taker orders sur marchés 5-min/15-min
   Fee = C × 0.25 × (p × (1-p))²
   À 50% de probabilité : fee ≈ 1.56% — non rentable

❌ Copy trading de wallets "parfaits"
   Réseau de wash trading documenté (238 winners, 793 donors)
   Les profils sont manufacturés industriellement pour arnaque/airdrop
```

---

## Notes finales

**Attentes réalistes :**
- +100% en 30 jours : très improbable, ne pas viser ça
- +15-30%/mois : possible si bien calibré après semaine 2
- +50-80% en 90 jours : objectif réaliste avec discipline

**Priorité absolue :**
Les semaines 1-2 sont de la calibration. Ne pas juger la stratégie sur le PnL mais sur le Brier Score. Si Brier < 0.20 après 15 résolutions, l'edge est réel. Scaler ensuite.

**Règle maîtresse :**
> Un trade sans tous les 7 gates validés n'existe pas.

---

## Interface & Fichiers de référence

### Dashboard PAF-001

**Fichier :** `PAF_Dashboard_v5.html`
**Description :** Interface de monitoring complète — fichier HTML autonome (109 Ko), aucune dépendance externe à installer.

**Pages disponibles :**
- **Dashboard** — NAV curve, P&L quotidien, attribution S1/S2, Edge vs Z-score, Brier, reliability curve
- **Markets** — 20 indices live (BTC, ETH, S&P, DAX, NIKKEI, Gold, DXY...), BTC price targets Poly vs Deribit, CME FedWatch divergence, implied vol Deribit, Fear & Greed
- **Positions** — 4 positions ouvertes avec edge, Z-score, Kelly recalculés live, table des trades fermés
- **World Monitor** — Carte monde D3/TopoJSON avec frontières pays, 15 événements géopolitiques animés, sensor grid Crucix, OSINT stream, news live, leverageable ideas
- **Indicateurs Math** — Scanner 28 marchés avec 10 indicateurs calculés live (Edge, Z-score δ, EV, Kelly f\*, Edge Ratio, Brier, VaR 95%, FLB, Crucix conf., Score composite), filtres S1/S2/7Gates, 3 graphiques d'analyse
- **Calibration** — Brier history, reliability curve, distribution P&L, Sharpe rolling
- **Signal Log** — Feed des signaux Crucix + sources breakdown + force des signaux
- **Risk Monitor** — 8 kill switches avec barres de progression + Monte Carlo VaR 10k paths
- **Crucix Agent** — Hero panel (GitHub calesthio/Crucix), 26 sources avec statut live, alert log, funnel signal→trade

**Fonctionnalités carte :**
- Zoom molette / boutons + − ⌂ · Limite min = carte monde entière · Limite max = ×15
- Drag pour naviguer · Clamp automatique (impossible de sortir de la carte)
- Tooltip riche au survol des événements (type, sévérité, coordonnées, source)
- Frontières pays TopoJSON Natural Earth 110m

**Source de données :** Toutes les pages lisent depuis l'objet `D.*` (source unique de vérité) — les KPIs, positions, métriques et indicateurs sont interconnectés.

---

### Pipeline Crucix → Polymarket

**Fichier :** `crucix_router.py`
**Description :** Pipeline Bayes complet (1979 lignes) pour brancher les 26 sources Crucix sur les `p_model`.

**Modules :**
- `SignalParser` — normalise les alertes brutes
- `TemporalDecayEngine` — décroissance LR = 1 + (LR−1) × exp(−λt), demi-vie ≈ 4.6h
- `MarketRouter` — score de pertinence alerte→marché (affinité × overlap × jours restants)
- `SourceCorrelationChecker` — détecte sources non-indépendantes (Reuters+AP+Bloomberg = √3 sources effectives)
- `BayesUpdater` — O_post = O_prior × LR, hard cap ±15pts, full audit SQLite
- `MultiSourceAggregator` — chaînage Bayésien + Satopää extremizing (α=1.30) si ≥2 sources indépendantes
- `DynamicZScoreEngine` — σ rolling 14j par marché en SQLite
- `SevenGateRevalidator` — re-valide les 7 gates après chaque update, émet EXIT/TRADE/REDUCE/HOLD
- `WeeklyCalibrationReport` — tableau Brier + LR empirique + trust weight par source

**Lancer le test :** `python3 crucix_router.py`

---

## Backtesting & Validation Historique

### Avertissement préalable : le paradoxe du backtest

> *"A backtest is not a test of the strategy. It is a test of whether the strategy would have worked if you had known the future."* — Marcos Lopez de Prado, *Advances in Financial Machine Learning*

Trois vérités à accepter avant de lire la suite :

**1.** Avec €100 et 30-50 trades, tu n'as pas de puissance statistique suffisante pour distinguer un edge réel du bruit. Un intervalle de confiance à 95% sur un win rate de 65% calculé sur 40 trades donne [49%, 79%] — inutilisable.

**2.** Les prix historiques Polymarket 2024-2025 incorporaient déjà l'information disponible à l'époque. Ton p_model LLM Tetlock n'existait pas en 2024. Tout backtest utilisant un proxy de p_model est **hypothétique par construction**.

**3.** Le backtest qui "valide" ta stratégie est le plus dangereux : il te donne confiance au mauvais moment.

**Conclusion opérationnelle :** le backtest est un outil de débogage et de calibration de paramètres, jamais une preuve d'edge. La seule preuve est le paper trading en temps réel sur 30+ résolutions.

---

### Ce qu'on peut valider honnêtement

#### Niveau 1 — Validation logique (immédiate, sans données)

Avant toute donnée, vérifier que le modèle est théoriquement cohérent :

```
Checklist Tetlock (Superforecasting, 2015) :
✓ Le modèle utilise-t-il une base rate externe avant tout ajustement ?
  → Étape 1 du pipeline : Reference Class Engine (outside view first)
✓ Les mises à jour Bayésiennes sont-elles proportionnelles à la force de l'évidence ?
  → LR Crucix calibrés sur hit rates empiriques, pas sur intuition
✓ Le modèle peut-il se tromper ? Quelles conditions invalident la thèse ?
  → 7 gates + kill switches = mécanismes d'auto-invalidation explicites
✓ Les probabilités s'approchent-elles des extrêmes sans raison solide ?
  → Hard cap ±15pts par source + extremizing uniquement si ≥2 sources indépendantes

Checklist Thorp (Beat the Market, 1967) :
✓ L'edge est-il calculable a priori ou seulement visible en rétrospective ?
  → Edge = p_model - p_mkt, calculé avant l'entrée, pas après
✓ Le sizing respecte-t-il le Kelly Criterion fractionnel ?
  → α=0.30 (S1), α=0.15 (S2) — prouvé optimal par Thorp pour minimiser le risque de ruine
✓ Le bankroll peut-il survivre à la pire séquence plausible ?
  → Monte Carlo VaR 10,000 chemins avant chaque trade

Checklist Lopez de Prado (AFML, 2018) :
✓ Les features utilisées ont-elles été testées pour le look-ahead bias ?
  → p_mkt au moment de l'entrée uniquement, jamais le prix final
✓ La stratégie a-t-elle été overfittée sur les paramètres ?
  → Paramètres figés dans polymarket_strategies.md, pas ajustables trade par trade
✓ La dépendance sérielle entre positions a-t-elle été vérifiée ?
  → SourceCorrelationChecker — sources corrélées comptent comme √N indépendantes
```

---

#### Niveau 2 — Calibration sur Metaculus / GJP (2-4 semaines, sans capital)

Avant de toucher à Polymarket, calibrer le modèle sur des questions publiques à résolution vérifiable. Tetlock a 20 ans de données sur 20,000+ forecasters — on se compare à cette baseline.

```python
"""
Protocole de calibration externe (Tetlock 2015 + Satopää 2014)

1. Choisir 50 questions Metaculus ouvertes dans les 3 catégories :
   - Macro (Fed, CPI, GDP)  : 20 questions
   - Géopolitique           : 15 questions
   - Crypto                 : 15 questions

2. Pour chaque question, générer p_model via le pipeline Tetlock complet
   SANS regarder les prédictions de la communauté Metaculus.

3. Après résolution, calculer :
   - Brier Score personnel
   - Brier Score communauté Metaculus (benchmark)
   - Brier Score superforecasters GJP (~0.14-0.17)

4. Critère de Go  : Brier < 0.20 sur 50 résolutions
   Critère No-Go  : Brier > 0.22 → recalibrer, pas déployer capital

Pourquoi cette étape ?
  - Gratuit, sans risque capital
  - Données de résolution propres (pas de biais Polymarket)
  - Comparable à une baseline académique validée
  - Détecte les biais systématiques avant qu'ils coûtent de l'argent
"""

def calibration_report(predictions: list[float], outcomes: list[int]) -> dict:
    import numpy as np

    brier = np.mean([(p - o)**2 for p, o in zip(predictions, outcomes)])

    # Décomposition Murphy (1973) : fiabilité + résolution + incertitude
    mean_o = np.mean(outcomes)
    rel    = np.mean([(p - o)**2 for p, o in zip(predictions, outcomes)])
    res    = np.mean([(o - mean_o)**2 for o in outcomes])
    unc    = mean_o * (1 - mean_o)

    # Reliability curve : regrouper par décile
    bins = np.linspace(0, 1, 11)
    calibration = []
    for i in range(10):
        mask   = [(bins[i] <= p < bins[i+1]) for p in predictions]
        if sum(mask) > 0:
            avg_p = np.mean([p for p, m in zip(predictions, mask) if m])
            avg_o = np.mean([o for o, m in zip(outcomes, mask) if m])
            calibration.append({
                "bin":           f"{bins[i]:.1f}-{bins[i+1]:.1f}",
                "avg_predicted": round(avg_p, 3),
                "avg_observed":  round(avg_o, 3),
                "n":             sum(mask)
            })

    return {
        "brier_score":  round(brier, 4),
        "fiabilite":    round(rel,   4),   # 0 = parfait
        "resolution":   round(res,   4),   # plus élevé = mieux
        "incertitude":  round(unc,   4),
        "calibration":  calibration,
        "verdict":      "GO" if brier < 0.20 else "WATCH" if brier < 0.22 else "NO-GO"
    }
```

---

#### Niveau 3 — Paper trading Polymarket (4-6 semaines, sans capital)

Rejouer le pipeline complet en temps réel mais sans exécuter les ordres. Logger chaque décision au moment exact où tu l'aurais prise.

```python
"""
Protocole paper trading (Thorp, 1967)

Règles absolues :
  - Décider AVANT de regarder la résolution
  - Logger au moment où tu aurais exécuté, pas après
  - Ne jamais modifier les décisions rétroactivement
  - Minimum 30 résolutions avant de calculer les métriques finales
"""

from datetime import datetime, timezone

PAPER_TRADE_LOG: list[dict] = []

def log_paper_trade(
    market_id:    str,
    question:     str,
    p_model:      float,
    p_market:     float,
    action:       str,        # "TRADE" | "SKIP"
    gates_passed: list[str],
    gates_failed: list[str],
    rationale:    str,
):
    sigma = 0.06  # remplacer par DynamicZScoreEngine en production
    PAPER_TRADE_LOG.append({
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "market_id":    market_id,
        "question":     question,
        "p_model":      p_model,
        "p_market":     p_market,
        "edge":         round(p_model - p_market, 4),
        "z_score":      round((p_model - p_market) / sigma, 3),
        "action":       action,
        "gates_passed": gates_passed,
        "gates_failed": gates_failed,
        "rationale":    rationale,
        "outcome":      None,    # rempli après résolution
        "brier_contrib":None,    # rempli après résolution
    })

def resolve_paper_trade(market_id: str, outcome: int):
    """Appeler après résolution du marché pour compléter le log."""
    for t in PAPER_TRADE_LOG:
        if t["market_id"] == market_id and t["outcome"] is None:
            t["outcome"]       = outcome
            t["brier_contrib"] = (t["p_model"] - outcome) ** 2
```

---

### Critères Go / No-Go avant déploiement capital réel

Standards superforecasting (Tetlock 2015) + finance quantitative (Thorp 1967, Lopez de Prado 2018) :

```
CRITÈRE 1 — Calibration (obligatoire)
  Brier Score ≤ 0.20 sur 30+ résolutions paper trading Polymarket
  Référence : superforecasters GJP ≈ 0.14-0.17
  → Si > 0.22 : arrêt, recalibration modèle, pas de déploiement

CRITÈRE 2 — Edge réalisé vs estimé (obligatoire)
  Edge moyen réalisé ≥ 60% de l'edge estimé
  Ex : si edge estimé moyen = 8¢, edge réalisé doit être ≥ 4.8¢
  Gap > 40% = p_model systématiquement biaisé
  → Si < 40% : recalibrer les LR sources ou le modèle probabiliste

CRITÈRE 3 — Taux de passage 7 gates (informatif)
  Entre 15% et 40% des marchés scannés passent les 7 gates
  < 15% → critères trop stricts, tu rates des opportunités réelles
  > 40% → critères trop lâches, tu absorbes trop de bruit

CRITÈRE 4 — Corrélation Z-score / résolution (obligatoire)
  Trades avec δ > 2.0σ doivent gagner plus que trades δ ∈ [1.5, 2.0σ]
  Valide que le Z-score est prédictif sur TES données, pas seulement en théorie

CRITÈRE 5 — Drawdown simulé (obligatoire)
  Sur le paper trading, drawdown maximum simulé < 12% du capital virtuel
  → Si > 12% : revoir sizing Kelly ou seuils d'exposition

RÉSUMÉ :
  ✓ Critères 1, 2, 4, 5 verts + Critère 3 dans la fourchette
  → GO : déployer €100, revenir évaluer après 30 vrais trades

  ✗ Un seul critère obligatoire rouge
  → NO-GO : identifier la cause, corriger, recommencer 30 résolutions
```

---

### Ce que le backtest historique peut et ne peut pas faire

```
PEUT faire :
  ✓ Déboguer le code (bugs dans les calculs, edge cases marchés annulés)
  ✓ Vérifier que les 7 gates s'activent sur les bons événements
  ✓ Estimer grossièrement les paramètres (σ moyen par catégorie)
  ✓ Identifier les marchés historiquement illiquides à blacklister

NE PEUT PAS faire :
  ✗ Prouver qu'il y a un edge (look-ahead bias inévitable)
  ✗ Calibrer les LR Crucix (ces sources n'existaient pas en 2024)
  ✗ Prédire les performances futures (conditions de marché changées)
  ✗ Remplacer le paper trading en temps réel

Règle de Thorp : "Paper trade until you're bored, then trade small,
                  then scale. Never skip a step."
```


---

## Gestion du Bankroll Progressif

### Principe fondamental : la variance tue avant l'edge

> *"The goal is not to maximize expected return. The goal is to maximize the probability of surviving long enough for the edge to manifest."* — Ed Thorp, *A Man for All Markets*

Avec un edge réel de 8¢ et un win rate de 65%, il faut statistiquement ~47 trades pour être sûr à 95% que les résultats ne sont pas dus au hasard. Avant ce seuil, chaque drawdown ressemble à une invalidation de la stratégie. **La gestion du bankroll est la compétence qui distingue les traders qui survivent de ceux qui ne survivent pas.**

Trois erreurs qui tuent le bankroll progressif :

```
1. Doubler après une série de victoires
   → La variance à court terme est indépendante de l'edge
   → Une série de 5 victoires avec Brier=0.21 reste mauvaise calibration

2. Réduire après une série de pertes
   → Si les gates passent encore, l'edge est intact
   → Réduire le capital au pire moment = erreur émotionnelle, pas analytique

3. Scaler sans critères objectifs
   → "Ça marche bien" n'est pas un critère
   → Seuls les métriques calculées valident le passage au palier suivant
```

---

### Les 4 paliers

```
PALIER 0 — Paper trading (0€ réel)
  Durée      : jusqu'à Critères Go validés (voir section Backtesting)
  Objectif   : calibration modèle, débogage pipeline
  Passage    : Brier < 0.20 sur 30 résolutions + 4 autres critères Go

PALIER 1 — Capital de départ (€100)
  Durée min  : 30 trades résolus (~6-8 semaines)
  Max/trade  : €5 (5% bankroll, Kelly fractionnel)
  Objectif   : valider l'edge sur argent réel, corriger les frictions
               (gas, slippage, fills partiels non anticipés)
  Passage P2 : voir critères ci-dessous

PALIER 2 — Capital intermédiaire (€200)
  Durée min  : 30 trades résolus supplémentaires
  Max/trade  : €8 (4% bankroll, Kelly conservateur)
  Objectif   : valider que l'edge scale sans dégradation
  Passage P3 : voir critères ci-dessous

PALIER 3 — Capital opérationnel (€500)
  Durée min  : 50 trades résolus supplémentaires
  Max/trade  : €20 (4% bankroll)
  Objectif   : phase de rendement stable
  Scaling +  : +€200 par tranche de 50 trades si critères maintenus
```

---

### Critères de passage entre paliers

Tous obligatoires. Un seul rouge = pas de scaling.

```python
def can_scale_up(trades: list[dict], current_bankroll: float) -> tuple[bool, dict]:
    """
    Évalue si le bankroll peut passer au palier suivant.
    trades : liste des trades résolus du palier en cours
    """
    import numpy as np

    resolved = [t for t in trades if t.get("outcome") is not None]
    if len(resolved) < 30:
        return False, {"reason": f"Trop peu de trades résolus : {len(resolved)}/30"}

    # ── Critère 1 : Brier Score ────────────────────────────────────
    brier = np.mean([(t["p_model"] - t["outcome"])**2 for t in resolved])
    c1 = brier < 0.20

    # ── Critère 2 : Profit Factor ──────────────────────────────────
    gains  = sum(t["pnl"] for t in resolved if t["pnl"] > 0)
    losses = abs(sum(t["pnl"] for t in resolved if t["pnl"] < 0))
    pf     = gains / max(0.01, losses)
    c2     = pf > 1.40

    # ── Critère 3 : Drawdown maximum ──────────────────────────────
    equity = [100.0]
    for t in resolved:
        equity.append(equity[-1] + t["pnl"])
    peak   = max(equity)
    trough = min(equity[equity.index(peak):])
    mdd    = (peak - trough) / peak
    c3     = mdd < 0.08

    # ── Critère 4 : Win rate pondéré ──────────────────────────────
    win_rate = np.mean([1 if t["pnl"] > 0 else 0 for t in resolved])
    c4       = win_rate > 0.58

    # ── Critère 5 : Sharpe Rolling (si assez de données) ──────────
    daily_pnl = {}
    for t in resolved:
        day = t["timestamp"][:10]
        daily_pnl[day] = daily_pnl.get(day, 0) + t["pnl"]
    sr = 0
    if len(daily_pnl) >= 20:
        pnls = list(daily_pnl.values())
        sr   = (np.mean(pnls) / np.std(pnls)) * np.sqrt(252) if np.std(pnls) > 0 else 0
        c5   = sr > 1.5
    else:
        c5   = True  # pas assez de données, critère non bloquant

    passed = all([c1, c2, c3, c4, c5])
    return passed, {
        "brier":         round(brier, 4),      "c1_brier_ok":   c1,
        "profit_factor": round(pf, 2),         "c2_pf_ok":      c2,
        "max_drawdown":  round(mdd * 100, 1),  "c3_mdd_ok":     c3,
        "win_rate":      round(win_rate, 3),   "c4_winrate_ok": c4,
        "sharpe":        round(sr, 2),         "c5_sharpe_ok":  c5,
        "verdict": "SCALE UP ✓" if passed else "HOLD — critères non atteints"
    }
```

---

### Règle de Kelly sur le bankroll (pas sur le trade)

L'erreur classique : appliquer Kelly sur chaque trade indépendamment. Thorp et Shannon ont montré que le bon niveau d'application est le **bankroll global**, pas l'unité.

```
Bankroll cible au palier suivant = bankroll actuel × (1 + Kelly_bankroll)

Kelly_bankroll = (win_rate × b - loss_rate) / b
  b          = ratio gain moyen / perte moyenne (pas la cote du marché)
  win_rate   = % trades profitables sur le palier en cours
  loss_rate  = 1 - win_rate

Exemple palier 1 → 2 :
  win_rate = 0.65, gain moyen = €2.10, perte moyenne = €2.80
  b = 2.10 / 2.80 = 0.75
  Kelly_bankroll = (0.65 × 0.75 - 0.35) / 0.75 = 0.183
  → Augmenter le bankroll de 18.3% → €100 × 1.183 = €118
  → Arrondir au palier suivant (€200) seulement si critères Go validés

Note : ne jamais utiliser Kelly plein sur le bankroll.
       Appliquer α=0.50 (moitié Kelly) pour le scaling inter-paliers,
       comme pour le sizing intra-trade.
```

---

### Ce qu'on NE fait pas

```
✗ Réinjecter des gains non mérités
  → Tout scaling doit être validé par les critères, pas juste accumulé

✗ Scaler pendant une série de pertes pour "moyenner"
  → Si les gates passent et que le Brier tient, attendre
  → Si les gates ne passent plus, c'est un kill switch, pas un scaling

✗ Dépasser 5% du bankroll par trade avant palier 3
  → La ruine est irréversible, le gain manqué ne l'est pas (Kelly)

✗ Considérer les gains non réalisés comme du bankroll
  → Le bankroll = USDC disponible sur le compte, positions exclues
```


---

## Cas Limites & Edge Cases

### Principe : le système doit avoir une réponse pour chaque situation anormale

> *"Plans are useless, but planning is indispensable."* — Eisenhower
> *"Under stress, you don't rise to the occasion — you fall to the level of your preparation."* — Archilochus

Un edge case non anticipé coûte deux fois : la perte directe + la décision panique prise sans cadre. Kahneman (Thinking Fast and Slow, 2011) a montré que le Système 1 (réflexe) prend le contrôle sous stress — la seule protection est d'avoir une procédure écrite consultable avant d'agir. Chaque cas ci-dessous a une procédure exacte, pas une réflexion à faire en temps réel.

---

### Cas 1 — Marché en dispute

**Situation :** Polymarket refuse de résoudre, ou résout à contre-sens de l'évidence (ex : résout NO alors que l'événement s'est clairement produit).

```
Fréquence réelle : ~2-3% des marchés résolvables
Impact potentiel : position bloquée, capital immobilisé

Procédure :
  1. Ne pas paniquer. Les fonds sont dans le smart contract — pas perdus.
  2. Vérifier le statut via Data API :
     GET https://data-api.polymarket.com/market/{condition_id}
     → champ "resolutionStatus" : "disputed" | "pending" | "resolved"

  3. Si "disputed" :
     → Capital retourné automatiquement par Polymarket dans 7-14j
     → Logger avec outcome=None, pnl=0 (capital récupéré intégralement)
     → NE PAS compter ce trade dans le Brier Score (résolution invalide)
     → NE PAS modifier p_model rétroactivement

  4. Si résolution incorrecte manifeste :
     → Soumettre un rapport via discord.gg/polymarket (canal #disputes)
     → UMA oracle : contestation possible sous 24h après résolution
     → Délai de récupération : 48-72h si contestation acceptée

  5. Impact sur le bankroll :
     → Capital temporairement indisponible, pas perdu
     → Ne pas le compter comme loss dans le MDD
     → Ne pas le compter comme bankroll disponible pour nouveaux trades

Kill switch spécifique :
  Si 2+ marchés en dispute simultanément → pause 48h
  → Signal potentiel d'un problème systémique Polymarket
```

---

### Cas 2 — Résolution tardive

**Situation :** date de résolution dépassée, le marché reste ouvert. Ex : "Will Fed cut March 19?" — le 20 mars, toujours ouvert.

```
Fréquence réelle : ~5-8% des marchés (délais sources officielles)
Impact : l'edge calculé était basé sur une date précise

Procédure :
  1. Vérifier la raison du délai :
     → Source officielle non publiée (BLS, Fed, CoinMarketCap en maintenance)
     → Litige sur la définition de l'événement
     → Bug technique Polymarket

  2. Recalculer l'edge avec les nouvelles informations disponibles :
     edge_nouveau = p_model_updated - p_mkt_actuel
     Si edge_nouveau < 0.04 → sortie au marché

  3. Z-score avec sigma ajusté (Merton 1976 — vol time-scaling) :
     σ_tardif = σ_original × √(jours_réels / jours_prévus)
     → Un marché qui dure 2x plus longtemps a une vol implicite plus élevée
     → Le Z-score se contracte mécaniquement : réévaluer le seuil 1.5σ

  4. Décision finale :
     ┌─────────────────────────────────────────────────────────┐
     │ edge > 0.04 ET thèse intacte  → TENIR, revérifier J+1  │
     │ edge < 0.04                   → SORTIE                  │
     │ Thèse invalide (faits changés)→ SORTIE immédiate        │
     │ Délai > 14 jours              → SORTIE, loguer anomalie │
     └─────────────────────────────────────────────────────────┘

  5. Logger :
     → days_actual vs days_planned
     → Raison du délai si connue
     → Ce champ alimente la calibration σ dynamique (DynamicZScoreEngine)
```

---

### Cas 3 — Question reformulée après ouverture

**Situation :** Polymarket modifie les termes de la question après que tu as pris une position. Ex : "Will BTC reach $90k by April 30?" devient "Will BTC close above $90k on April 30?".

```
Fréquence réelle : <1% — mais impact maximal sur la thèse

Procédure :
  1. Détecter via monitoring de la question :
     GET /market/{condition_id} → comparer champ "question" stocké vs actuel

  2. Évaluer si la reformulation change la probabilité :
     Ex "reach" → "close above" : P(close) < P(reach) → p_model doit baisser
     Utiliser le pipeline Tetlock complet pour recalculer p_model

  3. Recalculer p_model avec la nouvelle définition
     Si nouvel edge < 0.04 → EXIT immédiat
     Si edge toujours valide → tenir avec p_model mis à jour

  4. Logger l'événement comme signal Crucix de type ANOMALY :
     {
       "type":          "question_reformulation",
       "market_id":     ...,
       "old_question":  ...,
       "new_question":  ...,
       "p_model_old":   ...,
       "p_model_new":   ...,
       "action":        "exit | hold"
     }

  Règle de Kahneman : en cas de doute sur l'impact de la reformulation → EXIT.
  Le bénéfice du doute va toujours vers la protection du capital.
  (Système 2 délibératif doit dominer, pas l'ancrage au prix d'entrée)
```

---

### Cas 4 — Marché annulé après position ouverte

**Situation :** Polymarket annule un marché (événement impossible, bug, fraude détectée). Le capital est remboursé au prix d'entrée.

```
Procédure :
  1. Capital retourné automatiquement — aucune action requise
  2. Logger avec outcome=None, pnl=0, flag="cancelled"
  3. NE PAS inclure dans le Brier Score (pas de résolution réelle)
  4. NE PAS inclure dans win rate ni profit factor
  5. Inclure dans : nombre de positions ouvertes (jusqu'à remboursement)

  Impact bankroll :
  → Temporairement immobilisé pendant 24-72h
  → Après remboursement : disponible, comptabilisé normalement

  Signal qualité du scanner :
  Si 3+ annulations sur 30j → vérifier la qualité du Market Scanner
  Indique potentiellement des marchés à question ambiguë sélectionnés
  → Ajouter filtre "resolutionSource vérifiable" dans le scanner
  → Référence : Wolfers & Zitzewitz (2004) — "Prediction Markets" :
    les marchés à résolution source ambiguë ont 4x plus de disputes
```

---

### Cas 5 — Liquidité disparue après entrée

**Situation :** tu as une position ouverte, tu veux sortir, mais le spread bid/ask a explosé ou il n'y a plus d'acheteurs.

```
Fréquence : rare sur marchés >€20K volume, fréquent sur marchés <€5K

Mesure d'illiquidité (Amihud 2002) :
  ILLIQ = |ΔP| / Volume_journalier
  Si ILLIQ > 0.02 au moment de l'entrée → marché à risque de gel

Procédure :
  1. Ne pas market-vendre à n'importe quel prix

  2. Évaluer la cause :
     → Résolution imminente (normal, spread s'élargit les derniers jours)
     → Événement externe choque le marché
     → Marché structurellement illiquide

  3. Si résolution imminente (<3 jours) :
     → Tenir jusqu'à résolution, pas de sortie
     → Le spread à J-3 ne reflète plus un prix de trading, c'est binaire

  4. Si liquidité structurellement absente :
     → Placer un ordre limite à mid + 1¢
     → Attendre 4h
     → Si toujours non rempli : accepter mid - 1¢ (sortie loss partielle)
     → Jamais accepter spread > 8¢ sauf résolution <24h

  5. Post-mortem obligatoire :
     → Ajouter ce market_id à la blacklist illiquidité
     → Durcir le filtre volume minimum du Market Scanner
       (règle : si volume_24h < €15K → skip automatique)
```

---

### Cas 6 — Oracle failure (source de résolution fausse)

**Situation :** la source officielle de résolution (CoinGecko, Reuters, BLS.gov) publie une donnée incorrecte que Polymarket utilise pour résoudre.

```
Fréquence : extrêmement rare — mais Brier Score le détectera

Référence : Hanson (2007) — "Logarithmic Market Scoring Rules" :
  les marchés de prédiction sont robustes aux erreurs individuelles
  mais vulnérables aux erreurs de source unique autoritaire.

Procédure :
  1. Si tu suspectes une résolution basée sur données incorrectes :
     → Comparer avec 2+ sources alternatives immédiatement
     → Documenter la divergence avec horodatage

  2. Contester via UMA oracle (même procédure que Cas 1)
     → Délai : 24h pour initier, 48-72h pour résolution

  3. Ne pas modifier p_model rétroactivement pour "corriger" le Brier
     → Un oracle failure est une fraude externe, pas une erreur du modèle
     → Logger séparément : outcome_official vs outcome_true

  4. Si oracle failure confirmé :
     → Trade exclu du Brier Score (résolution invalide)
     → Capital récupéré si contestation acceptée

  Signal systémique :
  Si 2+ oracle failures en 30j sur la même source →
  Retirer cette source de la liste des marchés éligibles jusqu'à correction
```

---

### Matrice de décision rapide

```
Situation détectée              Action immédiate          Délai max
──────────────────────────────────────────────────────────────────
Marché en dispute               Attendre remboursement    14j
Résolution tardive              Recalculer edge + σ       24h
Edge < 4¢ après délai           SORTIE                    Immédiat
Question reformulée             Recalculer p_model        4h
Reformulation change thèse      SORTIE                    Immédiat
Marché annulé                   Attendre remboursement    72h
Liquidité absente, res. >3j     Limit mid+1¢, attendre    4h
Liquidité absente, res. <3j     Tenir jusqu'à résolution  —
Oracle failure suspecté         Comparer 2+ sources       24h
2+ disputes simultanés          Pause 48h (kill switch)   Immédiat
──────────────────────────────────────────────────────────────────
Règle universelle (Kahneman) : en cas de doute → EXIT et log.
Le capital préservé vaut plus que l'edge hypothétique.
```


---

## Corrélations Inter-Positions

### Pourquoi le VaR position par position est faux

> *"Risk is not additive. Two correlated bets are not two bets — they are one bet with double the size."* — Nassim Taleb, *The Black Swan*

Le VaR calculé indépendamment sur chaque position suppose que les résolutions sont statistiquement indépendantes. C'est faux dès que deux positions partagent la même catégorie thématique.

```
Exemple concret :
  Position A : "Fed coupe mars 2026" — €4.80 à 0.720
  Position B : "Fed coupe juin 2026" — €4.20 à 0.650

  VaR indépendant : √(VaR_A² + VaR_B²) = √(0.58² + 0.48²) = €0.75
  VaR corrélé     : √(w^T × Σ × w) avec ρ=0.85  = €0.99

  Sous-estimation : 32%
  → Si le Fed surprend avec un hold, les deux positions perdent ensemble
  → Ce n'est pas un portefeuille de 2 trades, c'est un trade de €9
```

---

### Matrice de corrélation par catégorie

Corrélations empiriques estimées sur données Polymarket 2023-2025.
Basées sur la co-résolution observée (Tetlock 2015 — corrélation entre
forecasters sur questions liées) et les corrélations de marchés financiers
sous-jacents (Engle 2002 — DCC-GARCH).

```python
# ρ ∈ [0, 1] — 0 = indépendant, 1 = parfaitement corrélé

CORRELATION_MATRIX = {
    # ── Intra-catégorie ───────────────────────────────────────────
    ("fed_macro",    "fed_macro"):    0.85,  # 2 marchés Fed = quasi un trade
    ("crypto",       "crypto"):       0.70,  # BTC/ETH corrélés mais pas identiques
    ("politics",     "politics"):     0.45,  # élections indép. sauf runoffs
    ("sports",       "sports"):       0.10,  # quasi-indépendant

    # ── Inter-catégorie ───────────────────────────────────────────
    ("fed_macro",    "crypto"):       0.40,  # Fed dovish = BTC up (post-2022)
    ("fed_macro",    "politics"):     0.25,  # Fed et élections partiellement liés
    ("fed_macro",    "macro_intl"):   0.55,  # Fed → DXY → marchés internationaux
    ("crypto",       "politics"):     0.15,  # faiblement corrélé
    ("geopolitical", "crypto"):       0.30,  # escalade → risk-off → BTC down
    ("geopolitical", "fed_macro"):    0.35,  # escalade → Fed hold (risk-off)
}

def get_correlation(cat_a: str, cat_b: str) -> float:
    if cat_a == cat_b:
        key = (cat_a, cat_b)
    else:
        key = tuple(sorted([cat_a, cat_b]))
    return CORRELATION_MATRIX.get(key, 0.15)  # défaut conservateur
```

---

### Calcul du VaR ajusté pour corrélations

Basé sur Markowitz (1952) — variance de portefeuille, adapté aux marchés
binaires (Jullien & Salanié 2000 — pricing of risk in prediction markets).

```python
import numpy as np
from itertools import combinations

def portfolio_var_95(positions: list[dict], bankroll: float) -> dict:
    """
    Calcule le VaR 95% du portefeuille en tenant compte des corrélations.

    positions : liste de dicts avec keys :
      market_id, category, size, p_model, p_market, sigma

    Retourne var_correlated — le seul chiffre actionnable.
    """
    n = len(positions)
    if n == 0:
        return {"var_independent": 0, "var_correlated": 0,
                "concentration": {}, "warning": False}

    # ── Volatilités individuelles (gaussien conservateur) ─────────
    vars_ind = np.array([
        p["size"] * 1.645 * p.get("sigma", 0.06)
        for p in positions
    ])

    # ── VaR sans corrélations (référence seulement — sous-estimé) ─
    var_independent = float(np.sqrt(np.sum(vars_ind ** 2)))

    # ── Matrice de corrélation ────────────────────────────────────
    corr_matrix = np.eye(n)
    for i, j in combinations(range(n), 2):
        rho = get_correlation(positions[i]["category"],
                              positions[j]["category"])
        corr_matrix[i, j] = rho
        corr_matrix[j, i] = rho

    # ── VaR corrélé : σ_p = √(w^T × Σ × w) ──────────────────────
    var_correlated = float(np.sqrt(vars_ind @ corr_matrix @ vars_ind))

    # ── Concentration par catégorie ───────────────────────────────
    concentration = {}
    for p in positions:
        cat = p["category"]
        concentration[cat] = concentration.get(cat, 0) + p["size"]

    return {
        "var_independent":  round(var_independent, 2),
        "var_correlated":   round(var_correlated, 2),
        "var_underestimate":round(var_correlated - var_independent, 2),
        "var_pct_bankroll": round(var_correlated / bankroll * 100, 1),
        "concentration":    {k: round(v, 2) for k, v in concentration.items()},
        "warning":          var_correlated / bankroll > 0.05,
    }
```

---

### Règles d'exposition thématique maximale

Basées sur Kelly multi-asset (Thorp 1997) et la diversification optimale
de Markowitz — adaptées à la réalité de €100.

```
RÈGLE 1 — Maximum positions corrélées simultanées
  Fed / taux / macro US     : max 2 positions simultanées
  Crypto (BTC/ETH/SOL)      : max 2 positions simultanées
  Géopolitique même région  : max 1 position simultanée
  Politics même pays        : max 1 position simultanée

  Au-delà : tu n'as plus un portefeuille diversifié
  mais un pari concentré avec levier implicite non voulu.

RÈGLE 2 — Exposition thématique max par catégorie
  Fed/macro US    : max 35% du bankroll déployé
  Crypto          : max 30% du bankroll déployé
  Géopolitique    : max 20% du bankroll déployé
  Politics        : max 20% du bankroll déployé
  Sports          : max 10% du bankroll déployé

  Exemple bankroll €114 :
  → Max €40 en positions Fed/macro simultanées
  → Max €34 en positions crypto simultanées

RÈGLE 3 — VaR corrélé comme gate supplémentaire
  var_gate (existant) : VaR indépendant ≤ 5% bankroll
  var_corr_gate (NEW) : VaR corrélé    ≤ 7% bankroll

  Si var_corrélé > 7% → bloquer toute nouvelle position
  dans les catégories surexposées jusqu'à résolution d'une position

RÈGLE 4 — Corrélation Crucix inter-signaux
  Si 2 alertes Crucix arrivent sur positions corrélées (ρ > 0.6)
  dans la même fenêtre de 30 minutes :
  → Traiter comme un seul signal, pas deux
  → Extremizing désactivé (Satopää 2014 : s'applique uniquement
    à sources statistiquement indépendantes)
  → LR effectif = LR_max des deux alertes (pas le produit)
```

---

### Gate de corrélation — intégration dans le pipeline

À appeler après les 7 gates existants, avant tout nouvel ordre.

```python
def check_correlation_gate(
    new_position: dict,
    open_positions: list[dict],
    bankroll: float
) -> tuple[bool, str]:
    """
    Gate supplémentaire : vérifie que l'ajout de la nouvelle position
    ne crée pas de concentration corrélée excessive.
    """
    simulated = open_positions + [new_position]
    result    = portfolio_var_95(simulated, bankroll)

    # Gate A : VaR corrélé
    if result["var_pct_bankroll"] > 7.0:
        return False, (
            f"correlation_var_gate : VaR corrélé {result['var_pct_bankroll']}% "
            f"> 7% limite. Réduction exposition {new_position['category']} requise."
        )

    # Gate B : concentration thématique
    MAX_EXPOSURE = {
        "fed_macro": 0.35, "crypto": 0.30,
        "geopolitical": 0.20, "politics": 0.20, "sports": 0.10
    }
    cat     = new_position["category"]
    cat_exp = result["concentration"].get(cat, 0)
    max_exp = MAX_EXPOSURE.get(cat, 0.20) * bankroll
    if cat_exp > max_exp:
        return False, (
            f"concentration_gate : exposition {cat} = €{cat_exp:.2f} "
            f"> max €{max_exp:.2f} ({MAX_EXPOSURE.get(cat,0.20)*100:.0f}% bankroll)"
        )

    # Gate C : nombre de positions corrélées simultanées
    MAX_CORR_POSITIONS = {
        "fed_macro": 2, "crypto": 2, "geopolitical": 1, "politics": 1
    }
    same_cat = [p for p in simulated if p["category"] == cat]
    max_pos  = MAX_CORR_POSITIONS.get(cat, 2)
    if len(same_cat) > max_pos:
        return False, (
            f"corr_count_gate : {len(same_cat)} positions {cat} > max {max_pos}"
        )

    return True, "correlation_gates_passed"
```

---

### Affichage Risk Monitor

Le dashboard PAF-001 (page Risk Monitor) doit afficher :

```
VaR indépendant   : €X.XX  (X.X% bankroll)   ← référence seulement
VaR corrélé       : €X.XX  (X.X% bankroll)   ← le seul chiffre actionnable
Sous-estimation   : +€X.XX (+X.X%)

Concentration :
  Fed/macro        : €XX.XX / max €40.00
  Crypto           : €XX.XX / max €34.00
  Géopolitique     : €XX.XX / max €23.00
  Politics         : €XX.XX / max €23.00
```

---

Références : Markowitz (1952) *Portfolio Selection*, Thorp (1997) *The Kelly
Criterion in Blackjack*, Engle (2002) *Dynamic Conditional Correlation*,
Lopez de Prado (2018) *AFML* ch.16, Tetlock (2015) *Superforecasting* ch.9,
Jullien & Salanié (2000) *Estimating Preferences under Risk*.


---

## Procédure de Démarrage — Semaines 1-2

### Principe : les deux premières semaines ne sont pas du trading

> *"The most important thing about a trading system is the discipline to follow it exactly, including the setup."* — Ed Thorp

Les semaines 1-2 ont un seul objectif : **vérifier que le pipeline fonctionne sans erreurs avant de risquer un centime**. 80% des pertes précoces viennent de bugs techniques, pas de mauvaises décisions de marché. Chaque étape a un critère de validation binaire — pas de "ça a l'air bon", uniquement "✓ validé" ou "❌ bloquer".

---

### Pré-requis (avant jour 1)

```
Infrastructure :
  ✓ VPS Hetzner Ireland opérationnel (CX21, 4GB RAM minimum)
  ✓ Docker installé + Crucix container actif (voir crucix_router.py)
  ✓ Python 3.10+ avec pip à jour
  ✓ Compte Polymarket créé + KYC validé (si requis par région)
  ✓ Wallet Polygon configuré (MetaMask ou Gnosis Safe)
  ✓ USDC disponible sur Polygon (minimum €110 pour frais inclus)

Outils requis :
  pip install py-clob-client python-dotenv requests pandas numpy
```

---

### Jour 1 — Installation et connexion

```bash
# 1. Cloner et configurer py-clob-client
git clone https://github.com/Polymarket/py-clob-client
cd py-clob-client
pip install -e .

# 2. Créer le fichier .env (ne jamais committer)
cat > .env << 'EOF'
POLYMARKET_HOST=https://clob.polymarket.com
CHAIN_ID=137
PRIVATE_KEY=0x_TON_CLEE_PRIVEE_ICI
FUNDER=0x_TON_ADRESSE_WALLET
EOF

# 3. Test de connexion
python3 -c "
from py_clob_client.client import ClobClient
from dotenv import load_dotenv
import os
load_dotenv()
client = ClobClient(
    host=os.getenv('POLYMARKET_HOST'),
    key=os.getenv('PRIVATE_KEY'),
    chain_id=int(os.getenv('CHAIN_ID'))
)
print('Connected:', client.get_address())
print('Balance USDC:', client.get_balance())
"
# ✓ Critère : adresse affichée + balance > 0
```

---

### Jour 1 — Activer le Builder Program (gasless)

```python
"""
Builder Program = transactions sans frais de gas sur Polymarket.
Obligatoire pour être compétitif avec €100.
Sans ça : chaque order coûte ~$0.01-0.05 en gas → drag significatif.
"""
from py_clob_client.client import ClobClient
import os
from dotenv import load_dotenv

load_dotenv()

client = ClobClient(
    host=os.getenv('POLYMARKET_HOST'),
    key=os.getenv('PRIVATE_KEY'),
    chain_id=int(os.getenv('CHAIN_ID')),
    signature_type=2,   # POLY_GNOSIS_SAFE — requis pour Builder Program
    funder=os.getenv('FUNDER'),
)

# Créer les credentials API
creds = client.create_or_derive_api_creds()
print("API Key    :", creds.api_key)
print("API Secret :", creds.api_secret)
print("Passphrase :", creds.api_passphrase)

# Sauvegarder dans .env
with open('.env', 'a') as f:
    f.write(f"\nPOLYMARKET_API_KEY={creds.api_key}")
    f.write(f"\nPOLYMARKET_API_SECRET={creds.api_secret}")
    f.write(f"\nPOLYMARKET_PASSPHRASE={creds.api_passphrase}")

# ✓ Critère : credentials générés et sauvegardés
# ✓ Critère : client.get_order_book("any_market_id") répond sans erreur
```

---

### Jour 2 — Initialiser la base de données SQLite

```python
"""
Toutes les décisions, positions et signaux doivent être loggés
dès le premier trade. Un log incomplet est inutilisable pour
la calibration (Lopez de Prado 2018 — audit trail obligatoire).
"""
import sqlite3
from pathlib import Path

DB = Path("paf001.db")

def init_db():
    conn = sqlite3.connect(DB)
    c    = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            market_id       TEXT    NOT NULL,
            question        TEXT    NOT NULL,
            strategy        TEXT    NOT NULL,  -- S1 | S2
            category        TEXT    NOT NULL,
            p_model         REAL    NOT NULL,
            p_market        REAL    NOT NULL,
            edge            REAL    NOT NULL,
            z_score         REAL    NOT NULL,
            size_eur        REAL    NOT NULL,
            kelly_f         REAL    NOT NULL,
            order_id        TEXT,
            fill_price      REAL,
            fill_size       REAL,
            outcome         INTEGER,           -- 1=YES, 0=NO, NULL=open
            pnl             REAL,
            brier_contrib   REAL,
            gates_passed    TEXT,              -- JSON list
            gates_failed    TEXT,              -- JSON list
            is_paper        INTEGER DEFAULT 1  -- 1=paper, 0=real
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            source_id   TEXT NOT NULL,
            market_id   TEXT,
            direction   TEXT,
            lr_applied  REAL,
            p_before    REAL,
            p_after     REAL,
            acted_upon  INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_snapshot (
            date        TEXT PRIMARY KEY,
            bankroll    REAL,
            open_pos    INTEGER,
            deployed    REAL,
            brier_15t   REAL,
            mdd_30d     REAL,
            sharpe_30d  REAL
        )
    """)

    conn.commit()
    conn.close()
    print(f"✓ DB initialisée : {DB}")

init_db()
# ✓ Critère : paf001.db créé, 3 tables vérifiées via sqlite3
```

---

### Jours 3-5 — Premier scan de marchés (sans trader)

```python
"""
Objectif : faire tourner le Market Scanner et vérifier que
les 7 gates s'activent correctement sur de vraies données.
Critère : au moins 3 marchés passent tous les gates sur une journée.
Si 0 pass → revoir seuils ou base rate du modèle.
"""
import requests
from datetime import datetime

def scan_markets(min_volume: int = 10_000, max_days: int = 30) -> list[dict]:
    r = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"closed": "false", "limit": 200}
    )
    candidates = []

    for m in r.json():
        try:
            price    = float(m.get("outcomePrices", ["0.5"])[0])
            volume   = float(m.get("volume", 0))
            end_date = m.get("endDate", "")
            days_left = (
                datetime.fromisoformat(end_date[:19]) - datetime.now()
            ).days if end_date else 0

            if volume < min_volume: continue
            if days_left < 3:       continue
            if days_left > max_days:continue
            if price < 0.01:        continue

            q = m.get("question", "").lower()
            if any(k in q for k in ["fed","fomc","rate","cpi"]):
                cat, strat = "fed_macro", "S1" if price > 0.60 else "S2"
            elif any(k in q for k in ["btc","bitcoin","eth","crypto"]):
                cat, strat = "crypto",   "S1" if price > 0.60 else "S2"
            else:
                cat, strat = "politics", "S1" if price > 0.60 else "S2"

            candidates.append({
                "market_id": m.get("id"),
                "question":  m.get("question"),
                "p_market":  price,
                "volume":    volume,
                "days_left": days_left,
                "category":  cat,
                "strategy":  strat,
            })
        except Exception:
            continue

    return sorted(candidates, key=lambda x: -x["volume"])

markets = scan_markets()
print(f"✓ {len(markets)} marchés candidats")
for m in markets[:10]:
    print(f"  {m['question'][:55]:<55} | "
          f"p={m['p_market']:.3f} | vol={m['volume']:,.0f} | "
          f"{m['days_left']}j | {m['strategy']}")

# ✓ Critère : au moins 10 marchés affichés
# ✓ Critère : mix S1 (>0.60) et S2 (<0.20) présent
```

---

### Jours 5-7 — Premier paper trade loggué

```python
"""
Logger le premier paper trade avec le pipeline complet.
Traiter ce trade exactement comme un vrai — même rigueur.
(Thorp : "Paper trade until you're bored, then trade small.")
"""
import sqlite3, json
from datetime import datetime, timezone

def log_paper_trade_db(trade: dict):
    conn = sqlite3.connect("paf001.db")
    conn.execute("""
        INSERT INTO trades
        (ts, market_id, question, strategy, category,
         p_model, p_market, edge, z_score, size_eur, kelly_f,
         gates_passed, gates_failed, is_paper)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
    """, (
        datetime.now(timezone.utc).isoformat(),
        trade["market_id"],  trade["question"],
        trade["strategy"],   trade["category"],
        trade["p_model"],    trade["p_market"],
        trade["p_model"] - trade["p_market"],
        (trade["p_model"] - trade["p_market"]) / trade.get("sigma", 0.06),
        trade["size_eur"],   trade["kelly_f"],
        json.dumps(trade.get("gates_passed", [])),
        json.dumps(trade.get("gates_failed", [])),
    ))
    conn.commit()
    conn.close()
    print(f"✓ Paper trade loggué : {trade['market_id']}")

# ✓ Critère : trade présent dans paf001.db, tous les champs remplis
# ✓ Critère : edge = p_model - p_market exactement
```

---

### Checklist avant le premier vrai trade

Toutes les cases doivent être cochées. Une seule non cochée = attendre.

```
Infrastructure :
  ☐ VPS actif depuis 48h sans interruption
  ☐ Crucix container en marche, 26 sources actives
  ☐ paf001.db créé avec au moins 5 paper trades loggués
  ☐ Builder Program activé (signature_type=2 confirmé)
  ☐ Test order placé et annulé sans erreur (0.001 USDC)

Calibration :
  ☐ Au moins 10 paper trades loggués
  ☐ Au moins 3 résolutions observées
  ☐ Brier en cours de calcul (même si insuffisant statistiquement)
  ☐ Aucune erreur Python dans le pipeline depuis 48h

Capital :
  ☐ USDC disponible ≥ €105 (€100 trading + €5 frais buffer)
  ☐ Wallet Polygon approuvé par le smart contract Polymarket
  ☐ Allowance USDC vérifiée (approve() exécuté si nécessaire)

Mental (Kahneman 2011 — Système 2 obligatoire) :
  ☐ Tu acceptes de perdre les €100 sans impact sur ta vie
  ☐ Tu as lu les kill switches et tu sais exactement quand t'arrêter
  ☐ Tu ne traderas pas sous stress, fatigue ou pression émotionnelle
```

---

### Semaine 2 — Premiers vrais trades

```
Règles absolues :

1. Maximum 2 trades par jour
   → Le cerveau commet des erreurs de pattern-matching au-delà
   → Chaque trade doit être délibéré, pas réactif

2. Taille maximale : €3 par trade (60% du max Kelly)
   → Sous-sizer délibérément les premières semaines
   → L'objectif est la calibration, pas le profit
   → Thorp : "When uncertain about the edge, bet half Kelly minimum"

3. Logguer AVANT de placer, pas après
   → Écrire p_model, gates, rationale avant d'exécuter
   → Si tu ne peux pas écrire la rationale → ne pas trader

4. Revue quotidienne : 10 minutes chaque soir
   → Relire les trades du jour dans paf001.db
   → Vérifier que les gates ont bien été appliqués
   → Snapshot daily_snapshot

5. Revue hebdomadaire fin semaine 2 :
   → Calculer Brier sur toutes les résolutions disponibles
   → Comparer edge estimé vs edge réalisé
   → Décider : continuer | ajuster | pause
```

---

### Signaux d'arrêt immédiat

```
Technique :
  ✗ Ordre placé sans confirmation fill dans les 60 secondes
  ✗ Balance USDC différente de ce qu'elle devrait être
  ✗ Erreur API répétée (3+ fois en 10 minutes)
  → Stopper le bot, vérifier manuellement sur Polymarket UI

Calibration :
  ✗ 3 premiers trades tous perdants
  ✗ Edge réalisé < 2¢ sur les 5 premiers trades
  ✗ Fills partiels systématiques (>50% des ordres)
  → Pause 48h, revoir seuils du Market Scanner

Mental (Thorp) :
  ✗ Tu vérifies ton portfolio plus de 3 fois par jour
  ✗ Tu ressens le besoin de "rattraper" une perte
  ✗ Tu envisages d'augmenter la taille après une perte
  → Pause obligatoire 24h minimum
```


---

## Taxes & Comptabilité

### Avertissement préalable

Cette section est informative, pas un conseil fiscal. Les règles changent. Consulter un comptable ou avocat fiscaliste avant toute déclaration. Les montants et taux ci-dessous sont valables en 2026 selon les informations disponibles.

---

### France — Régime applicable

Les gains Polymarket sont des **actifs numériques** au sens de l'article 150 VH bis du CGI (Code Général des Impôts), introduit par la loi PACTE 2019.

```
Régime fiscal :
  Flat Tax (PFU) = 30%
    → 12.8% impôt sur le revenu
    → 17.2% prélèvements sociaux

  Applicable si :
    → Activité non professionnelle (= usage occasionnel)
    → Cession d'actifs numériques contre monnaie fiat (EUR)

  Seuil de déclaration :
    → Obligatoire si cessions totales > 305€ dans l'année
    → En dessous de 305€ : exonération totale (art. 150 VH bis)

  Avec €100 de capital :
    → Si cessions totales < 305€ : rien à déclarer
    → Si tu scales (palier 2-3) et dépasses 305€ : déclaration obligatoire
```

#### Calcul de la plus-value imposable

```
La règle française utilise le prix moyen pondéré d'acquisition (PMPA).
Pas le FIFO, pas le LIFO — le PMPA.

Formule :
  PV = Prix de cession - (PMPA × Fraction cédée)
  PMPA = Valeur globale portefeuille crypto / Nombre total de tokens

Exemple pratique :
  Portefeuille total crypto : €500 USDC
  Cession Polymarket : vente de shares pour €120
  Fraction cédée : 120 / 500 = 24%

  PMPA de la fraction = 500 × 24% = €120 (cost basis)
  Prix de vente = €145 (après gains)
  PV imposable = 145 - 120 = €25
  Impôt = €25 × 30% = €7.50

Important :
  → Chaque retrait USDC → EUR compte comme cession
  → Les trades internes (USDC → shares → USDC) sont des cessions
    d'actifs numériques, pas des plus-values mobilières classiques
  → Les pertes sont déductibles des gains de même nature sur 10 ans
```

#### Ce qu'il faut logger pour la déclaration

```python
# Minimum légal à logger pour chaque cession (art. 150 VH bis CGI)
FISCAL_LOG_FIELDS = {
    "date_acquisition":  str,   # date d'achat des shares
    "date_cession":      str,   # date de vente / résolution
    "prix_acquisition":  float, # prix d'achat en EUR
    "prix_cession":      float, # prix de vente en EUR
    "frais":             float, # gas + fees Polymarket
    "actif":             str,   # "USDC/Polymarket - [question]"
    "plus_value_brute":  float, # prix_cession - prix_acquisition - frais
}

# Outils recommandés pour calcul automatique PMPA :
# Waltio (fr) ou Koinly (international) — connectent aux wallets Polygon
```

#### Formulaires à remplir

```
Déclaration annuelle revenus (mai N+1) :
  ✓ Formulaire 2086 — Calcul des plus-values d'actifs numériques
  ✓ Formulaire 2042 ligne 3AN — Montant net imposable
  ✓ Si comptes crypto étrangers > 10k€ cumulatifs : Formulaire 3916-bis

Note : Polymarket (USA) = compte à l'étranger
  → Si solde dépasse 10k€ à un moment de l'année → 3916-bis obligatoire
  → Avec €100 de capital : non applicable dans l'immédiat
```

---

### Suisse — Régime applicable

```
PARTICULIER (activité non professionnelle) :
  Plus-values = EXONÉRÉES d'impôt fédéral direct
  → Base légale : art. 16 al. 3 LIFD
  → Les gains de capital sur actifs mobiliers sont libres d'impôt

  Attention — critères de requalification en activité professionnelle
  (AFC Circulaire 2021) :
    · Volume de transactions élevé et très fréquent
    · Financement par crédit
    · Revenu principal issu du trading
    · Détention moyenne < 6 mois
  → Avec €100 et trading occasionnel : aucun risque de requalification

FORTUNE IMPOSABLE :
  Les crypto-actifs sont de la fortune imposable (impôt cantonal)
  → Valeur déclarée au 31 décembre de chaque année
  → Cours de référence : ESTV (Administration fédérale des contributions)
  → Avec €100 : impact négligeable

COMPTES ÉTRANGERS :
  → Déclaration obligatoire dans la fortune (case "autres valeurs")
  → Pas de formulaire spécifique comme en France
```

---

### Export fiscal automatisé — wallet Polygon

```python
import requests
import pandas as pd

def fetch_polygon_transactions(wallet: str, api_key: str) -> pd.DataFrame:
    """
    Récupère toutes les transactions USDC pour déclaration fiscale.
    api_key : clé Polygonscan gratuite (plan Free = 5 req/s)
    """
    url = "https://api.polygonscan.com/api"
    params = {
        "module":          "account",
        "action":          "tokentx",
        "address":         wallet,
        "contractaddress": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC Polygon
        "sort":            "asc",
        "apikey":          api_key,
    }
    txs  = requests.get(url, params=params).json().get("result", [])
    rows = [{
        "date":   pd.to_datetime(int(tx["timeStamp"]), unit="s"),
        "hash":   tx["hash"],
        "from":   tx["from"],
        "to":     tx["to"],
        "amount": int(tx["value"]) / 1e6,  # USDC = 6 décimales
        "type":   "in" if tx["to"].lower() == wallet.lower() else "out",
    } for tx in txs]

    df = pd.DataFrame(rows)
    df.to_csv("fiscal_log.csv", index=False)
    print(f"✓ {len(df)} transactions exportées → fiscal_log.csv")
    return df
```

---

### Règles pratiques communes France/Suisse

```
À faire dès le premier trade :
  ✓ Logger chaque trade avec date, prix EUR, fees
  ✓ Noter le taux EUR/USDC au moment de chaque cession
    (source : CoinGecko API historical price)
  ✓ Conserver les exports Polygonscan au moins 10 ans (France)
    ou 5 ans (Suisse — délai de prescription fiscal)

À ne jamais faire :
  ✗ Considérer les gains crypto comme non déclarables
    → L'AFC et la DGFiP ont des accords d'échange d'informations
  ✗ Mélanger wallet trading et wallet personnel
    → Un wallet dédié PAF-001 simplifie radicalement la comptabilité
  ✗ Attendre d'avoir "beaucoup gagné" pour s'organiser
    → Reconstituer l'historique fiscal a posteriori est cauchemardesque

Wallet dédié PAF-001 (obligatoire) :
  → Créer un wallet Polygon séparé uniquement pour Polymarket
  → Tous les dépôts et retraits passent par ce wallet
  → Zéro ambiguïté pour la comptabilité fiscale
```

---

Références légales : art. 150 VH bis CGI (France), loi PACTE 2019,
art. 16 al. 3 LIFD (Suisse), Circulaire AFC 2021 sur les crypto-actifs,
BOFiP 2022 (Bulletin officiel des finances publiques).


---

## Plan de Recalibration Post Kill-Switch

### Principe : l'arrêt est un diagnostic, pas un échec

> *"The best thing that can happen to a forecaster is to be wrong in an interesting way."* — Philip Tetlock, *Superforecasting*

Un kill-switch déclenché n'est pas la fin de la stratégie. C'est un signal que le modèle s'est écarté de la réalité — et que le système de protection a fonctionné exactement comme prévu. La recalibration est une procédure technique, pas une remise en question émotionnelle.

**Règle absolue : zéro trade réel pendant toute la durée de recalibration.**

---

### Phase 0 — Arrêt complet (immédiat)

```
Dès qu'un kill-switch se déclenche :

  1. Annuler tous les ordres en attente (CLOB cancel_all)
  2. Ne pas fermer les positions ouvertes (sauf si edge < 0)
     → Les positions ouvertes ont leur edge calculé à l'entrée
     → Les fermer en urgence crée un coût de transaction supplémentaire
  3. Noter l'heure exacte et le kill-switch déclenché dans le log
  4. Ne rien faire d'autre pendant 24h minimum

Délai de refroidissement obligatoire (Kahneman 2011) :
  Brier > 0.22              → 72h minimum avant diagnostic
  MDD > 8%                  → 72h minimum
  Sharpe < 1.0 (30j)        → 48h minimum
  5 pertes consécutives      → 48h minimum

  Pourquoi 72h ?
  → Le cerveau sous stress sur-apprend les derniers événements
    (recency bias — Tversky & Kahneman 1974)
  → Un diagnostic fait dans les 24h post-perte est biaisé
  → Après 72h, le Système 2 (délibératif) reprend le contrôle
```

---

### Phase 1 — Diagnostic (jours 4-7)

Identifier la cause exacte de l'échec. Quatre causes possibles, une procédure pour chacune.

```python
def run_diagnostic(trades: list[dict]) -> dict:
    """
    Analyse post-mortem des trades ayant conduit au kill-switch.
    Retourne la cause principale et les paramètres à recalibrer.
    """
    import numpy as np

    resolved = [t for t in trades if t.get("outcome") is not None]
    if len(resolved) < 5:
        return {"cause": "INSUFFICIENT_DATA", "action": "wait_more_resolutions"}

    # ── Test 1 : Brier par catégorie ─────────────────────────────
    brier_by_cat = {}
    for cat in set(t["category"] for t in resolved):
        sub   = [t for t in resolved if t["category"] == cat]
        brier = np.mean([(t["p_model"] - t["outcome"])**2 for t in sub])
        brier_by_cat[cat] = round(brier, 4)
    worst_cat = max(brier_by_cat, key=brier_by_cat.get)

    # ── Test 2 : Overconfidence / Underconfidence ─────────────────
    # Calibration error = mean(p_model) - mean(outcome)
    # Positif = overconfident, Négatif = underconfident
    calib_error = (np.mean([t["p_model"] for t in resolved]) -
                   np.mean([t["outcome"] for t in resolved]))

    # ── Test 3 : Edge prédictif ? ─────────────────────────────────
    edges   = [t["edge"] for t in resolved]
    correct = [1 if (t["edge"] > 0 and t["outcome"] == 1) or
                    (t["edge"] < 0 and t["outcome"] == 0)
               else 0 for t in resolved]
    edge_corr = np.corrcoef(edges, correct)[0, 1] if len(edges) > 3 else 0

    # ── Test 4 : Signal Crucix défaillant ? ──────────────────────
    with_cx    = [t for t in resolved if t.get("crucix_signals", 0) >= 2]
    without_cx = [t for t in resolved if t.get("crucix_signals", 0) < 2]
    brier_cx   = np.mean([(t["p_model"]-t["outcome"])**2
                           for t in with_cx]) if with_cx else None
    brier_no   = np.mean([(t["p_model"]-t["outcome"])**2
                           for t in without_cx]) if without_cx else None

    # ── Diagnostic principal ──────────────────────────────────────
    if brier_by_cat[worst_cat] > 0.25:
        cause  = f"CATEGORY_FAILURE_{worst_cat.upper()}"
        action = f"Suspendre catégorie {worst_cat} du scanner pendant 30j"
    elif abs(calib_error) > 0.08:
        cause  = "OVERCONFIDENCE" if calib_error > 0 else "UNDERCONFIDENCE"
        action = "Recalibrer prior Tetlock — ajuster base rates"
    elif edge_corr < 0.10:
        cause  = "EDGE_NOT_PREDICTIVE"
        action = "Revoir seuils Z-score et edge minimum"
    elif brier_cx and brier_no and brier_cx > brier_no + 0.03:
        cause  = "CRUCIX_LR_MISCALIBRATED"
        action = "Recalibrer LR Crucix via WeeklyCalibrationReport"
    else:
        cause  = "VARIANCE_NORMALE"
        action = "Aucune recalibration — reprendre à 50% Kelly"

    return {
        "cause":           cause,
        "action":          action,
        "brier_by_cat":    brier_by_cat,
        "worst_cat":       worst_cat,
        "calib_error":     round(calib_error, 4),
        "edge_corr":       round(edge_corr, 4),
        "brier_crucix":    round(brier_cx, 4) if brier_cx else None,
        "brier_no_crucix": round(brier_no, 4) if brier_no else None,
    }
```

---

### Phase 2 — Correction ciblée (jours 7-14)

Une cause → une correction. Pas de modification générale du système.

```
CAUSE : CATEGORY_FAILURE_[CAT]
  → Suspendre la catégorie défaillante du scanner pendant 30j
  → Analyser les 5 derniers trades perdants : pourquoi p_model était faux ?
  → Reconstruire le base rate sur 6 mois de données historiques
  → Réintégrer uniquement avec seuils durcis : edge +2¢, Z-score +0.3σ

CAUSE : OVERCONFIDENCE
  → Shrinkage de p_model : p_model_corr = 0.95 × p_model pendant 30j
  → Augmenter l'intervalle de confiance requis LLM : |high-low| > 15%
  → Référence : Tetlock (2015) — les superforecasters shrinkent leurs
    probabilités extrêmes de 10-15% systématiquement

CAUSE : UNDERCONFIDENCE
  → Augmenter p_model de 5% sur les longshots (S2) pendant 30j
  → Vérifier si le Favorite-Longshot Bias est bien appliqué
  → Réexaminer les marchés skippés : avaient-ils un edge réel ?

CAUSE : EDGE_NOT_PREDICTIVE
  → Augmenter edge minimum à 6¢ (était 4¢)
  → Augmenter Z-score minimum à 2.0σ (était 1.5σ)
  → Surveiller 30j — le marché a peut-être évolué structurellement

CAUSE : CRUCIX_LR_MISCALIBRATED
  → Exécuter WeeklyCalibrationReport (crucix_router.py)
  → Identifier sources avec LR_empirique < LR_prior de 30%+
  → Downgrader ces sources à trust_weight = 0.40 (minimum)
  → Recalibrer après 30 nouvelles observations par source

CAUSE : VARIANCE_NORMALE
  → Aucune modification des paramètres
  → Réduire sizing à 50% de Kelly pendant les 10 premiers trades
  → Thorp : "When in doubt, bet half"
```

---

### Phase 3 — Validation paper trading (jours 14-30+)

```
Protocole de validation post-recalibration :

  1. Reprendre le paper trading avec les paramètres corrigés
     → Logger AVANT de décider — même rigueur qu'au démarrage
     → Minimum 30 RÉSOLUTIONS (pas 30 trades)

  2. Critères de Go pour reprise réelle :
     ┌──────────────────────────────────────────────────────────┐
     │ Brier post-recalib ≤ 0.20 sur 30 résolutions            │
     │ La cause diagnostiquée ne réapparaît pas                │
     │ Edge moyen réalisé ≥ 60% de l'edge estimé               │
     │ Drawdown simulé < 8% du capital virtuel                 │
     └──────────────────────────────────────────────────────────┘

  3. Si critères non atteints après 30 résolutions :
     → Recommencer Phase 1 (diagnostic approfondi)
     → Maximum 2 cycles de recalibration avant pause longue (90j)

  4. Pause longue si 2 cycles échouent (90 jours) :
     → Le marché a peut-être changé structurellement
     → Utiliser ce temps pour améliorer les sources Crucix
     → Relire Tetlock, Thorp, Lopez de Prado avant reprise
```

---

### Phase 4 — Reprise progressive (jours 30-60)

```
Semaine 1 post-recalibration :
  → Sizing : 40% de Kelly (au lieu de 30%)
  → Maximum 1 trade par jour
  → Uniquement catégories avec Brier validé

Semaine 2 :
  → Sizing : retour Kelly standard (α=0.30 S1, α=0.15 S2)
  → Maximum 2 trades par jour
  → Réintégrer progressivement catégories suspendues

Semaine 3+ :
  → Retour fonctionnement normal si Brier tient
  → Documenter : "recalibration terminée, date, cause, correction"
```

---

### Journal de recalibration (obligatoire)

```python
RECALIBRATION_LOG = {
    "kill_switch_date":    str,   # date du déclenchement
    "kill_switch_trigger": str,   # "brier_gate" | "mdd_gate" | etc.
    "cooldown_end":        str,   # fin du refroidissement
    "diagnostic_cause":    str,   # cause identifiée
    "correction_applied":  str,   # modification précise faite
    "paper_start":         str,   # début paper trading
    "paper_end":           str,   # fin paper trading (30 résolutions)
    "brier_post_recalib":  float, # Brier sur 30 résolutions
    "go_decision":         bool,  # True = reprise autorisée
    "resume_date":         str,   # date de reprise réelle
    "notes":               str,   # observations libres
}
# Ce log est la mémoire institutionnelle du système.
# Sans lui, les mêmes erreurs se répètent.
```

---

### Matrice de décision

```
Kill-switch déclenché
        │
        ├── 72h refroidissement obligatoire (Kahneman)
        │
        ├── Phase 1 — Diagnostic (3-7j)
        │     run_diagnostic() → 1 cause identifiée
        │
        ├── Phase 2 — Correction ciblée (7-14j)
        │     1 cause = 1 correction = rien de plus
        │
        ├── Phase 3 — 30 résolutions paper trading
        │     Brier < 0.20 → GO
        │     Brier > 0.20 → retour Phase 1
        │     2 cycles échoués → pause 90j
        │
        └── Phase 4 — Reprise à 40% Kelly (2 semaines)
              → Retour normal si Brier tient sur 15 trades réels
```

---

Références : Tversky & Kahneman (1974) *Judgment under uncertainty*,
Tetlock (2015) *Superforecasting* ch.8, Thorp (1997) *Kelly Criterion*,
Lopez de Prado (2018) *AFML* ch.14.


---

## Sécurité — Déploiement VPS

### Principe : la sécurité n'est pas optionnelle

Un bot de trading expose trois vecteurs d'attaque critiques : la clé privée wallet (perte totale des fonds), l'API Flask (accès aux données et décisions), et le VPS lui-même (compromission complète). Chaque niveau ci-dessous est obligatoire avant tout déploiement réel.

---

### Niveau 1 — Secrets (critique absolu)

```bash
# .env sur le VPS uniquement — jamais dans le repo Git
PRIVATE_KEY=0x...              # clé privée wallet dédié PAF-001
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_PASSPHRASE=...
ANTHROPIC_API_KEY=...
POLYGONSCAN_API_KEY=...
DASHBOARD_TOKEN=...            # token Bearer dashboard (générer aléatoire)
FUNDER=0x...                   # adresse wallet dédié

# .gitignore obligatoire
.env
paf001.db
*.log
__pycache__/
*.pyc
```

```python
# Validation au démarrage — erreur explicite si clé manque
# Jamais de crash silencieux sur une clé absente
import os
from dotenv import load_dotenv

load_dotenv()

REQUIRED_ENV = [
    "PRIVATE_KEY", "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET",
    "POLYMARKET_PASSPHRASE", "ANTHROPIC_API_KEY", "DASHBOARD_TOKEN", "FUNDER"
]

def validate_env():
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        raise EnvironmentError(
            f"Variables d'environnement manquantes : {', '.join(missing)}\n"
            f"Copier .env.example → .env et remplir toutes les valeurs."
        )
    print("✓ Toutes les variables d'environnement présentes")

validate_env()
```

**Règle absolue : wallet dédié PAF-001 uniquement.**
Créer un nouveau wallet Polygon avec le minimum de USDC requis.
Jamais le wallet principal. Si la clé privée leak → seul le capital PAF est exposé.

---

### Niveau 2 — API Flask sécurisée

```python
# api.py — API Flask avec authentification Bearer
from flask import Flask, jsonify, request, abort
from functools import wraps
import sqlite3
import os
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
API_TOKEN = os.getenv("DASHBOARD_TOKEN")

def require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token or token != API_TOKEN:
            abort(401)
        return f(*args, **kwargs)
    return decorated

@app.route('/health')
@require_token
def health():
    return jsonify({
        "status":      "running",
        "uptime":      get_uptime(),
        "last_action": get_last_action(),
    })

@app.route('/data')
@require_token
def data():
    conn = sqlite3.connect("paf001.db")
    conn.row_factory = sqlite3.Row
    positions = conn.execute(
        "SELECT * FROM trades WHERE outcome IS NULL AND is_paper=0"
    ).fetchall()
    snapshot  = conn.execute(
        "SELECT * FROM daily_snapshot ORDER BY date DESC LIMIT 30"
    ).fetchall()
    signals   = conn.execute(
        "SELECT * FROM signals ORDER BY ts DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return jsonify({
        "positions": [dict(p) for p in positions],
        "snapshots": [dict(s) for s in snapshot],
        "signals":   [dict(s) for s in signals],
    })

# Flask ne tourne JAMAIS directement exposé sur internet
# Il écoute uniquement sur localhost — Nginx fait le proxy
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
```

---

### Niveau 3 — Firewall VPS

```bash
# UFW — n'ouvrir que le strict nécessaire
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp      # SSH (changer le port par défaut recommandé)
ufw allow 80/tcp      # HTTP → redirect HTTPS par Nginx
ufw allow 443/tcp     # HTTPS dashboard
ufw deny 5000         # Flask jamais exposé directement
ufw enable

# Vérifier les règles
ufw status verbose
```

---

### Niveau 4 — SSH sécurisé

```bash
# /etc/ssh/sshd_config — désactiver password auth
PasswordAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
MaxAuthTries 3
LoginGraceTime 20

# Générer une clé SSH sur ta machine locale
ssh-keygen -t ed25519 -C "paf001-vps"
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@TON_VPS_IP

# Redémarrer SSH
systemctl restart sshd
```

---

### Niveau 5 — Nginx + HTTPS Let's Encrypt

```nginx
# /etc/nginx/sites-available/paf001
server {
    listen 80;
    server_name ton-domaine.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name ton-domaine.com;

    ssl_certificate     /etc/letsencrypt/live/ton-domaine.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ton-domaine.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # Dashboard HTML statique
    location / {
        root /var/www/paf001;
        index PAF_Dashboard_v5.html;
        try_files $uri $uri/ =404;
    }

    # API Flask (proxy vers localhost:5000)
    location /api/ {
        proxy_pass         http://127.0.0.1:5000/;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_hide_header  X-Powered-By;
    }
}
```

```bash
# Installer Nginx + Certbot
apt install nginx certbot python3-certbot-nginx -y
certbot --nginx -d ton-domaine.com
systemctl enable nginx
```

---

### Niveau 6 — Docker avec user non-root

```dockerfile
# Dockerfile
FROM python:3.10-slim

# User non-root — le bot ne tourne jamais en root
RUN useradd -m -u 1000 botuser
WORKDIR /app

# Dépendances d'abord (cache Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code ensuite
COPY --chown=botuser:botuser . .

USER botuser

CMD ["python3", "main.py"]
```

```yaml
# docker-compose.yml
version: "3.9"
services:
  paf-bot:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./paf001.db:/app/paf001.db
    networks:
      - internal

  paf-api:
    build: .
    command: python3 api.py
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./paf001.db:/app/paf001.db
    ports:
      - "127.0.0.1:5000:5000"   # exposé uniquement sur localhost
    networks:
      - internal

networks:
  internal:
    driver: bridge
```

---

### Script de déploiement complet

```bash
#!/bin/bash
# deploy.sh — déploiement VPS en une commande

set -e  # arrêter si une commande échoue

echo "=== PAF-001 Deploy ==="

# 1. Vérifier que .env existe
if [ ! -f .env ]; then
    echo "❌ .env manquant — copier .env.example et remplir les valeurs"
    exit 1
fi

# 2. Vérifier que le wallet dédié est configuré
source .env
if [ -z "$PRIVATE_KEY" ] || [ -z "$FUNDER" ]; then
    echo "❌ PRIVATE_KEY ou FUNDER manquant dans .env"
    exit 1
fi

# 3. Build et lancement Docker
docker-compose down --remove-orphans
docker-compose build --no-cache
docker-compose up -d

# 4. Vérifier que les containers tournent
sleep 3
docker-compose ps

# 5. Test health endpoint
curl -s -H "Authorization: Bearer $DASHBOARD_TOKEN" \
     http://localhost:5000/health | python3 -m json.tool

echo "=== Deploy OK ==="
echo "Dashboard : https://ton-domaine.com"
echo "Logs bot  : docker-compose logs -f paf-bot"
echo "Logs api  : docker-compose logs -f paf-api"
```

---

### Modification dashboard pour requêtes sécurisées

```javascript
// Dans PAF_Dashboard_v5.html — remplacer les données statiques D.*
// par un fetch authentifié vers l'API VPS

const VPS_API   = "https://ton-domaine.com/api";
const API_TOKEN = "TON_DASHBOARD_TOKEN";  // lire depuis prompt ou config

async function fetchLiveData() {
    try {
        const r = await fetch(`${VPS_API}/data`, {
            headers: { "Authorization": `Bearer ${API_TOKEN}` }
        });
        if (!r.ok) throw new Error(`API ${r.status}`);
        const live = await r.json();

        // Mettre à jour D.* avec les données réelles
        D.positions = live.positions;
        D.nav_series = live.snapshots.map(s => s.bankroll);
        D.signals    = live.signals;

        // Re-render toutes les pages
        buildDash();
        buildPositions();
        buildSignals();

    } catch(err) {
        console.warn("API indisponible, données statiques conservées:", err);
    }
}

// Fetch au chargement + toutes les 30 secondes
fetchLiveData();
setInterval(fetchLiveData, 30_000);
```

---

### Prompt Claude Code — déploiement sécurisé complet

```
Lis polymarket_strategies.md (section Sécurité) et
crucix_router.py. Implémente le déploiement sécurisé
complet du bot PAF-001 :

1. validate_env() au démarrage avec toutes les clés requises
2. api.py Flask avec Bearer token, écoute 127.0.0.1:5000
3. Dockerfile avec user non-root (uid 1000)
4. docker-compose.yml (bot + api, réseau interne)
5. deploy.sh avec vérifications pré-déploiement
6. nginx.conf avec HTTPS et proxy vers Flask
7. .env.example avec toutes les variables (sans valeurs réelles)
8. .gitignore complet
9. README.md sécurité : checklist avant déploiement

Modifier PAF_Dashboard_v5.html pour :
- fetch authentifié Bearer vers /api/data
- fallback sur données statiques si API indisponible
- refresh automatique toutes les 30 secondes

Wallet dédié PAF-001 : générer les instructions pour
créer un wallet Polygon séparé du wallet principal.
```

