import { SERVICES } from "$lib/server/services";
import { json } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";

export const GET: RequestHandler = async () => {
	const resp = await fetch(`${SERVICES.evaluation}/v1/admin/models`);
	const data = await resp.json();
	return json(data);
};
