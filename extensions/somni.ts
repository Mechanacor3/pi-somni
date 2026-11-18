/**
 * Somni — layered memory with offline "dreaming" for Pi.
 *
 * All memory policy lives in somni/store.py (stdlib-only, tested offline).  This
 * extension is a thin adapter.  It is not a security boundary: run untrusted agents
 * in a container whose only writable mount is the Somni workspace.
 *
 * What it does in a session:
 *   - session_start: create the store if needed (SOMNI_WORKSPACE, default
 *     `<cwd>/.pi/somni`) and open a recording session.
 *   - before_agent_start: inject the bounded, salience-ordered claim index plus the
 *     claims most relevant to the prompt as one hidden custom message.
 *   - while running: record the user prompt (observation), each tool result
 *     (tool_result) and the assistant's final text (agent_output) into the
 *     hash-chained transcript.  Text is clipped to the store's record limit.
 *   - tools: somni_recall (claims for a query), somni_search (exact transcript
 *     search), somni_note (add a cited claim while working).
 *   - /somni-dream [focus]: close the session, let the model propose a dream pass
 *     over recent transcript through the somni_dream tool, then reopen.
 *   - session_shutdown: close the session.
 *
 * Testbed mode (SOMNI_RECORD=0): nothing is recorded automatically; a trusted driver
 * records exactly the text it delivered, using somni/store.py's CLI.
 */

import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const STORE_PY = process.env.SOMNI_STORE ?? resolve(HERE, "../somni/store.py");
const PYTHON = process.env.SOMNI_PYTHON ?? "python3";
const SUBJECT = process.env.SOMNI_SUBJECT ?? "pi";
const RECORD = process.env.SOMNI_RECORD !== "0";
const INJECT_BYTES = Number(process.env.SOMNI_INJECT_BYTES ?? 4096);
const MAX_RECORD_BYTES = 8000;
const DREAM_WINDOW = 120;

const state = { workspace: process.env.SOMNI_WORKSPACE ?? "", dreaming: false, ready: false };

function run(args: string[], input = "") {
	const out = spawnSync(PYTHON, ["-I", STORE_PY, "--workspace", state.workspace, "--subject-id", SUBJECT, ...args], {
		input,
		encoding: "utf8",
		timeout: 120_000,
		maxBuffer: 8 * 1024 * 1024,
		env: { PATH: process.env.PATH ?? "/usr/bin:/bin", LC_ALL: "C.UTF-8" },
	});
	if (out.error || out.status !== 0) {
		throw new Error(`somni: ${out.error?.message ?? out.stderr?.trim().slice(-500) ?? out.status}`);
	}
	return out.stdout;
}

function clip(text: string) {
	const bytes = Buffer.from(text, "utf8");
	if (bytes.length <= MAX_RECORD_BYTES) return text;
	return `${bytes.subarray(0, MAX_RECORD_BYTES - 32).toString("utf8")} …[truncated]`;
}

function record(source: "ordinary_observation" | "tool_result" | "agent_output", text: string) {
	if (!RECORD || !state.ready || state.dreaming || !text.trim()) return;
	try {
		run(["record", "--source", source, "--text", clip(text)]);
	} catch {
		// Recording is best-effort; a full or closed store must not break the session.
	}
}

function setPhase(phase: "open" | "closed") {
	try {
		run([phase === "open" ? "session-open" : "session-close"]);
	} catch (err) {
		if (!String(err).includes(`already ${phase}`)) throw err;
	}
}

function textOf(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content.map((part: any) => (part?.type === "text" ? part.text : "")).join("");
}

function asText(value: unknown) {
	return [{ type: "text" as const, text: typeof value === "string" ? value : JSON.stringify(value, null, 1) }];
}

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "somni_recall",
		label: "Somni recall",
		description: "Recall your memory claims most relevant to a query (each claim cites transcript evidence).",
		parameters: Type.Object({ query: Type.String(), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })) }),
		async execute(_id: string, params: { query: string; limit?: number }) {
			const py = `import sys,json;sys.path.insert(0,${JSON.stringify(dirname(STORE_PY))});import store as s
m=s.SomniMemory(sys.argv[1],sys.argv[2]).load();print(json.dumps(m.recall(sys.argv[3],int(sys.argv[4]))))`;
			const out = spawnSync(PYTHON, ["-I", "-c", py, state.workspace, SUBJECT, params.query.slice(0, 2000), String(params.limit ?? 5)], {
				encoding: "utf8",
				timeout: 60_000,
			});
			if (out.status !== 0) throw new Error(out.stderr.slice(-400));
			const claims = JSON.parse(out.stdout) as { id: string; text: string; status: string; evidence: unknown[] }[];
			return { content: asText(claims.map((c) => `${c.id} [${c.status}; ${c.evidence.length} cited] ${c.text}`).join("\n") || "no matching claims"), details: claims };
		},
	});
	pi.registerTool({
		name: "somni_search",
		label: "Somni search",
		description: "Search your recorded transcript for an exact phrase (case-insensitive). Use it to find evidence IDs.",
		parameters: Type.Object({ query: Type.String(), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 50 })) }),
		async execute(_id: string, params: { query: string; limit?: number }) {
			const out = JSON.parse(run(["search", params.query.slice(0, 256), "--limit", String(params.limit ?? 10)]));
			return { content: asText(out.map((r: any) => `[${r.id}] (${r.source}) ${String(r.text).slice(0, 600)}`).join("\n") || "no matches"), details: out };
		},
	});
	pi.registerTool({
		name: "somni_note",
		label: "Somni note",
		description:
			"Add a durable memory claim. Cite transcript IDs as evidence; status 'verified' requires an exact quote " +
			"from each cited record, otherwise use 'uncertain'. Salience 1-5.",
		parameters: Type.Object({
			text: Type.String(),
			status: Type.Union([Type.Literal("verified"), Type.Literal("uncertain")]),
			salience: Type.Optional(Type.Integer({ minimum: 1, maximum: 5 })),
			evidence: Type.Array(Type.Object({ ref: Type.String(), quote: Type.Optional(Type.String()) }), { minItems: 1, maxItems: 16 }),
		}),
		async execute(_id: string, params: any) {
			const claim = { text: params.text, status: params.status, salience: params.salience ?? 3, evidence: params.evidence, links: [] };
			const id = run(["note", "--input", "-"], JSON.stringify(claim)).trim();
			return { content: asText(`saved ${id}`), details: { id } };
		},
	});
	pi.registerTool({
		name: "somni_dream",
		label: "Somni dream",
		description:
			"Only during /somni-dream: submit one consolidation pass as JSON text " +
			'{"schema":1,"operations":[...]} with add/correct/retract operations. Invalid operations are rejected individually.',
		parameters: Type.Object({ proposal_json: Type.String() }),
		async execute(_id: string, params: { proposal_json: string }) {
			if (!state.dreaming) throw new Error("dreaming is only allowed during /somni-dream");
			const py = `import sys,json;sys.path.insert(0,${JSON.stringify(dirname(STORE_PY))});import store as s
m=s.SomniMemory(sys.argv[1],sys.argv[2]).load();print(json.dumps(m.dream(json.loads(sys.stdin.read()),partial=True)))`;
			const out = spawnSync(PYTHON, ["-I", "-c", py, state.workspace, SUBJECT], { input: params.proposal_json, encoding: "utf8", timeout: 60_000 });
			if (out.status !== 0) throw new Error(out.stderr.trim().split("\n").slice(-1)[0] ?? "dream refused");
			return { content: asText(out.stdout), details: JSON.parse(out.stdout) };
		},
	});

	pi.registerCommand("somni-dream", {
		description: "Consolidate recent transcript into cited memory claims (offline dreaming)",
		handler: async (args: string) => {
			const recent = JSON.parse(
				spawnSync(PYTHON, ["-I", "-c", `import sys,json;sys.path.insert(0,${JSON.stringify(dirname(STORE_PY))});import store as s
m=s.SomniMemory(sys.argv[1],sys.argv[2]).load();print(json.dumps([{"id":r["id"],"source":r["source"],"text":r["text"][:1500]} for r in m.transcript[-${DREAM_WINDOW}:]]))`, state.workspace, SUBJECT], { encoding: "utf8" }).stdout || "[]",
			);
			setPhase("closed");
			state.dreaming = true;
			const focus = args?.trim() || "lessons that will help in future sessions";
			pi.sendUserMessage(
				`Dream pass. Review these transcript records and call somni_dream once with a proposal ` +
					`(add/correct/retract claims; cite record IDs; quote exactly for 'verified'). Focus: ${focus}.\n\n` +
					recent.map((r: any) => `[${r.id}] (${r.source}) ${r.text}`).join("\n"),
			);
			// The dream turn runs after this handler returns; agent_settled reopens recording.
		},
	});

	pi.on("agent_settled", async () => {
		if (!state.dreaming) return;
		state.dreaming = false;
		if (RECORD) setPhase("open");
	});

	pi.on("session_start", async (_event, ctx) => {
		if (!state.workspace) state.workspace = join(ctx.cwd, ".pi", "somni");
		if (!existsSync(state.workspace)) mkdirSync(state.workspace, { recursive: true });
		if (!existsSync(join(state.workspace, "somni-v1"))) run(["init", "--lineage-id", "pi", "--attempt-id", "session"]);
		if (RECORD) setPhase("open");
		state.ready = true;
	});

	pi.on("session_shutdown", async () => {
		if (!state.ready || !RECORD) return;
		try {
			setPhase("closed");
		} catch {
			// already closed
		}
	});

	pi.on("before_agent_start", async (event) => {
		if (!state.ready || state.dreaming) return undefined;
		record("ordinary_observation", `USER: ${event.prompt}`);
		let index = "";
		try {
			index = run(["inject", "--max-bytes", String(Math.min(Math.max(INJECT_BYTES, 256), 8192))]);
		} catch {
			return undefined;
		}
		if (!/- n\d{6}/.test(index)) return undefined; // no claims yet
		return { message: { customType: "somni-index", content: index, display: false } };
	});

	pi.on("tool_result", async (event) => {
		if (event.toolName.startsWith("somni_")) return undefined;
		const body = textOf(event.content);
		record("tool_result", `TOOL ${event.toolName}(${JSON.stringify(event.input ?? {}).slice(0, 1000)}) -> ${body}`);
		return undefined;
	});

	pi.on("message_end", async (event) => {
		const message = event.message as any;
		if (message?.role !== "assistant") return undefined;
		const body = textOf(message.content);
		if (body.trim()) record("agent_output", `AGENT: ${body}`);
		return undefined;
	});
}
