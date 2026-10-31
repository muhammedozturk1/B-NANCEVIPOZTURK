-- Otomatik üretildi: python -m scripts.smc.export_schema

CREATE TABLE IF NOT EXISTS smc_audit_logs (
	id SERIAL NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	actor VARCHAR(64) NOT NULL, 
	action VARCHAR(64) NOT NULL, 
	details JSON NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_smc_audit_logs_ts ON smc_audit_logs (ts);

CREATE TABLE IF NOT EXISTS smc_bot_commands (
	id SERIAL NOT NULL, 
	command VARCHAR(32) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	requested_by VARCHAR(64) NOT NULL, 
	result TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	processed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS smc_bot_config (
	id SERIAL NOT NULL, 
	key VARCHAR(64) NOT NULL, 
	value JSON NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (key)
);

CREATE TABLE IF NOT EXISTS smc_bot_state (
	id INTEGER NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	reason TEXT, 
	safe_mode_reasons JSON NOT NULL, 
	manual_hold_reasons JSON NOT NULL, 
	last_heartbeat_at TIMESTAMP WITH TIME ZONE, 
	worker_started_at TIMESTAMP WITH TIME ZONE, 
	ws_market_connected BOOLEAN NOT NULL, 
	ws_user_connected BOOLEAN NOT NULL, 
	rest_ok BOOLEAN NOT NULL, 
	clock_offset_ms INTEGER NOT NULL, 
	day_key VARCHAR(10), 
	day_start_equity FLOAT, 
	month_start_equity FLOAT, 
	month_key VARCHAR(7), 
	consecutive_losses INTEGER NOT NULL, 
	cooldown_until TIMESTAMP WITH TIME ZONE, 
	equity FLOAT, 
	wallet_balance FLOAT, 
	available_balance FLOAT, 
	margin_used FLOAT, 
	unrealized_pnl FLOAT, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS smc_market_snapshots (
	id SERIAL NOT NULL, 
	symbol VARCHAR(32) NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	mark_price FLOAT, 
	spread_pct FLOAT, 
	depth_usdt FLOAT, 
	open_interest FLOAT, 
	funding_rate FLOAT, 
	fear_greed INTEGER, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_smc_market_snapshots_symbol ON smc_market_snapshots (symbol);

CREATE INDEX IF NOT EXISTS ix_smc_market_snapshots_ts ON smc_market_snapshots (ts);

CREATE TABLE IF NOT EXISTS smc_pnl_snapshots (
	id SERIAL NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	equity FLOAT NOT NULL, 
	available_balance FLOAT NOT NULL, 
	unrealized_pnl FLOAT NOT NULL, 
	margin_used FLOAT NOT NULL, 
	open_risk FLOAT NOT NULL, 
	daily_pnl FLOAT NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_smc_pnl_snapshots_ts ON smc_pnl_snapshots (ts);

CREATE TABLE IF NOT EXISTS smc_risk_events (
	symbol VARCHAR(32), 
	id SERIAL NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	kind VARCHAR(48) NOT NULL, 
	details JSON NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_smc_risk_events_ts ON smc_risk_events (ts);

CREATE TABLE IF NOT EXISTS smc_signals (
	id SERIAL NOT NULL, 
	symbol VARCHAR(32) NOT NULL, 
	side VARCHAR(8) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	candle_time BIGINT NOT NULL, 
	score INTEGER NOT NULL, 
	max_score INTEGER NOT NULL, 
	accepted BOOLEAN NOT NULL, 
	reject_reason TEXT, 
	entry FLOAT, 
	stop FLOAT, 
	tp1 FLOAT, 
	tp2 FLOAT, 
	tp3 FLOAT, 
	snapshot JSON NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_smc_signal_candle UNIQUE (symbol, side, candle_time)
);

CREATE INDEX IF NOT EXISTS ix_smc_signals_created_at ON smc_signals (created_at);

CREATE INDEX IF NOT EXISTS ix_smc_signals_symbol ON smc_signals (symbol);

CREATE TABLE IF NOT EXISTS smc_symbols (
	symbol VARCHAR(32) NOT NULL, 
	tick_size VARCHAR(32) NOT NULL, 
	step_size VARCHAR(32) NOT NULL, 
	min_qty VARCHAR(32) NOT NULL, 
	max_qty VARCHAR(32) NOT NULL, 
	market_step_size VARCHAR(32) NOT NULL, 
	market_max_qty VARCHAR(32) NOT NULL, 
	min_notional FLOAT NOT NULL, 
	max_leverage INTEGER NOT NULL, 
	maint_margin_rate FLOAT NOT NULL, 
	maint_amount FLOAT NOT NULL, 
	in_universe BOOLEAN NOT NULL, 
	quote_volume_24h FLOAT, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (symbol)
);

CREATE TABLE IF NOT EXISTS smc_system_events (
	level VARCHAR(10) NOT NULL, 
	message TEXT NOT NULL, 
	id SERIAL NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	kind VARCHAR(48) NOT NULL, 
	details JSON NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_smc_system_events_ts ON smc_system_events (ts);

CREATE TABLE IF NOT EXISTS smc_telegram_events (
	text TEXT NOT NULL, 
	sent BOOLEAN NOT NULL, 
	error TEXT, 
	id SERIAL NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	kind VARCHAR(48) NOT NULL, 
	details JSON NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_smc_telegram_events_ts ON smc_telegram_events (ts);

CREATE TABLE IF NOT EXISTS smc_trial_state (
	id INTEGER NOT NULL, 
	trial_started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	trial_expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expired_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS smc_users (
	id SERIAL NOT NULL, 
	username VARCHAR(64) NOT NULL, 
	password_hash VARCHAR(256) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	last_login_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (username)
);

CREATE TABLE IF NOT EXISTS smc_positions (
	id SERIAL NOT NULL, 
	signal_id INTEGER, 
	symbol VARCHAR(32) NOT NULL, 
	side VARCHAR(8) NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	leverage INTEGER NOT NULL, 
	planned_entry FLOAT NOT NULL, 
	entry_price FLOAT, 
	requested_qty FLOAT NOT NULL, 
	filled_qty FLOAT NOT NULL, 
	remaining_qty FLOAT NOT NULL, 
	notional FLOAT, 
	initial_stop FLOAT NOT NULL, 
	stop_price FLOAT NOT NULL, 
	tp1_price FLOAT, 
	tp2_price FLOAT, 
	tp3_price FLOAT, 
	tp_plan JSON NOT NULL, 
	risk_amount FLOAT NOT NULL, 
	open_risk FLOAT NOT NULL, 
	liq_price FLOAT, 
	be_active BOOLEAN NOT NULL, 
	tp_missing BOOLEAN NOT NULL, 
	signal_score INTEGER, 
	exit_reason VARCHAR(32), 
	opened_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	entry_filled_at TIMESTAMP WITH TIME ZONE, 
	closed_at TIMESTAMP WITH TIME ZONE, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(signal_id) REFERENCES smc_signals (id)
);

CREATE INDEX IF NOT EXISTS ix_smc_positions_symbol ON smc_positions (symbol);

CREATE INDEX IF NOT EXISTS ix_smc_positions_state ON smc_positions (state);

CREATE TABLE IF NOT EXISTS smc_signal_components (
	id SERIAL NOT NULL, 
	signal_id INTEGER NOT NULL, 
	name VARCHAR(32) NOT NULL, 
	points INTEGER NOT NULL, 
	max_points INTEGER NOT NULL, 
	passed BOOLEAN NOT NULL, 
	mandatory BOOLEAN NOT NULL, 
	details JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(signal_id) REFERENCES smc_signals (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_smc_signal_components_signal_id ON smc_signal_components (signal_id);

CREATE TABLE IF NOT EXISTS smc_fills (
	id SERIAL NOT NULL, 
	position_id INTEGER, 
	symbol VARCHAR(32) NOT NULL, 
	exchange_trade_id VARCHAR(40) NOT NULL, 
	exchange_order_id VARCHAR(40), 
	side VARCHAR(8) NOT NULL, 
	price FLOAT NOT NULL, 
	qty FLOAT NOT NULL, 
	commission FLOAT NOT NULL, 
	commission_asset VARCHAR(16), 
	realized_pnl FLOAT NOT NULL, 
	is_maker BOOLEAN NOT NULL, 
	ts TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_smc_fill_trade UNIQUE (symbol, exchange_trade_id), 
	FOREIGN KEY(position_id) REFERENCES smc_positions (id)
);

CREATE INDEX IF NOT EXISTS ix_smc_fills_position_id ON smc_fills (position_id);

CREATE TABLE IF NOT EXISTS smc_orders (
	id SERIAL NOT NULL, 
	position_id INTEGER, 
	symbol VARCHAR(32) NOT NULL, 
	client_id VARCHAR(40) NOT NULL, 
	exchange_id VARCHAR(40), 
	is_algo BOOLEAN NOT NULL, 
	purpose VARCHAR(16) NOT NULL, 
	side VARCHAR(8) NOT NULL, 
	type VARCHAR(32) NOT NULL, 
	price FLOAT, 
	trigger_price FLOAT, 
	qty FLOAT NOT NULL, 
	filled_qty FLOAT NOT NULL, 
	avg_price FLOAT, 
	status VARCHAR(24) NOT NULL, 
	active BOOLEAN NOT NULL, 
	raw JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(position_id) REFERENCES smc_positions (id), 
	UNIQUE (client_id)
);

CREATE INDEX IF NOT EXISTS ix_smc_orders_position_id ON smc_orders (position_id);

CREATE TABLE IF NOT EXISTS smc_trades (
	id SERIAL NOT NULL, 
	position_id INTEGER NOT NULL, 
	symbol VARCHAR(32) NOT NULL, 
	side VARCHAR(8) NOT NULL, 
	entry_price FLOAT NOT NULL, 
	entry_time TIMESTAMP WITH TIME ZONE NOT NULL, 
	quantity FLOAT NOT NULL, 
	notional FLOAT NOT NULL, 
	leverage INTEGER NOT NULL, 
	stop_price FLOAT NOT NULL, 
	tp1_price FLOAT, 
	tp2_price FLOAT, 
	tp3_price FLOAT, 
	risk_amount FLOAT NOT NULL, 
	planned_rr FLOAT, 
	realized_rr FLOAT, 
	gross_pnl FLOAT NOT NULL, 
	net_pnl FLOAT NOT NULL, 
	commission FLOAT NOT NULL, 
	funding FLOAT NOT NULL, 
	slippage FLOAT NOT NULL, 
	exit_price FLOAT, 
	exit_time TIMESTAMP WITH TIME ZONE NOT NULL, 
	exit_reason VARCHAR(32) NOT NULL, 
	signal_score INTEGER, 
	tp_hits JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (position_id), 
	FOREIGN KEY(position_id) REFERENCES smc_positions (id)
);

CREATE INDEX IF NOT EXISTS ix_smc_trades_symbol ON smc_trades (symbol);
