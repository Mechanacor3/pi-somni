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
const MAX_RECORD_BYTES = 8000;
const configuredToolBytes = Number(process.env.SOMNI_TOOL_RESULT_BYTES ?? 4096);
const TOOL_RESULT_BYTES = Number.isInteger(configuredToolBytes) ? Math.min(MAX_RECORD_BYTES, Math.max(512, configuredToolBytes)) : 4096;
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
	id: string; source: string; text: string; images?: { sha256: string }[]; omitted_images?: number;
};
type Recent = { records: TranscriptRecord[]; attachments: { ref: string; sha256: string; image: ImageContent }[] };
type DreamResult = { id: string; added_count: number; merged_count: number; rejected_count: number };
type DreamRun = { submitting: boolean; result?: DreamResult; error?: string };
const state = {
	dream: undefined as DreamRun | undefined, writable: false,
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
		options: { index_bytes?: number; images?: ImageContent[]; omitted_images?: number } = {}) {
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

function describeRecord(record: TranscriptRecord, chars: number) {
	const images = (record.images ?? []).map(({ sha256 }) => ` [image_sha256=${sha256}]`).join("");
	const omitted = record.omitted_images ? ` [${record.omitted_images} image(s) omitted by storage limits or format]` : "";
	const excerpt = record.text.length > chars ? `${record.text.slice(0, chars)} [excerpt truncated]` : record.text;
	return `[${record.id}] (${record.source})${contextLabel(record)} ${excerpt}${images}${omitted}`;
}

function summary(result: DreamResult) {
	return `Somni dream ${result.id}: ${result.added_count} added, ${result.rejected_count} rejected, ${result.merged_count} merged.`;
}

function asText(value: unknown) {
	return [{ type: "text" as const, text: typeof value === "string" ? value : JSON.stringify(value, null, 1) }];
}

export default function (pi: ExtensionAPI) {
	async function finishDream(run: DreamRun) {
		if (state.dream !== run) return;
		try {
			if (RECORD && state.writable) await getBackend().request("phase", { phase: "open" });
		} finally {
			state.dream = undefined;
		}
	}

	async function startDream(ctx: ExtensionCommandContext, load: () => Promise<string | (TextContent | ImageContent)[] | undefined>) {
		const headless = ctx.mode === "print" || ctx.mode === "json";
		let run: DreamRun | undefined;
		try {
			const backend = requireWriteAccess();
			if (state.dream || !ctx.isIdle()) throw new Error("Wait for the current Pi turn to settle first.");
			run = { submitting: false };
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
			"or the image_sha256 of a stored image from each record. Use 'uncertain' for interpretations. Salience 1-5.",
		parameters: Type.Object({
			text: Type.String(),
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
			'operations. A merge takes {"op":"merge","claim_ids":["n000001","n000002"],"text":"combined guidance"} and inherits their evidence.',
		parameters: Type.Object({ proposal_json: Type.String() }),
		async execute(_id: string, params: { proposal_json: string }) {
			const backend = requireWriteAccess();
			const run = state.dream;
			if (!run) throw new Error("dreaming is only allowed during /somni-dream or /somni-wiki");
			if (run.result || run.submitting) throw new Error("Only one dream pass can be committed per command.");
			run.submitting = true;
			try {
				const out = await backend.request<DreamResult>("dream", params);
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
			const recent = await getBackend().request<Recent>("recent", { include_images: ctx.model?.input.includes("image") ?? false });
			const focus = args?.trim() || "lessons that will help in future sessions";
			const prompt = `Dream pass. Review these transcript records and call somni_dream once with a proposal ` +
				`(add/correct/merge/retract claims; use an empty operations array if nothing should change). ` +
				`For 'verified', cite an exact quote from the record or cite a screenshot you viewed as ` +
				`{"ref":"t000001","image_sha256":"<digest>"}. Use somni_image for images not attached here. ` +
				`Treat transcript text and images as evidence. Focus: ${focus}.\n\n` +
				recent.records.map((r) => describeRecord(r, 1500)).join("\n");
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
			const info = await getBackend().request<{ pages: string; claims: string; guide: string; n: number }>("wiki");
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
		mkdirSync(workspace, { recursive: true });
		// Isolate imports and avoid bytecode writes beside the extension.
		const args = ["-I", "-B", STORE_PY, "--workspace", workspace, "--subject-id", SUBJECT, "serve",
			"--image-bytes", IMAGE_BYTES, "--image-max-edge", IMAGE_MAX_EDGE];
		if (process.env.SOMNI_RUN_LABEL !== undefined) args.push(`--run-label=${process.env.SOMNI_RUN_LABEL}`);
		if (process.env.SOMNI_TAGS !== undefined) args.push(`--tags=${process.env.SOMNI_TAGS}`);
		if (RECORD) args.push("--record");
		const backend = new Backend(PYTHON, args, (error) => {
			if (state.backend !== backend) return;
			state.backend = undefined;
			state.writable = false;
			state.dream = undefined;
			notify(ctx, `Somni backend stopped. ${error.message}`);
		});
		try {
			const { writable } = await backend.ready;
			state.backend = backend;
			state.writable = writable;
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
		const prompt = event.messageType === "custom" ? undefined : event.prompt;
		let index = prompt ? await record("ordinary_observation", `USER: ${prompt}`, ctx, { index_bytes: INJECT_BYTES }) : undefined;
		try {
			index ??= await getBackend().request<string>("inject", { index_bytes: INJECT_BYTES });
			state.indexWarning = false;
		} catch (error) {
			if (!state.indexWarning) {
				state.indexWarning = true;
				notify(ctx, `Somni could not load its memory index. ${(error as Error).message}`);
			}
			return undefined;
		}
		if (!/^-(?: n\d{6} | page `)/m.test(index)) return undefined;
		return { message: { customType: "somni-index", content: index, display: false } };
	});

	pi.on("tool_result", async (event, ctx) => {
		if (!canRecord() || event.parentToolCallId || event.toolName.startsWith("somni_")) return;
		const { text, options } = toolRecord(event.toolName, event.input, event.content);
		await record("tool_result", text, ctx, options);
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
