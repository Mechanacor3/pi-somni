/** Deterministic tool results used by the offline Pi tests. */
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
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
