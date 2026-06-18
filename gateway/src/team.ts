import type { Request } from "express";

/**
 * The SINGLE team resolver for the whole gateway. Both the proxy (budget caps +
 * routing) and the rules write API resolve a request's team through this one
 * function, so there is never a second way to identify a team.
 *
 * Today the team is just the `x-slice-team` header (default "default"). Real auth
 * comes later; when it does, it changes here and nowhere else.
 */
export function teamFrom(req: Request): string {
  const header = req.headers["x-slice-team"];
  return typeof header === "string" && header.trim() ? header.trim() : "default";
}
