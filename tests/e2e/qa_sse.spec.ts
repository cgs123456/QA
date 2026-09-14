/**
 * Phase 1a SSE 冒烟（API 契约级，可在无 Tauri 壳/无浏览器下运行）：
 * 建库→导入→提问→流式答案→切库→再问指向新库→删除级联。
 *
 * 运行（仓库根）：
 *   set INTERVIEWCOPILOT_PYTHON=<python.exe>
 *   set INTERVIEWCOPILOT_DB=<临时db路径，可选>
 *   pnpm test:e2e
 *
 * 说明：spec 自带 sidecar 子进程（DB 指向系统临时目录，不污染开发库）；
 * UI 页面的点击走查见 docs/PROGRESS.md 人工 E2E（需 Tauri 壳）。
 */
import { test, expect } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as readline from "node:readline";

let proc: ChildProcess | undefined;
let baseURL = "";
let token = "";

function authHeaders(): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

async function api(
  method: string,
  urlPath: string,
  body?: unknown,
): Promise<{ status: number; json: unknown }> {
  const res = await fetch(`${baseURL}${urlPath}`, {
    method,
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let parsed: unknown;
  try {
    parsed = text ? (JSON.parse(text) as unknown) : null;
  } catch {
    parsed = text;
  }
  return { status: res.status, json: parsed };
}

async function readHandshake(
  stream: NodeJS.ReadableStream | null,
): Promise<{ protocol_version: string; port: number; auth_token: string }> {
  if (stream == null) throw new Error("sidecar stdout missing");
  const rl = readline.createInterface({ input: stream, crlfDelay: Infinity });
  for await (const line of rl) {
    const trimmed = line.trim();
    if (trimmed.startsWith("{")) {
      rl.close();
      return JSON.parse(trimmed) as {
        protocol_version: string;
        port: number;
        auth_token: string;
      };
    }
  }
  throw new Error("no handshake line");
}

async function readSSE(urlPath: string): Promise<{
  headers: Record<string, string | null>;
  events: Array<Record<string, unknown>>;
}> {
  const res = await fetch(`${baseURL}${urlPath}`, { headers: authHeaders() });
  const headers: Record<string, string | null> = {
    "content-type": res.headers.get("content-type"),
    "cache-control": res.headers.get("cache-control"),
    "x-accel-buffering": res.headers.get("x-accel-buffering"),
  };
  const text = await res.text();
  const events: Array<Record<string, unknown>> = [];
  for (const frame of text.split("\n\n")) {
    for (const line of frame.split("\n")) {
      if (line.startsWith("data:")) {
        events.push(JSON.parse(line.slice(5).trim()) as Record<string, unknown>);
      }
    }
  }
  return { headers, events };
}

test.beforeAll(async () => {
  const python = process.env["INTERVIEWCOPILOT_PYTHON"] ?? "python";
  const tmpDb =
    process.env["INTERVIEWCOPILOT_DB"] ??
    path.join(
      fs.mkdtempSync(path.join(os.tmpdir(), "qa-e2e-")),
      "e2e.db",
    );
  proc = spawn(python, ["sidecar/src/main.py"], {
    cwd: process.cwd(),
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, INTERVIEWCOPILOT_DB: tmpDb },
  });
  const hs = await readHandshake(proc.stdout);
  expect(hs.protocol_version).toBe("1.0");
  baseURL = `http://127.0.0.1:${hs.port}`;
  token = hs.auth_token;
}, 60000);

test.afterAll(() => {
  proc?.kill();
});

const SEED = JSON.parse(
  fs.readFileSync("sidecar/tests/eval/seed_demo.json", "utf-8") as string,
);

test("qa sse contract: import→ask→switch→delete", async () => {
  // 建库 A 并导入种子。
  let r = await api("POST", "/store", { name: "E2E库A" });
  expect(r.status).toBe(200);
  const storeA = (r.json as { id: string }).id;
  r = await api("POST", "/knowledge/compile", {
    store_id: storeA,
    format: "json",
    content: JSON.stringify(SEED),
  });
  expect(r.status).toBe(200);
  expect(
    ((r.json as { stats: { qa_inserted: number } }).stats.qa_inserted),
  ).toBe(8);

  // 直接返回档：retrieval → done，官方答案，无 LLM。
  r = await api("POST", "/qa/ask", {
    question: "公司成立时间",
    store_id: storeA,
  });
  expect(r.status).toBe(200);
  const taskId = (r.json as { task_id: string }).task_id;
  const stream = await readSSE(`/qa/stream?task_id=${taskId}`);
  expect(stream.headers["content-type"] ?? "").toContain("text/event-stream");
  expect(stream.headers["x-accel-buffering"]).toBe("no");
  expect(stream.events.map((e) => e["type"])).toEqual(["retrieval", "done"]);
  expect(stream.events[0]["action"]).toBe("direct");
  const done = stream.events[1]["result"] as {
    text: string;
    llm_calls: number;
  };
  expect(done.text).toBe("公司成立于2015年，总部位于北京。");
  expect(done.llm_calls).toBe(0);

  // 无匹配 → Fail-Closed 文案。
  r = await api("POST", "/qa/ask", {
    question: "量子电动力学xyz",
    store_id: storeA,
  });
  const failTask = (r.json as { task_id: string }).task_id;
  const failStream = await readSSE(`/qa/stream?task_id=${failTask}`);
  expect(failStream.events.map((e) => e["type"])).toEqual(["retrieval", "done"]);
  expect(
    (failStream.events[1]["result"] as { text: string }).text,
  ).toBe("知识库未命中");

  // 建库 B 并切换当前库：无 store_id 提问指向新库。
  r = await api("POST", "/store", { name: "E2E库B" });
  const storeB = (r.json as { id: string }).id;
  r = await api("POST", "/knowledge/compile", {
    store_id: storeB,
    format: "markdown",
    content: "# B库\n\n## 独家字段X\n\n独家内容。",
  });
  expect(r.status).toBe(200);
  r = await api("PUT", "/stores/current", { store_id: storeB });
  expect(r.status).toBe(200);
  r = await api("POST", "/qa/ask", { question: "独家字段X" });
  const taskB = (r.json as { task_id: string }).task_id;
  const streamB = await readSSE(`/qa/stream?task_id=${taskB}`);
  expect(streamB.events[0]["store_id"]).toBe(storeB);

  // 删除级联：A 详情 404。
  r = await api("DELETE", `/store/${storeA}`);
  expect(r.status).toBe(200);
  r = await api("GET", `/store/${storeA}`);
  expect(r.status).toBe(404);
});
