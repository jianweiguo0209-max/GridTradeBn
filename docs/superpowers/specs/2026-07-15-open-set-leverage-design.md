# 开格设杠杆(修 -2027 拒单) 设计

> 状态:**已获用户批准(2026-07-15,降范围至 A/B/C)**。经 brainstorming 实证调查,范围从"档位感知
> set_leverage + 选币可行性排除+回填(块 D)"**收敛为仅 A/B/C**(open 设杠杆)。块 D 被实证证伪(见 §一)。
> 实现前遇本文未覆盖的分歧点,不确定就问,勿猜。

## 一、问题、机制与实证(为何降到 A/B/C)

**根因**:`grid_executor.open`(grid_executor.py:78-158)全程**无 `set_leverage` 调用**——开格完全依赖币安
账户对该币的默认/残留杠杆。这是 account-leverage spec(2026-07-07 §非目标"维持不设仓位杠杆")的
**HL 专属决策**:HL 按资产 maxLeverage 自动收保证金、从不需设仓位杠杆。**迁到币安后失效**——币安有
杠杆档位(leverage bracket),默认档位可能撑不住仓位名义 → **-2027 "Exceeded the maximum allowable
position at current leverage"**。testnet 2026-07-15:05:00 轮 KITE 越过 -1111 精度拒(cbc38f3)后紧撞 -2027。

**机制(实证坐实)**:在杠杆 L 下,最大可持名义 = 「maxLev≥L 的最大 maxNotional」。杠杆越高、档位名义
上限越小。worst 名义 ≈ gearing×cap。**KITE 默认卡在 5x(档 $5k),worst 若 $5k–$10k 就 -2027;但设到
4x(档 $10k)就放得下。KITE 是"可行但没设杠杆",不是"不可行"。**

**实证调查(2026-07-15,demo 只读 SSH + 主网公开数据)**:
- 杠杆档位是**币安期货通用机制、主网也有**(主网公开 exchangeInfo `requiredMarginPercent` 证),非 demo
  独有;但 **demo 更严**(KITE demo 5x vs 主网 20x)、更易撞 -2027。
- **demo 511 个 COIN 的 bracket-1 maxLev 分布**:75x:137、50x:235、25x:69、20x:51、10x:6、5x:3、其余高。
  低 maxLev(≤10x)仅 9 币(1.8%),多是低量币(top-55% 量过滤多半已剔)。
- **真·不可行币(worst 连 4x 档都放不下,即设对杠杆也开不成)**:worst $2500(当前 ~$3k 权益)= **0 币**;
  worst $8000(~$10k)= 0;worst $25000(~$30k)= **2 币**(2/511)。worst/格 ≈ 0.833×equity(AL=5,N=12)。
- 公开 exchangeInfo 的 `requiredMarginPercent` 是统一 5%(20x)占位,**拿不到主网 per-coin maxLev**(真值在
  私有 leverageBracket);demo 是可鉴权的保守下界。

**结论(降范围依据)**:当前 sizing 下**没有一个币是真不可行的**——`set_leverage` 就是全部的解。原设想的
"选币可行性排除+回填(块 D)"当前排除 **0 个币**、$30k 权益才排 2 个,纯 YAGNI → **移出本 spec、暂缓**(权益长到
~$30k+ 或实测到不可行币再做)。真·不可行币(0 个,未来极少)仍由 `open_proposals` 逐提议隔离兜底(f4d053b 现状)。

## 二、总览(A/B/C)

`open()` 在建 grid 后、挂第一张限价单前,按杠杆档位算出并 `set_leverage(symbol, L)`,L 取「能容 worst 名义的
最紧档的**下一档**(减一档留余量)」,clamp[ceil(gearing), symbol_maxLev]。**fail-open**:tiers/set_leverage 任
何异常 → 告警+继续(退化为不设杠杆的现状,-2027 由 open_proposals 隔离)。

**不变量(不改)**:`core/` 纯函数策略、回测引擎几何(golden/core 逐位不变)、选币/准入门链、现有 set_leverage
的 cross+int 语义、仓位体系(gearing/frac/cap)。**回测无关**:set_leverage 是纯实盘 API,FakeExchange no-op、
不碰选币口径,回测行为零变化(无同口径顾虑)。

**非目标**:块 D(选币可行性排除+回填、maxQty超低价币准入)——暂缓;降 cap 救不可行币;mainnet prod。

## 三、组件

### 3.1 A｜适配器 `fetch_leverage_tiers(symbol)`

- `base.ExchangeAdapter.fetch_leverage_tiers(symbol) -> list`:默认 `[]`(fail-open;子类未实现即退化不设杠杆)。
- `BinanceAdapter.fetch_leverage_tiers(symbol)`:调 ccxt `fetch_leverage_tiers([symbol])`(私有只读
  `fapiPrivateGetLeverageBracket`;**demo 已验支持**),归一化为 `[{'maxLeverage': int, 'maxNotional': float}, …]`
  (取 ccxt tier 的 `maxLeverage`/`maxNotional`)。**实例缓存**(每币一次;档位表稳定,同 `max_leverage` 缓存范式)。
  取数异常 → `[]`(fail-open)。
- `FakeExchange.fetch_leverage_tiers(symbol)`:返回 `seed_leverage_tiers` 播种值,默认 `[]`(回测/离线 no-op)。
  加测试钩子 `seed_leverage_tiers(symbol, tiers)`。

### 3.2 B｜纯函数 `leverage_policy`(新文件 `gridtrade/execution/leverage_policy.py`,与 fuse_policy 并列)

```python
import math

def cap_at_leverage(tiers, L):
    """在设定杠杆 L 时的最大可持名义 = maxLev≥L 的最大 maxNotional;无 → 0。"""
    vals = [t['maxNotional'] for t in tiers if t['maxLeverage'] >= L]
    return max(vals) if vals else 0.0

def feasible(worst_notional, tiers, gearing):
    """worst 名义能否在 ≥ceil(gearing) 杠杆下持有(保证金撑得住)。tiers 空 → True(fail-open,
    不因读不到档位而判死)。仅供告警,不做排除(块 D 暂缓)。"""
    if not tiers:
        return True
    return worst_notional <= cap_at_leverage(tiers, math.ceil(gearing))

def pick_leverage(worst_notional, tiers, gearing):
    """能容 worst 名义的最紧档的**下一档** maxLev(减一档留余量),clamp[ceil(gearing), 最高档 maxLev]。
    tiers 空 → None(fail-open,调用方不设杠杆)。worst 超所有档(不可行)→ 最低档 maxLev 尽力(feasible 会告警)。
    减一档≥gearing 由 floor clamp 保证;≤symbol_max 由 ceil clamp 保证。"""
    if not tiers:
        return None
    brs = sorted(tiers, key=lambda t: -t['maxLeverage'])   # 高杠杆(小名义)在前
    floor = math.ceil(float(gearing))
    top = brs[0]['maxLeverage']                            # 最高档 = symbol maxLev
    idx = next((i for i, b in enumerate(brs) if b['maxNotional'] >= worst_notional), None)
    if idx is None:                                        # worst 超所有档(不可行)→ 最低档尽力
        raw = brs[-1]['maxLeverage']
    else:
        raw = brs[min(idx + 1, len(brs) - 1)]['maxLeverage']   # 减一档
    return int(min(max(raw, floor), top))
```

**减一档语义验证**(demo 实测):
- 1000PEPE worst $2000,档 25x:$5k/20x:$10k/…:tightest=25x(idx0)→减一档=20x→L=20(worst $2k 在 $10k 档、
  余量 $8k)。
- KITE worst $8000,档 5x:$5k/4x:$10k/3x:$30k:tightest=4x(idx1)→减一档=3x→floor clamp 到 ceil(3.4)=4→L=4
  (worst $8k 在 4x 档 $10k、放得下、可行)。
- KITE worst $12000(不可行):tightest=3x(idx2)→减一档=2x→clamp 到 4→L=4;但 feasible=$12k>cap_at(4)=$10k
  → 告警;set 4x 尽力、-2027 由 open_proposals 隔离。

### 3.3 C｜`open()` 挂单前设杠杆(grid_executor.py,entry 后 :89、挂单前 :119 之间)

```python
        # 设仓位杠杆(spec 2026-07-15):HL 从不设、币安默认档位可能撑不住 worst 名义 → -2027。
        # 减一档 L 留余量;fail-open:tiers/set_leverage 异常退化为不设(现状),-2027 由 open_proposals 隔离。
        worst_notional = order_num * int(grid_params['grid_count']) * entry
        try:
            from gridtrade.execution.leverage_policy import pick_leverage, feasible
            _tiers = self.adapter.fetch_leverage_tiers(symbol)
            _L = pick_leverage(worst_notional, _tiers, self.gearing)
            if _L is not None:
                self.adapter.set_leverage(symbol, _L)
                if not feasible(worst_notional, _tiers, self.gearing):
                    print('[leverage] WARN %s worst名义 $%.0f 超 ceil(gearing) 档上限——设 %dx 尽力,'
                          '可能 -2027(极罕见,open_proposals 隔离兜底)' % (symbol, worst_notional, _L), flush=True)
                else:
                    print('[leverage] %s set %dx (worst名义 $%.0f)' % (symbol, _L, worst_notional), flush=True)
        except Exception as exc:                          # fail-open:绝不因设杠杆失败而阻断开格
            print('[leverage] WARN %s set_leverage 跳过(fail-open): %r' % (symbol, exc), flush=True)
```

- `worst_notional = order_num × grid_count × entry`(与 fuse worst 同源,order_num/entry 已在作用域)。
- **不缓存**(评估后否决):set_leverage 权重 1、每轮 ≤12 次,权重可忽略;缓存增状态+外部改杠杆的 staleness
  风险,不值。始终设(idempotent;cross 已吞 -4046)。
- 回测:FakeExchange.fetch_leverage_tiers→`[]` → pick_leverage→None → 不设;set_leverage no-op。全程零变化。

## 四、可观测性

- 每次开格 open 打一行:`[leverage] {symbol} set {L}x (worst名义 ${worst})`;不可行则 WARN(见 §3.3)。
- 作用:实盘可见每格实际设的杠杆与 worst 名义;字段/档位异常(fail-open 触发)或不可行币(未来权益长大)可见。

## 五、测试(TDD)

- **`leverage_policy` 纯函数**:`cap_at_leverage`(各档/无匹配)、`feasible`(可行/不可行/tiers 空 fail-open)、
  `pick_leverage`(减一档;idx0 减一档;floor clamp(KITE worst$8k→4);ceil clamp;不可行 worst 超所有档;tiers 空→None)。
  用 §3.2 实测档位(KITE/1000PEPE)当夹具。
- **A 适配器**:`FakeExchange.seed_leverage_tiers` + `fetch_leverage_tiers` 回读;base 默认 `[]`;
  BinanceAdapter 归一化(mock ccxt fetch_leverage_tiers 响应 → [{maxLeverage,maxNotional}])+ 实例缓存(二次不重取)+
  取数异常→`[]`。
- **C open()**:FakeExchange seed 档位 → open 后断言 set_leverage 被以正确 L 调用(记录 lev_calls);
  tiers 空 → 不调 set_leverage(退化);set_leverage 抛异常 → open 不中断(fail-open)、其余挂单/丝照常;
  不可行 worst → WARN 日志(capsys)。
- **golden/core parity**:`tests/core/`+`tests/golden/` 逐位不变(本改动不碰引擎/几何,只在 open 加一段实盘 API)。
- 全量 pytest 全绿;**不部署**(部署由主运维会话按避开整点 HH:00–HH:12 手动做)。

## 六、风险/开放项

- demo tiers 值比主网小 → testnet 会更早设低杠杆(更保守);主网档位大、多数币设较高减一档 L(margin 更省)。可接受。
- 减一档在 worst 贴下界档时余量收窄(仍 ≥1 档)且不低于 ceil(gearing)。
- 真·不可行币(当前 0 个)靠 feasible 告警 + open_proposals 隔离;块 D(排除+回填)暂缓,待权益 ~$30k+ 或实测触发再评估。
- **部署后 testnet 核对**:开格日志出现 `[leverage] … set Nx`;低 maxLev 币(如 KITE)不再 -2027 卡 OPENING。
