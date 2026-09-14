/**
 * POST /api/ingest/gw/plan — upload plan for GW St. Pölten data folders.
 *
 * The browser sends only the relative paths of the folders the user picked;
 * the data-structure service decides which products they hold and which files
 * belong in the upload. Keeping that logic in one place means the frontend
 * never re-implements the classification rules.
 *
 * On top, this route settles each product's machine against the index (see
 * planLocation): a collecting folder is not a machine, a bare product folder
 * takes its machine over, and ambiguous cases become a choice for the user.
 */
import { json } from '@sveltejs/kit';
import { SERVICES } from '$lib/server/services';
import { knownLocations, knownMachines } from '$lib/server/gw-products';
import { planLocation, unionByFile, type Location, type MachineChoice } from '$lib/gw-location';
import type { RequestHandler } from './$types';

interface PlannedProduct {
	product_id: string;
	machine: string | null;
	category: string | null;
	cnc_candidates: string[];
	einstellblaetter: string[];
	bilder: string[];
	stueckliste: string | null;
	upload_paths: string[];
	warnings: string[];
	machine_choices?: MachineChoice[];
}

const LIST_FIELDS = ['cnc_candidates', 'einstellblaetter', 'bilder'] as const;

const ALSO_ON = 'Produkt liegt auch auf';

export const POST: RequestHandler = async ({ request }) => {
	const body = await request.text();
	let resp: Response;
	try {
		resp = await fetch(`${SERVICES.dataStructure}/v1/structure/plan`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body
		});
	} catch (err) {
		return json(
			{
				error: 'upstream_unavailable',
				detail: `Strukturierungsdienst nicht erreichbar: ${err instanceof Error ? err.message : String(err)}`
			},
			{ status: 502 }
		);
	}
	const plan = await resp.json().catch(() => ({}));
	if (!resp.ok) return json(plan, { status: resp.status });
	const products = (plan.products ?? []) as PlannedProduct[];

	// 1. Settle each product's machine against the index.
	let allMachines: Location[] = [];
	try {
		allMachines = await knownMachines();
	} catch {
		// Index unreachable: trust the folder names as the paths describe them.
	}
	for (const product of products) {
		// The service's "also on" note was computed from folder names that may
		// turn out not to be machines – recomputed below.
		product.warnings = product.warnings.filter((w) => !w.startsWith(ALSO_ON));
		let known: Location[] = [];
		try {
			known = await knownLocations(product.product_id);
		} catch {
			// no index – decide on the path alone
		}
		const { location, choices, notes } = planLocation({
			pathMachine: product.machine,
			pathCategory: product.category,
			known,
			allMachines
		});
		product.machine = location?.machine ?? null;
		product.category = location?.category ?? (location ? null : product.category);
		if (choices.length) product.machine_choices = choices;
		product.warnings.push(...notes);
	}

	// 2. Picking "Chiron 243" and, again, its folder "11221.7199" on its own
	//    yields the same product twice once the machine is settled. Merge –
	//    two rows would upload it twice, the second replacing the first.
	const merged = new Map<string, PlannedProduct>();
	for (const product of products) {
		const key = product.machine ? `${product.product_id}|${product.machine}` : `${product.product_id}|?${merged.size}`;
		const existing = merged.get(key);
		if (!existing) {
			merged.set(key, product);
			continue;
		}
		// The same folder picked twice (as part of its machine and on its own)
		// must not send every PDF and photo twice.
		for (const field of LIST_FIELDS) {
			existing[field] = unionByFile(product.product_id, existing[field], product[field]);
		}
		existing.stueckliste = existing.stueckliste ?? product.stueckliste;
		existing.upload_paths = [
			...existing.cnc_candidates,
			...(existing.stueckliste ? [existing.stueckliste] : [])
		];
		existing.warnings = [
			...existing.warnings.filter((w) => !w.startsWith('Maschine aus dem Bestand')),
			'Ordner mehrfach gewählt – zusammengeführt'
		];
	}

	// 3. "Also on" from the settled machines.
	const result = [...merged.values()];
	const machinesByProduct = new Map<string, string[]>();
	for (const p of result) {
		if (p.machine) machinesByProduct.set(p.product_id, [...(machinesByProduct.get(p.product_id) ?? []), p.machine]);
	}
	for (const p of result) {
		const others = (machinesByProduct.get(p.product_id) ?? []).filter((m) => m !== p.machine);
		if (others.length) p.warnings.push(`${ALSO_ON}: ${others.join(', ')}`);
	}

	plan.products = result;
	return json(plan);
};
