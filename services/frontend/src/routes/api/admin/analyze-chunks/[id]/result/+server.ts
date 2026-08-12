import { json } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";
import { SERVICES } from "$lib/server/services";

export const GET: RequestHandler = async ({ params }) => {
  const resp = await fetch(
    `${SERVICES.evaluation}/v1/admin/analyze-chunks/${params.id}/result`
  );
  return json(await resp.json());
};
