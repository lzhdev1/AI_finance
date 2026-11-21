# QuantLab 基金经理 Agent

QuantLab 管理一个独立的人民币模拟基金账户。从工作台的一笔成交，可以追到当时冻结的申赎指令、研究报告、工具调查、板块行情、新闻和已验证的预测。两版历史回放在各自独立的账户和归档中查看。

## 启动工作台

当前项目使用 Docker Compose 和已有的共享 PostgreSQL。数据库容器在外部网络 `shared-db` 上使用名称 `shared-postgres`，数据库、用户与密码由项目根目录的 `.env` 提供。首次启动需要该实例、数据库及网络已就绪。

首次克隆后，将 `.env.example` 复制为 `.env`，填入共享数据库的密码和模型服务凭据。在线经理的记忆与模拟账本保存在 PostgreSQL，历史回放展示文件位于本机的 `data/manager-replay/`。迁移已有工作台时需另行迁移这些数据。

```powershell
docker compose up -d --build
docker compose ps -a
```

- 工作台地址为 [http://localhost:3000](http://localhost:3000)。
- API 文档位于 [http://localhost:8000/api/docs](http://localhost:8000/api/docs)。
- 一次性服务 `init` 应以状态 0 退出。它应用幂等表结构并初始化基金主数据。API、调度器和前端持续运行。数据库卷归共享实例管理。
- 模型使用 OpenAI 兼容接口，读取 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL_DEEP`（缺省回退至 `LLM_MODEL_FAST`）、`LLM_TIMEOUT`、`LLM_MAX_TOKENS`。容器内修改 Python 代码后执行 `docker compose restart api scheduler`。修改环境变量后执行 `docker compose up -d --force-recreate api scheduler frontend`。

### 本机工作台连接线上经理

在 Windows 本机的 `.env` 设置 `COMPOSE_FILE=docker-compose.yml;docker-compose.workstation.yml`，然后设置以下工作台专用变量。只使用基础 `docker-compose.yml` 时仍按上节方式运行独立开发库和经理。

| 变量 | 用途 |
| --- | --- |
| `QUANTLAB_WORKSTATION_SSH_TARGET` | SSH 转发账号及主机，格式为 `user@host` |
| `QUANTLAB_WORKSTATION_SSH_DIR` | 仓库外的绝对目录，内含该转发账号的专用密钥 `id_ed25519` 和预先核对的主机公钥文件 `known_hosts` |
| `QUANTLAB_WORKSTATION_PGUSER`、`QUANTLAB_WORKSTATION_PGPASSWORD` | 线上 `quantlab` 库为本机工作台分配的数据库凭据 |

首次配置服务器时，先安装 `socat`。在项目根目录以 root 执行 `install -D -o root -g root -m 0755 infra/workstation-db/server-relay.sh /usr/local/libexec/quantlab-postgres-stdio`。将工作台的专用公钥加入服务器 SSH 账号的 `authorized_keys`，按下例替换公钥占位符。该账号需能运行脚本中的 Docker 查询。SSH 会强制为这把密钥执行该脚本，只将会话的标准输入输出接入 `shared-postgres` 的 PostgreSQL 端口。

```text
restrict,command="/usr/local/libexec/quantlab-postgres-stdio" ssh-ed25519 <工作台公钥的 Base64 部分>
```

准备好本机 lzhshow 使用的 Docker 网络 `quantlab-portfolio`。首次从独立开发库切换时先执行 `docker compose -f docker-compose.yml stop scheduler`。如果此前为本机作品集单独启动过生产 Compose 的预览容器，先执行 `docker compose -f docker-compose.prod.yml stop frontend api`，将预览入口交给工作台。随后执行 `docker compose up -d --build`。默认启动的两个工作台都通过容器内的 SSH 隧道连接线上业务库，本机原数据库和两版历史回放文件均保留。

本机工作台的“运行研究”由服务器经理接单。lzhshow 的 `/fund-manager` 预览保留只读访问。可在本机与线上打开同一报告，核对报告编号和最新调度心跳。自动值守及线上数据库初始化均由服务器管理。

## 作品集容器部署

生产使用 `docker-compose.prod.yml`，镜像内包含后端代码与配置。作品集网关加入外部 Docker 网络 `quantlab-portfolio`，把 `/fund-manager` 和 `/fund-manager/*` 去掉前缀后代理到 `http://quantlab-portfolio-web:80`。工作台的 API 与静态资源都使用同一子路径，服务只通过容器网络访问。公开 API 仅接受读取请求，自动研究和模拟申赎由调度容器继续执行。

准备共享 PostgreSQL、外部网络和项目根目录的 `.env`。设置 `POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD` 及模型配置。数据库主机和网络默认分别为 `shared-postgres` 与 `shared-db`，可用 `QUANTLAB_PGHOST`、`QUANTLAB_DB_NETWORK` 覆盖。沿用已有经理时，先把业务库恢复到目标库，并复制 `data/manager-replay/`。每个在线账户只运行一份调度器。

```sh
docker compose -f docker-compose.prod.yml build api frontend
docker compose -f docker-compose.prod.yml up -d --no-build
```

异机构建时设置 `QUANTLAB_BACKEND_IMAGE`、`QUANTLAB_FRONTEND_IMAGE` 为对应版本的镜像标签，目标机用 `docker load` 导入两份镜像后执行第二条命令。已有开发工作台在同机运行时，作品集可以复用该数据库和现有调度器，只需启动公开的两个服务：

```sh
docker compose -f docker-compose.prod.yml up -d --no-build --no-deps api frontend
```

## 在哪里看经理的工作

| 视图 | 可以核对的内容 |
| --- | --- |
| 经理总览 | 模拟资产、基准、现金、研究焦点、最近行为、运行状态。首次结算后展示净值曲线。 |
| 市场与资金 | 行业与概念目录、行情与资金流的日期、覆盖度和来源，含缺数状态。 |
| 投研与决策 | 跟踪板块、资金与量价证据、竞争性的行为解释、未来五个交易日的验证条件、目标仓位与冻结指令。 |
| 预测档案 | 板块方向、具体基金单位净值的独立预测、到期表现及市场量化展望。[查看基金预测](http://localhost:3000/?view=forecasts&pred_tab=funds) |
| 组合账本 | 当前经理的持仓、冻结指令、成交流水和申赎费用。 |
| 策略回放 | 独立账户的逐日策略、新闻、成交、费用复核、沪深300对照曲线和预测成绩。 |

“运行研究”将任务写入数据库，由调度容器刷新新闻、读取已有行情和账户记录、复核到期预测，再生成一份新报告。它可以在周末或盘后运行。这个入口只产生研究记录；模拟申赎由交易日的盘中决策任务提交。收盘行情与基金净值由单独的采集任务更新。

## 自动运行

以下为上海时间，具体配置位于 `backend/configs/settings.yaml`。

| 时间 | 任务 |
| --- | --- |
| 每天每小时 05 分 | 值守进程刷新新闻，检查新收录的报道、行情、盘中快照及资金流。有新资料时自主排队研究，固定时段由当班任务接续。节假日也运行。 |
| 每天 08:30 | 刷新新闻，整理当日研究。 |
| 工作日 08:10 | 重试此前缺失的成交净值，继续处理未确认指令。 |
| 交易日 09:30、10:30、11:30、13:30、14:00、14:20 | 采集板块和指数快照。 |
| 交易日 14:45 | 更新新闻和盘中快照，整理已确认的持仓与到期预测经验，为收盘前决策做准备。 |
| 交易日 14:50–14:55 | 14:50 开始最后复核，目标在 14:55 前公布当天的模拟申购金额、赎回份额或维持持仓决定。14:54 有独立发布任务，无法及时确认可执行方案时会记录维持持仓及原因。 |
| 工作日 18:30 | 更新交易日历、基金净值、行业与概念行情、指数、资金流及新闻，重新研究。 |
| 工作日 20:30、22:30 | 刷新组合需要的净值，按日期顺序结算、补估值并验证到期预测。20:40 单独重试预测评分。 |

调度器启动时检查数据时效，必要时补采，检查遗留指令。15:00 之后不补发当日申赎。经理的开户、研究和调仓无需手动建仓或录入板块；新数据库会在采集过程中尝试发现可投基金映射。

打开「投研与决策 → 决策指令」查看今日结果、发布时间、依据及预测目标日期。页面直接读取当天的指令，历史记录另列。交易日到达目标时间仍无指令时会显示延迟；到 15:00 仍未发布会显示错过当日窗口。已发布的模拟指令保留至基金净值公布后结算。[本机决策指令](http://localhost:3000/?view=decisions)

## 研究、策略与记忆

- 候选板块来自已入库行情。系统比较近 5、20 个交易日动量和有数据时的资金强度，并向模型提供候选板块的行情、相关资讯、历史观点、持仓、近期成交以及预测样本。报告显示截止时间。板块指数缺失时，可使用明确标识的映射基金净值作为行情代理。
- 市场页列出全部已收录的行业和概念，按板块显示最新收盘、资金日期与可测算的期限。收盘资金来自同花顺行业／概念资金榜，净额为流入减流出。东方财富主力净流入采用大单与超大单口径。公开资金榜缺交易日期时，采集器先与该板块同源、有日期的收盘行情核对，核对成功的当期资金才进入在线排名。不同来源的资金强度分别排名。盘中快照有主力资金时，报告单独保存时间和口径。
- 模型为所选板块分别给出“只跟踪”或“评估配置”的意图，并记录证据引用和失效条件。只跟踪的板块可以发布预测，其基金不会获得目标仓位。执行层根据结构化意图、可投基金和风控上限计算目标。模型超时、响应不合格或关闭时，报告标记规则模式及原因。数据不足时允许放弃发布预测和配置目标。
- 1 日和 5 日指交易所交易日。原版以五日动量方向筛选历史收益窗口。新版按五日和二十日趋势方向联合分组，以 12 个先验样本向板块自身历史基准收缩，并用此前已评分的新版预测校准上涨概率。校准按到期日等权，最多使用最近 60 个到期日，加入 30 天中性先验，调整幅度上限为 10 个百分点。预期收益单独估算。同板块、同基准日、同期限保留首次发布的预测，用发布时锁定的价格序列和交易日核验。
- 记忆来自数据库，包括既往研究、持仓、成交、已验证方向命中、实际收益和概率误差。具有足够已验证样本且近期方向表现较差的板块会被降权。这是可审计的经验反馈，模型权重本身没有训练更新。
- 在线研究先向模型展示截至当时有行情的板块目录。模型自行挑选板块并提出最多四个只读请求，可调取板块行情与资金证据、检索近七日已公开且已收录的相关新闻、查阅该板块历史研究和已结算的预测经验。程序执行后把结果交回模型形成决策，逐次查询和空结果都随报告保存。实际申赎仍经交易日历、费率、额度和风险规则确认。历史封存版本保留原有流程。
- 历史名称匹配仅代表候选基金与板块的关联。代理基金可能存在跟踪偏差。新闻按来源和时间记录，标题或正文匹配只是检索依据。预测的样本数、使用的代理和真实表现比模型措辞更适合衡量策略质量。

### 经理如何检验交易动机

每次在线研究都会对照板块资金榜、价格区间、过去五日涨幅、当前成交额相对历史中位数的倍数，以及同日沪深300走势。经理同时考虑买方主动增配、获利者兑现、被动调仓和普遍降风险等解释。市场页显示行业和概念的当轮初筛结果。跟踪板块的报告保存每个解释的支持、反证及未来五个交易日的观察条件。此后遇到计划中的交易日，经理会调出原报告、当天要检验的条件与已公布的阶段收益和资金净额；盘中尚未产生的收盘结果显示待核验。公开数据能核验后续价格和资金变化，交易账户的身份与真实意图仍不可见。

前期上涨后，若信号日同时出现冲高弱收、成交额达到历史中位数的 1.2 倍且有已核验净流出，新增配置金额乘以 0.75。这个系数属于预设风控。首次发布时冻结行为分类及基础概率。相同板块、预测标的、期限和行为分类积累 12 个到期且不重叠的样本后，经理才用已实现误差校准方向概率，单次幅度最多 5 个百分点。工作台显示配对样本上基础概率与实际发布概率的 Brier 分数，两者越低越好。同一天后续报告更新假设时，旧预测仍按首发记录评分。

板块判断和基金净值判断分别存档。盘中研究使用已公布并已收录的行情和净值。在交易日 T 收盘前，1 日和 5 日预测分别评估从 T 日收盘到下一个及第 5 个交易日收盘的区间涨跌。具体基金按对应交易日的单位净值核验。T 日收盘价及净值在决策时尚未知，待基准日和目标日数据齐全后再评分。新增仓位还要同时满足基金自身五日方向、预期收益和费用门槛。周末或盘后研究从下一个可申请日计算持有成本，报告注明假设赎回日。经理每天重新评估持仓。

盘中采集优先查看持仓、最近关注和上一交易日资金净额较大的同花顺板块，每轮最多 16 个。分时价格需要匹配同源板块代码，源站时间须落在当日交易时段且距决策不超过 30 分钟。同花顺分时提供价格，收盘资金榜单独标注所属交易日。东方财富目录只有匹配到同源、带时间戳的板块时才使用其盘中主力资金估算。

## 模拟交易口径

默认初始资金为 100,000 元。单只风险基金目标上限 35%，单一板块上限 45%，月度双边换手预算为初始资金的 60%；风险赎回可继续执行。已确认净值触及配置的 12% 硬回撤时，经理的风控目标转为现金。执行层还会检查资产可投性、现金、最小金额、无交易带和估值完整性。

申购冻结金额，赎回冻结份额。结算只接受对应交易日的公布净值。现金、基金份额、订单和当日估值在同一事务中更新。缺净值、账本断档或错过截止时间均可在日志或决策卡里核对。重复运行不会重复成交。全额赎回后最新持仓为空，流水仍然可查。

新版经理要求新建仓连续两个交易日出现配置目标，新增风险仓位需要五日预期收益覆盖模拟往返费率，并以板块过去二十日波动率和 0.6% 的单基金日波动预算约束仓位。普通减仓比较相邻交易日的目标和每个 FIFO 批次的赎回费。若未来七个自然日可节约的费用足以抵消预计下跌，经理暂缓该批次赎回。组合硬止损仍可直接退出，延迟卖出时会相应减少新申购。逐日决策保存研究目标、约束及冻结的指令。

在线账户按批次限制可赎份额。招商中证银行指数 C（014028）通常在有效申购日 T 的下一个工作日登记份额，T+2 起可申请赎回。模拟账户保守地在赎回申请后 T+7 交易日结束才释放该产品的款项用于后续申购。实际到账取决于登记机构与销售渠道。[基金管理人招募说明书，第 30、36 页](https://static.cmfchina.com/fundarticle/20260519/20023023257605.pdf#page=36)

其余尚未逐只核验的基金标为模拟假设，默认按境内交易日历 T+1 确认、T+2 可赎，赎回资金入账后按下轮可用处理。扣费按 FIFO 批次和模拟费率表执行。产品开放限制、分红、拆分和实际到账时间可能影响与真实基金的可比性。账户仅支持模拟申赎。

## 独立账户历史回放

打开[半年回放](http://localhost:3000/?view=replay)，按建仓、加仓、减仓、调仓、清仓或持有筛选交易日，展开当日记录查看研究和结算。历史新闻为中新网财经逐日归档的标题，带原始时间、链接。每个交易日按上海时间 14:30 生成决策，研究只读取该时点已公开的资料和本账户先前形成的经验。收盘后才按对应交易日的真实净值结算。无法还原的净值披露时间采用次日上午 08:30 的可见时间假设。历史基金池根据现有板块映射构建，实际披露版本和基金池均有局限。

本次区间为 2026-05-21 至 2026-11-20，使用专库 `quantlab_replay_202611`。复用下列命令会从已提交的交易日继续；每个交易日的模型研究、预测与账户变化一并提交。完整账户保留在专库，工作台读取 `data/manager-replay/latest.json`。运行中的视图每五个交易日发布一次，末日结算后发布全量结果。若要保留另一个区间的独立复盘，可使用新的 `quantlab_replay_*` 数据库。

```powershell
# 首次创建专库（示例沿用本项目现有数据库角色）
docker exec shared-postgres createdb -U postgres -O quantlab quantlab_replay_202611
docker compose exec -e PGDATABASE=quantlab_replay_202611 -T api python -c "from quantlab import db; db.apply_schema()"
docker compose exec -T api python -m quantlab.replay_data --target-db quantlab_replay_202611 --start 2026-05-21 --end 2026-11-20
docker compose run --rm --no-deps -e PGDATABASE=quantlab_replay_202611 api python -u scripts/replay_manager.py --start 2026-05-21 --end 2026-11-20
docker compose exec -e PGDATABASE=quantlab_replay_202611 -T api python scripts/audit_manager_replay.py --require-complete
```

沪深300使用期初前一交易日收盘价作基数。组合收益含模拟申赎费用，最大回撤按收盘资产和期初资金计算。预测口径为决策日收盘至未来第 1 或 5 个交易日收盘；不同板块分别计数，连续日期的五日预测可能重叠。上涨概率为 50% 的预测计入 Brier 分数，方向命中率只统计已验证且明确给出方向的条目。

全期结算后，程序向原指数来源补核已到期且缺行情的预测，记录起止收盘价、来源和核验时间。工作台在最终成绩和对应预测上标注“期后补核”。历史决策时能看到的经验始终由原始评分台账决定。

### 逐笔复盘和版本对照

工作台的半年回放保留原版和优化版的独立账户。每笔申购和赎回可追溯到 FIFO 批次、当时选定的板块、实际成交净值和该批次费率。预测复盘按期限显示上涨频率、预测概率与样本分布。共同样本表只比较两版均预测过的相同板块或基金代理、基准日、到期日和期限。概率误差使用共同已评分样本，方向命中使用两版均给出方向的子集。

原版结果已封存为 `data/manager-replay/v1.json`。优化版使用另一数据库、另一资金账本及 `v2.json`。下例从原版回放库复制原始行情、新闻和主数据；原版的持仓、研究、预测和期后补核结果均留在原库。两版的模型响应分别生成，不能假定同一输入必然得到相同文本。策略改动参考了原版在该半年的表现，因此同区间重跑属于样本内比较。

两版决策、成交采用同一套模拟费率。复盘另按赎回日已公布的个人 C 类产品资料概要和实际 FIFO 持有天数重算费用，显示覆盖批次、金额差额和原件链接。缺少对应公告的批次保留账本费用并列为未核。板块归因还提供同板块、同持有交易日数的历史窗口对照，用于区分板块在这半年的平均表现和实际持有日期的偏差。这些事后统计只进入复盘页面。

```powershell
# 首次创建优化版专库
docker exec shared-postgres createdb -U postgres -O quantlab quantlab_replay_202611_v2
docker compose exec -e PGDATABASE=quantlab_replay_202611_v2 -T api python -c "from quantlab import db; db.apply_schema()"
docker compose exec -T api python -m quantlab.replay_data --phase frozen --source-db quantlab_replay_202611 --target-db quantlab_replay_202611_v2 --start 2026-05-21 --end 2026-11-20
docker compose run --rm --no-deps -e PGDATABASE=quantlab_replay_202611_v2 api python -u scripts/replay_manager.py --version v2 --start 2026-05-21 --end 2026-11-20
docker compose exec -e PGDATABASE=quantlab_replay_202611_v2 -T api python scripts/audit_manager_replay.py --archive /app/data/manager-replay/v2.json --require-complete
```

单独刷新某版的事后归因，可在该版专库执行 `python -m quantlab.replay_comparison --attribution v1` 或 `--attribution v2`。刷新归因只生成复盘文件。

## 验证与运维

```powershell
docker compose logs --tail=100 scheduler
docker compose exec scheduler python -m quantlab.scheduler --list
docker compose exec api python scripts/ingest.py status
docker compose exec -e QUANTLAB_TEST_POSTGRES=1 api python -m pytest tests -q -p no:cacheprovider
```

PostgreSQL 集成测试使用独立的临时 schema。旧策略回放使用独立数据库；在线经理库拒绝执行旧的批量回放和清仓重置脚本。

主要后端模块位于 `backend/src/quantlab/`。`research.py` 负责研究与预测，`execution.py`、`runner.py`、`pod_store.py` 负责风控与账本，`station.py` 负责持久化任务与看板，`scheduler.py` 编排日程。前端为 React/Vite，由 nginx 代理同源 `/api`。
