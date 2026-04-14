import { redirect } from '@sveltejs/kit';

export const ssr = false;

export function load() {
	redirect(307, '/neumann');
}
