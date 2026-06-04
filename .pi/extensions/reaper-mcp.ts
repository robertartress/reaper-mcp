import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

const SERVER = join(process.cwd(), ".venv", "bin", "reaper-mcp-server");
const PREFIX = "reaper_";

const CONFIRM_TOOLS = new Set([
  "create_project",
  "load_project",
  "save_project",
  "delete_track",
  "remove_fx",
  "remove_send",
  "start_recording",
  "render_project",
  "render_stems",
  "render_time_selection",
  "apply_mastering_chain",
  "apply_limiter",
  "normalize_project",
]);

type Pending = {
  resolve: (value: any) => void;
  reject: (reason?: any) => void;
};

class McpStdioClient {
  private proc: ChildProcessWithoutNullStreams | null = null;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private stdoutBuffer = "";
  private stderrTail = "";

  start() {
    if (this.proc) return;
    if (!existsSync(SERVER)) {
      throw new Error(`REAPER MCP server not found at ${SERVER}. Run: python3.12 -m venv .venv && .venv/bin/python -m pip install -e .`);
    }

    this.proc = spawn(SERVER, [], {
      cwd: process.cwd(),
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
    });

    this.proc.stdout.setEncoding("utf8");
    this.proc.stdout.on("data", (chunk: string) => this.handleStdout(chunk));
    this.proc.stderr.setEncoding("utf8");
    this.proc.stderr.on("data", (chunk: string) => {
      this.stderrTail = (this.stderrTail + chunk).slice(-4000);
    });
    this.proc.on("exit", (code, signal) => {
      const err = new Error(`REAPER MCP server exited (${code ?? signal}). ${this.stderrTail}`.trim());
      for (const p of this.pending.values()) p.reject(err);
      this.pending.clear();
      this.proc = null;
    });
  }

  async initialize() {
    this.start();
    await this.request("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "pi-reaper-mcp", version: "0.1.0" },
    });
    this.notify("notifications/initialized", {});
  }

  async listTools() {
    const result = await this.request("tools/list", {});
    return result.tools as Array<{ name: string; description?: string; inputSchema?: any }>;
  }

  async callTool(name: string, args: any) {
    return await this.request("tools/call", { name, arguments: args ?? {} }, 120_000);
  }

  stop() {
    if (!this.proc) return;
    this.proc.kill("SIGTERM");
    this.proc = null;
  }

  private notify(method: string, params: any) {
    if (!this.proc) throw new Error("MCP process is not running");
    this.proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
  }

  private request(method: string, params: any, timeoutMs = 30_000): Promise<any> {
    if (!this.proc) throw new Error("MCP process is not running");
    const id = this.nextId++;
    this.proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Timed out waiting for MCP response to ${method}. ${this.stderrTail}`.trim()));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (reason) => { clearTimeout(timer); reject(reason); },
      });
    });
  }

  private handleStdout(chunk: string) {
    this.stdoutBuffer += chunk;
    for (;;) {
      const idx = this.stdoutBuffer.indexOf("\n");
      if (idx < 0) break;
      const line = this.stdoutBuffer.slice(0, idx).trim();
      this.stdoutBuffer = this.stdoutBuffer.slice(idx + 1);
      if (!line) continue;
      let msg: any;
      try { msg = JSON.parse(line); } catch { continue; }
      if (typeof msg.id === "number" && this.pending.has(msg.id)) {
        const p = this.pending.get(msg.id)!;
        this.pending.delete(msg.id);
        if (msg.error) p.reject(new Error(msg.error.message ?? JSON.stringify(msg.error)));
        else p.resolve(msg.result);
      }
    }
  }
}

function normalizeSchema(schema: any) {
  if (!schema || typeof schema !== "object") return { type: "object", additionalProperties: true };
  return {
    type: "object",
    properties: schema.properties ?? {},
    required: schema.required ?? [],
    additionalProperties: schema.additionalProperties ?? true,
  };
}

function resultToText(result: any): string {
  if (Array.isArray(result?.content)) {
    return result.content.map((c: any) => c?.text ?? JSON.stringify(c)).join("\n");
  }
  return JSON.stringify(result, null, 2);
}

export default async function (pi: ExtensionAPI) {
  const client = new McpStdioClient();

  try {
    await client.initialize();
    const tools = await client.listTools();

    for (const tool of tools) {
      const originalName = tool.name;
      pi.registerTool({
        name: `${PREFIX}${originalName}`,
        label: `REAPER: ${originalName}`,
        description: tool.description ?? `Call REAPER MCP tool ${originalName}`,
        promptSnippet: `Control REAPER DAW: ${originalName}`,
        promptGuidelines: [
          `Use ${PREFIX}${originalName} only for explicit REAPER/DAW tasks requested by the user.`,
          `Ask before using ${PREFIX}${originalName} if it may overwrite files, delete project data, record audio, or render long audio.`,
        ],
        parameters: normalizeSchema(tool.inputSchema),
        async execute(_toolCallId, params, _signal, onUpdate, ctx) {
          if (CONFIRM_TOOLS.has(originalName)) {
            const ok = await ctx.ui.confirm(
              `Run REAPER tool ${originalName}?`,
              `Arguments:\n${JSON.stringify(params, null, 2)}`,
            );
            if (!ok) {
              return { content: [{ type: "text", text: `Cancelled ${originalName}.` }], details: { cancelled: true } };
            }
          }
          onUpdate?.({ content: [{ type: "text", text: `Calling REAPER MCP tool ${originalName}...` }] });
          const result = await client.callTool(originalName, params);
          return {
            content: [{ type: "text", text: resultToText(result) }],
            details: result,
          };
        },
      });
    }

    pi.registerCommand("reaper-mcp", {
      description: "Show REAPER MCP extension status",
      handler: async (_args, ctx) => {
        ctx.ui.notify(`REAPER MCP extension loaded ${tools.length} tools using ${SERVER}`, "info");
      },
    });
  } catch (error: any) {
    pi.registerCommand("reaper-mcp", {
      description: "Show REAPER MCP extension startup error",
      handler: async (_args, ctx) => {
        ctx.ui.notify(`REAPER MCP failed to load: ${error?.message ?? String(error)}`, "error");
      },
    });
    throw error;
  }

  pi.on("session_shutdown", async () => {
    client.stop();
  });
}
