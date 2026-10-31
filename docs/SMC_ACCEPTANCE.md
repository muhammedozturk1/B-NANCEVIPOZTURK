# SMC Scalper — acceptance criteria → kod eşlemesi

| Kriter | Uygulama | Test |
|---|---|---|
| İşlem başına risk %5, equity'ye göre dinamik | `backend/smc/risk/sizing.py::compute_position_size` | `tests/smc/test_sizing.py` |
| Max leverage 10x, borsa limiti, risk ile karışmıyor | `sizing.py` (lev_cap, margin), `config.py` doğrulaması | `test_leverage_*` |
| Max 3 pozisyon, toplam açık risk %15 | `backend/smc/risk/engine.py::evaluate_trade` | `tests/smc/test_risk_engine.py` |
| Günlük max zarar %10 | `engine.py::daily_loss_breached`, `backend/smc/worker.py::refresh_account` | `test_daily_loss_limit_blocks` |
| 3 ardışık SL → 30 dk cooldown | `engine.py::register_trade_result` | `test_cooldown_after_three_consecutive_sl` |
| Isolated margin, one-way | `gateway.ensure_symbol_setup`, `backend/smc/worker.py::bootstrap` | bootstrap testi |
| Native SL / TP, partial TP, BE | `backend/smc/execution/trade_manager.py` | `test_tp1_moves_sl_to_breakeven...` |
| Partial fill yönetimi | `trade_manager._open` | `test_partial_fill_uses_filled_qty_only` |
| WebSocket + reconnect | `backend/smc/market_data/ws.py` | entegrasyon |
| REST rate limiter, server time | `backend/smc/market_data/rest.py` | `tests/smc/test_infra.py` |
| PostgreSQL, 17 tablo + komut kuyruğu (hepsi smc_ önekli) | `backend/smc/database/models.py`, `migrations/smc_0001_initial.sql` | tüm testler |
| Restart reconciliation | `backend/smc/execution/reconciliation.py` | `test_reconcile_*` |
| 72 saat trial persistence | `backend/smc/core/trial.py`, `backend/smc/worker.py::expire` | `test_trial_*`, `test_worker_expire_*` |
| Kill switch | `backend/smc/execution/kill_switch.py`, `backend/smc/worker.py::kill_switch` | `test_kill_switch_*`, `test_worker_kill_switch_and_resume` |
| Dashboard auth | `backend/smc/api/auth.py`, `backend/smc/api/main.py` (session + CSRF + rate limit) | `test_dashboard_auth_health_and_kill_csrf` |
| Telegram alerts | `backend/smc/notifications/telegram.py` | RecordingNotifier ile |
| API secret güvenliği | `.env.example`, `.gitignore`, `render.yaml` (`sync: false`), CI kontrolü | — |
| Symbol filters | `backend/smc/market_data/universe.py`, `sizing.round_*` | `test_*rounding*` |
| Liquidation buffer | `sizing.liquidation_buffer_ok` | `test_liquidation_*` |
| Commission/slippage | `sizing.per_unit_loss`, `trade_manager._summarize` | `test_fees_and_slippage_*` |
| Backtest (aynı motorlar, look-ahead yok) | `backend/smc/backtest/engine.py` | `test_backtest_*`, `test_no_look_ahead_*` |
| Integration tests | `tests/smc/integration/test_testnet.py` | `RUN_TESTNET_INTEGRATION=1` |
| Audit logs | `repo.audit`, `repo.transition` | — |
