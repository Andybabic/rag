export interface UseCaseDef {
	slug: string; // URL path: /neumann, /gw-stpoelten, ...
	apiId: string; // Backend ID: neumann, gw_stpoelten, ...
	label: string;
	desc: string;
	color: string; // Tailwind bg class
	accent: string; // Hex for borders etc.
}

export const USE_CASES: UseCaseDef[] = [
	{
		slug: 'neumann',
		apiId: 'neumann',
		label: 'Firma Neumann',
		desc: 'Maschinenwartung',
		color: 'bg-blue-600',
		accent: '#2563eb'
	},
	{
		slug: 'gw-stpoelten',
		apiId: 'gw_stpoelten',
		label: 'GW St. Poelten',
		desc: 'CNC-Ruestung',
		color: 'bg-emerald-600',
		accent: '#059669'
	},
	{
		slug: 'wiener-linien',
		apiId: 'wiener_linien',
		label: 'Wiener Linien',
		desc: 'Wissensassistent',
		color: 'bg-purple-600',
		accent: '#7c3aed'
	},
	{
		slug: 'ustp',
		apiId: 'ustp',
		label: 'USTP',
		desc: 'Neuer Use Case',
		color: 'bg-amber-600',
		accent: '#d97706'
	}
];

const bySlug = new Map(USE_CASES.map((uc) => [uc.slug, uc]));
const byApiId = new Map(USE_CASES.map((uc) => [uc.apiId, uc]));

export function getUseCaseBySlug(slug: string): UseCaseDef | undefined {
	return bySlug.get(slug);
}

export function getUseCaseByApiId(apiId: string): UseCaseDef | undefined {
	return byApiId.get(apiId);
}

export function isValidSlug(slug: string): boolean {
	return bySlug.has(slug);
}
