import type { ApiErrorCode } from "../../../shared/protocol.ts";

/**
 * Throw from any route handler: the server's error handler replies `statusCode` with
 * `{ error: message, code }` (ApiErrorBody). Use for every error a client branches on.
 */
export class ApiError extends Error {
  constructor(
    readonly statusCode: number,
    readonly code: ApiErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const badRequest = (message: string) => new ApiError(400, "validation", message);
export const notFound = (message = "not found") => new ApiError(404, "not_found", message);

/** zod safeParse error -> "path: message; ..." (400 validation). */
export function zodError(issues: { path: (string | number)[]; message: string }[]): ApiError {
  return badRequest(issues.map((i) => `${i.path.join(".") || "body"}: ${i.message}`).join("; "));
}
