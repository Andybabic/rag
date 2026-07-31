import { json } from "@sveltejs/kit";
import { SERVICES } from "$lib/server/services";
import type { RequestHandler } from "./$types";

export const POST: RequestHandler = async ({ params, request }) => {
  const body = await request.json();
  const resp = await fetch(
    `${SERVICES.evaluation}/v1/admin/analyze-chunks/${params.id}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  return json(await resp.json());
};
