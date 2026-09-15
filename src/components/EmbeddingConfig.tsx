import type { EmbeddingState, RebuildStatus } from "../hooks/useEmbedding";

/**
 * 向量检索配置（F5.3/F6.5 用户面）：provider 选择 + 重建进度 + 当前态显示。
 * 本组件不发请求：数据与回调全由父级（Settings + useEmbedding* hooks）注入，
 * 便于单测直驱 props。
 */
export function EmbeddingConfig({
  state,
  loadError,
  switching,
  switchError,
  rebuild,
  message,
  onSwitch,
}: {
  state: EmbeddingState | null;
  loadError: string | null;
  switching: boolean;
  switchError: string | null;
  rebuild: RebuildStatus | null;
  message: string | null;
  onSwitch: (name: string) => void;
}) {
  const rebuilding = state?.rebuilding ?? null;
  const tokens = rebuild?.tokens ?? rebuilding?.tokens ?? 0;
  return (
    <div>
      <h3>向量检索（embedding，双 provider）</h3>
      {loadError != null && <p data-testid="emb-load-error">向量配置加载失败：{loadError}</p>}
      {state == null && loadError == null && <p>加载中…</p>}
      {state != null && (
        <>
          <p data-testid="emb-current">
            当前：{state.active}（{state.dim} 维，表 {state.table}）
          </p>
          {rebuilding != null && (
            <div data-testid="emb-rebuilding">
              <p>
                重建中（{rebuilding.from} → {rebuilding.to}）：检索仍走旧向量 +
                FTS，结果不受影响。
              </p>
              <p data-testid="emb-progress">
                进度：{rebuilding.done}
                {rebuilding.total != null ? ` / ${rebuilding.total}` : ""} 行
                {tokens > 0 && `（已用 token ${tokens}）`}
              </p>
            </div>
          )}
          <ul>
            {state.available.map((e) => (
              <li key={e.name}>
                {e.display}：
                {e.kind === "cloud"
                  ? "需要 API Key（上方保存 openai 槽位）"
                  : e.ready
                    ? "权重就绪 ✓"
                    : "权重未下载（见下方模型下载）"}
                {e.note !== "" && ` —— ${e.note}`}
                <button
                  data-testid={`emb-select-${e.name}`}
                  type="button"
                  disabled={switching || rebuilding != null || e.name === state.active}
                  onClick={() => onSwitch(e.name)}
                >
                  {e.name === state.active ? "使用中" : "切换到此"}
                </button>
              </li>
            ))}
          </ul>
          {switching && <p>切换中…（先校验可达，再起后台重建）</p>}
          {switchError != null && <p data-testid="emb-error">切换失败：{switchError}</p>}
          {message != null && <p data-testid="emb-message">{message}</p>}
        </>
      )}
    </div>
  );
}
