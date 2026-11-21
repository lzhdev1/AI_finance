SET LOCAL search_path TO quantlab, public;

CREATE TABLE IF NOT EXISTS agent_research (
    report_id bigserial PRIMARY KEY,
    as_of timestamptz NOT NULL,
    data_as_of date,
    mode text NOT NULL,
    payload jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_research_recent ON agent_research (as_of DESC, report_id DESC);

CREATE TABLE IF NOT EXISTS agent_board_forecast (
    forecast_id bigserial PRIMARY KEY,
    issued_at timestamptz NOT NULL,
    board_id integer NOT NULL REFERENCES board(board_id),
    board text NOT NULL,
    proxy_code text NOT NULL DEFAULT '',
    base_date date NOT NULL,
    target_date date NOT NULL CHECK (target_date > base_date),
    horizon_days integer NOT NULL CHECK (horizon_days IN (1,5)),
    prob_up double precision NOT NULL CHECK (prob_up BETWEEN 0 AND 1),
    expected_return double precision NOT NULL,
    confidence text NOT NULL,
    samples integer NOT NULL,
    realized_return double precision,
    hit boolean,
    scored_at timestamptz,
    UNIQUE(board_id, base_date, horizon_days)
);
CREATE INDEX IF NOT EXISTS agent_forecast_due ON agent_board_forecast(target_date) WHERE scored_at IS NULL;

CREATE TABLE IF NOT EXISTS agent_report_forecast (
    report_id bigint NOT NULL REFERENCES agent_research(report_id),
    forecast_id bigint NOT NULL REFERENCES agent_board_forecast(forecast_id),
    PRIMARY KEY (report_id, forecast_id)
);

-- One classification at first issue. Subsequent reports can revise their outlook
-- for risk management, but cannot relabel the training observation.
CREATE TABLE IF NOT EXISTS agent_behavior_observation (
    forecast_id bigint PRIMARY KEY REFERENCES agent_board_forecast(forecast_id),
    version integer NOT NULL,
    regime text NOT NULL,
    base_prob_up double precision NOT NULL CHECK (base_prob_up BETWEEN 0 AND 1),
    feedback jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_fund_forecast (
    forecast_id bigserial PRIMARY KEY,
    report_id bigint NOT NULL REFERENCES agent_research(report_id),
    instrument_id integer NOT NULL REFERENCES instrument(instrument_id),
    issued_at timestamptz NOT NULL,
    base_date date NOT NULL,
    target_date date NOT NULL CHECK (target_date > base_date),
    horizon_days integer NOT NULL CHECK (horizon_days IN (1,5)),
    evidence jsonb NOT NULL,
    base_nav numeric(12,6),
    target_nav numeric(12,6),
    realized_return double precision,
    hit boolean,
    scored_at timestamptz,
    UNIQUE (instrument_id, base_date, horizon_days)
);
CREATE INDEX IF NOT EXISTS agent_fund_due ON agent_fund_forecast(target_date) WHERE scored_at IS NULL;
CREATE TABLE IF NOT EXISTS agent_report_fund_forecast (
    report_id bigint NOT NULL REFERENCES agent_research(report_id),
    forecast_id bigint NOT NULL REFERENCES agent_fund_forecast(forecast_id),
    PRIMARY KEY (report_id, forecast_id)
);

-- Actual service calls are metered by wall-clock time, independently of simulated NAV dates.
CREATE TABLE IF NOT EXISTS usage_tracking (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO usage_tracking(singleton) VALUES (true) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS agent_usage_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    context_id uuid NOT NULL,
    run_id bigint,
    scope text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('llm','search','data','tool')),
    provider text NOT NULL,
    operation text NOT NULL,
    model text,
    status text NOT NULL CHECK (status IN ('success','error')),
    input_tokens bigint CHECK (input_tokens >= 0),
    output_tokens bigint CHECK (output_tokens >= 0),
    total_tokens bigint CHECK (total_tokens >= 0),
    cached_tokens bigint CHECK (cached_tokens >= 0),
    reasoning_tokens bigint CHECK (reasoning_tokens >= 0),
    cost_basis text NOT NULL CHECK (cost_basis IN ('reported','estimated','no_charge','unknown')),
    amount numeric CHECK (amount >= 0),
    currency text CHECK (currency ~ '^[A-Z]{3}$'),
    price_note text,
    source_url text,
    detail text,
    CHECK ((cost_basis IN ('reported','estimated') AND amount IS NOT NULL AND currency IS NOT NULL)
        OR (cost_basis='unknown' AND amount IS NULL AND currency IS NULL)
        OR (cost_basis='no_charge' AND amount=0 AND amount IS NOT NULL AND currency IS NULL))
);
CREATE INDEX IF NOT EXISTS usage_by_time ON agent_usage_event(occurred_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS usage_by_context ON agent_usage_event(context_id);

CREATE TABLE IF NOT EXISTS agent_usage_report (
    report_id bigint PRIMARY KEY REFERENCES agent_research(report_id) ON DELETE CASCADE,
    context_id uuid NOT NULL UNIQUE
);
