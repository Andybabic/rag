import { SERVICES } from "$lib/server/services";
import { json } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";

export const GET: RequestHandler = async ({ url }) => {
	const useCase = url.searchParams.get('use_case') ?? '';
	const qs = useCase ? `?use_case=${encodeURIComponent(useCase)}` : '';
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/models${qs}`);
	const data = await resp.json();
	return json(data);
};
