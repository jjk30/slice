import { describe, it, expect, beforeEach, vi } from "vitest";
import type { Request, Response } from "express";

/**
 * Rules write-API tests. The Postgres helpers and refreshTeamRules are MOCKED
 * (an in-memory store stands in for team_rules) — no network, no DB. pricing's
 * isPricedModel and the shared teamFrom run for real, so validation is genuinely
 * exercised.
 */

// Shared mock state, hoisted so the vi.mock factories can see it.
const mocks = vi.hoisted(() => {
  const store: { team: string; from_model: string; to_model: string }[] = [];
  return {
    store,
    fetchTeamRulesForTeam: vi.fn(async (team: string) => store.filter((r) => r.team === team)),
    upsertTeamRule: vi.fn(async (team: string, from_model: string, to_model: string) => {
      const existing = store.find((r) => r.team === team && r.from_model === from_model);
      if (existing) existing.to_model = to_model;
      else store.push({ team, from_model, to_model });
    }),
    deleteTeamRule: vi.fn(async (team: string, from_model: string) => {
      const before = store.length;
      for (let i = store.length - 1; i >= 0; i--) {
        if (store[i].team === team && store[i].from_model === from_model) store.splice(i, 1);
      }
      return before - store.length;
    }),
    refreshTeamRules: vi.fn(async () => {}),
  };
});

vi.mock("../src/db", () => ({
  fetchTeamRulesForTeam: mocks.fetchTeamRulesForTeam,
  upsertTeamRule: mocks.upsertTeamRule,
  deleteTeamRule: mocks.deleteTeamRule,
}));
vi.mock("../src/router", () => ({ refreshTeamRules: mocks.refreshTeamRules }));

import { listRules, saveRule, removeRule } from "../src/rules";

// --- Fake req/res ------------------------------------------------------------
function mkReq(opts: {
  team?: string;
  body?: unknown;
  query?: Record<string, string>;
}): Request {
  const headers: Record<string, string> = {};
  if (opts.team) headers["x-slice-team"] = opts.team;
  return {
    headers,
    body: opts.body,
    query: opts.query ?? {},
    method: "POST",
  } as unknown as Request;
}

interface CapturedRes {
  statusCode: number;
  body: unknown;
  status(code: number): CapturedRes;
  json(b: unknown): CapturedRes;
  send(b: unknown): CapturedRes;
  sendStatus(code: number): CapturedRes;
  setHeader(k: string, v: string): void;
}
function mkRes(): CapturedRes {
  const res: CapturedRes = {
    statusCode: 0,
    body: undefined,
    status(code) {
      this.statusCode = code;
      return this;
    },
    json(b) {
      this.body = b;
      return this;
    },
    send(b) {
      this.body = b;
      return this;
    },
    sendStatus(code) {
      this.statusCode = code;
      return this;
    },
    setHeader() {},
  };
  return res;
}
const asRes = (r: CapturedRes) => r as unknown as Response;

beforeEach(() => {
  mocks.store.length = 0;
  vi.clearAllMocks();
});

describe("POST /api/rules validation", () => {
  it("rejects an unknown (unpriced) to_model with 400 and writes nothing", async () => {
    const res = mkRes();
    await saveRule(
      mkReq({ team: "acme", body: Buffer.from(JSON.stringify({ from_model: "claude-opus-4-8", to_model: "totally-made-up" })) }),
      asRes(res),
    );
    expect(res.statusCode).toBe(400);
    expect(String((res.body as { error: string }).error)).toContain("not a known, priced model");
    expect(mocks.upsertTeamRule).not.toHaveBeenCalled();
    expect(mocks.refreshTeamRules).not.toHaveBeenCalled();
  });

  it("rejects an empty from_model with 400", async () => {
    const res = mkRes();
    await saveRule(
      mkReq({ team: "acme", body: Buffer.from(JSON.stringify({ from_model: "  ", to_model: "gpt-4o" })) }),
      asRes(res),
    );
    expect(res.statusCode).toBe(400);
    expect(mocks.upsertTeamRule).not.toHaveBeenCalled();
  });

  it("rejects a non-JSON body with 400", async () => {
    const res = mkRes();
    await saveRule(mkReq({ team: "acme", body: Buffer.from("not json") }), asRes(res));
    expect(res.statusCode).toBe(400);
    expect(mocks.upsertTeamRule).not.toHaveBeenCalled();
  });
});

describe("rules write API: save -> list -> delete", () => {
  it("saves a valid rule, refreshes the map, and returns the saved rule", async () => {
    const res = mkRes();
    await saveRule(
      mkReq({ team: "acme", body: Buffer.from(JSON.stringify({ from_model: "claude-opus-4-8", to_model: "gpt-4o" })) }),
      asRes(res),
    );
    expect(res.statusCode).toBe(200);
    expect(res.body).toEqual({ team: "acme", from_model: "claude-opus-4-8", to_model: "gpt-4o" });
    expect(mocks.upsertTeamRule).toHaveBeenCalledWith("acme", "claude-opus-4-8", "gpt-4o");
    expect(mocks.refreshTeamRules).toHaveBeenCalledTimes(1);
  });

  it("lists the team's rules as an array after a save", async () => {
    await saveRule(
      mkReq({ team: "acme", body: Buffer.from(JSON.stringify({ from_model: "claude-opus-4-8", to_model: "gpt-4o" })) }),
      asRes(mkRes()),
    );

    const res = mkRes();
    await listRules(mkReq({ team: "acme" }), asRes(res));
    expect(res.statusCode).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body).toEqual([{ team: "acme", from_model: "claude-opus-4-8", to_model: "gpt-4o" }]);
  });

  it("scopes the list to the requesting team", async () => {
    await saveRule(
      mkReq({ team: "acme", body: Buffer.from(JSON.stringify({ from_model: "claude-opus-4-8", to_model: "gpt-4o" })) }),
      asRes(mkRes()),
    );

    const res = mkRes();
    await listRules(mkReq({ team: "globex" }), asRes(res));
    expect(res.body).toEqual([]); // globex has no rules
  });

  it("deletes a rule by from_model query param, then the list is empty", async () => {
    await saveRule(
      mkReq({ team: "acme", body: Buffer.from(JSON.stringify({ from_model: "claude-opus-4-8", to_model: "gpt-4o" })) }),
      asRes(mkRes()),
    );

    const delRes = mkRes();
    await removeRule(mkReq({ team: "acme", query: { from_model: "claude-opus-4-8" } }), asRes(delRes));
    expect(delRes.statusCode).toBe(200);
    expect(res_deleted(delRes)).toBe(1);
    expect(mocks.refreshTeamRules).toHaveBeenCalled();

    const listRes = mkRes();
    await listRules(mkReq({ team: "acme" }), asRes(listRes));
    expect(listRes.body).toEqual([]);
  });

  it("delete of a missing rule returns ok with deleted: 0 (idempotent)", async () => {
    const res = mkRes();
    await removeRule(mkReq({ team: "acme", query: { from_model: "claude-opus-4-8" } }), asRes(res));
    expect(res.statusCode).toBe(200);
    expect(res_deleted(res)).toBe(0);
  });
});

function res_deleted(res: CapturedRes): number {
  return (res.body as { deleted: number }).deleted;
}
