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
 *   - /somni-wiki [goal]: dreaming that builds representations: the model gathers
 *     claims not yet on any page into linked wiki pages ([[slug]] links, [nXXXXXX]
 *     citations). The store renders a readable Obsidian vault in somni-v1/vault/.
 *   - session_shutdown: close the session.
 *
 * Testbed mode (SOMNI_RECORD=0): nothing is recorded automatically; a trusted driver
 * records exactly the text it delivered, using somni/store.py's CLI.
 */

import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Backend } from "./backend.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const STORE_PY = process.env.SOMNI_STORE ?? resolve(HERE, "../somni/store.py");
const PYTHON = process.env.SOMNI_PYTHON ?? "python3";
const SUBJECT = process.env.SOMNI_SUBJECT ?? "pi";
const RECORD = process.env.SOMNI_RECORD !== "0";
const configuredBytes = Number(process.env.SOMNI_INJECT_BYTES ?? 4096);
const INJECT_BYTES = Number.isInteger(configuredBytes) ? Math.min(Math.max(configuredBytes, 256), 8192) : 4096;
const MAX_RECORD_BYTES = 8000;
const state = {
	dreaming: false, writable: false,
	backend: undefined as Backend | undefined,
	recordWarning: false, indexWarning: false,
};

function getBackend() {
	if (!state.backend) throw new Error("Somni backend is unavailable.");
	return state.backend;
}

function requireWriteAccess() {
	if (!state.writable) {
		throw new Error("Somni memory changes require the session that owns recording.");
	}
	return getBackend();
}

function clip(text: string) {
	const bytes = Buffer.from(text, "utf8");
	if (bytes.length <= MAX_RECORD_BYTES) return text;
	return `${bytes.subarray(0, MAX_RECORD_BYTES - 32).toString("utf8")} …[truncated]`;
}

async function record(source: "ordinary_observation" | "tool_result" | "agent_output", text: string, ctx: ExtensionContext, inject = false) {
	if (!RECORD || !state.writable || state.dreaming || !text.trim()) return;
	try {
		const output = await getBackend().request<string>("record", {
			source, text: clip(text), ...(inject ? { index_bytes: INJECT_BYTES } : {}),
		});
		state.recordWarning = false;
		return output;
	} catch (error) {
		if (!state.recordWarning) {
			state.recordWarning = true;
			ctx.ui.notify(`Somni could not record an event. ${(error as Error).message}`, "warning");
		}
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
		description: "Recall the wiki pages and memory claims most relevant to a query (pages cite claims; claims cite transcript evidence).",
		parameters: Type.Object({ query: Type.String(), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })) }),
		async execute(_id: string, params: { query: string; limit?: number }) {
			const hits = await getBackend().request<any[]>("recall", { query: params.query.slice(0, 2000), limit: params.limit ?? 5 });
			const lines = hits.slice(0, params.limit ?? 5).map((h) => {
				if (h.slug) return `# ${h.title} (page ${h.slug})\n${h.body}`;
				const lineage = h.merged_from ? `; merged from ${h.merged_from.join(", ")}` : "";
				return `${h.id} [${h.status}; ${h.evidence.length} cited${lineage}] ${h.text}`;
			});
			return { content: asText(lines.join("\n\n") || "no matching memory"), details: hits };
		},
	});
	pi.registerTool({
		name: "somni_search",
		label: "Somni search",
		description: "Search your recorded transcript for an exact phrase (case-insensitive). Use it to find evidence IDs.",
		parameters: Type.Object({ query: Type.String(), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 50 })) }),
		async execute(_id: string, params: { query: string; limit?: number }) {
			const out = await getBackend().request<any[]>("search", { query: params.query.slice(0, 256), limit: params.limit ?? 10 });
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
			const id = await requireWriteAccess().request<string>("note", { ...params, salience: params.salience ?? 3 });
			return { content: asText(`saved ${id}`), details: { id } };
		},
	});
	pi.registerTool({
		name: "somni_dream",
		label: "Somni dream",
		description:
			"Only during /somni-dream or /somni-wiki: submit one consolidation pass as JSON text " +
			'{"schema":1,"operations":[...]} with add/correct/merge/retract/promote claim operations and page/drop_page wiki ' +
			'operations. A merge takes {"op":"merge","claim_ids":["n000001","n000002"],"text":"combined guidance"} and inherits their evidence.',
		parameters: Type.Object({ proposal_json: Type.String() }),
		async execute(_id: string, params: { proposal_json: string }) {
			const backend = requireWriteAccess();
			if (!state.dreaming) throw new Error("dreaming is only allowed during /somni-dream");
			const out = await backend.request("dream", params);
			return { content: asText(out), details: out };
		},
	});

	pi.registerCommand("somni-dream", {
		description: "Consolidate recent transcript into cited memory claims (offline dreaming)",
		handler: async (args: string) => {
			const backend = requireWriteAccess();
			const recent = await backend.request<{ id: string; source: string; text: string }[]>("recent");
			await backend.request("phase", { phase: "closed" });
			state.dreaming = true;
			const focus = args?.trim() || "lessons that will help in future sessions";
			pi.sendUserMessage(
				`Dream pass. Review these transcript records and call somni_dream once with a proposal ` +
					`(add/correct/retract claims; cite record IDs; quote exactly for 'verified'). Focus: ${focus}.\n\n` +
					recent.map((r) => `[${r.id}] (${r.source}) ${r.text}`).join("\n"),
			);
			// The dream turn runs after this handler returns; agent_settled reopens recording.
		},
	});

	pi.registerCommand("somni-wiki", {
		description: "Gather claims into linked wiki pages (dreaming that builds representations)",
		handler: async (args: string) => {
			const backend = requireWriteAccess();
			const info = await backend.request<{ pages: string; claims: string; guide: string; n: number }>("wiki");
			if (!info.n) {
				pi.sendMessage({ customType: "somni-wiki", content: "Every active claim is already on a wiki page.", display: true });
				return;
			}
			await backend.request("phase", { phase: "closed" });
			state.dreaming = true;
			const goal = args?.trim() || "what will help in future sessions";
			pi.sendUserMessage(
				`Wiki pass. Goal: ${goal}. ${info.guide} Gather the claims below into pages and call somni_dream once with ` +
					`page operations: {"op":"page","slug":"lowercase-hyphenated","title":...,"body":markdown citing [nXXXXXX] and ` +
					`linking [[slug]],"claims":[...]}. Extend an existing page by reusing its slug.\n\n## Existing pages\n${info.pages}\n\n## Claims not yet on a page\n${info.claims}`,
			);
		},
	});

	pi.on("agent_settled", async () => {
		if (!state.dreaming) return;
		state.dreaming = false;
		if (RECORD && state.writable) await getBackend().request("phase", { phase: "open" });
	});

	pi.on("session_start", async (_event, ctx) => {
		const workspace = process.env.SOMNI_WORKSPACE ?? join(ctx.cwd, ".pi", "somni");
		state.writable = state.dreaming = state.recordWarning = state.indexWarning = false;
		mkdirSync(workspace, { recursive: true });
		const args = ["-I", STORE_PY, "--workspace", workspace, "--subject-id", SUBJECT, "serve"];
		if (RECORD) args.push("--record");
		const backend = new Backend(PYTHON, args, (error) => {
			if (state.backend !== backend) return;
			state.backend = undefined;
			state.writable = state.dreaming = false;
			ctx.ui.notify(`Somni backend stopped. ${error.message}`, "warning");
		});
		try {
			const { writable } = await backend.ready;
			state.backend = backend;
			state.writable = writable;
			if (RECORD && !writable) {
				ctx.ui.notify("Somni opened without recording because another Pi session is recording in this Somni workspace.", "warning");
			}
		} catch (error) {
			await backend.close();
			ctx.ui.notify(`Somni could not open its memory store. ${(error as Error).message}`, "warning");
		}
	});

	pi.on("session_shutdown", async () => {
		const backend = state.backend;
		state.backend = undefined;
		state.writable = state.dreaming = false;
		await backend?.close();
	});

	pi.on("before_agent_start", async (event, ctx) => {
		if (!state.backend || state.dreaming) return undefined;
		let index = event.prompt ? await record("ordinary_observation", `USER: ${event.prompt}`, ctx, true) : undefined;
		try {
			index ??= await getBackend().request<string>("inject", { index_bytes: INJECT_BYTES });
			state.indexWarning = false;
		} catch (error) {
			if (!state.indexWarning) {
				state.indexWarning = true;
				ctx.ui.notify(`Somni could not load its memory index. ${(error as Error).message}`, "warning");
			}
			return undefined;
		}
		if (!/^-(?: n\d{6} | page `)/m.test(index)) return undefined;
		return { message: { customType: "somni-index", content: index, display: false } };
	});

	pi.on("tool_result", async (event, ctx) => {
		if (event.toolName.startsWith("somni_")) return undefined;
		const body = textOf(event.content);
		await record("tool_result", `TOOL ${event.toolName}(${JSON.stringify(event.input ?? {}).slice(0, 1000)}) -> ${body}`, ctx);
		return undefined;
	});

	pi.on("message_end", async (event, ctx) => {
		const message = event.message as any;
		if (message?.role !== "assistant") return undefined;
		const body = textOf(message.content);
		if (body.trim()) await record("agent_output", `AGENT: ${body}`, ctx);
		return undefined;
	});
}
