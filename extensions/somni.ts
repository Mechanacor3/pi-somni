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
 *   - before_agent_start: inject a bounded index of relevant pages and claims as
 *     one hidden custom message.
 *   - while running: record the user prompt (observation), each tool result
 *     (tool_result) and the assistant's text blocks (agent_output) into the
 *     hash-chained transcript. Tool results can attach bounded images.
 *   - tools: somni_recall (claims for a query), somni_search (exact transcript
 *     search), somni_note (add a cited claim while working).
 *   - /somni-dream [focus]: close the session, let the model propose a dream pass
 *     over recent transcript through the somni_dream tool, then reopen.
 *   - /somni-wiki [goal]: dreaming that builds representations: the model gathers
 *     claims not yet on any page into linked wiki pages ([[slug]] links, [nXXXXXX]
 *     citations). The store renders a readable Obsidian vault in somni-v1/vault/.
 *   - session_shutdown: close the session.
 *   - SOMNI_ACTIONS=1: action words. Dreams may define small Python functions that
 *     call this session's tools as rpc.TOOL(...), rpc.pi.TOOL(...) or
 *     rpc.mcp.SERVER.TOOL(...) (somni/actions.py). Each word is registered as a Pi
 *     tool; its calls run one at a time through ctx.executeTool(), so they pass the
 *     same validation and tool_call guards as the agent's own calls. Words live as
 *     editable files in somni-v1/actions/; edits are recorded at the next session.
 *
 * Testbed mode (SOMNI_RECORD=0): nothing is recorded automatically; a trusted driver
 * records exactly the text it delivered, using somni/store.py's CLI.
 */

import { Type, type ImageContent, type TextContent } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionCommandContext, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createHash } from "node:crypto";
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
const LEARNING = process.env.SOMNI_CHEL_LEARNING === "1";
const ACTIONS = process.env.SOMNI_ACTIONS === "1";
const ACTION_ALLOW = process.env.SOMNI_ACTION_TOOLS?.split(",").map((name) => name.trim()).filter(Boolean);
const MAX_ACTION_DEPTH = 4;
const configuredActionCalls = Number(process.env.SOMNI_ACTION_MAX_CALLS ?? 16);
const ACTION_MAX_CALLS = Number.isInteger(configuredActionCalls) ? Math.min(64, Math.max(1, configuredActionCalls)) : 16;
const DREAM_TOOLS = new Set(["somni_recall", "somni_search", "somni_image", "somni_dream"]);
const MAX_INDEX_QUERY_BYTES = 65_536;
const MAX_RECORD_BYTES = 8000;
const configuredToolBytes = Number(process.env.SOMNI_TOOL_RESULT_BYTES ?? 4096);
const TOOL_RESULT_BYTES = Number.isInteger(configuredToolBytes) ? Math.min(MAX_RECORD_BYTES, Math.max(512, configuredToolBytes)) : 4096;
const configuredDreamRecordBytes = Number(process.env.SOMNI_DREAM_RECORD_BYTES ?? 1500);
const DREAM_RECORD_BYTES = Number.isInteger(configuredDreamRecordBytes) ? Math.min(8192, Math.max(64, configuredDreamRecordBytes)) : 1500;
const configuredDreamRecords = Number(process.env.SOMNI_DREAM_RECORDS ?? 120);
const DREAM_RECORDS = Number.isInteger(configuredDreamRecords) ? Math.min(120, Math.max(1, configuredDreamRecords)) : 120;
const DIGEST_TOOLS = new Set((process.env.SOMNI_DIGEST_TOOLS ?? "stream_read").split(",").map((name) => name.trim()));
const MAX_IMAGE_INPUT = 4 * 1024 * 1024;
const MAX_RECORD_IMAGES = 4;
const IMAGE_BYTES = process.env.SOMNI_IMAGE_BYTES ?? "262144";
const IMAGE_MAX_EDGE = process.env.SOMNI_IMAGE_MAX_EDGE ?? "0";
const Evidence = Type.Array(Type.Object({
	ref: Type.String(), quote: Type.Optional(Type.String()), image_sha256: Type.Optional(Type.String({ pattern: "^[0-9a-f]{64}$" })),
}), { minItems: 1, maxItems: 16 });

type RecordContext = { tags?: string[]; run_label?: string; run_labels?: string[] };
type TranscriptRecord = RecordContext & {
	id: string; source: string; text: string; tool_ok?: boolean; images?: { sha256: string }[]; omitted_images?: number;
};
type Recent = { records: TranscriptRecord[]; attachments: { ref: string; sha256: string; image: ImageContent }[] };
type DreamResult = {
	id: string; learning_count?: number; action_count?: number; added_count: number; merged_count: number; rejected_count: number;
	rejected?: { index: number; reason: string }[];
};
type ActionWord = { name: string; description: string; params: Record<string, string>; uses: string[]; source: string };
type ActionStep = { call?: { tool: string; args: Record<string, unknown> }; result?: unknown };
type DreamRun = { reads: number; submissions: number; submitting: boolean; result?: DreamResult; error?: string };
const state = {
	dream: undefined as DreamRun | undefined, writable: false,
	backend: undefined as Backend | undefined,
	recordWarning: false, indexWarning: false,
	words: new Map<string, string>(), // registered action word -> source
	wordCalls: new Set<string>(), // tool call IDs of running action words
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

function utf8Prefix(text: string, maxBytes: number) {
	// UTF-8 uses at least one byte per UTF-16 code unit. Skip encoding the tail of large results.
	const bytes = Buffer.from(text.slice(0, maxBytes), "utf8");
	let end = Math.min(bytes.length, maxBytes);
	while (end > 0 && (bytes[end] & 0xc0) === 0x80) end--;
	return bytes.subarray(0, end).toString("utf8");
}

function clip(text: string, maxBytes: number) {
	const marker = " [truncated]";
	if (Buffer.byteLength(text) <= maxBytes) return text;
	return utf8Prefix(text, maxBytes - marker.length) + marker;
}

function canRecord() {
	return RECORD && state.writable && !state.dream;
}

function notify(ctx: ExtensionContext, message: string) {
	if (ctx.mode === "json" || ctx.mode === "print") console.error(message);
	else ctx.ui.notify(message, "warning");
}

async function record(source: "ordinary_observation" | "tool_result" | "agent_output", text: string, ctx: ExtensionContext,
		options: { tool_ok?: boolean; index_bytes?: number; query?: string; images?: ImageContent[]; omitted_images?: number } = {}) {
	if (!canRecord() || !text.trim()) return;
	try {
		const output = await getBackend().request<string>("record", {
			source, text: clip(text, MAX_RECORD_BYTES), ...options,
		});
		state.recordWarning = false;
		return output;
	} catch (error) {
		if (!state.recordWarning) {
			state.recordWarning = true;
			notify(ctx, `Somni could not record an event. ${(error as Error).message}`);
		}
	}
}

function toolRecord(name: string, input: unknown, content: (TextContent | ImageContent)[]) {
	const args = clip(JSON.stringify(input ?? {}), Math.min(1000, Math.floor(TOOL_RESULT_BYTES / 3)));
	const prefix = `TOOL ${name}(${args}) -> `;
	const texts = content.filter((part) => part.type === "text").map((part) => part.text);
	const byteCount = texts.reduce((total, part) => total + Buffer.byteLength(part), 0);
	const digestOnly = DIGEST_TOOLS.has(name);
	let body: string;
	if (byteCount && (digestOnly || Buffer.byteLength(prefix) + byteCount > TOOL_RESULT_BYTES)) {
		const hash = createHash("sha256");
		for (const part of texts) hash.update(part);
		const marker = `[body ${digestOnly ? "omitted" : "truncated"}: ${byteCount} UTF-8 bytes sha256=${hash.digest("hex")}]`;
		let remaining = Math.max(0, TOOL_RESULT_BYTES - Buffer.byteLength(prefix + marker) - 1);
		const preview: string[] = [];
		if (!digestOnly) {
			for (const part of texts) {
				if (!remaining) break;
				const piece = utf8Prefix(part, remaining);
				preview.push(piece);
				remaining -= Buffer.byteLength(piece);
				if (piece !== part) break;
			}
		}
		body = preview.length ? preview.join("") + " " + marker : marker;
	} else {
		body = texts.join("");
	}
	const images: ImageContent[] = [];
	let omitted = 0;
	const maxInput = Number(IMAGE_BYTES) && Number(IMAGE_MAX_EDGE) ? MAX_IMAGE_INPUT : Number(IMAGE_BYTES);
	for (const part of content) {
		if (part.type !== "image") continue;
		if (maxInput > 0 && images.length < MAX_RECORD_IMAGES && part.data.length <= Math.ceil(maxInput / 3) * 4) {
			images.push({ type: "image", data: part.data, mimeType: part.mimeType });
		} else {
			omitted++;
		}
	}
	return { text: clip(prefix + body, TOOL_RESULT_BYTES), options: { images, omitted_images: omitted } };
}

function contextLabel(record: RecordContext) {
	const labels = record.run_labels ?? (record.run_label ? [record.run_label] : []);
	const tags = record.tags ?? [];
	return `${labels.length ? ` [run: ${labels.join(", ")}]` : ""}${tags.length ? ` [tags: ${tags.join(", ")}]` : ""}`;
}

function describeRecord(record: TranscriptRecord, chars = record.text.length) {
	const images = (record.images ?? []).map(({ sha256 }) => ` [image_sha256=${sha256}]`).join("");
	const omitted = record.omitted_images ? ` [${record.omitted_images} image(s) omitted by storage limits or format]` : "";
	const excerpt = record.text.length > chars ? `${record.text.slice(0, chars)} [excerpt truncated]` : record.text;
	const status = record.tool_ok === undefined ? "" : record.tool_ok ? " [tool ok]" : " [tool error]";
	return `[${record.id}] (${record.source})${status}${contextLabel(record)} ${excerpt}${images}${omitted}`;
}

function summary(result: DreamResult) {
	const learning = result.learning_count ? ` ${result.learning_count} ledger revision(s) accepted.` : "";
	const actions = result.action_count ? ` ${result.action_count} action word change(s).` : "";
	return `Somni dream ${result.id}: ${result.added_count} added, ${result.rejected_count} rejected, ${result.merged_count} merged.${learning}${actions}`;
}

const PARAM_TYPES: Record<string, () => any> = {
	string: () => Type.String(), integer: () => Type.Integer(), number: () => Type.Number(), boolean: () => Type.Boolean(),
	array: () => Type.Array(Type.Any()), object: () => Type.Record(Type.String(), Type.Any()),
};

function resultValue(content: (TextContent | ImageContent)[]) {
	// Must match how toolRecord() writes the body, so recorded calls replay exactly.
	const text = content.filter((part) => part.type === "text").map((part) => part.text).join("");
	try {
		return JSON.parse(text) as unknown;
	} catch {
		return text;
	}
}

function asText(value: unknown) {
	return [{ type: "text" as const, text: typeof value === "string" ? value : JSON.stringify(value, null, 1) }];
}

export default function (pi: ExtensionAPI) {
	let lastCtx: ExtensionContext | undefined;
	async function finishDream(run: DreamRun) {
		if (state.dream !== run) return;
		try {
			if (RECORD && state.writable) await getBackend().request("phase", { phase: "open" });
		} finally {
			state.dream = undefined;
		}
		if (run.result?.action_count && lastCtx) await registerWords(lastCtx);
	}

	async function startDream(ctx: ExtensionCommandContext, load: () => Promise<string | (TextContent | ImageContent)[] | undefined>) {
		const headless = ctx.mode === "print" || ctx.mode === "json";
		let run: DreamRun | undefined;
		try {
			const backend = requireWriteAccess();
			if (state.dream || !ctx.isIdle()) throw new Error("Wait for the current Pi turn to settle first.");
			run = { submitting: false, reads: 0, submissions: 0 };
			state.dream = run;
			const content = await load();
			if (content === undefined) {
				state.dream = undefined;
				return;
			}
			await backend.request("phase", { phase: "closed" });
			// A triggered custom message starts the turn before waitForIdle checks Pi's state.
			pi.sendMessage({ customType: "somni-dream-input", content, display: false }, { triggerTurn: true });
			await ctx.waitForIdle();
			if (!run.result) throw new Error(run.error ?? "The dream ended without a committed pass. The model must call somni_dream.");
		} catch (error) {
			if (headless) process.exitCode = 1;
			notify(ctx, `Somni dream failed. ${(error as Error).message}`);
		} finally {
			if (run) await finishDream(run);
		}
	}

	function submitCommand(ctx: ExtensionCommandContext, load: () => Promise<string | (TextContent | ImageContent)[] | undefined>) {
		const task = startDream(ctx, load);
		if (ctx.mode === "json" || ctx.mode === "print") return task;
		// RPC clients get the handled reply first and can then wait for agent_settled.
		void task.catch((error: Error) => notify(ctx, `Somni dream cleanup failed. ${error.message}`));
	}

	/** Tools an action word may call: everything callable except Somni's own tools (and SOMNI_ACTION_TOOLS, if set). */
	function actionTargets() {
		return pi.getAllTools().filter((tool) => !tool.name.startsWith("somni_")
			&& !["hidden", "model-only"].includes((tool as { exposure?: string }).exposure ?? "direct")
			&& (!ACTION_ALLOW || ACTION_ALLOW.includes(tool.name) || state.words.has(tool.name)));
	}

	async function runWord(word: ActionWord, params: Record<string, unknown>, toolCallId: string,
			signal: AbortSignal | undefined, ctx: any) {
		if (toolCallId.split("/").length > MAX_ACTION_DEPTH) throw new Error(`Action words nest at most ${MAX_ACTION_DEPTH} deep.`);
		const history: { tool: string; args: Record<string, unknown>; result: unknown }[] = [];
		const calls: { tool: string; args: Record<string, unknown>; ok: boolean }[] = [];
		state.wordCalls.add(toolCallId);
		try {
			for (;;) {
				const step = await getBackend().request<ActionStep>("action_step", { name: word.name, params, history, max_calls: ACTION_MAX_CALLS });
				if (!step.call) {
					return { content: asText(JSON.stringify(step.result ?? null)), details: { word: word.name, calls } };
				}
				const { tool, args } = step.call;
				if (ACTION_ALLOW && !ACTION_ALLOW.includes(tool) && !state.words.has(tool)) {
					throw new Error(`${word.name} may not call ${tool} (SOMNI_ACTION_TOOLS).`);
				}
				// Resolves to { toolCall, result: { content, details }, isError, durationMs }; never rejects.
				const outcome = await ctx.executeTool(tool, args, { signal });
				const value = resultValue(outcome.result?.content ?? []);
				calls.push({ tool, args, ok: !outcome.isError });
				if (outcome.isError) {
					const done = calls.slice(0, -1).map((call) => call.tool).join(", ") || "none";
					throw new Error(`${word.name} stopped at call ${calls.length} (${tool}): ` +
						`${clip(typeof value === "string" ? value : JSON.stringify(value), 600)}. Earlier calls already ran: ${done}.`);
				}
				history.push({ tool, args, result: value });
			}
		} finally {
			state.wordCalls.delete(toolCallId);
		}
	}

	/** Register each stored word as a Pi tool once the tools it calls exist (MCP tools may connect later). */
	async function registerWords(ctx: ExtensionContext) {
		if (!ACTIONS || !state.backend) return;
		let words: ActionWord[];
		try {
			words = await getBackend().request<ActionWord[]>("action_list");
		} catch (error) {
			notify(ctx, `Somni could not load its action words. ${(error as Error).message}`);
			return;
		}
		const others = new Set(pi.getAllTools().map((tool) => tool.name).filter((name) => !state.words.has(name)));
		const pending = new Map(words.map((word) => [word.name, word]));
		for (let changed = true; changed;) {
			changed = false;
			for (const [name, word] of pending) {
				if (others.has(name)) {
					pending.delete(name);
					notify(ctx, `Somni action word ${name} is shadowed by a tool of the same name.`);
					continue;
				}
				const ready = word.uses.every((use) => use.startsWith("mcp__") || others.has(use) || state.words.has(use));
				if (!ready) continue;
				pending.delete(name);
				changed = true;
				if (state.words.get(name) === word.source) continue;
				const description = `${word.description} (Learned action word: calls ${word.uses.join(", ")}. ` +
					"Returns JSON. Its source is in Somni's actions folder.)";
				pi.registerTool({
					name, label: name, description,
					parameters: Type.Object(Object.fromEntries(Object.entries(word.params).map(([key, kind]) => [key, PARAM_TYPES[kind]()]))),
					reachesTools: () => word.uses,
					async execute(toolCallId: string, params: any, signal: AbortSignal | undefined, _onUpdate: unknown, toolCtx: any) {
						return runWord(word, params, toolCallId, signal, toolCtx);
					},
				} as any);
				state.words.set(name, word.source);
				if (!pi.getActiveTools().includes(name)) pi.setActiveTools([...pi.getActiveTools(), name]);
			}
		}
	}

	function actionGuide(words: ActionWord[]) {
		const tools = actionTargets().filter((tool) => !state.words.has(tool.name)).map((tool) =>
			`- ${tool.name}: ${clip((tool.description ?? "").replace(/\s+/g, " "), 160)} params ${clip(JSON.stringify(tool.parameters ?? {}), 300)}`);
		const lexicon = words.map((word) => `### ${word.name}\n${word.source}`).join("\n\n") || "(none yet)";
		return "\n\nAction words. When the transcript shows the same sequence of tool calls recurring, you may capture it as an " +
			"action word: one Python function the agent can then call as a single tool. Submit " +
			'{"op":"define_action","source":"<python>","tests":[{"params":{...},"refs":["tXXXXXX",...],"result":<optional>}]}. ' +
			"The source is one `def name(param: str|int|float|bool|list|dict, ...)` whose body starts with a one-line " +
			"docstring. Call tools as rpc.TOOL(key=value) or rpc.TOOL({...}) (rpc.pi.TOOL and rpc.mcp.SERVER.TOOL also work); " +
			"a call returns the tool's parsed JSON result. Allowed: assignments, if, for over lists or range, comprehensions, " +
			"arithmetic, dict/list indexing, .get/.items/.keys/.values/.lower/.split and similar, and abs min max round int " +
			"float str len bool sorted sum any all range divmod. No imports, while, try, lambda, classes or I/O. " +
			"Each test replays recorded calls: given params, the word must make exactly the calls in refs (same tools, same " +
			"arguments, in order; each ref a successful tool_result) and then return. Together the tests must exercise every " +
			"tool the word calls; add a test with fewer refs for each branch that stops early. Generalize with parameters, not " +
			"constants copied from one episode. Use revise_action {source, tests?} to repair a word and retire_action " +
			`{name, reason} to remove one.\n\nCallable tools:\n${tools.join("\n")}\n\nCurrent action words:\n${lexicon}\n`;
	}

	pi.registerTool({
		name: "somni_recall",
		label: "Somni recall",
		description: "Recall the wiki pages and memory claims most relevant to a query (pages cite claims; claims cite transcript evidence).",
		parameters: Type.Object({ query: Type.String(), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })) }),
		async execute(_id: string, params: { query: string; limit?: number }) {
			const hits = await getBackend().request<any[]>("recall", { query: params.query.slice(0, 2000), limit: params.limit ?? 5 });
			const lines = hits.slice(0, params.limit ?? 5).map((h) => {
				if (h.kind === "rule") return `Rule ${h.id} (${h.support} supporting transitions): ${h.text}\n${JSON.stringify({ when: h.when, then: h.then })}`;
				if (h.slug) return `# ${h.title} (page ${h.slug})\n${h.body}`;
				const lineage = h.merged_from ? `; merged from ${h.merged_from.join(", ")}` : "";
				return `${h.id} [${h.status}; ${h.evidence.length} cited${lineage}]${contextLabel(h)} ${h.text}`;
			});
			return { content: asText(lines.join("\n\n") || "no matching memory"), details: hits };
		},
	});
	pi.registerTool({
		name: "somni_search",
		label: "Somni search",
		description: "Search your transcript text, run labels, and tags for an exact phrase. Use it to find evidence IDs and image digests.",
		parameters: Type.Object({ query: Type.String(), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 50 })) }),
		async execute(_id: string, params: { query: string; limit?: number }) {
			const out = await getBackend().request<TranscriptRecord[]>("search", { query: params.query.slice(0, 256), limit: params.limit ?? 10 });
			return { content: asText(out.map((r) => describeRecord(r, 600)).join("\n") || "no matches"), details: out };
		},
	});
	pi.registerTool({
		name: "somni_image",
		label: "Somni image",
		description: "View a screenshot or other stored image by its transcript record ID and SHA-256 from somni_search.",
		parameters: Type.Object({ ref: Type.String({ pattern: "^t[0-9]{6}$" }), sha256: Type.String({ pattern: "^[0-9a-f]{64}$" }) }),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			if (!ctx.model?.input.includes("image")) throw new Error("Viewing Somni images requires a vision-capable Pi model.");
			const image = await getBackend().request<ImageContent>("image", { ...params, during_dream: Boolean(state.dream) });
			return { content: [...asText(`[${params.ref}] image_sha256=${params.sha256}`), image], details: params };
		},
	});
	pi.registerTool({
		name: "somni_note",
		label: "Somni note",
		description:
			"Add a durable memory claim. Cite transcript IDs as evidence; 'verified' requires an exact quote " +
			"or the image_sha256 of a stored image from each record. Use 'uncertain' for interpretations. Salience 1-5. " +
			"For longer claims, an optional one-line gist of up to 160 UTF-8 bytes appears in the memory index.",
		parameters: Type.Object({
			text: Type.String(),
			gist: Type.Optional(Type.String({ maxLength: 160 })),
			status: Type.Union([Type.Literal("verified"), Type.Literal("uncertain")]),
			salience: Type.Optional(Type.Integer({ minimum: 1, maximum: 5 })),
			evidence: Evidence,
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
			'operations. A merge takes {"op":"merge","claim_ids":["n000001","n000002"],"text":"combined guidance"} and inherits their evidence.' +
			(ACTIONS ? " With action words enabled, define_action/revise_action/retire_action operations are also accepted." : "") +
			" Rejected operations are listed with reasons; fix them and resubmit if attempts remain.",
		parameters: Type.Object({ proposal_json: Type.String() }),
		async execute(_id: string, params: { proposal_json: string }) {
			const backend = requireWriteAccess();
			const run = state.dream;
			if (!run) throw new Error("dreaming is only allowed during /somni-dream or /somni-wiki");
			if (run.result || run.submitting) throw new Error("Only one dream pass can be committed per command.");
			run.submitting = true;
			try {
				const out = await backend.request<DreamResult>("dream", {
					proposal_json: params.proposal_json,
					...(ACTIONS ? {
						tools: pi.getAllTools().map((tool) => tool.name).filter((name) => !state.words.has(name)),
						action_allow: ACTION_ALLOW ?? null,
					} : {}),
				});
				run.result = out;
				return { content: asText(out), details: out };
			} catch (error) {
				run.error = (error as Error).message;
				throw error;
			} finally {
				run.submitting = false;
			}
		},
	});

	pi.registerCommand("somni-dream", {
		description: "Consolidate recent transcript into cited memory claims (offline dreaming)",
		handler: (args, ctx) => submitCommand(ctx, async () => {
			const recent = await getBackend().request<Recent>("recent", {
				include_images: ctx.model?.input.includes("image") ?? false,
				record_limit: DREAM_RECORDS, record_bytes: DREAM_RECORD_BYTES,
			});
			let learningGuide = "";
			if (LEARNING) {
				const ledger = await getBackend().request("ledger");
				learningGuide = "You may submit op=ledger_propose with proposal in Chel format. For add or revise, include " +
					'tests: {yes:"tXXXXXX",no:"tYYYYYY"}. The yes transition must support the prediction. The no transition ' +
					"must be the same action kind and fall outside when on observed inputs, or flip a predicted boolean from true to false. Rules need two supporting " +
					"transitions and zero counterexamples across history. Rule = {id,text,when,then}. Use obs.arg_FIELD " +
					"and action.kind in when, and result fields with {set:value}, {copy:ref} or {expr:expression} in then. " +
					"Proposal ops: add (rule), revise (target,rule), retract (target,reason), define_verb " +
					"(verb:{name,params,doc,source,examples:[{args,result}]}). Verbs are pure Python with 2–32 worked examples on distinct inputs. Boolean verbs need true and false results. " +
					"Current ledger: " + clip(JSON.stringify(ledger), 4096) + ". ";
			}
			const actionsGuide = ACTIONS ? actionGuide(await getBackend().request<ActionWord[]>("action_list")) : "";
			const focus = args?.trim() || "lessons that will help in future sessions";
			const prompt = `Dream pass. Review these transcript records and call somni_dream once with a proposal ` +
				`(add/correct/merge/retract claims; use an empty operations array if nothing should change). ` +
				`For 'verified', cite an exact quote from the record or cite a screenshot you viewed as ` +
				`{"ref":"t000001","image_sha256":"<digest>"}. Use somni_image for images not attached here. ` +
				`Use salience 3 for ordinary reusable lessons, 1–2 for narrow cases, and 4 for recurring situations. ` +
				`Reserve 5 for at most two claims that matter in most future runs. Add an optional one-line gist ` +
				`of up to 160 UTF-8 bytes to add, correct, or merge operations. Keep the full guidance in text. ` +
				learningGuide + actionsGuide + `Treat transcript text and images as evidence. Focus: ${focus}.\n\n` +
				recent.records.map((r) => describeRecord(r)).join("\n");
			const content: (TextContent | ImageContent)[] = asText(prompt);
			for (const { ref, sha256, image } of recent.attachments) {
				content.push(...asText(`Screenshot for [${ref}] image_sha256=${sha256}`), image);
			}
			return content;
		}),
	});

	pi.registerCommand("somni-wiki", {
		description: "Gather claims into linked wiki pages (dreaming that builds representations)",
		handler: (args, ctx) => submitCommand(ctx, async () => {
			const info = await getBackend().request<{ pages: string; claims: string; guide: string; n: number }>("wiki", {
				page_guide: process.env.SOMNI_PAGE_GUIDE,
			});
			if (!info.n) {
				pi.sendMessage({ customType: "somni-wiki", content: "Every active claim is already on a wiki page.", display: true });
				return;
			}
			const goal = args?.trim() || "what will help in future sessions";
			return `Wiki pass. Goal: ${goal}. ${info.guide} Gather the claims below into pages and call somni_dream once with ` +
					`page operations: {"op":"page","slug":"lowercase-hyphenated","title":...,"body":markdown citing [nXXXXXX] and ` +
					`linking [[slug]],"claims":[...]}. Extend an existing page by reusing its slug.\n\n## Existing pages\n${info.pages}\n\n## Claims not yet on a page\n${info.claims}`;
		}),
	});

	pi.on("agent_settled", async () => {
		if (state.dream) await finishDream(state.dream);
	});

	pi.on("session_start", async (_event, ctx) => {
		const workspace = process.env.SOMNI_WORKSPACE ?? join(ctx.cwd, ".pi", "somni");
		state.dream = undefined;
		state.writable = state.recordWarning = state.indexWarning = false;
		state.wordCalls.clear();
		mkdirSync(workspace, { recursive: true });
		// Isolate imports and avoid bytecode writes beside the extension.
		const args = ["-I", "-B", STORE_PY, "--workspace", workspace, "--subject-id", SUBJECT, "serve",
			"--image-bytes", IMAGE_BYTES, "--image-max-edge", IMAGE_MAX_EDGE];
		if (process.env.SOMNI_RUN_LABEL !== undefined) args.push(`--run-label=${process.env.SOMNI_RUN_LABEL}`);
		if (process.env.SOMNI_TAGS !== undefined) args.push(`--tags=${process.env.SOMNI_TAGS}`);
		if (RECORD) args.push("--record");
		if (LEARNING) args.push("--learning");
		if (ACTIONS) args.push("--actions");
		const backend = new Backend(PYTHON, args, (error) => {
			if (state.backend !== backend) return;
			state.backend = undefined;
			state.writable = false;
			state.dream = undefined;
			notify(ctx, `Somni backend stopped. ${error.message}`);
		});
		try {
			const { writable, action_sync: synced } = await backend.ready as { writable: boolean; action_sync?: any };
			state.backend = backend;
			state.writable = writable;
			lastCtx = ctx;
			if (synced?.error) notify(ctx, `Somni could not record your action word edits. ${synced.error}`);
			else if (synced) {
				const rejected = (synced.rejected ?? []).map((r: { reason: string }) => r.reason).join("; ");
				notify(ctx, `Somni recorded ${synced.action_count ?? 0} action word edit(s) as pass ${synced.id}.` +
					(rejected ? ` Rejected: ${rejected}` : ""));
			}
			await registerWords(ctx);
			if (RECORD && !writable) {
				notify(ctx, "Somni opened without recording because another Pi session is recording in this Somni workspace.");
			}
		} catch (error) {
			await backend.close();
			notify(ctx, `Somni could not open its memory store. ${(error as Error).message}`);
		}
	});

	pi.on("session_shutdown", async () => {
		const backend = state.backend;
		state.backend = undefined;
		state.writable = false;
		state.dream = undefined;
		await backend?.close();
	});

	pi.on("before_agent_start", async (event, ctx) => {
		if (!state.backend || state.dream) return undefined;
		if (ACTIONS) await registerWords(ctx);
		const prompt = event.messageType === "custom" ? undefined : event.prompt;
		const options = { index_bytes: INJECT_BYTES, query: prompt ? utf8Prefix(prompt, MAX_INDEX_QUERY_BYTES) : undefined };
		let index = prompt ? await record("ordinary_observation", `USER: ${prompt}`, ctx, options) : undefined;
		try {
			index ??= await getBackend().request<string>("inject", options);
			state.indexWarning = false;
		} catch (error) {
			if (!state.indexWarning) {
				state.indexWarning = true;
				notify(ctx, `Somni could not load its memory index. ${(error as Error).message}`);
			}
			return undefined;
		}
		if (!/^-(?: n\d{6} | rule | page `)/m.test(index)) return undefined;
		return { message: { customType: "somni-index", content: index, display: false } };
	});

	pi.on("tool_call", (event) => {
		const run = state.dream;
		if (!run) return;
		if (!DREAM_TOOLS.has(event.toolName) || run.result || run.submitting) {
			return { block: true, reason: "Offline dreams may only inspect Somni evidence and submit one pass." };
		}
		if (event.toolName === "somni_dream") {
			if (++run.submissions > 3) return { block: true, reason: "Dream submission attempt limit reached." };
		} else if (++run.reads > 16) {
			return { block: true, reason: "Dream evidence read limit reached. Submit the pass." };
		}
	});

	pi.on("tool_result", async (event, ctx) => {
		// Calls an action word makes are recorded too: they are the evidence for revising or composing words.
		if (event.parentToolCallId && !state.wordCalls.has(event.parentToolCallId)) return;
		if (!canRecord() || event.toolName.startsWith("somni_")) return;
		const { text, options } = toolRecord(event.toolName, event.input, event.content);
		await record("tool_result", text, ctx, { ...options, tool_ok: event.isError === false });
	});

	pi.on("message_end", async (event, ctx) => {
		const { message } = event;
		if (message?.role !== "assistant") return;
		const result = state.dream?.result;
		if (result && message.stopReason === "stop" && !message.content.some((part) => part.type === "toolCall")) {
			return { message: { ...message, content: [...message.content, ...asText(summary(result))] } };
		}
		if (!canRecord()) return;
		for (const part of message.content) {
			if (part.type === "text" && part.text.trim()) await record("agent_output", `AGENT: ${part.text}`, ctx);
		}
	});
}
