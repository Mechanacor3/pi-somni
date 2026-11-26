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
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Backend } from "./backend.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const CONFIG_PY = resolve(HERE, "../somni/config.py");

/**
 * somni.conf. Settings from SOMNI_CONFIG, or from ~/.config/somni/somni.conf and <cwd>/.pi/somni.conf, fill in
 * each SOMNI_* variable that the environment leaves unset; everything below reads the variables, and the backend
 * and daydream workers inherit them. somni/config.py is the one parser and schema, shared with `somni doctor`.
 */
const CONFIG = (() => {
	const applied = new Set<string>();
	const problems: string[] = [];
	const home = process.env.HOME ?? process.env.USERPROFILE ?? "";
	const user = join(process.env.XDG_CONFIG_HOME || join(home, ".config"), "somni", "somni.conf");
	// No file to read: SOMNI_CONFIG=none, or a daydream worker, which inherits its session's resolved settings.
	// Skipping the spawn matters under memory pressure, where a Python start can take seconds.
	if (process.env.SOMNI_CONFIG === "none" || process.env.SOMNI_DAYDREAM === "1") return { applied, problems };
	if (process.env.SOMNI_CONFIG === undefined && !existsSync(user) && !existsSync(join(process.cwd(), ".pi", "somni.conf"))) {
		return { applied, problems };
	}
	const out = spawnSync(process.env.SOMNI_PYTHON ?? "python3", ["-I", "-B", CONFIG_PY, "resolve", "--cwd", process.cwd(), "--json"],
		{ encoding: "utf8", timeout: 15_000 });
	try {
		if (out.error || out.status !== 0) throw new Error(out.error?.message || out.stderr.trim() || `exit ${out.status}`);
		const resolved = JSON.parse(out.stdout) as { env: Record<string, string>; errors: { file?: string; line?: number; message: string }[] };
		for (const [name, value] of Object.entries(resolved.env)) {
			if (process.env[name] === undefined) {
				process.env[name] = value;
				applied.add(name);
			}
		}
		for (const e of resolved.errors) problems.push(`${e.file ? basename(e.file) : "config"}${e.line ? `:${e.line}` : ""}: ${e.message}`);
	} catch (error) {
		problems.push(`could not read the config files: ${(error as Error).message}`);
	}
	return { applied, problems };
})();

/**
 * The skill's "Using a word" section. Pi lists a --skill skill only when the read tool is active, so a session
 * without read gets this text in its memory index instead (once per session, with the first index that lists a word).
 */
const WORD_GUIDE = (() => {
	try {
		const skill = readFileSync(resolve(HERE, "../skills/somni-action-words/SKILL.md"), "utf8");
		const section = skill.match(/^## Using a word\n([\s\S]*?)(?=^## )/m)?.[1]?.trim();
		if (section) return `## Using a learned action word\n${section}`;
	} catch {
		// Fall through to the short version if the skill file is not shipped beside the extension.
	}
	return "## Using a learned action word\nCall it like any tool; it returns JSON. If one of its calls fails, the word " +
		"stops and earlier calls stay done. Check the effect before you rely on its result.";
})();
const STORE_PY = process.env.SOMNI_STORE ?? resolve(HERE, "../somni/store.py");
const PYTHON = process.env.SOMNI_PYTHON ?? "python3";
const SUBJECT = process.env.SOMNI_SUBJECT ?? "pi";
const RECORD = process.env.SOMNI_RECORD !== "0" && process.env.SOMNI_DAYDREAM !== "1";
const configuredBytes = Number(process.env.SOMNI_INJECT_BYTES ?? 4096);
const INJECT_BYTES = Number.isInteger(configuredBytes) ? Math.min(Math.max(configuredBytes, 256), 8192) : 4096;
const ACTIONS = process.env.SOMNI_ACTIONS === "1";
// Tools /somni-deepsleep may call for real, directly or from a draft word; everything it calls is recorded.
const DEEPSLEEP_TOOLS = (process.env.SOMNI_DEEPSLEEP_TOOLS ?? "").split(",").map((name) => name.trim()).filter(Boolean);
const ACTION_ALLOW = process.env.SOMNI_ACTION_TOOLS?.split(",").map((name) => name.trim()).filter(Boolean);
const MAX_ACTION_DEPTH = 4;
const MAX_WORD_IMAGES = 2; // images a word passes on from its final call
const MAX_TIMED_CALLS = 64; // nested-call timings kept on a word's result record
const configuredActionCalls = Number(process.env.SOMNI_ACTION_MAX_CALLS ?? 16);
const ACTION_MAX_CALLS = Number.isInteger(configuredActionCalls) ? Math.min(64, Math.max(1, configuredActionCalls)) : 16;
// Daydreams: live dream passes run by a child Pi while this session keeps recording.
const DAYDREAM_CHILD = process.env.SOMNI_DAYDREAM === "1"; // this process is a daydream worker
// A forked daydream worker: the parent session's ID, which stays the provider's prompt cache key.
const FORK_OF = DAYDREAM_CHILD ? process.env.SOMNI_DAYDREAM_FORK_OF || undefined : undefined;
const DAYDREAM_FORK = process.env.SOMNI_DAYDREAM_FORK === "1";
const envNumber = (name: string, fallback: number, min: number, max: number) => {
	const value = Number(process.env[name] ?? fallback);
	return Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
};
const DAYDREAM_TURNS = Math.floor(envNumber("SOMNI_DAYDREAM_EVERY_TURNS", 0, 0, 10_000));
const DAYDREAM_MINUTES = envNumber("SOMNI_DAYDREAM_EVERY_MINUTES", 0, 0, 1440);
const DAYDREAM_PER_HOUR = Math.floor(envNumber("SOMNI_DAYDREAM_PER_HOUR", 4, 1, 60));
const DAYDREAM_TIMEOUT_MS = envNumber("SOMNI_DAYDREAM_TIMEOUT_SECONDS", 300, 10, 7200) * 1000;
// A daydream's record listing stays small: newest first into this many bytes, tool results as one-line stubs.
const DAYDREAM_PROMPT_BYTES = Math.floor(envNumber("SOMNI_DAYDREAM_PROMPT_BYTES", 16384, 2048, 131072));
const STUB_CHARS = 240;
const DAYDREAMS = !DAYDREAM_CHILD && (DAYDREAM_TURNS > 0 || DAYDREAM_MINUTES > 0);
const DAYDREAM_FOCUS = process.env.SOMNI_DAYDREAM_FOCUS?.trim() || "what will help later in this session and in future " +
	"ones, from the newest records: what the user told you (preferences, corrections, and facts about them and their " +
	"work), facts the work turned up and where they came from, and steps or checks that worked or failed. An answer the " +
	"agent could not find is not a fact, so never turn a failed lookup into a claim. Keep the pass small; leave wiki " +
	"pages to the night dream";
const AUTH_ERROR = /\b(401|403)\b|unauthori[sz]ed|authenticat|token expired|credential|not logged in|api key/i;
const DREAM_TOOLS = new Set(["somni_recall", "somni_search", "somni_image", "somni_dream", "somni_action_try"]);
// Dream budgets. Every Somni tool result in a dream ends with what is left of them.
const DREAM_MAX_READS = Math.floor(Math.min(envNumber("SOMNI_DREAM_MAX_READS", 16, 1, 64),
	DAYDREAM_CHILD ? envNumber("SOMNI_DAYDREAM_MAX_READS", 6, 1, 64) : 64));
const DREAM_MAX_IMAGES = Math.floor(envNumber("SOMNI_DREAM_MAX_IMAGES", 16, 0, 64));
const DREAM_MAX_SUBMISSIONS = Math.floor(envNumber("SOMNI_DREAM_MAX_SUBMISSIONS", 3, 1, 10));
const LENIENT_IMAGE_EVIDENCE = process.env.SOMNI_LENIENT_IMAGE_EVIDENCE === "1";
// A dream's evidence reads are kept until a pass commits; a retry on the same records within this window gets them back.
const DREAM_CACHE_SECONDS = Math.floor(envNumber("SOMNI_DREAM_CACHE_MINUTES", 60, 0, 10_080) * 60);
const MAX_SEARCH_LIMIT = 50;
const MAX_RECALL_LIMIT = 20;
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
	ref: Type.String(), quote: Type.Optional(Type.String()), image_sha256: Type.Optional(Type.String({ minLength: 1, maxLength: 64 })),
}), { minItems: 1, maxItems: 16 });

type RecordContext = { tags?: string[]; run_label?: string; run_labels?: string[] };
type OutcomeReport = { ref: string; text: string; claimed_success: boolean; overclaim: boolean };
type TranscriptRecord = RecordContext & {
	id: string; source: string; text: string; at?: string; tool_ok?: boolean; images?: { sha256: string }[];
	omitted_images?: number; outcome?: { by?: string; verdict?: string }; report?: OutcomeReport;
};
type Strength = { level: "fresh" | "fading" | "faint"; activation: number; last_used?: string; first_seen?: string };
type PastVersion = { id: string; text: string; until_ref?: string; until?: string | null };
type RelatedClaim = { id: string; text: string; status: string; refs: string[]; strength: Strength; history?: PastVersion[];
	standing?: boolean };
type Recent = { records: TranscriptRecord[]; attachments: { ref: string; sha256: string; image: ImageContent }[];
	reviewed_through?: string; claims?: RelatedClaim[] };

/** How a dream should treat a record that disagrees with a claim. Kept in step with TIME_GUIDE in somni/dream.py. */
const TIME_GUIDE = "Records are dated (the --- day --- lines). When a record disagrees with a claim, decide what happened. " +
	"If the world changed, the claim was true until then: use update ({\"op\":\"update\",\"claim_id\":...} with the " +
	"newer evidence), which keeps the old claim as history. If the claim was wrong all along, use correct or retract. " +
	"Never leave two active claims that disagree; when both were true at different times, update the older one and say " +
	"since when in the text. Age is not doubt: never write 'verify before relying', 'may be stale' or 'check the files " +
	"first' into a claim that nothing has contradicted. Date it instead ('as of 2026-01-12'); it stays the best " +
	"information until a newer record changes it. ";

/** Lasting preferences of the user's, listed first in every index. Kept in step with STANDING_GUIDE in somni/dream.py. */
const STANDING_GUIDE = "Standing preferences: when the user says how they want work done in general (style, formatting, " +
	"tools, conventions, how to report back; for example \"I like 2-space indents\" or \"always run the tests before you " +
	"say it's done\"), or corrects the agent the same way more than once, add the claim with \"standing\": true, phrased " +
	"as an instruction and citing the USER record where they said it. Standing claims head every session's index " +
	"whatever the task, so the user needn't repeat them. Keep them for lasting preferences: not one-off requests (\"use " +
	"4 spaces in this file\"), not facts about the project, and nothing about Somni itself. At most 12 can be active, so " +
	"update or merge one rather than adding a near-duplicate. When the user changes a preference, update it (it stays " +
	"standing); when it no longer applies, update it with \"standing\": false or retract it. ";

/** Which store a lesson belongs in, when a personal store sits beside the project's. */
const SCOPE_GUIDE = "Two memories: this project's, and the user's personal memory, shared by all their projects " +
	"(its claims and records have IDs starting p:). Add \"scope\": \"personal\" to an add for a lesson about the user " +
	"that holds in any project: their preferences, how they like to work and be told things, facts about them. Lessons " +
	"about this project, its code, tools and people stay here (leave scope out). Cite this project's records as usual; " +
	"Somni copies them into the personal memory. To correct, update, merge or retract a personal claim, use its p: ID. ";

/** Keeps Somni's own machinery out of daytime memory. Kept in step with SELF_GUIDE in somni/dream.py. */
const SELF_GUIDE = "Somni's own machinery is not the user's work. Don't write claims about how dreams, action words or " +
	"rpc.TOOL calls work: every dream gets those instructions fresh, and in the daytime they confuse the agent about " +
	"which tools it has. If the user was configuring or building Somni, keep what they decided (a setting, a " +
	"preference) and say in the claim that it is about the Somni memory tool. In a claim's text, never phrase rpc.* " +
	"as a way to call a tool. This is about claims only: an action word's source still calls tools as rpc.TOOL(...), " +
	"and a word for the user's own recurring work is worth defining even if it was first sketched while talking " +
	"about Somni. ";

/** How a dream should weigh outcomes from outside the agent. Kept in step with OUTCOME_GUIDE in somni/dream.py. */
const OUTCOME_GUIDE = "OUTCOME records come from outside the agent, after the fact: a grader, CI or tests, a reviewer, the " +
	"user. About whether something worked they outrank the agent's own reports and its counters; when they disagree, " +
	"the outcome is right, and a claim about the result should cite the outcome (an exact quote from it can be " +
	"verified). Each outcome shows the agent's report it judges. When the agent said it was done and the outcome says " +
	"it failed, that pair is the most useful thing to learn from: write what the agent should have checked before " +
	"reporting success, citing both records. ";

/** Lines for `records` with a `--- YYYY-MM-DD ---` header wherever the day changes. */
function withDays(records: TranscriptRecord[], render: (record: TranscriptRecord) => string) {
	const lines: string[] = [];
	let day: string | undefined;
	for (const record of records) {
		const today = record.at?.slice(0, 10);
		if (today && today !== day) lines.push(`--- ${(day = today)} ---`);
		lines.push(render(record));
	}
	return lines;
}

/**
 * One line about a claim's age and history, or "" for a fresh claim with none. The agent sees "as of <date>", and
 * "faint" only for a faint claim. Labelling week-old claims "fading" made agents distrust memory they had no other
 * source for, and made dreams write "verify before relying" into claims.
 */
function timeClause(strength?: Strength, history?: PastVersion[]) {
	const parts: string[] = [];
	if (strength && strength.level !== "fresh") {
		parts.push(`as of ${strength.last_used?.slice(0, 10)}${strength.level === "faint" ? "; faint" : ""}`);
	}
	if (history?.length) {
		const [last] = history;
		parts.push(`since ${last.until?.slice(0, 10) ?? last.until_ref}; before that (${last.id}): ${clip(last.text, 120)}`);
	}
	return parts.join("; ");
}

function relatedClaims(claims: RelatedClaim[] = []) {
	if (!claims.length) return "";
	return "Current claims these records may confirm, update or contradict:\n" + claims.map((c) => {
		const when = timeClause(c.strength, c.history);
		const seen = c.strength.first_seen ? `, first seen ${c.strength.first_seen.slice(0, 10)}` : "";
		return `- ${c.id} [${c.status}${c.standing ? ", standing" : ""}${seen}; evidence ${c.refs.join(",")}${when ? `; ${when}` : ""}] ${c.text}`;
	}).join("\n") + "\n\n";
}
type DreamResult = {
	id: string; action_count?: number; added_count: number; merged_count: number; rejected_count: number;
	rejected?: { index: number; reason: string }[];
};
type ActionWord = { name: string; description: string; params: Record<string, string>; uses: string[]; source: string };
type CallTiming = { tool: string; ok: boolean; start_ms: number; ms: number };

/** A model-supplied limit, clamped into 1..max instead of failing the call; `note` says so when it changed. */
function clampLimit(requested: number | undefined, fallback: number, max: number) {
	const value = Math.min(max, Math.max(1, Math.floor(requested ?? fallback)));
	return { value, note: requested !== undefined && requested !== value ? `(limit ${requested} clamped to ${value})\n` : "" };
}

/** "key +0ms 640ms, mouse_click +655ms 912ms FAILED": when each nested call began (from the word's start) and how long it took. */
function callTimeline(calls: CallTiming[]) {
	const shown = calls.slice(0, 16).map((call) => `${call.tool} +${call.start_ms}ms ${call.ms}ms${call.ok ? "" : " FAILED"}`);
	return shown.join(", ") + (calls.length > shown.length ? `, ... ${calls.length - shown.length} more` : "");
}
type ActionStep = { call?: { tool: string; args: Record<string, unknown> }; result?: unknown };
type DreamKind = "dream" | "wiki" | "daydream" | "deepsleep";
type DreamRun = { reads: number; submissions: number; submitting: boolean; result?: DreamResult; error?: string;
	kind: DreamKind; started?: boolean; providerError?: string; exhausted?: string; resumed?: number;
	live?: boolean }; // other sessions are recording: the pass is bound to the head it saw, and the phase stays open
type CachedRead = { tool: string; args: unknown; text: string; images?: { ref: string; sha256: string }[] };
const state = {
	dream: undefined as DreamRun | undefined, writable: false,
	contributor: false, // another session owns recording; this one appends its records beside it (a sub-agent, say)
	peer: false, // ...as another session of the user's: its records are the user's own, and it may save notes
	personal: false, // a personal store (one memory of the user for all projects) sits beside the project's
	backend: undefined as Backend | undefined,
	recordWarning: false, indexWarning: false,
	words: new Map<string, string>(), // registered action word -> source
	wordCalls: new Map<string, string>(), // tool call ID of each running action word -> its name
	wordTimings: new Map<string, CallTiming[]>(), // a finished word's nested-call timings, until its result is recorded
	configWarned: false, // config file problems were shown (once per process)
	wordGuideShown: false, // the "Using a word" guidance went into this session's index (sessions without read)
	workspace: "",
	usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: 0, calls: 0 },
};
const day = {
	child: undefined as ChildProcess | undefined,
	starts: [] as number[], // start times in the last hour
	lastCount: 0, // transcript count the last daydream started from
	turns: 0,
	timer: undefined as ReturnType<typeof setInterval> | undefined,
	notice: "",
	before: undefined as string | undefined, // the last pass when the running daydream started
	announced: undefined as string | undefined, // the live pass whose notice is already queued
	forkBroken: false, // a forked daydream failed, so this session's daydreams start cold
	lessons: "", // the claims the announced pass added, shown to the model with its notice
	catchUp: false, // the running or last daydream reviews turns an earlier session left undreamed
};

/** The claims a live pass added, one line each, for the hidden notice: the session can use them right away. */
async function passLessons(pass: any) {
	if (!pass?.added_ids?.length) return "";
	try {
		const claims = await getBackend().request<{ id: string; text: string; status: string }[]>("claims", { ids: pass.added_ids });
		return claims.map((c) => `- ${c.id} (${c.status}): ${c.text}`).join("\n");
	} catch {
		return "";
	}
}

/** The one-line notice for a committed live pass, or undefined if `pass` isn't a new one. */
function livePassNotice(pass: any) {
	if (!pass || !pass.live || pass.id === day.before || pass.id === day.announced) return undefined;
	day.announced = pass.id;
	const words = pass.action_count ? `, ${pass.action_count} action word change(s)` : "";
	const u = pass.usage;
	const spent = u ? `, $${u.cost.toFixed(2)}, ${Math.round(u.totalTokens / 1000)}k tok, ` +
		`${Math.round((u.duration_ms ?? 0) / 1000)}s` : "";
	const from = day.catchUp ? " from your last session's undreamed turns" : "";
	return `Somni daydream ${pass.id}${from}: +${pass.added_count ?? 0} claim(s), ${pass.merged_count ?? 0} merged` +
		`${words}, ${pass.rejected_count} rejected (records up to ${pass.transcript_count}${spent}).`;
}

/** How to start another Pi, as Pi's own subagent example does; SOMNI_DAYDREAM_COMMAND overrides it. */
function piInvocation(args: string[]) {
	if (process.env.SOMNI_DAYDREAM_COMMAND) return { command: process.env.SOMNI_DAYDREAM_COMMAND, args };
	const script = process.argv[1];
	const virtual = script !== undefined && (script.startsWith("/$bunfs/root/") || /^[A-Za-z]:[\\/]~BUN[\\/]root[\\/]/.test(script));
	if (script && !virtual && existsSync(script)) return { command: process.execPath, args: [script, ...args] };
	if (!/^(node|bun)(\.exe)?$/.test(basename(process.execPath).toLowerCase())) return { command: process.execPath, args };
	return { command: "pi", args };
}

// Pi flags that pick the mode, session or prompt. A forked daydream drops them and keeps every other flag, so it loads
// the same extensions, tools and system prompt as its session and its requests start with the same cached prefix.
const FORK_DROP_VALUE = new Set(["--mode", "--session", "--session-id", "--fork", "--session-dir", "--name", "-n",
	"--export", "--use-theme", "--tui-mode"]);
const FORK_DROP = new Set(["--print", "-p", "--continue", "-c", "--resume", "-r", "--no-session", "--verbose"]);
const FORK_KEEP_VALUE = new Set(["--provider", "--model", "--api-key", "--system-prompt", "--append-system-prompt",
	"--models", "--tools", "-t", "--exclude-tools", "-xt", "--thinking", "--extension", "-e", "--skill",
	"--prompt-template", "--theme"]);

/** The session's own Pi arguments, without its mode, session or messages. */
export function forkArgs(argv: string[]) {
	const kept: string[] = [];
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--") break; // the rest are messages
		const flag = arg.split("=", 1)[0];
		if (FORK_DROP_VALUE.has(flag)) {
			if (!arg.includes("=")) i++;
		} else if (FORK_KEEP_VALUE.has(flag)) {
			kept.push(arg);
			if (!arg.includes("=") && i + 1 < argv.length) kept.push(argv[++i]);
		} else if (arg.startsWith("-") && !FORK_DROP.has(flag)) {
			kept.push(arg); // other flags are taken to be switches; a message after one is dropped
		}
	}
	return kept;
}

function getBackend() {
	if (!state.backend) throw new Error("Somni backend is unavailable.");
	return state.backend;
}

function requireWriteAccess() {
	if (!state.writable) {
		throw new Error("Another Pi session owns recording in this Somni memory. Run this there, or close that session and try again.");
	}
	return getBackend();
}

/** This session's part: it may take over recording once the session that owned it has closed. */
async function refreshRole(ctx?: ExtensionContext) {
	if (!state.backend || !(state.contributor || state.peer)) return;
	const role = await state.backend.request<{ writable: boolean; contributor?: boolean; peer?: boolean }>("role", {});
	if (role.writable && !state.writable && ctx) notify(ctx, "Somni: this session now owns recording; the one that did has closed.");
	state.writable = role.writable;
	state.contributor = Boolean(role.contributor);
	state.peer = Boolean(role.peer);
}

/** The personal store's workspace for this session, or undefined when personal memory is off. */
function personalPath(ctx: ExtensionContext) {
	const value = (process.env.SOMNI_PERSONAL ?? "auto").trim();
	if (!value || value === "none" || (value === "auto" && ctx.mode !== "tui")) return undefined;
	const path = value === "auto" ? "~/.somni" : value;
	return path === "~" || path.startsWith("~/") ? join(homedir(), path.slice(2)) : resolve(ctx.cwd, path);
}

/** A second session of the user's (interactive Pi) is a peer; a scripted one, such as a sub-agent, is a contributor. */
function isPeer(ctx: ExtensionContext) {
	const role = process.env.SOMNI_SESSION_ROLE;
	return role ? role === "peer" : ctx.mode === "tui";
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
	// A deep-sleep run records: its live tries are real calls, and the words it defines cite them.
	return RECORD && (state.writable || state.contributor) && (!state.dream || state.dream.kind === "deepsleep");
}

function notify(ctx: ExtensionContext, message: string) {
	if (ctx.mode === "json" || ctx.mode === "print") console.error(message);
	else ctx.ui.notify(message, "warning");
}

async function record(source: "ordinary_observation" | "tool_result" | "agent_output", text: string, ctx: ExtensionContext,
		options: { tool_ok?: boolean; index_bytes?: number; query?: string; images?: ImageContent[]; omitted_images?: number;
			action_words?: string[]; calls?: CallTiming[]; via_word?: string; word?: boolean } = {}) {
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
	if (record.source === "outcome") {
		const { by, verdict } = record.outcome ?? {};
		const label = ["OUTCOME, from outside the agent", by && `by ${by}`, verdict && `verdict ${verdict}`].filter(Boolean).join("; ");
		const report = record.report;
		const pair = report ? `\n    judges the agent's report [${report.ref}]: ${report.text}` +
			(report.overclaim ? "\n    The agent reported success; this outcome says it failed." : "") : "";
		return `[${record.id}] (${label})${contextLabel(record)} ${excerpt}${images}${omitted}${pair}`;
	}
	return `[${record.id}] (${record.source})${status}${contextLabel(record)} ${excerpt}${images}${omitted}`;
}

/**
 * A daydream's record listing: the newest records that fit in DAYDREAM_PROMPT_BYTES. A tool result shows its call
 * and the start of its output; `somni_search "t000123"` reads the rest. Prompts and replies are shown as usual.
 */
function daydreamListing(records: TranscriptRecord[]) {
	const render = (record: TranscriptRecord) => record.source === "tool_result" && record.text.length > STUB_CHARS
		? `${describeRecord(record, STUB_CHARS)} (somni_search "${record.id}" reads it)` : describeRecord(record);
	let used = 0;
	let start = records.length;
	while (start > 0) {
		const size = Buffer.byteLength(render(records[start - 1]), "utf8") + 1;
		if (start < records.length && used + size > DAYDREAM_PROMPT_BYTES) break;
		used += size;
		start--;
	}
	const note = start ? `[${start} older record(s) left out to keep this daydream small; somni_search finds them]\n` : "";
	return note + withDays(records.slice(start), render).join("\n");
}

/**
 * A forked daydream's prompt: a dream step. The fork holds the session's conversation, so it needs only record IDs to
 * cite, not their text. It writes lessons as unverified claims; the night's dream checks them against the records.
 */
function dreamStepPrompt(focus: string, recent: Recent) {
	// The fork's conversation holds this session's records but not another session's (a sub-agent's, or another of the
	// user's own sessions): those come in full.
	const from = (r: TranscriptRecord) => (r.tags ?? []).find((t) => t.startsWith("contributor") || t.startsWith("session"));
	const anchors = withDays(recent.records, (r) => from(r)
		? `${describeRecord(r)} [from ${from(r)!.startsWith("session") ? "another of the user's sessions" : "another session, such as a sub-agent"}]`
		: `[${r.id}] (${r.source}) ${clip(r.text.replace(/\s+/g, " "), 100)}`);
	return "Dream step. You are a copy of this session, forked to reflect while the session keeps working. Don't continue " +
		"the user's task or call its tools. Earlier dream steps appear above as \"Somni daydream\" notices with what they " +
		`added. From the work since the last step, write down the lessons worth keeping: ${focus}.\n\n` +
		"Call somni_dream once. Each lesson is one add operation with status \"uncertain\", one or two sentences of text, " +
		'evidence [{"ref":"t000123"}] citing the records below where it shows (no quotes needed), salience 1–5 (3 for an ' +
		"ordinary lesson, 5 only for what matters in most sessions) and an optional one-line gist. Don't repeat what an " +
		"earlier step or the memory index already holds; correct or retract a claim the work has shown to be wrong, and " +
		"update one whose subject has changed since (the old claim stays as history). Use an " +
		"empty operations array if nothing new was learned. Tonight's dream checks these lessons against the records. " +
		STANDING_GUIDE + SELF_GUIDE + "\n\n" +
		(recent.reviewed_through ? `Records through ${recent.reviewed_through} were reviewed by earlier steps.\n` : "") +
		`Records since then (ID, source and the start of each; the full text is in the conversation above, except for ` +
		`records from another session, given in full):\n${anchors.join("\n")}`;
}

function summary(result: DreamResult) {
	const actions = result.action_count ? ` ${result.action_count} action word change(s).` : "";
	return `Somni dream ${result.id}: ${result.added_count} added, ${result.rejected_count} rejected, ${result.merged_count} merged.${actions}`;
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

	/** Why a dream committed nothing, for the store's dream_failures log. */
	function failureOf(run: DreamRun, error: Error) {
		if (run.providerError) return { cause: "provider_error", detail: run.providerError };
		if (run.exhausted) return { cause: "budget_exhausted", detail: `${run.exhausted} limit reached. ${run.error ?? ""}` };
		if (run.error) return { cause: run.error.startsWith("nothing committed: all") ? "all_rejected" : "invalid_submission", detail: run.error };
		if (run.submissions === 0) return { cause: "no_submission", detail: error.message };
		return { cause: "other", detail: error.message };
	}

	async function recordFailure(failure: { cause: string; detail: string }, kind: DreamKind, run?: DreamRun) {
		try {
			const images = run ? await getBackend().request<number>("dream_images", {}) : 0;
			await getBackend().request("dream_failure", { ...failure, detail: clip(failure.detail, 500), kind,
				reads: Math.min(run?.reads ?? 0, 10_000), submissions: Math.min(run?.submissions ?? 0, 10_000), images });
		} catch {
			// Diagnostics only: a failed log write must not hide the dream's own error.
		}
	}

	/**
	 * If an earlier attempt on these same records was cut off, hand its evidence reads to this one, so the retry
	 * doesn't spend turns repeating them. Images it viewed are attached again and stay citable.
	 */
	async function withCachedReads(content: string | (TextContent | ImageContent)[], run: DreamRun) {
		if (!DREAM_CACHE_SECONDS) return content;
		let reads: CachedRead[] = [];
		try {
			reads = await getBackend().request<CachedRead[]>("dream_cache_take", { kind: run.kind, max_age_seconds: DREAM_CACHE_SECONDS });
		} catch {
			return content;
		}
		if (!reads.length) return content;
		run.resumed = reads.length;
		const parts: (TextContent | ImageContent)[] = typeof content === "string" ? asText(content) : [...content];
		parts.push(...asText(`\n\n## Evidence from an interrupted attempt\nAn earlier attempt on these same records stopped ` +
			`before committing a pass. These are the ${reads.length} read(s) it made; their results are below, so you don't need ` +
			`to repeat them (they don't count against your read budget). Cite them as usual.\n`));
		for (const read of reads) {
			parts.push(...asText(`\n${read.tool}(${clip(JSON.stringify(read.args ?? {}), 300)}) ->\n${read.text}`));
			for (const { ref, sha256 } of read.images ?? []) {
				try {
					const image = await getBackend().request<ImageContent>("image", { ref, sha256, during_dream: true, max_images: DREAM_MAX_IMAGES });
					parts.push(...asText(`Screenshot for [${ref}] image_sha256=${sha256}`), image);
				} catch {
					// Past the image budget or gone: the text above still names it.
				}
			}
		}
		return parts;
	}

	async function startDream(ctx: ExtensionCommandContext, load: () => Promise<string | (TextContent | ImageContent)[] | undefined>,
			kind: "dream" | "wiki" | "deepsleep") {
		const headless = ctx.mode === "print" || ctx.mode === "json";
		let run: DreamRun | undefined;
		try {
			await refreshRole(ctx);
			const backend = requireWriteAccess();
			if (state.dream || !ctx.isIdle()) throw new Error("Wait for the current Pi turn to settle first.");
			run = { submitting: false, reads: 0, submissions: 0, kind: DAYDREAM_CHILD ? "daydream" : kind };
			state.dream = run;
			const loaded = await load();
			if (loaded === undefined) {
				state.dream = undefined;
				return;
			}
			const content = await withCachedReads(loaded, run);
			// A daydream worker leaves the phase alone: the owning session is still recording. So does deep sleep,
			// which records its real tries and commits a live pass.
			// So does a dream while other sessions of this memory are recording: it reviews the records up to now.
			const others = !DAYDREAM_CHILD && kind !== "deepsleep" && RECORD
				? (await backend.request<{ others?: number }>("role", {})).others ?? 0 : 0;
			if (others) {
				run.live = true;
				notify(ctx, `Somni: ${others} other Pi session(s) are recording in this memory, so this ${kind} reviews ` +
					"the records up to now and they keep recording.");
			} else if (!DAYDREAM_CHILD && kind !== "deepsleep") await backend.request("phase", { phase: "closed" });
			// A triggered custom message starts the turn before waitForIdle checks Pi's state.
			run.started = true;
			pi.sendMessage({ customType: "somni-dream-input", content, display: false }, { triggerTurn: true });
			await ctx.waitForIdle();
			if (!run.result) throw new Error(run.error ?? "The dream ended without a committed pass. The model must call somni_dream.");
		} catch (error) {
			if (headless) process.exitCode = 1;
			if (run?.started && !run.result) await recordFailure(failureOf(run, error as Error), run.kind, run);
			if (run?.started && !run.result && DREAM_CACHE_SECONDS && run.reads > 0) {
				notify(ctx, `Somni kept this dream's ${run.reads} evidence read(s); a retry on the same records within ` +
					`${DREAM_CACHE_SECONDS / 60} min gets them back.`);
			}
			notify(ctx, `Somni dream failed. ${(error as Error).message}`);
		} finally {
			if (run) await finishDream(run);
		}
	}

	function submitCommand(ctx: ExtensionCommandContext, load: () => Promise<string | (TextContent | ImageContent)[] | undefined>,
			kind: "dream" | "wiki" | "deepsleep") {
		const task = startDream(ctx, load, kind);
		if (ctx.mode === "json" || ctx.mode === "print") return task;
		// RPC clients get the handled reply first and can then wait for agent_settled.
		void task.catch((error: Error) => notify(ctx, `Somni dream cleanup failed. ${error.message}`));
	}

	/**
	 * Start a daydream if one is due: a child Pi runs /somni-dream against a `serve --daydream` backend,
	 * which reads up to the current transcript head and commits one live pass while this session records on.
	 * Never blocks: skipped while one runs, when nothing new was recorded, or past SOMNI_DAYDREAM_PER_HOUR.
	 * `catchUp` is the one a session starts with when an earlier session ended before its next daydream; it starts cold,
	 * since this session's conversation doesn't hold those turns.
	 */
	async function maybeDaydream(ctx: ExtensionContext, catchUp = false) {
		if (!DAYDREAMS || day.child || !canRecord() || !state.writable) return; // the owner runs daydreams
		const now = Date.now();
		day.starts = day.starts.filter((start) => now - start < 3_600_000);
		if (day.starts.length >= DAYDREAM_PER_HOUR) return;
		let status: { transcript_count: number; last_pass: { id: string } | null };
		try {
			status = await getBackend().request("status");
		} catch {
			return;
		}
		if (status.transcript_count <= day.lastCount) return;
		day.lastCount = status.transcript_count;
		day.starts.push(now);
		day.turns = 0;
		day.before = status.last_pass?.id;
		day.catchUp = catchUp;
		const model = process.env.SOMNI_DAYDREAM_MODEL || (ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : undefined);
		// A fork starts from the saved session and its cached prefix; it needs the session's model to reuse the cache.
		const sessionFile = ctx.sessionManager.getSessionFile();
		const fork = DAYDREAM_FORK && !catchUp && !day.forkBroken && sessionFile && existsSync(sessionFile) && !process.env.SOMNI_DAYDREAM_MODEL;
		const forkDir = fork ? join(state.workspace, "somni-v1", "forks", `${process.pid}-${now}`) : undefined;
		if (forkDir) mkdirSync(forkDir, { recursive: true });
		// The fork keeps its session's ID (in its own session folder, so nothing collides): providers route requests and
		// look up cached prefixes by it, through headers an extension can't always reach.
		const args = fork ? [...forkArgs(process.argv.slice(2)), "--mode", "json", "--fork", sessionFile, "--session-dir", forkDir!,
			"--session-id", ctx.sessionManager.getSessionId(),
			...(process.env.SOMNI_DAYDREAM_THINKING ? ["--thinking", process.env.SOMNI_DAYDREAM_THINKING] : []),
			"-p", `/somni-dream ${DAYDREAM_FOCUS}`] : ["--mode", "json", "--no-session", "--no-approve", "--no-extensions", "--no-builtin-tools", "--no-mcp",
			"--no-skills", "--no-prompt-templates", "--no-context-files", "-e", fileURLToPath(import.meta.url),
			...(ACTIONS ? ["--skill", resolve(HERE, "../skills/somni-action-words")] : []),
			...(model ? ["--model", model] : []),
			...(process.env.SOMNI_DAYDREAM_THINKING ? ["--thinking", process.env.SOMNI_DAYDREAM_THINKING] : []),
			"-p", `/somni-dream ${DAYDREAM_FOCUS}`];
		const invocation = piInvocation(args);
		let stderr = "";
		const child = spawn(invocation.command, invocation.args, {
			cwd: ctx.cwd, shell: false, stdio: ["ignore", "ignore", "pipe"],
			env: { ...process.env, SOMNI_DAYDREAM: "1", SOMNI_RECORD: "0", SOMNI_WORKSPACE: state.workspace,
				...(fork ? { SOMNI_DAYDREAM_FORK_OF: ctx.sessionManager.getSessionId() } : {}) },
		});
		day.child = child;
		child.stderr?.on("data", (chunk: Buffer) => { stderr = (stderr + chunk.toString()).slice(-2000); });
		let timedOut = false;
		const timeout = setTimeout(() => { timedOut = true; child.kill("SIGTERM"); }, DAYDREAM_TIMEOUT_MS);
		child.on("error", (error) => { stderr += ` ${error.message}`; });
		child.on("close", async (code, signal) => {
			clearTimeout(timeout);
			// day.child stays set until the notice is settled, so a turn starting meanwhile checks the store itself.
			const done = () => { if (day.child === child) day.child = undefined; };
			if (forkDir) rmSync(forkDir, { recursive: true, force: true }); // the fork's session copy
			try {
				const after = await getBackend().request<{ last_pass: any }>("status");
				const pass = after.last_pass;
				if (pass?.live && pass.id !== day.before) {
					// The next turn may have announced it already, while this worker was still exiting.
					const notice = livePassNotice(pass);
					if (!notice) return done();
					day.notice = notice;
					const lessons = await passLessons(pass);
					// A turn may have started meanwhile and shown the notice; then its lessons arrive too late for it.
					if (day.notice === notice) day.lessons = lessons;
					done();
					notify(ctx, notice);
					return;
				} else if (AUTH_ERROR.test(stderr)) {
					day.notice = "Somni daydream skipped: auth. The daydream's Pi could not authenticate with its provider. " +
						clip(stderr.trim().split("\n").slice(-2).join(" "), 240);
				} else {
					if (timedOut) await recordFailure({ cause: "timeout", detail: `stopped after ${DAYDREAM_TIMEOUT_MS / 1000}s` }, "daydream");
					day.notice = `Somni daydream made no pass (${signal ? `stopped by ${signal}` : `exit ${code}`}). ` +
						clip(stderr.trim().split("\n").slice(-3).join(" "), 300);
					if (forkDir && !timedOut) {
						day.forkBroken = true;
						day.notice += " Later daydreams in this session start cold instead of forking it.";
					}
				}
			} catch (error) {
				day.notice = `Somni daydream finished, but its result could not be read. ${(error as Error).message}`;
			}
			done();
			notify(ctx, day.notice);
		});
	}

	/** Whether an earlier session left a turn of the user's that no pass has reviewed yet. */
	async function undreamedTurns() {
		try {
			const status = await getBackend().request<{ transcript_count: number; last_pass: { transcript_count: number } | null }>("status");
			const since = status.last_pass?.transcript_count ?? 0;
			if (status.transcript_count <= since) return false;
			const records = await getBackend().request<TranscriptRecord[]>("records_since", { count: since });
			return records.some((r) => r.source === "ordinary_observation" && r.text.startsWith("USER:"));
		} catch {
			return false;
		}
	}

	/** Tools an action word may call: everything callable except Somni's own tools (and SOMNI_ACTION_TOOLS, if set). */
	function actionTargets() {
		return pi.getAllTools().filter((tool) => !tool.name.startsWith("somni_")
			&& !["hidden", "model-only"].includes((tool as { exposure?: string }).exposure ?? "direct")
			&& (!ACTION_ALLOW || ACTION_ALLOW.includes(tool.name) || state.words.has(tool.name)));
	}

	async function runWord(word: ActionWord, params: Record<string, unknown>, toolCallId: string,
			signal: AbortSignal | undefined, ctx: any) {
		return runSteps(word.name, (history) => getBackend().request<ActionStep>("action_step",
			{ name: word.name, params, history, max_calls: ACTION_MAX_CALLS }), toolCallId, signal, ctx,
			(tool) => !ACTION_ALLOW || ACTION_ALLOW.includes(tool) || state.words.has(tool), "SOMNI_ACTION_TOOLS");
	}

	/** Run a word, or a deep-sleep draft, one call at a time: the backend replays it and returns its next call. */
	async function runSteps(name: string, next: (history: { tool: string; args: Record<string, unknown>; result: unknown }[]) =>
			Promise<ActionStep>, toolCallId: string, signal: AbortSignal | undefined, ctx: any,
			allowed: (tool: string) => boolean, allowedBy: string) {
		const word = { name };
		if (toolCallId.split("/").length > MAX_ACTION_DEPTH) throw new Error(`Action words nest at most ${MAX_ACTION_DEPTH} deep.`);
		const history: { tool: string; args: Record<string, unknown>; result: unknown }[] = [];
		const calls: ({ args: Record<string, unknown> } & CallTiming)[] = [];
		let last: { tool: string; images: ImageContent[] } | undefined;
		// Wall-clock timing of each nested call, so a dream can see when a word is too slow for what it does.
		const started = Date.now();
		state.wordCalls.set(toolCallId, word.name);
		try {
			for (;;) {
				const step = await next(history);
				if (!step.call) {
					// Images from the word's final call show the state the word ended in, so they go to the caller;
					// images from earlier calls may predate later actions, so they are not passed on.
					// No extra text: a calling word parses this result's text as JSON.
					const images = last?.images.slice(0, MAX_WORD_IMAGES) ?? [];
					return { content: [...asText(JSON.stringify(step.result ?? null)), ...images],
						details: { word: word.name, calls, ...(images.length ? { images_from: last!.tool } : {}) } };
				}
				const { tool, args } = step.call;
				if (!allowed(tool)) throw new Error(`${word.name} may not call ${tool} (${allowedBy}).`);
				// Resolves to { toolCall, result: { content, details }, isError, durationMs }; never rejects.
				const begin = Date.now();
				const outcome = await ctx.executeTool(tool, args, { signal });
				const content: (TextContent | ImageContent)[] = outcome.result?.content ?? [];
				const value = resultValue(content);
				last = { tool, images: content.filter((part): part is ImageContent => part.type === "image") };
				calls.push({ tool, args, ok: !outcome.isError, start_ms: begin - started, ms: Date.now() - begin });
				if (outcome.isError) {
					const done = calls.slice(0, -1).map((call) => call.tool).join(", ") || "none";
					throw new Error(`${word.name} stopped at call ${calls.length} (${tool}): ` +
						`${clip(typeof value === "string" ? value : JSON.stringify(value), 600)}. Earlier calls already ran: ${done}.`);
				}
				history.push({ tool, args, result: value });
			}
		} finally {
			state.wordCalls.delete(toolCallId);
			state.wordTimings.set(toolCallId, calls.slice(0, MAX_TIMED_CALLS).map(({ tool, ok, start_ms, ms }) => ({ tool, ok, start_ms, ms })));
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
					"Returns JSON. If a call fails it stops, and earlier calls stay done. Its return value is what the " +
					"word reports, so check the effect before relying on it. Its source is in Somni's actions folder.)";
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

	/**
	 * Store the declarations of the tools this recording session can call. Dreams often run
	 * without them (no MCP server, or before MCP tools register), but words are for task sessions.
	 */
	let sentCatalog = "";
	let sentNames = new Set<string>();
	async function recordToolCatalog(ctx: ExtensionContext) {
		if (!ACTIONS || !canRecord() || !state.writable) return; // the owner keeps the tool catalog
		const tools = actionTargets().filter((tool) => !state.words.has(tool.name)).slice(0, 128).map((tool) => {
			const parameters = (tool.parameters ?? {}) as Record<string, unknown>;
			return { name: tool.name, description: clip((tool.description ?? "").replace(/\s+/g, " "), 300),
				parameters: Buffer.byteLength(JSON.stringify(parameters)) <= 2048 ? parameters : { omitted: "schema over 2048 bytes" } };
		});
		const key = JSON.stringify(tools);
		if (key === sentCatalog) return;
		try {
			await getBackend().request("action_tools", { tools });
			sentCatalog = key;
			sentNames = new Set(tools.map((tool) => tool.name));
		} catch (error) {
			notify(ctx, `Somni could not record this session's tools for action words. ${(error as Error).message}`);
		}
	}

	type CatalogEntry = { description: string; parameters: unknown; declared: boolean };
	type Candidate = { tools: string[]; count: number; same_args: number; args: unknown[]; varying: string[]; refs: string[][] };
	/** How a word calls a tool (matches actions.call_path). */
	function callPath(tool: string) {
		const parts = tool.split("__");
		return tool.startsWith("mcp__") && parts.length === 3 ? `rpc.mcp.${parts[1]}.${parts[2]}` : `rpc.${tool}`;
	}

	function candidateGuide(candidates: Candidate[]) {
		if (!candidates.length) return "";
		const lines = candidates.map((c) => {
			const calls = c.tools.map((tool, i) => `${callPath(tool)}(${clip(JSON.stringify(c.args[i] ?? {}), 100)})`).join(" -> ");
			const repeat = c.same_args >= 2 ? `${c.count}x, ${c.same_args}x with these exact arguments` : `${c.count}x`;
			const varying = c.varying.length ? `; arguments that vary: ${c.varying.join(", ")}` : "";
			return `- ${repeat}: ${calls}${varying}. Refs of occurrences: ${c.refs.map((r) => r.join(" ")).join(" | ")}`;
		});
		return "\n\nRecurring sequences Somni found in the transcript (candidates for action words; each ref group is " +
			"a ready-made test, and a varying argument is a likely parameter):\n" + lines.join("\n") + "\nFor each candidate " +
			"that occurs 3 or more times, either define a word for it in this pass or say in one line why not.\n";
	}

	type Workbench = Record<string, { tests: { params: unknown; refs: string[]; result?: unknown }[]; selftests: string[];
		uses_words: string[]; used_by: string[]; runs: { ref: string; ok: boolean; args: unknown; result: string; confirmed: boolean | null; calls?: CallTiming[];
			stale?: string }[] }>;

	/** Live tool declarations for the backend, which merges them with the task-session catalog and adds signatures. */
	function liveTools() {
		return actionTargets().filter((tool) => !state.words.has(tool.name)).slice(0, 128).map((tool) => ({
			name: tool.name, description: clip((tool.description ?? "").replace(/\s+/g, " "), 300),
			parameters: Buffer.byteLength(JSON.stringify(tool.parameters ?? {})) <= 4096 ? tool.parameters ?? {} : {},
		}));
	}

	function actionGuide(words: ActionWord[], catalog: Record<string, CatalogEntry & { live?: boolean; signature: string }>,
			candidates: Candidate[] = [], bench: Workbench = {}) {
		const tools = Object.entries(catalog).filter(([name]) => !state.words.has(name)
			&& (!ACTION_ALLOW || ACTION_ALLOW.includes(name)))
			.map(([, entry]) => `- ${clip(entry.signature, 400)}${entry.live ? "" : " (task sessions)"}`);
		const lexicon = words.map((word) => {
			const info = bench[word.name];
			const lines = [`### ${word.name}`];
			if (info?.uses_words.length || info?.used_by.length) {
				lines.push(`Uses words: ${info.uses_words.join(", ") || "none"}. Used by: ${info.used_by.join(", ") || "none"}.`);
			}
			lines.push("```python", word.source, "```");
			for (const test of info?.tests ?? []) {
				lines.push(`Test: ${clip(JSON.stringify(test.params), 200)} replays ${test.refs.join(" ") || "no calls"}` +
					(test.result !== undefined ? ` -> ${clip(JSON.stringify(test.result), 200)}` : ""));
			}
			if (info?.selftests.length) lines.push(`Self-tests in the source: ${info.selftests.join(", ")}.`);
			for (const run of info?.runs ?? []) {
				const status = !run.ok ? "FAILED" : run.confirmed === true ? "ok, confirmed" : run.confirmed === false ? "ok, UNCONFIRMED" : "ok";
				const stale = run.stale ? ` [${run.stale}: not grounds to retire]` : "";
				lines.push(`Run ${run.ref} ${status}${stale}: ${clip(JSON.stringify(run.args), 160)} -> ${clip(run.result, 240)}`);
				if (run.calls?.length) lines.push(`  timing: ${callTimeline(run.calls)}`);
			}
			if (info && !info.runs.length) lines.push("No recorded runs yet.");
			return lines.join("\n");
		}).join("\n\n") || "(none yet)";
		return `

## Action words

An action word is one Python function, built from a sequence of tool calls that recurs in the transcript, that the agent can then call as a single tool. Write one as a developer would: read the words and their recent runs below, try your source with somni_action_try, then submit it.

Submit {"op":"define_action","source":"<python>","tests":[...]}. Repair with {"op":"revise_action","source":...,"tests"?:[...]}; remove with {"op":"retire_action","name":...,"reason":...,"runs":["t000123"]}, citing the failing runs it is retired for. Revise or retire a word when its runs show it did not have its effect. Runs marked as older source or older Somni failed on code that has changed since; they are not grounds to retire, and a retirement that cites only them is rejected. A word that other words use cannot be retired; revise or retire them first, in the same pass.

Writing a word:
- One \`def name(param: str|int|float|bool|list|dict, ...)\` whose body starts with a one-line docstring (the tool description). Allowed: assignments, if, for over lists or range, comprehensions, arithmetic, indexing, .get/.items/.keys/.values/.lower/.split and similar, and abs min max round int float str len bool sorted sum any all range divmod. date, datetime, timedelta, floor and ceil are predefined for date math (no import: write datetime.fromisoformat(s), floor(x)). No other imports, while, try, lambda, classes or I/O.
- Call tools by the signatures listed below: rpc.mcp.SERVER.TOOL(key=value) for MCP tools, rpc.TOOL(key=value) for others (another word is called the same way). A call returns the tool's parsed JSON result. dir(rpc.mcp.SERVER) and help(rpc.mcp.SERVER.TOOL) list tools and their signatures.
- Generalize with parameters, not constants copied from one episode. Check cheap preconditions (a status or lookup tool) first and branch on them. Return state read back after acting, not a count of your own steps: the agent takes the return value as the outcome. When the word changes something, return a top-level "confirmed": true only when a read-back proves the effect, and false when it does not; the agent's index counts confirmed and unconfirmed runs. Revise or retire a word whose runs are mostly unconfirmed. Only the final call's images (e.g. a screenshot) reach the agent, so end with the looking call.

Testing (the gate runs both kinds, and together they must exercise every tool the word calls):
- tests: [{"params":{...},"refs":["t000012","t000013"],"result":<optional>}]. Given params, the word must make exactly the recorded calls in refs (same tools and arguments, in order; successful, untruncated tool_results), then return.
- Self-tests in the source, after the word, for assertions on results:
\`\`\`python
from rpc import replay

def name(...):
    ...

def test_places_one_block():
    with replay("t000012", "t000013"):
        result = name(count=1)
        assert result["placed"] == 1
\`\`\`
  Inside replay(...), the word's calls must match those records, in order, and get their recorded results.
- somni_action_try {"source":..., "tests"?:[...]} dry-runs the gate and returns what each test's call returned, or the first error; {"inspect":"rpc.mcp.SERVER"} lists tools with signatures. Nothing is saved.

Words run in task sessions, so they may call tools marked "(task sessions)" even though this dream cannot.

### Callable tools
${tools.join("\n") || "(none)"}

### Current action words
${lexicon}
${candidateGuide(candidates)}`;
	}

	pi.registerTool({
		name: "somni_recall",
		label: "Somni recall",
		description: "Recall the wiki pages and memory claims most relevant to a query (pages cite claims; claims cite transcript evidence).",
		parameters: Type.Object({ query: Type.String(), limit: Type.Optional(Type.Integer({ description: `1-${MAX_RECALL_LIMIT}` })) }),
		async execute(_id: string, params: { query: string; limit?: number }) {
			const limit = clampLimit(params.limit, 5, MAX_RECALL_LIMIT);
			const hits = await getBackend().request<any[]>("recall", { query: params.query.slice(0, 2000), limit: limit.value });
			const lines = hits.slice(0, limit.value).map((h) => {
				if (h.kind === "rule") return `Rule ${h.id} (${h.support} supporting transitions): ${h.text}\n${JSON.stringify({ when: h.when, then: h.then })}`;
				if (h.slug) return `# ${h.title} (page ${h.slug})\n${h.body}`;
				const lineage = h.merged_from ? `; merged from ${h.merged_from.join(", ")}` : "";
				if (h.state === "past") {
					const until = h.until?.slice(0, 10) ?? h.until_ref;
					return `${h.id} [PAST: true until ${until}; now ${h.superseded_by}; ${h.status}]${contextLabel(h)} ${h.text}`;
				}
				const when = timeClause(h.strength, h.history);
				const outcomes = h.outcome_evidence?.length ? `; outcome-backed (${h.outcome_evidence.join(", ")})` : "";
				return `${h.id} [${h.personal ? "personal, " : ""}${h.status}${h.standing ? ", standing" : ""}; ${h.evidence.length} cited${outcomes}${lineage}${when ? `; ${when}` : ""}]${contextLabel(h)} ${h.text}`;
			});
			return { content: asText(limit.note + (lines.join("\n\n") || "no matching memory")), details: hits };
		},
	});
	pi.registerTool({
		name: "somni_search",
		label: "Somni search",
		description: "Search your transcript text, run labels, and tags for an exact phrase. The transcript keeps what this " +
			"session, earlier sessions and sub-agents saw, so search it when something you need has moved or is gone (a " +
			"rotated log, a moved page) or was only seen by a sub-agent. Also finds evidence IDs and image digests; a " +
			"record ID such as t000123 returns that record.",
		parameters: Type.Object({ query: Type.String(), limit: Type.Optional(Type.Integer({ description: `1-${MAX_SEARCH_LIMIT}` })) }),
		async execute(_id: string, params: { query: string; limit?: number }) {
			const limit = clampLimit(params.limit, 10, MAX_SEARCH_LIMIT);
			const out = await getBackend().request<TranscriptRecord[]>("search", { query: params.query.slice(0, 256), limit: limit.value });
			const chars = /^t\d{6}$/.test(params.query.trim()) ? DREAM_RECORD_BYTES : 600;
			const dated = (r: TranscriptRecord) => describeRecord(r, chars) + (r.at ? ` (recorded ${r.at.slice(0, 10)})` : "");
			return { content: asText(limit.note + (out.map(dated).join("\n") || "no matches")), details: out };
		},
	});
	pi.registerTool({
		name: "somni_image",
		label: "Somni image",
		description: "View a screenshot or other stored image by its transcript record ID and SHA-256 from somni_search.",
		parameters: Type.Object({ ref: Type.String({ pattern: "^t[0-9]{6}$" }),
			sha256: Type.String({ minLength: 1, maxLength: 64, description: "The image's SHA-256, or a unique prefix of it" }) }),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			if (!ctx.model?.input.includes("image")) throw new Error("Viewing Somni images requires a vision-capable Pi model.");
			const { sha256, image } = await getBackend().request<{ sha256: string; image: ImageContent }>("image", {
				...params, during_dream: Boolean(state.dream), max_images: DREAM_MAX_IMAGES, resolve: true });
			return { content: [...asText(`[${params.ref}] image_sha256=${sha256}`), image], details: { ref: params.ref, sha256 } };
		},
	});
	pi.registerTool({
		name: "somni_note",
		label: "Somni note",
		description:
			"Add a durable memory claim: something that will help in a later session, such as the user's preference or correction, " +
			"a project fact, or a fix that worked. Never save secrets. Cite transcript IDs as evidence; 'verified' requires an exact quote " +
			"or the image_sha256 of a stored image from each record. Use 'uncertain' for interpretations. Salience 1-5. " +
			"For longer claims, an optional one-line gist of up to 160 UTF-8 bytes appears in the memory index. " +
			"Set standing: true when the user tells you how they want work done from now on (\"I like 2-space indents\"): " +
			"it then heads every later session's index. Cite the user's message as evidence; not for one-off requests. " +
			"Set scope: \"personal\" for something about the user that holds in every project (when the index shows a " +
			"personal memory); leave it out for this project's facts.",
		parameters: Type.Object({
			text: Type.String(),
			gist: Type.Optional(Type.String({ maxLength: 160 })),
			status: Type.Union([Type.Literal("verified"), Type.Literal("uncertain")]),
			salience: Type.Optional(Type.Integer({ minimum: 1, maximum: 5 })),
			standing: Type.Optional(Type.Boolean()),
			scope: Type.Optional(Type.Union([Type.Literal("project"), Type.Literal("personal")])),
			evidence: Evidence,
		}),
		async execute(_id: string, params: any) {
			if (!state.writable && !state.peer) requireWriteAccess();
			const id = await getBackend().request<string>("note", { ...params, salience: params.salience ?? 3 });
			return { content: asText(`saved ${id}`), details: { id } };
		},
	});
	pi.registerTool({
		name: "somni_dream",
		label: "Somni dream",
		description:
			"Only during /somni-dream or /somni-wiki: submit one consolidation pass as JSON text " +
			'{"schema":1,"operations":[...]} with add/correct/update/merge/retract/promote claim operations (add/correct/update/merge take an optional ' +
			'"standing": true for a lasting preference of the user\'s, and "scope": "personal" for a lesson about the user ' +
			'that belongs in their personal memory) and page/drop_page wiki ' +
			'operations. A merge takes {"op":"merge","claim_ids":["n000001","n000002"],"text":"combined guidance"} and inherits their evidence. ' +
			"Use update (fields as correct) when the world changed since a claim was true; it keeps the old claim as history " +
			"and needs evidence newer than the old claim's. Use correct when the claim was wrong." +
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
					...(LENIENT_IMAGE_EVIDENCE ? { lenient_images: true } : {}),
					...(run.kind === "deepsleep" || run.live ? { live: true } : {}),
					// A daydream worker reports what it spent up to this submission; it lands on the live pass.
					...(DAYDREAM_CHILD ? { usage: { ...state.usage, duration_ms: Math.round(process.uptime() * 1000) } } : {}),
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

	pi.registerTool({
		name: "somni_share",
		label: "Somni share",
		description:
			"Only when the user asks to share what this memory learned (for example after /somni-share): write a skill " +
			"other people's agents can load, as SKILL.md plus EVIDENCE.md. Each guideline is an instruction in your own " +
			"words, general enough for someone else's projects, citing the claim IDs it rests on. Somni adds what backs " +
			"each one and refuses a draft with personal details in it. It writes files only; it publishes nothing.",
		parameters: Type.Object({
			name: Type.String({ description: "lowercase-hyphenated, up to 64 characters" }),
			description: Type.String({ description: "what the skill does and when to load it, up to 1024 characters" }),
			intro: Type.Optional(Type.String()),
			guidelines: Type.Array(Type.Object({ text: Type.String(), claims: Type.Array(Type.String()) })),
		}),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			const out = await getBackend().request<{ path: string; name: string; guidelines: number; replaced: boolean }>(
				"share", { proposal: params, cwd: ctx?.cwd ?? process.cwd(), home: homedir(),
					deny: process.env.SOMNI_NEVER_SHARE ?? "" });
			const text = `${out.replaced ? "Replaced" : "Wrote"} the share "${out.name}" (${out.guidelines} guideline(s)) ` +
				`in ${out.path}: SKILL.md and EVIDENCE.md. Read both before sharing them; nothing was published.`;
			return { content: asText(text), details: out };
		},
	});

	if (ACTIONS) pi.registerTool({
		name: "somni_action_try",
		label: "Somni action try",
		description: "Only during /somni-dream or /somni-deepsleep: dry-run an action word against the gate without saving " +
			"it. Pass source (and optional tests) to see each test's returned value or the first error, or inspect (e.g. " +
			"\"rpc.mcp.body\" or \"rpc.mcp.body.key\") to list tools and their signatures. During /somni-deepsleep, " +
			"live: true with params runs the draft against the real tools, call after call with no model turn between " +
			"them, records each call, and returns their record IDs to cite as the draft's test.",
		parameters: Type.Object({
			source: Type.Optional(Type.String()),
			tests: Type.Optional(Type.Array(Type.Any())),
			inspect: Type.Optional(Type.String()),
			live: Type.Optional(Type.Boolean()),
			params: Type.Optional(Type.Record(Type.String(), Type.Any())),
		}),
		async execute(toolCallId: string, params: { source?: string; tests?: unknown[]; inspect?: string; live?: boolean;
				params?: Record<string, unknown> }, signal: AbortSignal | undefined, _update: unknown, ctx: any) {
			if (!state.dream) throw new Error("somni_action_try is only available during /somni-dream or /somni-deepsleep");
			if (params.live) return liveTry(params.source ?? "", params.params ?? {}, toolCallId, signal, ctx);
			if (!params.source && !params.inspect) throw new Error("pass source (a word) or inspect (an rpc path)");
			const out = await getBackend().request<any>("action_try", {
				...params,
				tools: pi.getAllTools().map((tool) => tool.name).filter((name) => !state.words.has(name)),
				action_allow: ACTION_ALLOW ?? null,
			});
			return { content: asText(out.inspect ?? out), details: out };
		},
	});

	/** A deep-sleep try: run a draft word against the real tools, recorded, and list the records its calls made. */
	async function liveTry(source: string, params: Record<string, unknown>, toolCallId: string,
			signal: AbortSignal | undefined, ctx: any) {
		if (state.dream?.kind !== "deepsleep") throw new Error("live tries are only for /somni-deepsleep");
		const name = /def\s+([a-z][a-z0-9_]{0,39})\s*\(/.exec(source)?.[1];
		if (!name) throw new Error("source must define one word: def name(...)");
		const before = (await getBackend().request<{ transcript_count: number }>("status")).transcript_count;
		state.wordCalls.set(toolCallId, name); // so its calls are recorded, with via_word naming the draft
		let outcome: string;
		try {
			const out = await runSteps(name, (history) => getBackend().request<ActionStep>("action_draft_step",
				{ source, params, history, max_calls: ACTION_MAX_CALLS }), toolCallId, signal, ctx,
				(tool) => DEEPSLEEP_TOOLS.includes(tool) || state.words.has(tool), "SOMNI_DEEPSLEEP_TOOLS");
			outcome = `The live try returned: ${(out.content[0] as TextContent | undefined)?.text ?? "null"}`;
		} catch (error) {
			outcome = `The live try failed: ${(error as Error).message}`;
		}
		// The calls' records land as their results come back; wait briefly for the last ones.
		let records: TranscriptRecord[] = [];
		for (let i = 0; i < 20; i++) {
			records = (await getBackend().request<TranscriptRecord[]>("records_since", { count: before }))
				.filter((r) => r.source === "tool_result");
			if (records.length >= (state.wordTimings.get(toolCallId)?.length ?? 0)) break;
			await new Promise((done) => setTimeout(done, 100));
		}
		const timing = (state.wordTimings.get(toolCallId) ?? []).map((c) => `${c.tool} +${c.start_ms}ms ${c.ms}ms`).join(", ");
		return { content: asText(`${outcome}\nTiming: ${timing || "no calls"}\nIts recorded calls (cite their IDs, in ` +
			`order, as the refs of this word's test in define_action):\n${records.map((r) => describeRecord(r, 400)).join("\n")}`),
			details: { records: records.map((r) => r.id) } };
	}

	pi.registerCommand("somni-dream", {
		description: "Consolidate recent transcript into cited memory claims (offline dreaming)",
		handler: (args, ctx) => submitCommand(ctx, async () => {
			const recent = await getBackend().request<Recent>("recent", {
				include_images: ctx.model?.input.includes("image") ?? false,
				record_limit: DREAM_RECORDS, record_bytes: DREAM_RECORD_BYTES, related: !FORK_OF && !DAYDREAM_CHILD,
			});
			if (FORK_OF) return asText(dreamStepPrompt(args?.trim() || DAYDREAM_FOCUS, recent));
			const actionsGuide = ACTIONS ? actionGuide(await getBackend().request<ActionWord[]>("action_list"),
				await getBackend().request<any>("action_catalog", { live: liveTools() }),
				await getBackend().request<Candidate[]>("action_candidates"),
				await getBackend().request<Workbench>("action_workbench")) : "";
			const focus = args?.trim() || "what will help in future sessions with this user and their work: their preferences and corrections, " +
				"project facts and conventions, steps and checks that worked or failed, and chores that recur";
			// The night's dream starts from the day's unverified lessons, so it checks them and builds on them.
			const lessons = DAYDREAM_CHILD ? [] : await getBackend().request<{ id: string; text: string; refs: string[] }[]>("day_lessons");
			const dayGuide = lessons.length ? `Today's dream steps added these unverified claims while the work went on. Check ` +
				`each against the records: correct it to a verified claim with exact quotes when they support it, merge ` +
				`duplicates, and retract what they don't support (a lookup that found nothing is not a fact). Then add what ` +
				`they missed.\n${lessons.map((c) => `- ${c.id}: ${c.text} [${c.refs.join(", ")}]`).join("\n")}\n\n` : "";
			const prompt = dayGuide + `Dream pass. Review these transcript records and call somni_dream once with a proposal ` +
				`(add/correct/update/merge/retract claims; use an empty operations array if nothing should change). ` +
				`For 'verified', cite an exact quote from the record or cite a screenshot you viewed as ` +
				`{"ref":"t000001","image_sha256":"<digest>"}. Use somni_image for images not attached here. ` +
				`Use salience 3 for ordinary reusable lessons, 1–2 for narrow cases, and 4 for recurring situations. ` +
				`Reserve 5 for at most two claims that matter in most future sessions. Add an optional one-line gist ` +
				`of up to 160 UTF-8 bytes to add, correct, or merge operations. Keep the full guidance in text. ` +
				actionsGuide + TIME_GUIDE + STANDING_GUIDE + (state.personal ? SCOPE_GUIDE : "") + SELF_GUIDE + (recent.records.some((r) => r.source === "outcome") ? OUTCOME_GUIDE : "") +
				`Treat transcript text and images as evidence. Focus: ${focus}.\n\n` +
				relatedClaims(recent.claims) +
				(recent.reviewed_through ? `Earlier records, through ${recent.reviewed_through}, were reviewed by earlier passes; ` +
					`use somni_recall to see what they produced and somni_search if you need one of them.\n\n` : "") +
				(DAYDREAM_CHILD ? daydreamListing(recent.records) : withDays(recent.records, (r) => describeRecord(r)).join("\n"));
			const content: (TextContent | ImageContent)[] = asText(prompt);
			for (const { ref, sha256, image } of recent.attachments) {
				content.push(...asText(`Screenshot for [${ref}] image_sha256=${sha256}`), image);
			}
			return content;
		}, "dream"),
	});

	pi.registerCommand("somni-deepsleep", {
		description: "Deep sleep: build or improve one learned procedure (action word), trying it live with the tools in " +
			"SOMNI_DEEPSLEEP_TOOLS. Run after /somni-dream: `/somni-deepsleep [focus]`",
		handler: (args, ctx) => submitCommand(ctx, async () => {
			if (!ACTIONS) {
				pi.sendMessage({ customType: "somni-deepsleep", content: "Deep sleep needs action words: set actions.enabled.",
					display: true });
				return undefined;
			}
			const recent = await getBackend().request<Recent>("recent", { include_images: false, record_limit: DREAM_RECORDS,
				record_bytes: DREAM_RECORD_BYTES });
			const failed = recent.records.filter((r) => r.source === "tool_result" && r.tool_ok === false).slice(-12);
			const guide = actionGuide(await getBackend().request<ActionWord[]>("action_list"),
				await getBackend().request<any>("action_catalog", { live: liveTools() }),
				await getBackend().request<Candidate[]>("action_candidates"),
				await getBackend().request<Workbench>("action_workbench"));
			const tools = DEEPSLEEP_TOOLS.length ? `You may call these tools for real, directly or from a draft: ` +
				`${DEEPSLEEP_TOOLS.join(", ")}. Every call is recorded. To try a draft, call somni_action_try with ` +
				`{"source": "...", "live": true, "params": {...}}: it runs the draft against the real tools, call after call ` +
				`with no model turn between them, and lists the records its calls made. When a live try succeeds, submit ` +
				`define_action (or revise_action) whose test cites those records, in order. ` :
				"No tools are allowed for real (SOMNI_DEEPSLEEP_TOOLS is empty), so build only from recorded calls. ";
			const focus = args?.trim() || "a chore the user asks for again and again, or a task that failed because its " +
				"calls could not follow each other fast enough";
			const prompt = `Deep sleep. Build or improve ONE learned procedure, an action word: one Python ` +
				`function that makes a sequence of tool calls in a single step. Pick the target that will save the most ` +
				`work or do what step-by-step calls cannot: ${focus}. Use a recurring sequence below, a procedure a claim ` +
				`describes, or a failed task below. A failure that says a token or window expired means the calls must ` +
				`run back to back, which a word does and step-by-step calls can't. ${tools}Then call somni_dream once. If ` +
				`nothing is worth building, submit an empty operations array.\n\n${guide}` +
				(failed.length ? `\n\nRecent failed calls:\n${failed.map((r) => describeRecord(r, 400)).join("\n")}` : "");
			return asText(prompt);
		}, "deepsleep"),
	});

	pi.registerCommand("somni-doctor", {
		description: "Check Somni's settings, runtime and memory store offline (no model call): `/somni-doctor [all]`",
		handler: async (args, ctx) => {
			// The doctor re-reads the config files itself, so hide the variables this process filled in from them.
			const env = { ...process.env };
			for (const name of CONFIG.applied) delete env[name];
			const session = { tools: pi.getActiveTools(), writable: state.writable };
			const argv = ["-I", "-B", resolve(HERE, "../somni/doctor.py"), "--cwd", ctx.cwd, "--session-json", JSON.stringify(session)];
			if (args?.trim() === "all") argv.push("--all");
			const report = await new Promise<string>((done) => {
				const child = spawn(process.env.SOMNI_PYTHON ?? "python3", argv, { env, stdio: ["ignore", "pipe", "pipe"] });
				let out = "", err = "";
				child.stdout.on("data", (chunk) => { out += chunk; });
				child.stderr.on("data", (chunk) => { err += chunk; });
				child.on("error", (error) => done(`Somni doctor could not start: ${error.message}`));
				child.on("close", () => done((out || err).trim() + `\n\nOffline: ${resolve(HERE, "../bin/somni")} doctor`));
			});
			// Headless Pi keeps stdout for the model's reply, so the report goes to stderr there.
			if (ctx.mode === "print" || ctx.mode === "json") console.error(report);
			else pi.sendMessage({ customType: "somni-doctor", content: report, display: true });
		},
	});

	pi.registerCommand("somni-share", {
		description: "Draft a skill other people can use from what memory learned about a topic, with the evidence " +
			"behind each guideline: `/somni-share <topic>`. Writes files only.",
		handler: async (args, ctx) => {
			if (!ctx.isIdle()) {
				notify(ctx, "Somni: wait for the current Pi turn to settle first.");
				return;
			}
			const topic = args?.trim() ?? "";
			const material = await getBackend().request<{ guide: string; claims: string; n: number }>("share_material", { topic });
			if (!material.n) {
				pi.sendMessage({ customType: "somni-share", content: topic ? `Somni has no current notes about "${topic}" ` +
					"to share yet. A night /somni-dream turns your sessions into notes." : "Somni has no current notes to share yet.",
					display: true });
				return;
			}
			const content = `${material.guide}\nTopic: ${topic || "what this memory knows best, as one skill"}\n\n` +
				`## Notes to pick from\n${material.claims}\n\nWhen the share is written, tell the user where it is and ` +
				"suggest they read SKILL.md and EVIDENCE.md before sharing them.";
			pi.sendMessage({ customType: "somni-share-input", content, display: false }, { triggerTurn: true });
			if (ctx.mode === "json" || ctx.mode === "print") await ctx.waitForIdle();
		},
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
			const goal = args?.trim() || "what will help in future sessions with this user and their work";
			return `Wiki pass. Goal: ${goal}. ${info.guide} Gather the claims below into pages and call somni_dream once with ` +
					`page operations: {"op":"page","slug":"lowercase-hyphenated or folder/page","title":...,"body":markdown citing [nXXXXXX] and ` +
					`linking [[slug]],"claims":[...]}. Extend an existing page by reusing its slug.\n\n## Existing pages\n${info.pages}\n\n## Claims not yet on a page\n${info.claims}`;
		}, "wiki"),
	});

	pi.on("agent_settled", async () => {
		if (state.dream) await finishDream(state.dream);
	});

	pi.on("message_end", (event: any) => {
		const run = state.dream;
		if (run && event.message?.role === "assistant" && event.message.stopReason === "error") {
			run.providerError = String(event.message.errorMessage ?? "provider error").slice(0, 500);
		}
		const usage = event.message?.role === "assistant" ? event.message.usage : undefined;
		if (!usage) return;
		const total = state.usage;
		for (const key of ["input", "output", "cacheRead", "cacheWrite", "totalTokens"] as const) {
			total[key] += Number.isFinite(usage[key]) ? usage[key] : 0;
		}
		total.cost += Number.isFinite(usage.cost?.total) ? usage.cost.total : 0;
		total.calls += 1;
	});

	pi.on("turn_end", async (_event, ctx) => {
		if (!DAYDREAMS || DAYDREAM_TURNS === 0 || state.dream) return;
		if (++day.turns >= DAYDREAM_TURNS) await maybeDaydream(ctx);
	});

	pi.on("session_start", async (_event, ctx) => {
		if (CONFIG.problems.length && !state.configWarned) {
			state.configWarned = true;
			notify(ctx, `Somni config: ${CONFIG.problems.length} problem(s); those settings keep their previous values. ` +
				`${CONFIG.problems.slice(0, 3).join("; ")}${CONFIG.problems.length > 3 ? "; ..." : ""}. Run /somni-doctor for details.`);
		}
		const workspace = process.env.SOMNI_WORKSPACE ?? join(ctx.cwd, ".pi", "somni");
		state.dream = undefined;
		state.writable = state.contributor = state.peer = state.personal = state.recordWarning = state.indexWarning = false;
		state.wordCalls.clear();
		state.wordTimings.clear();
		state.wordGuideShown = false;
		sentCatalog = "";
		sentNames = new Set();
		mkdirSync(workspace, { recursive: true });
		state.workspace = workspace;
		// Isolate imports and avoid bytecode writes beside the extension.
		const args = ["-I", "-B", STORE_PY, "--workspace", workspace, "--subject-id", SUBJECT, "serve",
			"--image-bytes", IMAGE_BYTES, "--image-max-edge", IMAGE_MAX_EDGE];
		if (process.env.SOMNI_RUN_LABEL !== undefined) args.push(`--run-label=${process.env.SOMNI_RUN_LABEL}`);
		if (process.env.SOMNI_TAGS !== undefined) args.push(`--tags=${process.env.SOMNI_TAGS}`);
		args.push(`--session-label=${ctx.sessionManager.getSessionId().slice(0, 36)}`);
		if (isPeer(ctx)) args.push("--peer");
		const personal = DAYDREAM_CHILD ? undefined : personalPath(ctx);
		if (personal) args.push(`--personal=${personal}`);
		if (RECORD && !DAYDREAM_CHILD) args.push("--record");
		if (DAYDREAM_CHILD) args.push("--daydream");
		if (ACTIONS) args.push("--actions");
		const backend = new Backend(PYTHON, args, (error) => {
			if (state.backend !== backend) return;
			state.backend = undefined;
			state.writable = state.contributor = state.peer = false;
			state.dream = undefined;
			notify(ctx, `Somni backend stopped. ${error.message}`);
		});
		try {
			const { writable, contributor, peer, personal: layered, action_sync: synced } = await backend.ready as
				{ writable: boolean; contributor?: boolean; peer?: boolean; personal?: boolean; action_sync?: any };
			state.personal = Boolean(layered);
			state.backend = backend;
			state.writable = writable;
			state.contributor = Boolean(contributor);
			state.peer = Boolean(peer);
			lastCtx = ctx;
			if (synced?.error) notify(ctx, `Somni could not record your action word edits. ${synced.error}`);
			else if (synced) {
				const rejected = (synced.rejected ?? []).map((r: { reason: string }) => r.reason).join("; ");
				notify(ctx, `Somni recorded ${synced.action_count ?? 0} action word edit(s) as pass ${synced.id}.` +
					(rejected ? ` Rejected: ${rejected}` : ""));
			}
			await registerWords(ctx);
			await recordToolCatalog(ctx);
			// Quitting between daydreams leaves the last turns undreamed: dream them now, beside this session's first turns.
			if (DAYDREAMS && writable && await undreamedTurns()) void maybeDaydream(ctx, true);
			if (DAYDREAMS && DAYDREAM_MINUTES > 0 && writable) {
				if (day.timer) clearInterval(day.timer);
				day.timer = setInterval(() => { if (lastCtx && !state.dream) void maybeDaydream(lastCtx); }, DAYDREAM_MINUTES * 60_000);
				day.timer.unref?.();
			}
			if (RECORD && !writable && !state.contributor) {
				notify(ctx, "Somni opened without recording because another Pi session is recording in this Somni workspace.");
			}
		} catch (error) {
			await backend.close();
			notify(ctx, `Somni could not open its memory store. ${(error as Error).message}`);
		}
	});

	pi.on("session_shutdown", async () => {
		if (day.timer) clearInterval(day.timer);
		day.timer = undefined;
		day.child?.kill("SIGTERM");
		const backend = state.backend;
		state.backend = undefined;
		state.writable = state.contributor = state.peer = false;
		state.dream = undefined;
		await backend?.close();
	});

	pi.on("before_agent_start", async (event, ctx) => {
		if (!state.backend || state.dream) return undefined;
		await refreshRole(ctx).catch(() => undefined);
		if (ACTIONS) {
			await registerWords(ctx);
			await recordToolCatalog(ctx); // MCP tools often register after session_start
		}
		const prompt = event.messageType === "custom" ? undefined : event.prompt;
		const options = { index_bytes: INJECT_BYTES, query: prompt ? utf8Prefix(prompt, MAX_INDEX_QUERY_BYTES) : undefined,
			...(ACTIONS && state.words.size ? { action_words: [...state.words.keys()] } : {}) };
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
		if (day.child && !day.notice) {
			// A daydream that has committed its pass but not yet exited is announced now, not a turn later.
			try {
				const pass = (await getBackend().request<{ last_pass: any }>("status")).last_pass;
				day.notice = livePassNotice(pass) ?? "";
				// The exit handler skips a pass announced here, so the user's notice is shown here too.
				if (day.notice) {
					notify(ctx, day.notice);
					day.lessons = await passLessons(pass);
				}
			} catch {
				// The worker's exit handler reports it instead.
			}
		}
		const lessons = day.notice && day.lessons ? `\nWhat this dream step added to memory (unverified claims until the night's ` +
			`dream checks them):\n${day.lessons}` : "";
		const notice = day.notice ? `${day.notice}${lessons}\n\n` : "";
		day.notice = "";
		day.lessons = "";
		if (!/^-(?: (?:p:)?n\d{6} | page `| word `)/m.test(index)) {
			return notice ? { message: { customType: "somni-index", content: notice.trim(), display: false } } : undefined;
		}
		let guide = "";
		if (ACTIONS && !state.wordGuideShown && /^- word `/m.test(index) && !pi.getActiveTools().includes("read")) {
			state.wordGuideShown = true;
			guide = `\n\n${WORD_GUIDE}`;
		}
		return { message: { customType: "somni-index", content: notice + index + guide, display: false } };
	});

	// A forked daydream keeps its session's prompt cache key, so its requests can reuse the session's cached prefix.
	// Providers also route by a session header (x-session-id, x-session-affinity); a fork's own ID would send its
	// requests away from the cache that holds the session's prefix, so the fork sends its session's ID there too.
	if (FORK_OF) {
		pi.on("before_provider_headers", (event) => {
			const headers = event.headers as Record<string, string | null>;
			for (const name of Object.keys(headers)) {
				if (/^(x-session-id|x-session-affinity|session[-_]id)$/i.test(name) && headers[name]) headers[name] = FORK_OF;
			}
		});
		pi.on("before_provider_request", (event) => {
			// Set in place and return nothing: another extension that rebuilds the payload from its own copy
			// (one that wraps tools, say) then still sends this key.
			const payload = event.payload as { prompt_cache_key?: string };
			if (payload && typeof payload === "object" && payload.prompt_cache_key) payload.prompt_cache_key = FORK_OF.slice(0, 64);
			return undefined;
		});
	}

	pi.on("tool_call", (event) => {
		const run = state.dream;
		if (!run) return;
		if (run.kind === "deepsleep" && DEEPSLEEP_TOOLS.includes(event.toolName) && !run.result) return; // a real try
		if (!DREAM_TOOLS.has(event.toolName) || run.result || run.submitting) {
			return { block: true, reason: "Offline dreams may only inspect Somni evidence and submit one pass." };
		}
		if (event.toolName === "somni_dream") {
			if (++run.submissions > DREAM_MAX_SUBMISSIONS) {
				run.exhausted = "submission";
				return { block: true, reason: `Dream submission limit reached (${DREAM_MAX_SUBMISSIONS}); this dream ends without a pass.` };
			}
		} else if (++run.reads > DREAM_MAX_READS) {
			run.exhausted = "read";
			return { block: true, reason: `Dream evidence read limit reached (${DREAM_MAX_READS} reads). Submit the pass now with ` +
				"somni_dream; you can still cite every record you have already read." };
		}
	});

	// Each Somni tool result in a dream ends with the budget that is left, so the dreamer can plan its reads.
	pi.on("tool_result", async (event) => {
		const run = state.dream;
		if (!run || event.parentToolCallId || !DREAM_TOOLS.has(event.toolName)) return;
		if (DREAM_CACHE_SECONDS && !event.isError && event.toolName !== "somni_dream") {
			const text = (event.content ?? []).filter((part: any) => part.type === "text").map((part: any) => part.text).join("");
			const shown = event.toolName === "somni_image" ? [event.details as { ref: string; sha256: string }] : [];
			try {
				await getBackend().request("dream_cache_put", { kind: run.kind, tool: event.toolName, args: event.input ?? {}, text,
					...(shown.length && shown[0]?.sha256 ? { images: shown.map(({ ref, sha256 }) => ({ ref, sha256 })) } : {}) });
			} catch {
				// The cache only saves a retry some turns; never fail the read over it.
			}
		}
		let images = "";
		try {
			const used = await getBackend().request<number>("dream_images", {});
			images = `images ${Math.max(0, DREAM_MAX_IMAGES - used)}/${DREAM_MAX_IMAGES}, `;
		} catch {
			// The budget line is advisory; a backend hiccup must not fail the tool result.
		}
		const left = Math.max(0, DREAM_MAX_SUBMISSIONS - run.submissions);
		let line = `[dream budget left: ${images}reads ${Math.max(0, DREAM_MAX_READS - Math.min(run.reads, DREAM_MAX_READS))}/` +
			`${DREAM_MAX_READS}, submissions ${left}/${DREAM_MAX_SUBMISSIONS}]`;
		if (event.toolName === "somni_dream" && event.isError) {
			line += left ? ` Nothing was committed. Fix or drop the rejected operations and resubmit; ${left} attempt${left === 1 ? "" : "s"} left.`
				: " No attempts left; this dream ends without a pass.";
		} else if (run.reads >= DREAM_MAX_READS && !run.result) {
			line += " No reads left: submit the pass now.";
		}
		return { content: [...(event.content ?? []), ...asText(line)] };
	});

	pi.on("tool_result", async (event, ctx) => {
		const calls = state.wordTimings.get(event.toolCallId);
		state.wordTimings.delete(event.toolCallId);
		// Calls an action word makes are recorded too: they are the evidence for revising or composing words.
		if (event.parentToolCallId && !state.wordCalls.has(event.parentToolCallId)) return;
		if (!canRecord() || event.toolName.startsWith("somni_")) return;
		// In one-shot runs MCP tools register after the turn starts; record their declarations once they are in use.
		if (ACTIONS && !sentNames.has(event.toolName) && !state.words.has(event.toolName)) await recordToolCatalog(ctx);
		const { text, options } = toolRecord(event.toolName, event.input, event.content);
		const via = event.parentToolCallId ? state.wordCalls.get(event.parentToolCallId) : undefined;
		await record("tool_result", text, ctx, { ...options, tool_ok: event.isError === false, ...(calls?.length ? { calls } : {}),
			...(via ? { via_word: via } : {}), ...(state.words.has(event.toolName) ? { word: true } : {}) });
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
