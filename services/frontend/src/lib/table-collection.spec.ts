import { describe, expect, it } from 'vitest';
import { tableCollection } from './table-collection';

describe('tableCollection', () => {
	it('keeps "<id>_default" where the prefix covers it', () => {
		expect(tableCollection('neumann', ['neumann_'])).toBe('neumann_default');
		expect(tableCollection('gw_stpoelten', ['gw_'])).toBe('gw_stpoelten_default');
	});

	it('moves into the prefix when "<id>_default" would be unsearchable', () => {
		// Before: wiener_linien_default – never read by the wl_-isolated search.
		expect(tableCollection('wiener_linien', ['wl_'])).toBe('wl_tabellen');
	});

	it('treats missing prefixes like the search does ("<id>_")', () => {
		expect(tableCollection('kunde_x', [])).toBe('kunde_x_default');
	});
});
