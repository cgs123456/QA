# M1 全方位 Review（2026-09-14，验收后）

> 方法：独立对抗审查（subagent 只读全仓，专找并发/错误吞没/安全/泄漏/逻辑硬伤）
> + 我逐条复核复现；机器核验（路由表/鉴权覆盖/R8 泄漏/越界/契约一致）全跑。
> 结论：发现实质问题 8 项（已修 7，接受 1），另附带修历史残留 bug 2。
> 本轮后：pytest 108、vitest 10、Playwright 1、tsc/eslint 0。

## 1. 铁律合规 verdict（R1–R8，证据）

| 铁律 | 结论 | 证据 |
|---|---|---|
| R1 唯一访问者 | 遵守 | Rust 零 sqlite 引用（grep）；一切经 HTTP（api.ts） |
| R2 握手 + Bearer | 遵守 | 握手单行 JSON；13 端点无 token →401 全覆盖单测新增；`compare_digest` |
| R3 单连接 + 扩展顺序 + 绝对锚定 | **修后遵守** | 写串行此前零执行（见 §2-F1）；顺序 enable→libsimple→jieba_dict→vec 代码在位；路径全绝对（单测锁定） |
| R4 列对齐 + 反假绿 | 遵守 | 全 FTS 断言先插数据（§3 复核清单）；列名逐字对齐 |
| R5 四路 s_i + 阈值 + rejected 过滤 | 遵守 | 融合三案精确复核；全部检索 SQL 含 rejected 过滤（含 vec JOIN 形态） |
| R6 事务纪律 | 遵守 | embedding 参数位 + SIGKILL 零残留单测；本次补齐写串行使“同一短事务”成立 |
| R7 生命周期 | 代码在位，运行待壳 | 轮询/退避≤3/Exit 强杀/taskkill 树；4 连败→事件→降级页→重试链完整（单测级） |
| R8 content-free | 遵守 | 双端泄漏 grep 零命中；traceback 只含种类名；token/key 永不落盘/日志/响应 |

## 2. 本轮发现与处置

### 已修复（7）

- **F1 R3 写串行零执行（高）**：`write_lock` 定义后无人持有；单例 `check_same_thread=False`
  跨线程共用。修：RLock + stores 三写函数 + compiler 事务 ngoại层持有；读不受影响（WAL）。
- **F2 TASKS 只增不减（高）**：TTL 只在 GET 检查，不来读的 ask 永久残留。修：ask/stream 入口 `_sweep_tasks`。
- **F3 _JOBS 只增不减（高）**：修：completed_at + 终端态 300s 清理 + 入口 sweep。
- **F4 下载非预期异常黑洞（高）**：`_run_download` 仅接三类下载异常，磁盘满等致 job 永 downloading。
  修： broad except 落 error 终态（种类名）。
- **F5 端口 TOCTOU（高）**：pick 后重绑竞态 + 占位者可冒充 health。修：启动 nonce 回显 + 匹配校验
  （附带：main 健康检查连接泄漏一并 finally 关闭）。
- **F6 FTS 异常全吞（中）**：`except OperationalError → []` 掩盖缺表/缺扩展/I-O/损坏/忙/锁。
  修：仅 SQLITE_ERROR(1) 非结构性语法问题兜底，其余上抛走 error 事件 + stderr 堆栈；单测锁定双向。
- **F7 后台异常无堆栈（中）**：修：`traceback.print_exc()` 进 stderr（Rust 落盘文件 → 降级页可查；
  回显给客户端的仍只有种类名）。
- **F8 健康检查连接泄漏（低）**：见 F5 一并修复。

### 附带修（review 牵出的历史残留，2）

- **H1 app.py 路由重复定义（真 bug）**：`/health` 与 `/sidecar/info` 在文件内定义两次
  （历史合并残留），致路由表重复 + OpenAPI 重复 Operation ID 警告。行为恰好无害
  （Starlette 首匹配 + 单执行有单测为证），现已删除重复块；路由表重查 14 唯一，
  与 `api-contract.md` 逐项一致。
- **H2 前端取消失效（真 bug）**：`sidecarFetch` 自建 AbortController 覆盖调用方 signal，
  `useQA.cancel()` 永不到达 fetch，且取消会被误报为超时。修：信号透传 + 主动取消原样抛
  AbortError（悬挂服务单测锁定）；附带 `pyproject.description` 过期文案更新。

### 接受（1，有记录的权衡）

- **A1 vec 路 blanket 降级**：embed 缺模型是预期内常见态（无模型环境必须能跑），
  降级 + warnings 上客户端已是可观测通道；不动。FTS 侧因“语法 vs 故障”可机械区分，
  已收窄（F6），两者不对称是有意为之。

### 未动（框架行为， Cosmetic）

- FastAPI 0.141 `_IncludedRouter` 路由表象 + OpenAPI `security` 列不显示 Depends：
  dump 脚本误报过“重复/缺失”，已证伪（行为单测全绿 + OpenAPI 路径表 14/14 对齐契约）。
  无动作。

## 3. 覆盖核验（机器）

- 鉴权：13 端点无 token →401 全覆盖单测 + `/health` 200（新增 `test_auth_coverage.py`）。
- R8：双端 `print/log × key|token|password` 零命中（握手 stdout 管道除外，属契约）；
  localStorage 仅 provider 名；key 输入框提交后清空。
- 越界：Phase 2/3 关键词（excel/pdf/stealth/tray/audio/cli/updater/rehearsal）零实现，
  仅占位与文档提及；Linux fallback 占位明确 Phase 2。
- 契约一致：OpenAPI 14 路径 == api-contract.md 14 行；models.md SHA == registry；
  benchmark 机器行被 check_size 消费（PASS）。

## 4. 债务与风险（诚实清单）

1. **Rust 零编译验证**（环境无工具链）：新增 save_api_key/log_tail/stderr 落盘/push 接线
   均只经人工审查 + 类型自查；首个 `cargo build/test/audit` + Cargo.lock 入仓仍待办。
2. **Ollama e2e 缺失**（本机无服务）：maybe 档真流式只经 MockTransport 验证。
3. **标定债**：阈值全初值；demo 基线 direct=0.15/FC=0.80 的结构性解读已落盘，
   100 题集待用户题目后重标定（标定纪律重申）。
4. **前端测试止于逻辑层**：vitest 覆盖解析/标签/错误分类/取消；组件交互（点击流）靠人工走查。
5. **_embedder 单例无失效处理**：模型文件中途删除后单例常驻内存旧句柄——重启即恢复，
   可接受，未修。
6. **TASKS/_JOBS 为进程内存**：sidecar 重启即失（by design：握手/token 同理）；前端刷新页
   面后旧 task_id 即 404——符合 TTL 语义，已在契约注明。
7. **pyproject/requirements-lock 一致性**：lock 由 clean venv freeze 生成，含 pytest；
   运行期最小集合未单独拆分（test 依赖随包是可接受的内部自用形态）。

## 5. 规模 snapshot

- sidecar 源码 40 py 文件；前端 33 ts/tsx；单测 pytest 108 / vitest 10 / Playwright 1；
  tsc + eslint 0；git log 11 commits（task-1…11，无 WIP）。
