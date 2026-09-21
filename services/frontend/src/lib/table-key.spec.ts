import { describe, expect, it } from 'vitest';
import { tableKey } from './table-key';

describe('tableKey', () => {
	it('erkennt denselben Export unter neuem Datum wieder', () => {
		const july = tableKey('Werkzeugliste_Ecomat_Hauptlieferant_2025-07.csv');
		const october = tableKey('Werkzeugliste Ecomat Hauptlieferant 2025-10.xlsx');
		expect(october).toBe(july);
	});

	it('ignoriert Versions- und Standanhängsel', () => {
		const base = tableKey('Werkzeugliste_Ecomat.csv');
		expect(tableKey('Werkzeugliste_Ecomat_v3.csv')).toBe(base);
		expect(tableKey('Werkzeugliste_Ecomat_final.csv')).toBe(base);
		expect(tableKey('Werkzeugliste_Ecomat_20250711.csv')).toBe(base);
	});

	it('hält verschiedene Tabellen auseinander', () => {
		expect(tableKey('Werkzeugliste_Ecomat.csv')).not.toBe(tableKey('Lieferantenliste.csv'));
	});

	it('normalisiert Schreibweise und Umlaute', () => {
		expect(tableKey('Werkzeug-Liste Fräsen.csv')).toBe(tableKey('werkzeug_liste_frasen.xlsx'));
	});

	it('behält einen Namen, der nur aus einem Datum besteht', () => {
		// Sonst hätten alle reinen Datumsdateien denselben leeren Schlüssel und
		// würden einander beim Import löschen.
		expect(tableKey('2025-07.csv')).toBe('2025_07');
		expect(tableKey('2025-10.csv')).not.toBe(tableKey('2025-07.csv'));
	});
});
