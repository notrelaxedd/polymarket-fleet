import type { ApiErrorCode } from "@shared/protocol";

/** Thrown by every api.ts helper (real backend and mock) for a non-2xx response. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: ApiErrorCode | null,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const isApiError = (e: unknown): e is ApiError => e instanceof ApiError;
