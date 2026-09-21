/**
 * Die Identität einer Tabelle über ihre Exporte hinweg.
 *
 * Eine Werkzeug- oder Bestandsliste kommt regelmässig neu. Über den
 * Datei-Hash lässt sie sich nicht ersetzen: Der ändert sich mit jedem
 * geänderten Bestand, die alten Zeilen blieben liegen, und zu einem Werkzeug
 * stünden am Ende drei Bestände nebeneinander, ohne dass jemand sagen kann,
 * welcher gilt.
 *
 * Deshalb trägt jede Zeile einen ``table_key``: den Dateinamen ohne Endung,
 * ohne Datums- oder Versionsanhängsel, normalisiert. „Werkzeugliste Ecomat
 * 2025-07.csv" und „werkzeugliste_ecomat_v3.xlsx" sind damit dieselbe
 * Tabelle, und der neue Import räumt den alten weg.
 */

/** Datums- und Versionsanhängsel am Ende eines Dateinamens. */
const TRAILING_STAMP =
	/[ _-]+(?:v\d+|\d{4}[-_.]?\d{2}(?:[-_.]?\d{2})?|\d{2}[-_.]\d{2}[-_.]\d{2,4}|\d{8}|stand|export|neu|final)$/i;

export function tableKey(fileName: string): string {
	let stem = fileName.replace(/\.[a-z0-9]+$/i, '');
	// Mehrfach: „Liste_2025-07_final" trägt zwei Anhängsel.
	for (let i = 0; i < 3; i++) {
		const stripped = stem.replace(TRAILING_STAMP, '');
		if (stripped === stem) break;
		stem = stripped;
	}
	const key = stem
		.toLowerCase()
		.normalize('NFKD')
		.replace(/[̀-ͯ]/g, '')
		.replace(/[^a-z0-9]+/g, '_')
		.replace(/^_+|_+$/g, '');
	// Ein Name, der nur aus einem Datum bestand, ist als Schlüssel wertlos –
	// dann lieber der ungekürzte Dateiname als eigene Tabelle.
	return key || fileName.toLowerCase().replace(/[^a-z0-9]+/g, '_');
}
