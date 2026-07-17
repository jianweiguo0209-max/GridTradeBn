# 实盘 offset 启用数组(LIVE_OPEN_OFFSETS)设计

> 状态:**已获用户批准(2026-07-17)**。范围:实盘选币轮的"整轮开仓门"(按 offset 相位)+ cap frac
> 按启用 offset 数重算(采用方案 B)+ 单测。仅实盘路径,回测不受影响。实现前遇本文未覆盖分歧点,
> 不确定就问。

## 一、目标与背景

在实盘上新增一个可配置的"启用 offset 数组",只在数组内的 offset 相位开仓。用于**实盘灰度上量/减仓**:
从少数 offset 起跑、观察、再逐步放宽到全 12 相位。

**offset 是换仓相位**(`core/selection.py:142` `compute_offset`):`period="12H"` → offset ∈ 0..11。
scheduler 每整点跑一次、每小时处理一个不同 offset 的 cohort;`choose_symbols=1` 意味着每个 offset 开 1
个币 → 满配 12 个并发格,恰好对齐 `max_concurrent=12`。即隐含不变式 `max_concurrent = period_hours ×
choose_symbols`。

**为什么天然可挂**:`run_scheduler_once`(scheduler.py:73-75)已算出 `offset`,且已有 `open_enabled`
开关(shock 刹车用),`open_enabled=False` 语义正是"只关不开"(cycles.py:396)。offset 门直接复用此开关。

**实盘/回测隔离**:改动只落在 `run_scheduler_once`(实盘路径)与 `config.py`(仅实盘 env);回测走另一条路,
零改动、零影响——延续本项目回测忠实原则。

## 二、总览

两个文件:

- `gridtrade/config.py` —— 解析 + fail-fast 校验 `LIVE_OPEN_OFFSETS`;并按启用数重算 `cap_equity_frac`。
- `gridtrade/runtime/scheduler.py` —— `run_scheduler_once` 里加 offset 门,不在启用集则本轮 `open_enabled=False`。

`cycles.run_scheduler_cycle` **不改**(`open_enabled=False` 已是"只关不开")。`MaxConcurrentGate` 保持 12。

## 三、语义(用户已定)

### 语义 1:整轮开仓门(而非只跳开仓)

当前 offset **不在**启用集时,本轮 `open_enabled=False`——**只关旧格、不开新格**。`close_tag` 照跑:

- 稳态下该 offset 从没开过格 → 关是 no-op;
- 开启过滤前遗留的旧 cohort,会在各自 12H 换仓轮被自然 `close` 排空(而非一直挂着待人工处理)。

等价于只跑 N/12 个 cohort。

### 语义 2:越界 fail-fast

启用数组里任一值不能 parse 成 int、或 ∉ `[0, period_hours)`(`period_hours = int(scheduler_period[:-1])`,
与 `compute_offset` 同源) → `load_deploy_config` 抛 `RuntimeError`,拒绝启动。报文带非法值 + 合法区间。
沿本仓退役键 / `FUSE_MIN_COVERAGE` 的"配置错了要响亮、禁静默 clamp"惯例。

### 语义 3:cap frac 按启用 offset 数 N 重算(方案 B)

offset 门把**实际可达并发**从 12 降到 N。为保持满配时账户杠杆仍达目标 AL,cap 的 equity 占比 frac 分母
改用 N(而非固定 12):

```
eff_concurrency = min(len(offsets) × choose_symbols, max_concurrent)   # offsets 非空(启用)
                = max_concurrent                                       # offsets 空(停用,默认)
cap_equity_frac = derive_frac(account_leverage, eff_concurrency, grid_gearing)
```

- `offsets` 必须**先去重再计数**(`"0,0,6"` → `{0,6}` → N=2),否则分母虚高把仓位做小。
- `choose_symbols` 取 `DEFAULT_STRATEGY_CONFIG['choose_symbols']`(=1);×它为正确性/未来防呆。
- clamp 到 `max_concurrent`:`MaxConcurrentGate` 仍是硬上限,denominator 不应超过它。
- **自洽性**:空集 → `eff=max_concurrent` → frac=0.2451(现值,**零行为变更**);显式全开 `0..11` →
  N=12 → frac=0.2451(与默认一致)。

**方案 B 的取舍(用户已知情选定)**:恒贴目标 AL(在天花板上);N 小时杠杆集中到少数币,最坏敞口比 12 币
分散脆(单币 gap-through 的逐仓滑点/强平动态);改 offset 集会在各 cohort 的 12H 换仓轮重定价旧仓。

## 四、组件详设

### 组件 1 — config.py:解析 + 校验 `live_open_offsets`

- `DeployConfig` 新增字段 `live_open_offsets: tuple = ()`(空=停用=全 offset 开)。
- 新增 int-CSV 解析:读 env `LIVE_OPEN_OFFSETS`(如 `"0,6"`);split → strip → 空串丢弃 → int() →
  去重 → 排序成 tuple。int() 失败即 `raise RuntimeError`(带原始值)。
- 校验:每个值 ∈ `[0, period_hours)`;越界即 `raise`(报文含非法值 + `[0, period_hours)`)。
- **落位顺序**:必须在算 `cap_equity_frac` 之前解析好 offsets(frac 依赖 N)。

### 组件 2 — config.py:cap_equity_frac 按 N 重算

- 计算 `eff_concurrency`(见语义 3),传入 `derive_frac`。空集时退回 `max_concurrent`。
- `DeployConfig(...)` 的 `cap_equity_frac=` 与 `live_open_offsets=` 一并回填。

### 组件 3 — scheduler.py:offset 门

- `run_scheduler_once` 内,`offset = compute_offset(...)`(现 line 75)之后、shock 块(现 line 142)之前,
  用 offset 门初始化 `open_enabled`:

  ```python
  open_enabled = True
  _oe = rt.config.live_open_offsets
  if _oe and offset not in _oe:
      open_enabled = False
      print('[offset-gate] offset=%d 不在实盘启用集 %s → 本轮只关不开'
            % (offset, sorted(_oe)), flush=True)
  ```

  与 shock 块是"或"关系(任一为真都只关不开;二者都只降不升,天然叠加——现有 `open_enabled = True`
  初始化行被本块替换)。
- `run_scheduler_cycle` 返回后,若被 offset 门拦,给 `result['offset_gated'] = True`(供测试/可观测,
  避免与 shock 的 `shock_braked` 混淆)。
- `main()` 启动日志追加打印启用集,便于运维核对。

### 连带效应(诚实披露,非阻塞)

方案 B 下单格 cap 变大 → 满仓名义变高 → 保险丝覆盖审计(scheduler.py §六 `audit_fuse_coverage`)可能对
部分币更早报"不足额",`FuseCoverageGate` 会自动降 cap 护全额,自保闭环,无需特殊处理。

## 五、测试(TDD)

**config**(`tests/test_config.py` 或同级):
- `LIVE_OPEN_OFFSETS` 解析:未设 → `()`;`"0,6"` → `(0, 6)`;`"6,0,6"` → 去重 `(0, 6)`;含空格容错。
- 越界 → `RuntimeError`:`period=12H` 配 `"12"` 或 `"15"` 抛错;非 int `"a"` 抛错;报文含合法区间。
- 校验用 `scheduler_period`:`SCHEDULER_PERIOD=24H` 时 `"15"` 放行。
- frac 重算:`derive_frac(5.0, 2, 3.4)=1.4706`(N=2);空集 → 0.2451;显式 `"0..11"`(全 12) → 0.2451;
  去重后计数(`"0,0,6"` → N=2 → 1.4706)。

**scheduler**(`tests/runtime/test_scheduler*.py`,假 runtime/now_fn 复用现有模式):
- offset ∈ 启用集 → 正常开(opened 非空、无 `offset_gated`)。
- offset ∉ 启用集 → `open_enabled=False`:opened=[]、`result['offset_gated']=True`、close 照跑。
- 空集(默认) → 恒开,不受 offset 影响(回归:零行为变更)。

## 六、文档

`.env.example` 加 `LIVE_OPEN_OFFSETS`:注释空=全开(默认);实盘灰度/减仓;方案 B 会按启用数重算 cap frac;
越界 fail-fast;示例 `# LIVE_OPEN_OFFSETS=0,6`。

## 七、非目标(YAGNI)

- 不改回测。
- 不改 `MaxConcurrentGate` 上限(保持 max_concurrent)。
- 不做面板 UI 展示(启动日志已够运维核对)。
- 不支持"只跳开仓、连关格也跳"的第二语义(用户已否)。
