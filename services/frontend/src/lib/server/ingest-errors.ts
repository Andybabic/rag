/**
 * Turn a failed ingestion step into a clear, actionable message so the user
 * knows *where* it broke (which service / stage) instead of an opaque
 * "TypeError: fetch failed".
 */

interface StageError {
	error: string;
	detail: string;
	stage: string;
	status: number;
}

/** Map a thrown fetch error (network/timeout) for a given stage. */
export function fetchError(stage: string, serviceLabel: string, err: unknown): StageError {
	const cause = (err as { cause?: { code?: string } })?.cause;
	const code = cause?.code ?? '';
	const msg = err instanceof Error ? err.message : String(err);

	if (code === 'UND_ERR_HEADERS_TIMEOUT' || code === 'UND_ERR_BODY_TIMEOUT') {
		return {
			error: 'timeout',
			stage,
			status: 504,
			detail:
				`Zeitüberschreitung im Schritt „${stage}" (${serviceLabel}). ` +
				`Das Dokument ist vermutlich groß oder gescannt (MinerU-OCR + Bildanalyse dauern lange). ` +
				`Falls das wiederholt passiert, das PDF weiter aufteilen oder FETCH_TIMEOUT_MS / MINERU_TIMEOUT erhöhen.`
		};
	}
	if (code === 'ECONNREFUSED' || code === 'ENOTFOUND' || code === 'ECONNRESET') {
		return {
			error: 'service_unreachable',
			stage,
			status: 502,
			detail: `${serviceLabel} nicht erreichbar (${code}) im Schritt „${stage}".`
		};
	}
	return {
		error: 'stage_failed',
		stage,
		status: 502,
		detail: `Fehler im Schritt „${stage}" (${serviceLabel}): ${msg}`
	};
}

/** Map a non-OK HTTP response from a downstream service for a given stage. */
export async function responseError(
	stage: string,
	serviceLabel: string,
	resp: Response
): Promise<StageError> {
	let detail = '';
	let downstream = '';
	try {
		const body = await resp.json();
		downstream = body.error ? `${body.error}: ` : '';
		detail = body.detail ?? JSON.stringify(body);
	} catch {
		detail = (await resp.text().catch(() => '')) || `HTTP ${resp.status}`;
	}
	return {
		error: 'stage_failed',
		stage,
		status: resp.status >= 500 ? 502 : resp.status,
		detail: `Schritt „${stage}" (${serviceLabel}) fehlgeschlagen [${resp.status}]: ${downstream}${detail}`
	};
}
