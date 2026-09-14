import { describe, expect, it } from 'vitest';
import { planLocation, resolveLocation, unionByFile } from './gw-location';

const chiron243 = { machine: 'Chiron 243', category: 'Fräsen groß' };
const matec1 = { machine: 'Matec 1', category: 'Fräsen groß' };
const chiron233 = { machine: 'Chiron 233-2', category: 'Fräsen mittel' };
const index = [chiron243, matec1, chiron233];

describe('resolveLocation', () => {
	it('takes over the one machine a product is known on', () => {
		expect(resolveLocation([chiron243, chiron243])).toEqual({ location: chiron243, choices: [] });
	});

	it('asks when the product lives on several machines', () => {
		expect(resolveLocation([chiron243, matec1])).toEqual({ location: null, choices: [chiron243, matec1] });
	});

	it('ignores points that never carried a machine', () => {
		expect(resolveLocation([{ machine: null, category: null }, matec1]).location).toEqual(matec1);
	});
});

describe('planLocation', () => {
	it('trusts a machine folder the index knows', () => {
		const r = planLocation({ pathMachine: 'Chiron 243', pathCategory: 'Fräsen groß', known: [], allMachines: index });
		expect(r.location).toEqual(chiron243);
		expect(r.notes).toEqual([]);
	});

	it('fills the category when only the machine folder was picked', () => {
		const r = planLocation({ pathMachine: 'Matec 1', pathCategory: null, known: [], allMachines: index });
		expect(r.location).toEqual(matec1);
	});

	it('does not turn a collecting folder into a machine', () => {
		// "Neue Produkte/11221.7199" – the re-upload must replace the Chiron 243 copy.
		const r = planLocation({ pathMachine: 'Neue Produkte', pathCategory: null, known: [chiron243], allMachines: index });
		expect(r.location).toEqual(chiron243);
		expect(r.notes).toEqual([
			'„Neue Produkte" ist keine bekannte Maschine',
			'Maschine aus dem Bestand übernommen: Chiron 243'
		]);
	});

	it('asks for a product on two machines instead of guessing', () => {
		const r = planLocation({ pathMachine: null, pathCategory: null, known: [matec1, chiron233], allMachines: index });
		expect(r.location).toBeNull();
		expect(r.choices).toEqual([matec1, chiron233]);
	});

	it('offers the known machines plus the folder name for a new product', () => {
		const r = planLocation({ pathMachine: 'Chiron 250', pathCategory: 'Fräsen groß', known: [], allMachines: index });
		expect(r.location).toBeNull();
		expect(r.choices).toEqual([...index, { machine: 'Chiron 250', category: 'Fräsen groß', isNew: true }]);
	});

	it('trusts the folder on a first upload, when the index knows nothing yet', () => {
		const tree = planLocation({ pathMachine: 'Chiron 243', pathCategory: 'Fräsen groß', known: [], allMachines: [] });
		expect(tree.location).toEqual(chiron243);
		expect(tree.notes).toEqual([]);
		const bare = planLocation({ pathMachine: 'Neue Produkte', pathCategory: null, known: [], allMachines: [] });
		expect(bare.location?.machine).toBe('Neue Produkte');
		expect(bare.notes[0]).toContain('bitte prüfen');
	});

	it('leaves a bare product folder open when nothing is known at all', () => {
		expect(planLocation({ pathMachine: null, pathCategory: null, known: [], allMachines: [] })).toEqual({
			location: null,
			choices: [],
			notes: []
		});
	});
});

describe('unionByFile', () => {
	it('keeps one entry per file when a folder was picked via two roots', () => {
		expect(
			unionByFile(
				'11221.7199',
				['GW/Chiron 243/11221.7199/Einstellblatt 1.pdf', 'GW/Chiron 243/11221.7199/1BEAR_1.jpg'],
				['Einzelne/11221.7199/Einstellblatt 1.pdf', 'Einzelne/11221.7199/Einstellblatt 2.pdf']
			)
		).toEqual([
			'GW/Chiron 243/11221.7199/Einstellblatt 1.pdf',
			'GW/Chiron 243/11221.7199/1BEAR_1.jpg',
			'Einzelne/11221.7199/Einstellblatt 2.pdf'
		]);
	});

	it('does not confuse files of different products', () => {
		expect(unionByFile('11221.7199', ['A/11221.7199/x.pdf'], ['A/11221.7238/x.pdf'])).toHaveLength(2);
	});
});
