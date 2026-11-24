/** Deterministic tool results used by the offline Pi tests. */
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	// FIXTURE_SESSION_HEADER=1 routes like a provider that sends the session ID as a header (OpenAI, for one).
	if (process.env.FIXTURE_SESSION_HEADER === "1") {
		pi.on("before_provider_headers", (event, ctx) => {
			event.headers["x-session-id"] = ctx.sessionManager.getSessionId();
		});
	}
	// FIXTURE_LATE_TOOL=1 registers late_tool only once the first turn starts, as MCP tools do in one-shot runs.
	let late = false;
	pi.on("before_agent_start", () => {
		if (process.env.FIXTURE_LATE_TOOL !== "1" || late) return;
		late = true;
		pi.registerTool({
			name: "late_tool", label: "Late tool", description: "A tool that registers after the session starts.",
			parameters: Type.Object({ n: Type.Integer() }),
			async execute(_id: string, params: { n: number }) {
				return { content: [{ type: "text" as const, text: JSON.stringify({ n: params.n }) }], details: {} };
			},
		} as any);
		pi.setActiveTools([...pi.getActiveTools(), "late_tool"]);
	});
	// FIXTURE_NO_FEE_TOOL=1 leaves lookup_fee out, like a dream session without the task's MCP server.
	if (process.env.FIXTURE_NO_FEE_TOOL !== "1") pi.registerTool({
		name: "lookup_fee",
		label: "Lookup fee",
		description: "Return the cancellation fee for a fare.",
		parameters: Type.Object({ cabin: Type.String(), price: Type.Number() }),
		async execute(_id: string, params: { cabin: string; price: number }) {
			const fee = params.cabin === "economy" ? Math.max(50, 0.15 * params.price) : 0.08 * params.price;
			const result = { cabin: params.cabin, fee, status: "preview" };
			return { content: [{ type: "text" as const, text: JSON.stringify(result) }], details: result };
		},
	});
	pi.registerTool({
		name: "screenshot",
		label: "Screenshot",
		description: "Return screenshot metadata and a tiny PNG.",
		parameters: Type.Object({}),
		async execute() {
			const details = { frame_time: 100, frame_age_ms: 25 };
			return { content: [
				{ type: "text" as const, text: JSON.stringify(details) },
				{ type: "image" as const, mimeType: "image/png",
					data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMInhkAAAJ/AT1GZIqVAAAAAElFTkSuQmCC" },
			], details };
		},
	});
	for (const name of ["stream_read", "large_result"]) {
		pi.registerTool({
			name,
			label: name,
			description: "Return a large payload.",
			parameters: Type.Object({}),
			async execute() {
				const text = name === "stream_read" ? "ABCD".repeat(524288) : "🌱".repeat(2048);
				return { content: [{ type: "text" as const, text }], details: {} };
			},
		});
	}
}
