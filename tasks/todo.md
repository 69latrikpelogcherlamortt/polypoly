# Remédiation PAF-001 — Checklist

## BLOC 0 — Initialisation
- [x] Lire tout le codebase
- [x] Créer structure tasks/
- [ ] Git init + snapshot initial

## BLOC 1 — Sécurité & Secrets (3/10 → 9/10)
- [ ] Créer .gitignore complet
- [ ] Sanitiser logs Telegram
- [ ] Dashboard auth via header (pas query string)
- [ ] Vérification permissions fichier clé

## BLOC 2 — Résilience (2.5/10 → 9/10)
- [ ] Graceful shutdown SIGTERM
- [ ] Scan ordres orphelins au démarrage
- [ ] Idempotency keys
- [ ] State recovery bankroll
- [ ] Circuit breaker API
- [ ] Kill switch manuel (.emergency_stop)

## BLOC 3 — Performance (3.5/10 → 9/10)
- [ ] Remplacer requests par aiohttp
- [ ] Remplacer time.sleep par asyncio.sleep
- [ ] SQLite WAL mode + indexes
- [ ] Log rotation (RotatingFileHandler)
- [ ] Troncation _returns_history (deque)
- [ ] Éliminer f-strings dans logs

## BLOC 4 — Risk Management (6/10 → 9/10)
- [ ] Max daily loss kill switch
- [ ] Concentration risk par catégorie
- [ ] Close position avec repricing AC
- [ ] DB helpers pour nouveaux kill switches

## BLOC 5 — Tests & CI/CD (0/10 → 9/10)
- [ ] conftest.py + fixtures
- [ ] test_sizing.py (10+ tests)
- [ ] test_gates.py (8+ tests)
- [ ] test_kill_switches.py (10+ tests)
- [ ] test_bayes.py (6+ tests)
- [ ] test_exit_rules.py (8+ tests)
- [ ] test_circuit_breaker.py
- [ ] test_database.py
- [ ] Integration: test_full_trade_flow.py
- [ ] GitHub Actions CI
- [ ] Pin dépendances (requirements.lock)

## BLOC 6 — Logging & Observabilité (4/10 → 9/10)
- [ ] JSON formatter
- [ ] Correlation IDs (ContextVar)
- [ ] TelegramHandler pour ERROR/CRITICAL
- [ ] Backup automatique SQLite

## BLOC 7 — Architecture (6.5/10 → 9/10)
- [ ] Corriger exceptions silencieuses
- [ ] Supprimer double basicConfig
- [ ] Type hints + pyproject.toml mypy

## BLOC 8 — Vérification finale
- [ ] Tous tests passent
- [ ] Couverture ≥ 80% modules critiques
- [ ] Smoke test DRY_RUN 30s
- [ ] audit_after.md rempli
