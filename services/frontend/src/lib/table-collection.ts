/**
 * Where uploaded tables go.
 *
 * Tables bypass the structuring service, so no plugin routes them. They must
 * land in a collection the use case's search reads – search is hard-isolated
 * to collections starting with one of the use case's prefixes. "<id>_default"
 * keeps working where it always did (neumann, gw_stpoelten, ustp); a use case
 * with its own prefix (wiener_linien → "wl_") gets "<prefix>tabellen".
 *
 * Not the use case's default collection: for GW St. Pölten that is
 * gw_cnc_steps, and table rows there would feed the tool recommendation.
 */
export function tableCollection(useCase: string, prefixes: string[]): string {
	const effective = prefixes.length ? prefixes : [`${useCase}_`];
	const legacy = `${useCase}_default`;
	if (effective.some((p) => legacy.startsWith(p))) return legacy;
	return `${effective[0]}tabellen`;
}
