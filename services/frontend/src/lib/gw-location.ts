/**
 * Where a GW product lives: machine and category.
 *
 * In the data set's tree the folder above a product folder is its machine
 * ("Fräsen groß/Chiron 243/11221.7199"). But the browser hands over paths
 * starting at whatever folder the user picked – "Neue Produkte/11221.7199"
 * would turn "Neue Produkte" into a machine, and the re-upload would sit next
 * to the real copy instead of replacing it. So a folder name only counts as a
 * machine when the index knows that machine; otherwise the product's machine
 * is taken over from the index, or the user picks.
 */

export interface Location {
	machine: string | null;
	category: string | null;
}

export interface MachineChoice extends Location {
	/** Not in the index yet – offered because the folder is named like this. */
	isNew?: boolean;
}

function byMachine<T extends Location>(list: T[]): T[] {
	const unique = new Map<string, T>();
	for (const loc of list) {
		if (loc.machine && !unique.has(loc.machine)) unique.set(loc.machine, loc);
	}
	return [...unique.values()];
}

/**
 * Location for a product uploaded without a trustworthy machine folder:
 * known on exactly one machine → that one; on several → the user picks
 * (guessing would overwrite the other machine's programs); unknown → open.
 */
export function resolveLocation(known: Location[]): { location: Location | null; choices: Location[] } {
	const all = byMachine(known);
	if (all.length === 1) return { location: all[0], choices: [] };
	return { location: null, choices: all.length > 1 ? all : [] };
}

/**
 * Decide machine and category for a planned product.
 *
 * @param pathMachine  folder above the product folder, if any
 * @param known        where this product is already indexed
 * @param allMachines  every machine the index knows
 */
export function planLocation({
	pathMachine,
	pathCategory,
	known,
	allMachines
}: {
	pathMachine: string | null;
	pathCategory: string | null;
	known: Location[];
	allMachines: Location[];
}): { location: Location | null; choices: MachineChoice[]; notes: string[] } {
	const notes: string[] = [];
	const machines = byMachine(allMachines);

	if (pathMachine) {
		const listed = machines.find((m) => m.machine === pathMachine);
		if (listed || machines.length === 0) {
			// An empty index cannot tell a machine from a collecting folder.
			if (!listed && !pathCategory) {
				notes.push(`Maschine aus Ordnername „${pathMachine}" übernommen – bitte prüfen`);
			}
			return {
				location: { machine: pathMachine, category: pathCategory ?? listed?.category ?? null },
				choices: [],
				notes
			};
		}
		notes.push(`„${pathMachine}" ist keine bekannte Maschine`);
	}

	const { location, choices } = resolveLocation(known);
	if (location) {
		notes.push(`Maschine aus dem Bestand übernommen: ${location.machine}`);
		return { location, choices: [], notes };
	}
	if (choices.length) {
		notes.push(`Liegt auf ${choices.map((c) => c.machine).join(' und ')} – bitte Maschine wählen`);
		return { location: null, choices, notes };
	}
	if (machines.length) {
		notes.push('Neues Produkt – bitte Maschine wählen');
		const offer: MachineChoice[] = [...machines];
		if (pathMachine) offer.push({ machine: pathMachine, category: pathCategory, isNew: true });
		return { location: null, choices: offer, notes };
	}
	return { location: null, choices: [], notes };
}

/** Path from the product folder on – the same file picked via two roots matches. */
export function withinProduct(path: string, productId: string): string {
	const i = path.lastIndexOf(`/${productId}/`);
	return i >= 0 ? path.slice(i + 1) : path;
}

/**
 * Union of path lists with one entry per file of the product.
 *
 * The same product folder picked twice – as part of its machine and on its
 * own – lists every file under two roots; uploading both would ingest each
 * PDF and photo twice.
 */
export function unionByFile(productId: string, ...lists: string[][]): string[] {
	const seen = new Map<string, string>();
	for (const path of lists.flat()) {
		const key = withinProduct(path, productId);
		if (!seen.has(key)) seen.set(key, path);
	}
	return [...seen.values()];
}
