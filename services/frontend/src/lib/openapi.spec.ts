import { describe, expect, it } from 'vitest';
import {
	curlFor,
	deref,
	listOperations,
	propertyRows,
	refName,
	responseSchema,
	sampleValue,
	typeLabel,
	type OpenApiSpec
} from './openapi';

/**
 * A miniature of what the pipeline actually emits — including the shapes that
 * trip up naive readers: Pydantic's `anyOf` for optional fields, `$ref`s into
 * components, and model-level examples.
 */
const spec: OpenApiSpec = {
	openapi: '3.1.0',
	info: { title: 'Kermit Pipeline API', version: '1.0.0', description: 'Ask the pipeline.' },
	paths: {
		'/api/v1/chat': {
			post: {
				summary: 'Ask a question',
				description: 'Queue a question.',
				requestBody: {
					content: { 'application/json': { schema: { $ref: '#/components/schemas/ChatRequest' } } }
				},
				responses: {
					'202': {
						description: 'Accepted',
						content: {
							'application/json': { schema: { $ref: '#/components/schemas/ChatAccepted' } }
						}
					},
					'401': { description: 'Token missing' }
				}
			}
		},
		'/api/v1/chat/{request_id}': {
			get: {
				summary: 'Poll the state',
				parameters: [
					{
						name: 'request_id',
						in: 'path',
						required: true,
						description: 'The id returned when asking.',
						schema: { type: 'string', examples: ['9c1d3f2a-0000-4000-8000-000000000000'] }
					}
				],
				responses: {
					'200': {
						description: 'OK',
						content: {
							'application/json': { schema: { $ref: '#/components/schemas/ChatAccepted' } }
						}
					}
				}
			}
		}
	},
	components: {
		schemas: {
			ChatRequest: {
				type: 'object',
				required: ['use_case', 'query'],
				properties: {
					use_case: { type: 'string', description: 'The use case.', examples: ['gw_stpoelten'] },
					query: { type: 'string', description: 'The question.' },
					session_id: {
						anyOf: [{ type: 'string' }, { type: 'null' }],
						description: 'Existing conversation.'
					},
					role: { type: 'string', default: 'default', description: 'Perspective.' },
					images: { type: 'array', items: { type: 'string' }, description: 'Base64 images.' }
				},
				examples: [{ use_case: 'gw_stpoelten', query: 'Welches Werkzeug?' }]
			},
			ChatAccepted: {
				type: 'object',
				required: ['request_id', 'status'],
				properties: {
					request_id: { type: 'string', examples: ['9c1d3f2a-0000-4000-8000-000000000000'] },
					status: { type: 'string', examples: ['queued'] },
					image_count: { type: 'integer' },
					error: { anyOf: [{ type: 'string' }, { type: 'null' }], description: 'Failure reason.' }
				}
			}
		}
	}
};

describe('deref', () => {
	it('resolves a $ref into components.schemas', () => {
		expect(deref({ $ref: '#/components/schemas/ChatRequest' }, spec).type).toBe('object');
	});

	it('returns an inline schema untouched', () => {
		expect(deref({ type: 'string' }, spec)).toEqual({ type: 'string' });
	});

	it('does not loop forever on a self-referencing $ref', () => {
		const cyclic: OpenApiSpec = {
			components: { schemas: { Loop: { $ref: '#/components/schemas/Loop' } } }
		};
		expect(deref({ $ref: '#/components/schemas/Loop' }, cyclic)).toBeDefined();
	});
});

describe('typeLabel', () => {
	it('names the model behind a $ref', () => {
		expect(typeLabel({ $ref: '#/components/schemas/ChatAccepted' }, spec)).toBe('ChatAccepted');
	});

	it('renders an optional field as a readable union', () => {
		// Pydantic emits `str | None` as anyOf — accurate, unreadable in a table.
		expect(typeLabel({ anyOf: [{ type: 'string' }, { type: 'null' }] }, spec)).toBe(
			'string | null'
		);
	});

	it('renders arrays with their item type', () => {
		expect(typeLabel({ type: 'array', items: { type: 'string' } }, spec)).toBe('string[]');
	});

	it('parenthesises a union inside an array so it cannot be misread', () => {
		expect(
			typeLabel({ type: 'array', items: { anyOf: [{ type: 'string' }, { type: 'null' }] } }, spec)
		).toBe('(string | null)[]');
	});
});

describe('sampleValue', () => {
	it('prefers the example declared next to the field', () => {
		expect(sampleValue({ type: 'string', examples: ['gw_stpoelten'] }, spec)).toBe('gw_stpoelten');
	});

	it('uses a model-level example for the whole body', () => {
		// This is the example an integrator should copy, so it must win over a
		// property-by-property reconstruction.
		expect(sampleValue({ $ref: '#/components/schemas/ChatRequest' }, spec)).toEqual({
			use_case: 'gw_stpoelten',
			query: 'Welches Werkzeug?'
		});
	});

	it('falls back to defaults, then to type placeholders', () => {
		expect(sampleValue({ type: 'string', default: 'default' }, spec)).toBe('default');
		expect(sampleValue({ type: 'integer' }, spec)).toBe(0);
		expect(sampleValue({ type: 'boolean' }, spec)).toBe(true);
	});

	it('skips the null branch of an optional field', () => {
		expect(sampleValue({ anyOf: [{ type: 'string' }, { type: 'null' }] }, spec)).toBe('string');
	});

	it('builds an object from its properties when no example is given', () => {
		const built = sampleValue({ $ref: '#/components/schemas/ChatAccepted' }, spec) as Record<
			string,
			unknown
		>;
		expect(built.status).toBe('queued');
		expect(built.image_count).toBe(0);
		expect(built.error).toBe('string');
	});

	it('stops recursing instead of hanging on a self-referencing model', () => {
		const cyclic: OpenApiSpec = {
			components: {
				schemas: {
					Node: {
						type: 'object',
						properties: { child: { $ref: '#/components/schemas/Node' } }
					}
				}
			}
		};
		expect(sampleValue({ $ref: '#/components/schemas/Node' }, cyclic)).toBeDefined();
	});
});

describe('propertyRows', () => {
	it('marks required fields and carries descriptions and defaults', () => {
		const rows = propertyRows({ $ref: '#/components/schemas/ChatRequest' }, spec);
		const byName = Object.fromEntries(rows.map((r) => [r.name, r]));

		expect(byName.use_case.required).toBe(true);
		expect(byName.use_case.description).toBe('The use case.');
		expect(byName.session_id.required).toBe(false);
		expect(byName.session_id.type).toBe('string | null');
		expect(byName.role.defaultValue).toBe('"default"');
	});
});

describe('listOperations', () => {
	it('flattens paths and methods into one list', () => {
		const ops = listOperations(spec);
		expect(ops.map((o) => `${o.method} ${o.path}`)).toEqual([
			'POST /api/v1/chat',
			'GET /api/v1/chat/{request_id}'
		]);
	});

	it('sorts status codes so 2xx comes before the failure modes', () => {
		const [post] = listOperations(spec);
		expect(post.responses.map((r) => r.status)).toEqual(['202', '401']);
	});

	it('picks the 2xx body as the documented response shape', () => {
		expect(refName(responseSchema(listOperations(spec)[0]))).toBe('ChatAccepted');
	});
});

describe('curlFor', () => {
	it('builds a POST call with the declared example body', () => {
		const [post] = listOperations(spec);
		const curl = curlFor(post, spec, 'https://kermit.example.org', 'kmt_secret');

		expect(curl).toContain('-X POST "https://kermit.example.org/api/v1/chat"');
		expect(curl).toContain('Authorization: Bearer kmt_secret');
		expect(curl).toContain('Content-Type: application/json');
		expect(curl).toContain('"use_case": "gw_stpoelten"');
	});

	it('substitutes path parameters with their example', () => {
		const get = listOperations(spec)[1];
		const curl = curlFor(get, spec, 'https://kermit.example.org', '');

		expect(curl).toContain('/api/v1/chat/9c1d3f2a-0000-4000-8000-000000000000');
		expect(curl).not.toContain('{request_id}');
		// No token pasted: fall back to a shell variable rather than a fake secret.
		expect(curl).toContain('Bearer $KERMIT_TOKEN');
		// A GET has no body to send.
		expect(curl).not.toContain('-d ');
	});

	it('tolerates a trailing slash on the base URL', () => {
		const [post] = listOperations(spec);
		expect(curlFor(post, spec, 'https://kermit.example.org/', 'x')).toContain(
			'"https://kermit.example.org/api/v1/chat"'
		);
	});
});
