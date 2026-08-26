/**
 * The metrics event arrives after 'final' and must survive into the result.
 *
 * The caller applies `final` wholesale once the stream ends, so anything a
 * consumer patches in from its own event handler is overwritten a moment
 * later. Merging inside streamQuery is what makes the late event stick — and
 * the chat export depends on it, because the export is built from client state
 * while the Eval page reads the same numbers from the database.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import { streamQuery } from './api';

function ndjsonResponse(lines: object[]) {
	const body = new ReadableStream<Uint8Array>({
		start(controller) {
			const enc = new TextEncoder();
			for (const l of lines) controller.enqueue(enc.encode(JSON.stringify(l) + '\n'));
			controller.close();
		}
	});
	return new Response(body, { status: 200 });
}

const CHUNK = { text: 'Die Ausstiegsseite wird durch Antippen der Pfeile geaendert.', score: 0.9 };

function stream(extra: object[] = []) {
	return ndjsonResponse([
		{ type: 'started', stage: 'manager' },
		{
			type: 'final',
			answer: 'Antwort [1].',
			audit: { processing_ms: 28000 },
			global_chunks: [{ ...CHUNK }],
			agent_steps: [{ step: 1, chunks: [{ ...CHUNK }] }],
			subagents: [{ role: 'facts', agent_steps: [{ step: 1, chunks: [{ ...CHUNK }] }] }]
		},
		...extra
	]);
}

const METRICS = {
	type: 'chunk_metrics',
	post_response_ms: 5280,
	by_text_prefix: {
		[CHUNK.text.trim().slice(0, 200)]: { similarity_to_rank_1: 0.98, answer_similarity: 0.71 }
	}
};

afterEach(() => vi.unstubAllGlobals());

async function run(extra: object[] = []) {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue(stream(extra)));
	return (await streamQuery('Wie?', 'wiener_linien', 's', 'default')) as Record<string, unknown>;
}

describe('streamQuery + chunk_metrics', () => {
	it('merges the late metrics into every place a chunk lives', async () => {
		const result = await run([METRICS]);
		const globalChunk = (result.global_chunks as Record<string, number>[])[0];
		const stepChunk = (result.agent_steps as { chunks: Record<string, number>[] }[])[0].chunks[0];
		const subChunk = (result.subagents as { agent_steps: { chunks: Record<string, number>[] }[] }[])[0]
			.agent_steps[0].chunks[0];

		for (const c of [globalChunk, stepChunk, subChunk]) {
			expect(c.similarity_to_rank_1).toBe(0.98);
			expect(c.answer_similarity).toBe(0.71);
		}
		expect((result.audit as Record<string, number>).post_response_ms).toBe(5280);
	});

	it('returns the answer even when no metrics event follows', async () => {
		const result = await run();
		expect(result.answer).toBe('Antwort [1].');
		expect((result.global_chunks as Record<string, unknown>[])[0].similarity_to_rank_1).toBeUndefined();
	});

	it('ignores a metrics event whose chunks it cannot match', async () => {
		const result = await run([
			{ ...METRICS, by_text_prefix: { 'ein ganz anderer Text': { similarity_to_rank_1: 1 } } }
		]);
		expect((result.global_chunks as Record<string, unknown>[])[0].similarity_to_rank_1).toBeUndefined();
		expect(result.answer).toBe('Antwort [1].');
	});
});
