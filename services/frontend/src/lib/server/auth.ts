/**
 * Stateless session tokens — signed with HMAC-SHA256 over AUTH_SECRET.
 *
 * Token format:  base64url(payload).base64url(hmac)
 * Payload:       { u: username, r: role, exp: epochSeconds }
 *
 * No DB lookup is needed to validate a session: the signature + expiry are
 * self-contained. To revoke everyone, rotate AUTH_SECRET.
 */
import { createHmac, timingSafeEqual } from 'node:crypto';
import { env } from '$env/dynamic/private';

export const SESSION_COOKIE = 'session';
export const SESSION_MAX_AGE = 60 * 60 * 24 * 7; // 7 days, in seconds

export type Role = 'admin' | 'user';

export interface SessionUser {
	username: string;
	role: Role;
}

interface Payload {
	u: string;
	r: Role;
	exp: number;
}

function getSecret(): string {
	const secret = env.AUTH_SECRET;
	if (!secret) {
		// Fail loud: an unsigned/empty-secret session is a security hole.
		throw new Error('AUTH_SECRET is not set — cannot sign or verify sessions');
	}
	return secret;
}

function b64urlEncode(input: string): string {
	return Buffer.from(input, 'utf8').toString('base64url');
}

function b64urlDecode(input: string): string {
	return Buffer.from(input, 'base64url').toString('utf8');
}

function sign(payloadB64: string): string {
	return createHmac('sha256', getSecret()).update(payloadB64).digest('base64url');
}

/** Create a signed session token for the given user. */
export function createSession(user: SessionUser, maxAge: number = SESSION_MAX_AGE): string {
	const payload: Payload = {
		u: user.username,
		r: user.role,
		exp: Math.floor(Date.now() / 1000) + maxAge
	};
	const payloadB64 = b64urlEncode(JSON.stringify(payload));
	return `${payloadB64}.${sign(payloadB64)}`;
}

/** Verify a token and return the user, or null if invalid/expired/tampered. */
export function verifySession(token: string | undefined): SessionUser | null {
	if (!token) return null;
	const dot = token.indexOf('.');
	if (dot < 1) return null;
	const payloadB64 = token.slice(0, dot);
	const sig = token.slice(dot + 1);

	let expected: string;
	try {
		expected = sign(payloadB64);
	} catch {
		return null;
	}
	const sigBuf = Buffer.from(sig);
	const expBuf = Buffer.from(expected);
	if (sigBuf.length !== expBuf.length || !timingSafeEqual(sigBuf, expBuf)) {
		return null;
	}

	let payload: Payload;
	try {
		payload = JSON.parse(b64urlDecode(payloadB64));
	} catch {
		return null;
	}
	if (typeof payload.exp !== 'number' || payload.exp < Math.floor(Date.now() / 1000)) {
		return null;
	}
	if (payload.r !== 'admin' && payload.r !== 'user') return null;
	return { username: payload.u, role: payload.r };
}
