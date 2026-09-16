# 手机伴侣安全边界（S8）—— `docs/companion-security.md`

> 版本：2026-09-16（S8 落地版）｜归属：Phase 3 / S8 ｜ 与 `docs/stealth-boundary.md` 互补，**不冲突、不重叠**。
>
> 实现：`src-tauri/src/companion/{mod,server,phone_page}.rs`；测试：`src-tauri/tests/companion_test.rs`
> （**27 例**，其中 3 例直接绑**真实局域网网卡**，不只是 127.0.0.1 的测试缝）。

---

## 1. 它到底是什么

主屏（本机）在**局域网 IP** 上开一个端口 `54322`，同时服务两件事：

| 请求 | 作用 |
|------|------|
| `GET /?token=…` | 返回手机端页面（自包含 HTML，无 CDN/外部资源） |
| `GET /ws?token=…`（WebSocket 升级） | 推送主屏的提词卡片 |

主屏把 `http://<lan_ip>:54322/?token=<token>` 渲染成二维码，手机扫码 → 浏览器打开页面 →
页面里的 JS 自己连 `ws://<lan_ip>:54322/ws?token=<token>`。

**没有云、没有中继、没有信令服务器**，全程只有同一个 Wi-Fi 内的两台设备。

---

## 2. 威胁模型

| 攻击面 | 假设 | 缓解 |
|--------|------|------|
| **局域网窃听** | 攻击者接入同一 Wi-Fi | 明文 WS 里只有提词卡片（问答文本），无密钥、无音频、无用户标识。**不要指望它保密**——要害数据不在这条链路上 |
| **二维码被拍照** | 公共场合展示时被拍 | 换 token（`rotate_companion_token`）即作废；同一 token 二次连接会**顶掉**旧连接（真机被踢是可见信号）；主屏随时「停止」/「断开」 |
| **未授权连接** | 旁观者扫码 | token = 32 字节 CSPRNG（URL-safe base64，43 字符）；页面与握手都要 token，错/缺一律 401 |
| **持久化后门** | 服务常驻被滥用 | **默认不监听**；只有用户点「开启伴侣屏」才起；应用退出（`RunEvent::Exit`）必停 |
| **手机端被注入** | 恶意脚本在二屏执行 | 二屏**只读**：上行帧被服务端丢弃，页面源码里没有 `send`/`fetch`/`XHR`（有 grep 断言） |
| **日志泄露 token** | 调试时随手打印 | token 只存在于 QR 编码的 URL 与内存；**前端拿不到明文**（命令只回 QR 的 SVG）；源码级断言禁止把 token 写进日志 |

---

## 3. 硬约束清单（违背即为 bug，每条都有测试）

| # | 约束 | 钉住它的测试 |
|---|------|--------------|
| 1 | **只绑局域网 IP**：`start()` 只接受 RFC1918 私有 IPv4，挑不到就拒绝启动（fail-closed） | `only_private_ipv4_counts_as_lan`、`pick_lan_ip_skips_loopback_and_public…`、`nothing_binds_the_wildcard_address` |
| 2 | **token 进 QR，不进日志**：不写 `eprintln`/`println`/`log` | `no_token_is_ever_written_to_a_log` |
| 3 | **无云中继、无公网暴露**：代码里没有 TURN/STUN/任何外发地址 | 人工评审 + 本文档 |
| 4 | **随时可吊销**：`stop`（全断+关端口）/ `revoke_client(id)`（单台）/ `rotate_token`（换 token） | `revoking_a_device_is_not_the_same_as_stopping_the_service`、`stop_closes_everyone_and_frees_the_port`、`rotating_the_token_renders_the_old_one_useless` |
| 5 | **二屏只读镜像**：上行帧丢弃、页面无上行 API、载荷复用 `LiveCard` | `uplink_frames_are_ignored_the_second_screen_is_read_only`、`the_phone_page_has_no_uplink_capability` |
| 6 | **默认关闭 + 退出必停** | `the_service_stops_on_app_exit` |
| 7 | **前端拿不到 token**：`CompanionStart` 只有 `info` + `qrSvg` | `the_phone_page…`（Rust）/ 前端 `CompanionPanel.test.tsx` 契约断言 |
| 8 | **生产路径本身也被测过**：`start()` 只绑私有 IPv4 + 固定端口 54322；端口被占时**报错并停在 Stopped**，不偷偷换端口 | `start_binds_a_private_lan_ip_and_refuses_when_there_is_none`、`a_busy_production_port_fails_loudly_instead_of_silently_moving` |

**单 token 单连接**：同一 token 再次握手时，旧连接被顶掉（`replaced_by_new_connection`）
而不是被拒绝——这样手机刷新页面才不会把自己锁在外面。代价是克隆设备会挤掉真机，
这个状态在主屏「已连接设备」列表里看得见（数量恒为 1）。

---

## 4. 协议规格（`protocol_version = 1`）

### 4.1 下行帧（主屏 → 手机）

```json
{"type":"welcome","protocolVersion":1}
{"type":"cards","cards":[{"id":"c1","question":"…","answer":"…","source":"字段直查","kind":"direct","atMs":1700000000000,"reused":false}]}
{"type":"revoked","reason":"server_stopped|revoked_by_host|token_rotated|replaced_by_new_connection"}
```

- `cards` 与 `teleprompter://cards` 的载荷**完全一致**（同一个 `LiveCard`，注意 `atMs` 是 camelCase）。
- 新连接会先收到 `welcome`，若已有卡片，紧接着补一帧最新快照（不留白）。
- 心跳：服务端每 15s 发 `Ping`，浏览器自动回 `Pong`。

### 4.2 上行

**没有**。任何上行帧都被服务端丢弃（不解析、不执行、不回错误）。

### 4.3 关闭语义

| 场景 | 服务端 | 手机端表现 |
|------|--------|-----------|
| token 错/缺 | HTTP 401，不建 WS | 「连接已失效，请重新扫码」 |
| 主屏点「停止」 | `revoked(server_stopped)` + `Close(4001)` + 关端口 | 「已被主屏断开」，**不重连** |
| 单台「断开」 | `revoked(revoked_by_host)` + `Close(4001)`，服务继续监听 | 同上；重新扫码还能连 |
| 「刷新二维码」 | `revoked(token_rotated)` + 换 token | 同上；旧码作废 |
| 手机锁屏/关页面 | 客户端发 Close | 服务端清理连接（不留幽灵） |
| 网络抖动 | — | 退避重试最多 5 次；4001 不重试 |

---

## 5. 与 `stealth-boundary.md` 的边界

| 项目 | `stealth-boundary.md` | 本文档 |
|------|----------------------|--------|
| 保护对象 | 主窗/提词窗（本机屏幕捕获） | 手机伴侣通道（LAN WS） |
| 核心手段 | `WDA_EXCLUDEFROMCAPTURE` / `sharingType(.none)` | token 鉴权 + 只绑局域网 + 吊销 |
| 信任边界 | 操作系统窗口属性 | 同一物理局域网 + 扫码的物理接触 |
| 明确不做 | 进程伪装/注入/反检测 | 公网暴露/云中继/token 持久化 |

**不承诺的事**：不做传输加密（LAN 明文 WS）、不做设备指纹、不做双向认证。
要害数据（API key、音频、会话账）都不经过这条链路。

---

## 6. 变更管理

改协议版本必须同时改四处：

1. Rust `companion::PROTOCOL_VERSION`；
2. `companion/phone_page.rs` 里的 `PROTOCOL_VERSION`（页面会比对）；
3. 本文档 §4；
4. `companion_test.rs` 的 `frames_match_the_wire_contract` / welcome 断言。

新增帧类型必须双向评审（Rust + 页面），防止单边扩展破坏兼容。

## 7. 手工验收清单（自动化覆盖不到的部分）

自动化已覆盖协议与鉴权的全部语义（`companion_test.rs` 27 例）。其中三例刻意**绑真实局域网网卡**，
因为回环上跑通不等于手机连得上：

| 用例 | 钉住什么 |
|------|----------|
| `start_binds_a_private_lan_ip_and_refuses_when_there_is_none` | 生产路径 `start()` 真的只绑 RFC1918 私有 IPv4 + 固定端口 54322；没有局域网时 fail-closed |
| `the_lan_interface_serves_the_page_and_the_card_stream` | 从**网卡地址**（非 127.0.0.1）取页面（200/401）与握手推卡片全通；stop 后端口真关 |
| `a_busy_production_port_fails_loudly_instead_of_silently_moving` | 54322 被占用时报错并停在 `Stopped`，不偷偷换端口继续监听 |

下面这些仍要拿真手机走一遍：

1. 主屏点「开启伴侣屏」→ 出二维码；手机连同一 Wi-Fi 扫码 → 页面显示「已连接」。
2. 主屏提词 → 手机 1 秒内看到同一张卡片，且**没有重复检索**（日志里检索次数不翻倍）。
3. 主屏点「断开」→ 手机提示已断开；点「停止」→ 端口关闭（`netstat` 里 54322 消失）。
4. 断开 Wi-Fi → 手机退避重试；重连后恢复。
5. 全程 `eprintln` 输出里**搜不到 token 明文**。
6. **Windows 防火墙**（真机连不上的头号原因）：主屏首次监听 54322 时系统可能弹
   「是否允许访问网络」——必须允许**专用网络**；选「公用网络」手机照样连不进来。
   排查办法：在 PC 浏览器里打开 `http://<lan_ip>:54322/`——**应当回 401**（说明路是通的，
   只是没带 token）；如果是转圈超时，就是被防火墙拦了，不是伴侣服务的问题。
