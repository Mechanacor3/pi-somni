import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";

type Reply = { id: number; result?: unknown; error?: string };

/** The settings the store itself reads. The backend gets these and nothing else from Pi's environment. */
const STORE_ENV = ["SOMNI_LINK_STYLE", "SOMNI_NOW", "SOMNI_FORGET_AFTER_DAYS", "SOMNI_MEMORY_DECAY", "SOMNI_DREAM_RECORDS", "SOMNI_RUN_LABEL"];
type Pending = { resolve: (value: unknown) => void; reject: (error: Error) => void };

export class Backend {
	readonly ready: Promise<{ writable: boolean }>;
	private readonly child: ChildProcessWithoutNullStreams;
	private readonly exited: Promise<void>;
	private readonly pending = new Map<number, Pending>();
	private serial = 0;
	private stderr = "";
	private closing = false;
	private failure?: Error;

	constructor(python: string, args: string[], onExit: (error: Error) => void) {
		const settings = Object.fromEntries(STORE_ENV.flatMap((name) => process.env[name] === undefined ? [] : [[name, process.env[name]]]));
		this.child = spawn(python, args, { env: { PATH: process.env.PATH ?? "/usr/bin:/bin", LC_ALL: "C.UTF-8", ...settings } });
		this.ready = this.expect<{ writable: boolean }>(0, 30_000);
		this.child.stdin.on("error", (error) => this.fail(error));
		this.child.stderr.setEncoding("utf8").on("data", (chunk: string) => {
			this.stderr = (this.stderr + chunk).slice(-500);
		});
		const output = createInterface({ input: this.child.stdout, crlfDelay: Infinity });
		output.on("line", (line) => {
			try {
				const reply = JSON.parse(line) as Reply;
				const pending = this.pending.get(reply.id);
				if (!pending) throw new Error("Unexpected Somni backend reply");
				if (reply.error !== undefined) pending.reject(new Error(reply.error));
				else pending.resolve(reply.result);
			} catch (error) {
				this.fail(error as Error);
				this.child.kill();
			}
		});
		this.child.once("error", (error) => this.fail(error));
		this.exited = new Promise((done) => {
			this.child.once("close", (code, signal) => {
				const error = this.failure ?? new Error(this.stderr.trim() || `Somni backend exited (${code ?? signal})`);
				this.fail(error);
				if (!this.closing) onExit(error);
				done();
			});
		});
	}

	private expect<T>(id: number, timeoutMs: number): Promise<T> {
		return new Promise((resolve, reject) => {
			const timeout = setTimeout(() => {
				this.fail(new Error("Somni backend timed out"));
				this.child.kill();
			}, timeoutMs);
			const finish = () => {
				clearTimeout(timeout);
				this.pending.delete(id);
			};
			this.pending.set(id, {
				resolve: (value) => { finish(); resolve(value as T); },
				reject: (error) => { finish(); reject(error); },
			});
		});
	}

	private fail(error: Error) {
		this.failure ??= error;
		for (const pending of this.pending.values()) pending.reject(error);
	}

	request<T>(method: string, args: object = {}): Promise<T> {
		if (this.failure || this.closing) return Promise.reject(this.failure ?? new Error("Somni backend is closing"));
		const id = ++this.serial;
		const message = JSON.stringify({ id, method, args }) + "\n";
		const promise = this.expect<T>(id, 120_000);
		this.child.stdin.write(message);
		return promise;
	}

	async close() {
		this.closing = true;
		this.child.stdin.end();
		const timeout = setTimeout(() => this.child.kill(), 5000);
		try {
			await this.exited;
		} finally {
			clearTimeout(timeout);
		}
	}
}
