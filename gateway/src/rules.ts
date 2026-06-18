import { Router, type Request, type Response } from "express";
import { logger } from "./logger";
import { teamFrom } from "./team";
import { isPricedModel } from "./pricing";
import { refreshTeamRules } from "./router";
import { fetchTeamRulesForTeam, upsertTeamRule, deleteTeamRule } from "./db";

/**
 * Per-team switch-rules WRITE API (and a read-back for the dashboard).
 *
 * Mounted at /api alongside the read-only stats router. Unlike the stats surface,
 * these are USER ACTIONS: every handler AWAITS its DB write and the subsequent
 * refreshTeamRules() (so the change is live with no restart), then returns a clear
 * success/error status — never fire-and-forget.
 *
 * Team is resolved by the shared teamFrom() (src/team.ts), the ONE team resolver
 * the proxy also uses — there is no second way to identify a team here.
 *
 * Validation is strict: to_model must be a real, priced model (so cost lookup and
 * the ranking keep working) and from_model must be non-empty. Bad input -> 400,
 * nothing written.
 */

export const rulesRouter = Router();

// CORS + preflight — same pattern as the stats router, plus the write verbs.
rulesRouter.use((req: Request, res: Response, next) => {
  res.setHeader("Access-Control-Allow-Origin", process.env.DASHBOARD_ORIGIN ?? "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") {
    res.sendStatus(204);
    return;
  }
  next();
});

/**
 * Parse a JSON OBJECT from the request body. The gateway's global `express.raw`
 * middleware populates req.body as a Buffer, so we parse it ourselves (a later
 * json() middleware would see an already-consumed stream). Returns null for
 * missing/invalid/non-object bodies.
 */
function parseJsonObject(req: Request): Record<string, unknown> | null {
  const raw: unknown = req.body;
  let text: string;
  if (Buffer.isBuffer(raw)) text = raw.toString("utf8");
  else if (typeof raw === "string") text = raw;
  else if (raw && typeof raw === "object") return raw as Record<string, unknown>; // already parsed (e.g. tests)
  else return null;

  if (!text.trim()) return null;
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
    return null;
  } catch {
    return null;
  }
}

/** Read a trimmed string field, or "" when absent/non-string. */
function readString(obj: Record<string, unknown>, key: string): string {
  const value = obj[key];
  return typeof value === "string" ? value.trim() : "";
}

/** GET /api/rules — list the current team's rules (an array). */
export async function listRules(req: Request, res: Response): Promise<void> {
  const team = teamFrom(req);
  try {
    const rules = await fetchTeamRulesForTeam(team);
    res.status(200).json(rules);
  } catch (err) {
    logger.warn({ err: (err as Error).message, team }, "list team rules failed");
    res.status(500).json({ error: "failed to list team rules" });
  }
}

/** POST /api/rules — upsert a rule { from_model, to_model } for the current team. */
export async function saveRule(req: Request, res: Response): Promise<void> {
  const team = teamFrom(req);

  const body = parseJsonObject(req);
  if (!body) {
    res.status(400).json({ error: "request body must be a JSON object" });
    return;
  }

  const fromModel = readString(body, "from_model");
  const toModel = readString(body, "to_model");

  if (!fromModel) {
    res.status(400).json({ error: "from_model is required and must be a non-empty string" });
    return;
  }
  if (!toModel) {
    res.status(400).json({ error: "to_model is required and must be a non-empty string" });
    return;
  }
  if (!isPricedModel(toModel)) {
    res.status(400).json({
      error: `to_model "${toModel}" is not a known, priced model (see src/pricing.ts)`,
    });
    return;
  }

  try {
    await upsertTeamRule(team, fromModel, toModel);
    await refreshTeamRules(); // make the rule live immediately
    res.status(200).json({ team, from_model: fromModel, to_model: toModel });
  } catch (err) {
    logger.warn({ err: (err as Error).message, team }, "save team rule failed");
    res.status(500).json({ error: "failed to save team rule" });
  }
}

/** DELETE /api/rules?from_model=... — remove a rule for the current team. */
export async function removeRule(req: Request, res: Response): Promise<void> {
  const team = teamFrom(req);

  // Identify the rule by team (header) + from_model (query param, with a body
  // fallback so either REST shape works).
  let fromModel =
    typeof req.query.from_model === "string" ? req.query.from_model.trim() : "";
  if (!fromModel) {
    const body = parseJsonObject(req);
    if (body) fromModel = readString(body, "from_model");
  }
  if (!fromModel) {
    res.status(400).json({ error: "from_model is required (query param or body)" });
    return;
  }

  try {
    const deleted = await deleteTeamRule(team, fromModel);
    await refreshTeamRules(); // reflect the removal immediately
    res.status(200).json({ ok: true, team, from_model: fromModel, deleted });
  } catch (err) {
    logger.warn({ err: (err as Error).message, team }, "delete team rule failed");
    res.status(500).json({ error: "failed to delete team rule" });
  }
}

rulesRouter.get("/rules", listRules);
rulesRouter.post("/rules", saveRule);
rulesRouter.delete("/rules", removeRule);
