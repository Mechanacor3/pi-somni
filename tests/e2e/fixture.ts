/** Test-only fixture: a deterministic tool that returns JSON, standing in for any API tool. */
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	pi.registerTool({
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
}
