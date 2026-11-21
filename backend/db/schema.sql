-- ============================================================================
-- QuantLab · A股基金投研 Agent · PostgreSQL 业务表结构
--
-- 设计约定：
--   1. 金额一律 numeric，禁止 float（浮点误差会污染净值与超额收益）。
--   2. 日期用 date（交易日），时间用 timestamptz（抓取时刻/发布时间）。
--   3. 所有外部数据表都带 source + ingested_at，保证「这条数据从哪来、什么时候进来的」可追溯。
--   4. 业务表分四域：market（行情/资金/新闻）、instruments（基金主数据与净值）、
--      pods（模拟仓全集）、ops（流水线与数据质量）。
--   5. 主键优先用业务主键（如 (board_id, trade_date)），天然幂等，重复抓取不会产生脏行。
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS quantlab;
SET search_path TO quantlab, public;

-- ============================================================================
-- 域一：market —— 市场侧数据
-- ============================================================================

-- 交易日历：只存交易日，存在即开盘
CREATE TABLE IF NOT EXISTS trade_calendar (
    trade_date  date        PRIMARY KEY,
    source      text        NOT NULL DEFAULT 'sina',
    ingested_at timestamptz NOT NULL DEFAULT now()
);

-- 板块（行业 / 概念 / 主题）
CREATE TABLE IF NOT EXISTS board (
    board_id   serial      PRIMARY KEY,
    code       text        NOT NULL,                       -- 数据源代码，如 BK0475
    name       text        NOT NULL,
    kind       text        NOT NULL CHECK (kind IN ('industry', 'concept')),
    source     text        NOT NULL DEFAULT 'eastmoney',
    is_active  boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, code)
);
CREATE INDEX IF NOT EXISTS idx_board_kind ON board (kind) WHERE is_active;

-- 板块日行情
CREATE TABLE IF NOT EXISTS board_quote (
    board_id      integer       NOT NULL REFERENCES board (board_id) ON DELETE CASCADE,
    trade_date    date          NOT NULL,
    close         numeric(12,4) NOT NULL,
    open          numeric(12,4),
    high          numeric(12,4),
    low           numeric(12,4),
    pct_chg       numeric(9,4),                            -- 涨跌幅（%）
    amplitude     numeric(9,4),                            -- 振幅（%）
    turnover_rate numeric(9,4),                            -- 换手率（%）
    amount        numeric(20,2),                           -- 成交额（元）
    volume        numeric(20,2),
    source        text          NOT NULL DEFAULT 'eastmoney',
    ingested_at   timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (board_id, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_board_quote_date ON board_quote (trade_date);

-- 板块资金流（主力 / 超大单 / 大单 / 中单 / 小单），按统计周期分档
CREATE TABLE IF NOT EXISTS board_fund_flow (
    board_id         integer       NOT NULL REFERENCES board (board_id) ON DELETE CASCADE,
    trade_date       date          NOT NULL,
    period           text          NOT NULL DEFAULT '1d' CHECK (period IN ('1d', '5d', '10d')),
    main_net         numeric(20,2),                        -- 主力净流入（元）
    main_net_pct     numeric(9,4),                         -- 主力净占比（%）
    super_large_net  numeric(20,2),
    large_net        numeric(20,2),
    medium_net       numeric(20,2),
    small_net        numeric(20,2),
    close_pct        numeric(9,4),                         -- 当日涨跌幅（%）
    source           text          NOT NULL DEFAULT 'eastmoney',
    ingested_at      timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (board_id, trade_date, period)
);
CREATE INDEX IF NOT EXISTS idx_board_flow_date ON board_fund_flow (trade_date, period);

-- 大盘资金流（全市场）
CREATE TABLE IF NOT EXISTS market_fund_flow (
    trade_date      date          PRIMARY KEY,
    main_net        numeric(20,2),
    main_net_pct    numeric(9,4),
    super_large_net numeric(20,2),
    large_net       numeric(20,2),
    medium_net      numeric(20,2),
    small_net       numeric(20,2),
    close_pct       numeric(9,4),
    source          text          NOT NULL DEFAULT 'eastmoney',
    ingested_at     timestamptz   NOT NULL DEFAULT now()
);

-- 指数行情（基准、对比用）
CREATE TABLE IF NOT EXISTS index_quote (
    index_code  text          NOT NULL,
    trade_date  date          NOT NULL,
    close       numeric(12,4) NOT NULL,
    pct_chg     numeric(9,4),
    source      text          NOT NULL DEFAULT 'eastmoney',
    ingested_at timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (index_code, trade_date)
);

-- 新闻（带发布时间，回测只允许用 published_at <= 决策时点的记录）
CREATE TABLE IF NOT EXISTS news_item (
    news_id      bigserial   PRIMARY KEY,
    source       text        NOT NULL,                     -- cls / eastmoney / cctv
    external_id  text,
    published_at timestamptz NOT NULL,
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    title        text        NOT NULL,
    body         text,
    url          text,
    content_hash text        NOT NULL,                     -- 去重指纹
    sentiment    numeric(5,4),                             -- -1 ~ 1，分析后回填
    themes       jsonb,                                    -- 命中的板块/主题
    UNIQUE (content_hash)
);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_item (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_themes ON news_item USING gin (themes);

-- ============================================================================
-- 域二：instruments —— 基金主数据与净值
-- ============================================================================

CREATE TABLE IF NOT EXISTS instrument (
    instrument_id serial      PRIMARY KEY,
    code          text        NOT NULL UNIQUE,
    name          text        NOT NULL,
    kind          text        NOT NULL CHECK (kind IN ('otc', 'etf')),  -- 场外 / 场内
    share_class   text        CHECK (share_class IN ('A', 'C') OR share_class IS NULL),
    role          text        NOT NULL DEFAULT 'risk'
                  CHECK (role IN ('risk', 'defensive')),              -- 防御资产不受单只上限约束
    track_index   text,                                    -- 跟踪指数或业绩基准
    is_active     boolean     NOT NULL DEFAULT true,
    listed_on     date,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- 老库补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
ALTER TABLE quantlab.instrument ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'risk';

-- 费率（外扣申购费 + 按持有天数分档赎回费）
CREATE TABLE IF NOT EXISTS instrument_fee (
    instrument_id     integer      NOT NULL REFERENCES instrument (instrument_id) ON DELETE CASCADE,
    side              text         NOT NULL CHECK (side IN ('buy', 'sell')),
    holding_days_max  integer      NOT NULL,               -- 持有天数 < 该值适用本档；buy 用 999999
    rate              numeric(8,6) NOT NULL,
    PRIMARY KEY (instrument_id, side, holding_days_max)
);

-- 基金净值
CREATE TABLE IF NOT EXISTS instrument_nav (
    instrument_id integer       NOT NULL REFERENCES instrument (instrument_id) ON DELETE CASCADE,
    trade_date    date          NOT NULL,
    nav           numeric(12,6) NOT NULL,                  -- 单位净值
    acc_nav       numeric(12,6),                           -- 累计净值（仅供参考，含分红跳变）
    source        text          NOT NULL DEFAULT 'eastmoney',
    ingested_at   timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_nav_date ON instrument_nav (trade_date);

-- 基金 ↔ 板块 暴露关系（一只基金可对应多个板块，权重表示主要暴露）
CREATE TABLE IF NOT EXISTS instrument_board (
    instrument_id integer     NOT NULL REFERENCES instrument (instrument_id) ON DELETE CASCADE,
    board_id      integer     NOT NULL REFERENCES board (board_id) ON DELETE CASCADE,
    weight        numeric(6,4) NOT NULL DEFAULT 1.0,
    source        text        NOT NULL DEFAULT 'manual',
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, board_id)
);

-- ============================================================================
-- 域三：pods —— 模拟仓（agent 自主创建/调仓/退役）
-- ============================================================================

-- 仓的身份证。thesis / invalidation_rules 在建仓当天写死，之后只读，防止事后改口。
CREATE TABLE IF NOT EXISTS pod (
    pod_id            serial      PRIMARY KEY,
    pod_no            integer     NOT NULL UNIQUE,          -- 展示用编号 #1 #2
    name              text        NOT NULL,
    strategy_key      text        NOT NULL,                 -- momentum_flow / reversal / defensive ...
    thesis            text        NOT NULL,                 -- 为什么看好（建仓时写死）
    entry_evidence    jsonb       NOT NULL DEFAULT '{}',    -- 建仓依据的证据快照
    invalidation_rules jsonb      NOT NULL DEFAULT '[]',    -- 失效条件（可量化）
    benchmark_code    text        NOT NULL DEFAULT '000300',
    initial_capital   numeric(14,2) NOT NULL DEFAULT 100000,
    cap_amount        numeric(14,2) NOT NULL DEFAULT 100000,
    status            text        NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'observing', 'suspended', 'retired')),
    opened_on         date        NOT NULL,
    min_eval_days     integer     NOT NULL DEFAULT 20,      -- 最短考核期（交易日）
    closed_on         date,
    close_reason      text,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pod_status ON pod (status);

-- 每仓每日净值与绩效
CREATE TABLE IF NOT EXISTS pod_nav (
    pod_id           integer       NOT NULL REFERENCES pod (pod_id) ON DELETE CASCADE,
    trade_date       date          NOT NULL,
    total_value      numeric(14,2) NOT NULL,                -- 总资产
    cash             numeric(14,2) NOT NULL DEFAULT 0,
    market_value     numeric(14,2) NOT NULL DEFAULT 0,
    unit_nav         numeric(16,6) NOT NULL,                -- 起点 1.0
    daily_return     numeric(10,6),
    cum_return       numeric(10,6),
    drawdown         numeric(10,6),                         -- 负数，当前回撤
    benchmark_return numeric(10,6),                         -- 同期基准累计收益（可比口径）
    excess_return    numeric(10,6),                         -- 相对基准超额
    PRIMARY KEY (pod_id, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_pod_nav_date ON pod_nav (trade_date);

-- 每日持仓快照
CREATE TABLE IF NOT EXISTS pod_position (
    pod_id        integer       NOT NULL REFERENCES pod (pod_id) ON DELETE CASCADE,
    trade_date    date          NOT NULL,
    instrument_id integer       NOT NULL REFERENCES instrument (instrument_id),
    weight        numeric(8,6)  NOT NULL,                   -- 持仓比例 0~1
    shares        numeric(18,6),
    market_value  numeric(14,2),
    avg_cost      numeric(14,6),
    PRIMARY KEY (pod_id, trade_date, instrument_id)
);

-- 订单（decided_on 决策日，execute_on 成交净值日；场外基金两者相差 1 天）
CREATE TABLE IF NOT EXISTS pod_order (
    order_id      bigserial   PRIMARY KEY,
    pod_id        integer     NOT NULL REFERENCES pod (pod_id) ON DELETE CASCADE,
    decided_on    date        NOT NULL,
    execute_on    date,
    instrument_id integer     NOT NULL REFERENCES instrument (instrument_id),
    side          text        NOT NULL CHECK (side IN ('buy', 'sell')),
    amount        numeric(14,2) NOT NULL,                   -- 成交金额
    shares        numeric(18,6),
    nav           numeric(12,6),
    fee           numeric(12,4) NOT NULL DEFAULT 0,
    reason        text,
    evidence      jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_order_pod_date ON pod_order (pod_id, decided_on);

-- 每日决策卡（每仓每天一张，唯一）
CREATE TABLE IF NOT EXISTS pod_decision (
    decision_id      bigserial   PRIMARY KEY,
    pod_id           integer     NOT NULL REFERENCES pod (pod_id) ON DELETE CASCADE,
    trade_date       date        NOT NULL,
    action           text        NOT NULL
                     CHECK (action IN ('hold', 'open', 'add', 'reduce', 'rebalance', 'close', 'pending')),
    confidence       numeric(4,3),                          -- 0~1
    summary          text        NOT NULL,                  -- 一句话结论
    rationale        text,                                  -- 为什么这么动（或为什么不动）
    tomorrow_outlook text,                                  -- 明日涨跌预测与依据
    long_term_view   text,                                  -- 为什么仍然看好该配置
    invalidation_note text,                                 -- 什么情况下本仓逻辑作废
    newcomer_verdict text,                                  -- 今天第一次看到它，该不该跟
    evidence_ids     bigint[]    NOT NULL DEFAULT '{}',
    model            text,
    prompt_version   text,
    payload          jsonb,                                 -- 结构化全文，便于前端渲染
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (pod_id, trade_date)
);

-- 预测台账：每天说的话，第二天必须被评分
CREATE TABLE IF NOT EXISTS pod_prediction (
    prediction_id   bigserial   PRIMARY KEY,
    pod_id          integer     NOT NULL REFERENCES pod (pod_id) ON DELETE CASCADE,
    trade_date      date        NOT NULL,                   -- 做出预测的日期
    horizon_days    integer     NOT NULL,                   -- 1 / 5
    target          text        NOT NULL,                   -- pod_nav / board_rank
    prob_up         numeric(5,4),
    expected_return numeric(10,6),
    board_ranking   jsonb,                                  -- 预测的板块强弱排序
    confidence      text        CHECK (confidence IN ('low', 'mid', 'high')),
    realized_return numeric(10,6),
    hit             boolean,
    brier           numeric(8,6),
    scored_at       timestamptz,
    UNIQUE (pod_id, trade_date, horizon_days, target)
);
CREATE INDEX IF NOT EXISTS idx_pred_unscored ON pod_prediction (trade_date) WHERE hit IS NULL;

-- 生命周期事件（create/suspend/resume/retire），只追加不修改
CREATE TABLE IF NOT EXISTS pod_event (
    event_id      bigserial   PRIMARY KEY,
    pod_id        integer     NOT NULL REFERENCES pod (pod_id) ON DELETE CASCADE,
    event         text        NOT NULL
                  CHECK (event IN ('create', 'suspend', 'resume', 'retire')),
    trade_date    date        NOT NULL,
    rule_triggered text,                                    -- 触发的是哪条规则
    reason        text,
    snapshot      jsonb,                                    -- 当时的绩效快照
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pod_event_pod ON pod_event (pod_id, trade_date);

-- 当日最看好（网页高亮）
CREATE TABLE IF NOT EXISTS daily_pick (
    trade_date             date        NOT NULL,
    rank                   integer     NOT NULL,
    pod_id                 integer     NOT NULL REFERENCES pod (pod_id) ON DELETE CASCADE,
    score                  numeric(8,4) NOT NULL,
    reason                 text,
    suggested_position_pct numeric(6,4),                    -- 建议给用户的实际仓位比例
    PRIMARY KEY (trade_date, rank)
);

-- ============================================================================
-- 域四：ops —— 流水线与数据质量
-- ============================================================================

CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id      bigserial   PRIMARY KEY,
    run_date    date        NOT NULL,
    phase       text        NOT NULL,                       -- ingest / decide / score
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text        NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'success', 'partial', 'failed')),
    stats       jsonb       NOT NULL DEFAULT '{}',
    error       text
);
CREATE INDEX IF NOT EXISTS idx_run_date ON pipeline_run (run_date DESC);

CREATE TABLE IF NOT EXISTS scheduler_state (
    service text PRIMARY KEY,
    heartbeat_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_manager ON pod(strategy_key)
    WHERE strategy_key = 'agent_manager';

CREATE TABLE IF NOT EXISTS data_quality (
    check_id   bigserial   PRIMARY KEY,
    run_id     bigint      REFERENCES pipeline_run (run_id) ON DELETE CASCADE,
    run_date   date        NOT NULL,
    target     text        NOT NULL,                        -- 表名或数据集
    check_name text        NOT NULL,                        -- missing_ratio / stale / outlier
    status     text        NOT NULL CHECK (status IN ('pass', 'warn', 'fail')),
    metric     numeric(14,6),
    detail     jsonb,
    checked_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dq_date ON data_quality (run_date, status);

-- 幂等写入需要唯一约束：同一天、同一目标、同一检查只保留一行（重跑覆盖）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_data_quality_run_target_check'
    ) THEN
        ALTER TABLE quantlab.data_quality
            ADD CONSTRAINT uq_data_quality_run_target_check
            UNIQUE (run_date, target, check_name);
    END IF;
END $$;

-- 证据台账：决策卡里每一条判断引用的原始数据点
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id bigserial   PRIMARY KEY,
    kind        text        NOT NULL,                       -- board_quote / fund_flow / news / nav
    as_of       timestamptz NOT NULL,                       -- 该证据的时点（回测按此过滤）
    source      text        NOT NULL,
    ref         jsonb       NOT NULL,                       -- 主键定位，如 {"board_id":12,"trade_date":"2026-10-30"}
    summary     text,                                       -- 一句话摘要，给卡片正文引用
    payload     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_evidence_asof ON evidence (as_of DESC);

-- ============================================================================
-- 领域五：intraday —— 盘中实时数据（决策必须赶在 15:00 前，只能靠它）
-- ============================================================================

-- 板块盘中实时行情：整点快照 + 14:30 后每 2 分钟
-- 早期只存涨跌幅，做"放量/资金动向"分析不够用，补上成交与资金字段
CREATE TABLE IF NOT EXISTS board_intraday (
    ts            timestamptz      NOT NULL,
    board_name    text             NOT NULL,
    pct_chg       double precision NOT NULL,          -- 相对昨收的涨跌幅（%）
    kind          text,                               -- industry / concept
    amount        double precision,                   -- 当日累计成交额（元）
    volume        double precision,                   -- 当日累计成交量（手）
    turnover_rate double precision,                   -- 换手率（%）
    main_net      double precision,                   -- 主力净流入（元，逐笔大单估算）
    main_ratio    double precision,                   -- 主力净占比（%）
    up_count      integer,                            -- 板块内上涨家数
    down_count    integer,                            -- 板块内下跌家数
    PRIMARY KEY (ts, board_name)
);
CREATE INDEX IF NOT EXISTS idx_board_intraday_name ON board_intraday (board_name, ts DESC);

-- 老库补列（幂等）
ALTER TABLE board_intraday ADD COLUMN IF NOT EXISTS amount        double precision;
ALTER TABLE board_intraday ADD COLUMN IF NOT EXISTS volume        double precision;
ALTER TABLE board_intraday ADD COLUMN IF NOT EXISTS turnover_rate double precision;
ALTER TABLE board_intraday ADD COLUMN IF NOT EXISTS main_net      double precision;
ALTER TABLE board_intraday ADD COLUMN IF NOT EXISTS main_ratio    double precision;
ALTER TABLE board_intraday ADD COLUMN IF NOT EXISTS up_count      integer;
ALTER TABLE board_intraday ADD COLUMN IF NOT EXISTS down_count    integer;

-- The source clock and original quote instrument are independent of the capture clock.
-- Older name-only rows stay in board_intraday for frozen archives.
CREATE TABLE IF NOT EXISTS board_spot_observation (
    captured_at timestamptz NOT NULL,
    source text NOT NULL,
    source_code text NOT NULL,
    catalog_code text NOT NULL,
    board_name text NOT NULL,
    kind text NOT NULL,
    source_at timestamptz,
    pct_chg double precision NOT NULL,
    main_net double precision,
    main_ratio double precision,
    PRIMARY KEY (captured_at,source,source_code)
);

-- 指数盘中实时行情（沪深300 / 上证 / 深证 / 创业板）
CREATE TABLE IF NOT EXISTS index_intraday (
    ts         timestamptz      NOT NULL,
    index_code text             NOT NULL,
    pct_chg    double precision NOT NULL,
    price      double precision,                       -- 点位，看板顶部直接展示
    PRIMARY KEY (ts, index_code)
);
ALTER TABLE index_intraday ADD COLUMN IF NOT EXISTS price double precision;

-- 仓位盘中估值曲线：由持仓权重 × 板块实时涨跌加权得出
-- 这是"估算值"，不是真实净值；真实净值收盘后才有，两者必须分开存
CREATE TABLE IF NOT EXISTS pod_intraday (
    pod_id   integer          NOT NULL REFERENCES pod (pod_id) ON DELETE CASCADE,
    ts       timestamptz      NOT NULL,
    est_pct  double precision NOT NULL,               -- 该仓当日估算涨跌（%）
    coverage double precision,                        -- 有多少权重被板块数据覆盖
    detail   jsonb,                                   -- 逐只基金的估算明细，可追溯
    PRIMARY KEY (pod_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_pod_intraday_pod ON pod_intraday (pod_id, ts DESC);

-- 30 分钟走势预测：14:30 做出，15:00 结算，误差留在表里供复盘
CREATE TABLE IF NOT EXISTS pod_forecast_30m (
    pod_id        integer          NOT NULL REFERENCES pod (pod_id) ON DELETE CASCADE,
    trade_date    date             NOT NULL,
    made_at       timestamptz      NOT NULL,
    horizon_min   integer          NOT NULL DEFAULT 30,
    start_pct     double precision,                   -- 预测时点的估值涨跌
    predicted_pct double precision,                   -- 预测的终点涨跌
    band_pct      double precision,                   -- 波动带宽（不确定度）
    slope_per_min double precision,                   -- 线性外推斜率（%/分钟）
    path          jsonb,                              -- [{offset_min, pct}] 预测路径
    actual_pct    double precision,                   -- 到期后实际值
    error_pct     double precision,
    settled_at    timestamptz,
    PRIMARY KEY (pod_id, trade_date)
);

-- 14:30 只出"指令"，晚间净值公布后才真正成交，所以卡片要能区分"待执行/已执行"
ALTER TABLE pod_decision ADD COLUMN IF NOT EXISTS executed_at timestamptz;
-- 允许 pending 状态（14:30 指令）
ALTER TABLE pod_decision DROP CONSTRAINT IF EXISTS pod_decision_action_check;
ALTER TABLE pod_decision ADD CONSTRAINT pod_decision_action_check
    CHECK (action IN ('hold', 'open', 'add', 'reduce', 'rebalance', 'close', 'pending'));

-- ============================================================================
-- 领域六：learning —— agent 的学习留痕（因子 IC 与概率校准的实证记录）
-- ============================================================================

-- 因子 IC 历史：每天算一次"这个因子排序对了没有"，是调整权重的唯一依据
CREATE TABLE IF NOT EXISTS factor_ic (
    trade_date   date             NOT NULL,
    factor       text             NOT NULL,
    ic_mean      double precision NOT NULL,          -- 与次日板块收益的秩相关均值
    ic_std       double precision,
    icir         double precision,                   -- 均值/标准差，衡量稳定性
    positive_rate double precision,                  -- IC 为正的天数占比
    samples      integer,                            -- 参与计算的天数
    PRIMARY KEY (trade_date, factor)
);
