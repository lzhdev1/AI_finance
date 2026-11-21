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
