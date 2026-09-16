# 网络基线（P13 立；此后所有会话引用此档，不再各说各话）

> 终结的矛盾：P4（`benchmark.md` F6.1/延期台账，2026-09-15）记“openai/claude/gemini/groq
> 网络可达（dummy key 回 401/401/400/401，均 <1s）”；M3（`acceptance-m3.md` §2.2，同日）
> 记“TCP 探针 `8.8.8.8:443`/`1.1.1.1:443`/`api.openai.com:443` 均 TimeoutError，外网不可达”。
> **双方都诚实，仪器不同**——结论以本档为准。

## 1. 机制（一句话）

本机出口被 TUN 透明劫持（DNS 返回 `198.18.x.x`/`fdfe::/48` 保留段、TCP 本地终结），
叠加**间歇性秒级首包抖动**。M3 的 1s 超时裸 TCP 探针撞上抖动即判死；
HTTP 层（完整握手 + 秒级以上超时）稳定可达。**以后判定可达性只看 HTTP 状态码，
不看裸 TCP 连通时间。**

## 2. 证据（`scripts/net_probe.py` 三段输出，两次运行）

运行：`MODE=system`（系统现状）与 `MODE=direct`（进程级旁路：清代理环境变量 +
`NO_PROXY=*`，**系统注册表未动**）各一次，2026-09-16，B 机。

### A. 代理快照（两次一致）

- 进程环境：`HTTP(S)_PROXY`/`ALL_PROXY` 全空；`NO_PROXY=127.0.0.1,localhost,::1`。
- 注册表：`ProxyEnable=1`，`ProxyServer=127.0.0.1:7897`，端口 **OPEN**（3ms）。
  `urllib.getproxies()` 只吐 `no` 项——**httpx/urllib 均不读注册表代理**，
  注册表代理只对 WinINET 系（浏览器等）生效，Python 侧等价于无代理。
- 六个云端 `*_API_KEY` 环境变量**全空**（只报有/无，不读值）。

### B. 直连 TCP（`socket.create_connection`，timeout=5s，不发字节）

| 目标 | system | direct |
|---|---|---|
| 8.8.8.8:443 | OK 12ms | OK 0ms |
| 1.1.1.1:443 | OK 1ms | OK 15ms |
| api.openai.com:443 | OK 15ms（另一次运行 5006/5008ms） | OK 5008ms |
| api.anthropic.com:443 | OK 1ms | OK 15ms |
| generativelanguage:443 | OK 14ms | OK 16ms |
| api.groq.com:443 | OK 5003ms（抖动轮转） | OK 16ms |

- `8.8.8.8` 0–12ms 建连 = 本地终结，不可能是真实跨网 RTT——TUN 劫持的直接证据。
- `getaddrinfo(api.openai.com)` → `198.18.0.46` + `fdfe:dcba:9876::2d`（保留段）——DNS 劫持的直接证据。
- ~5s 的 OK 不是“慢速通”，是抖动轮转（openai/groq 交替出现，复测即消失）——
  M3 用 1s 超时恰好撞上这类抖动，`TimeoutError` 是仪器误判，不是断网。

### C. 代理感知 HTTP（httpx 0.28.1，dummy key，三次运行 12/12 一致）

| provider | system 默认 | system 显式走 7897 | direct |
|---|---|---|---|
| openai | 401 | 401 | 401 |
| groq | 401 | 401 | 401 |
| claude | 401 | 401 | 401 |
| gemini | 400（Google 无效 key 用 400，前轮已映射为 auth） | 400 | 400 |

- 401/400 = 对端活着并拒绝了假 key = **路径可达**（与 P4 的 0.5–0.9s 口径一致）。
- 显式走注册表代理同样全通——7897 本身转发正常，只是 Python 默认不走它。

## 3. 裁定与引用纪律

1. **可达性结论：云端四家 HTTP 可达**（本档 C 节，稳定复现）。M3 §2.2 的“外网不可达”
   作废，被本档取代；M3 §2.2 的 TCP 超时记录保留为“仪器误判案例”，不再作为延期依据。
2. 此后任何“网络通/不通”的断言必须注明仪器（三段中的哪一段 + 超时值），
   裸 TCP ≤1s 超时的结论一律视为无效证据。
3. 云端联测仍缺的唯一输入是 **key**（六个环境变量全空，见 A 节），不是网络。
   key 到位后按 P13 步骤 2 收数；未到位前沿用本档，不重测、不借数。
4. 复测入口：`python scripts/net_probe.py [--direct]`（退出码恒 0，只记录不判定）。
   真 key 绝不进探针（探针只用 `sk-probe-DUMMY` 假值 + key 有/无布尔值）。
