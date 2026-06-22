export interface UseCaseDef {
	slug: string; // URL path: /neumann, /gw-stpoelten, ...
	apiId: string; // Backend ID: neumann, gw_stpoelten, ...
	label: string;
	desc: string;
	color: string; // Tailwind bg class
	accent: string; // Hex for borders etc.
	roles?: string[];
}

// Fallback list, used before the DB-backed list has hydrated (and as a safety
// net if the use-case endpoint is unreachable). The live list comes from the
// backend via the root layout load → setUseCases(); see /api/use-cases.
const FALLBACK_USE_CASES: UseCaseDef[] = [
	{ slug: 'neumann', apiId: 'neumann', label: 'Firma Neumann', desc: 'Maschinenwartung', color: 'bg-blue-600', accent: '#2563eb' },
	{ slug: 'gw-stpoelten', apiId: 'gw_stpoelten', label: 'GW St. Poelten', desc: 'CNC-Ruestung', color: 'bg-emerald-600', accent: '#059669' },
	{ slug: 'wiener-linien', apiId: 'wiener_linien', label: 'Wiener Linien', desc: 'Wissensassistent', color: 'bg-purple-600', accent: '#7c3aed' },
	{ slug: 'ustp', apiId: 'ustp', label: 'USTP', desc: 'Neuer Use Case', color: 'bg-amber-600', accent: '#d97706' }
];

// Mutated in place so existing `{#each USE_CASES}` references stay valid.
export const USE_CASES: UseCaseDef[] = [...FALLBACK_USE_CASES];

let bySlug = new Map(USE_CASES.map((uc) => [uc.slug, uc]));
let byApiId = new Map(USE_CASES.map((uc) => [uc.apiId, uc]));

/** Replace the in-memory use-case list (called once on app load from the DB). */
export function setUseCases(list: UseCaseDef[] | undefined | null): void {
	if (!list || list.length === 0) return; // keep fallback on empty/error
	USE_CASES.splice(0, USE_CASES.length, ...list);
	bySlug = new Map(USE_CASES.map((uc) => [uc.slug, uc]));
	byApiId = new Map(USE_CASES.map((uc) => [uc.apiId, uc]));
}

export function getUseCaseBySlug(slug: string): UseCaseDef | undefined {
	return bySlug.get(slug);
}

export function getUseCaseByApiId(apiId: string): UseCaseDef | undefined {
	return byApiId.get(apiId);
}

export function isValidSlug(slug: string): boolean {
	return bySlug.has(slug);
}
