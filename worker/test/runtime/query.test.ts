import { env } from "cloudflare:workers";
import { beforeAll, beforeEach, describe, expect, it } from "vitest";
import { queryResults, QueryError, RESPONSE_BYTES } from "../../src/registry-query";
import { updateQuery } from "../../src/registry-write";
import worker from "../../src/index";

const release = "a".repeat(64);
const changed = "b".repeat(64);
const db = env.QUERY;
const id = (n: number) => `PALOMAR-2026-08-27-${String(n).padStart(6, "0")}`;
async function operation(value: Record<string, unknown>, rel = release, full = true) {
  return updateQuery(new Request("https://data.palomar-registry.org/_operations/results", {
    method: "PUT", body: JSON.stringify({ release: rel, full, ...value }),
  }), db);
}
async function record(n: number, chunks = ["finite group", "elliot theorem"], rel = release, full = true, date = "2026-08-27T12:00:00Z", repository = "1".repeat(64)) {
  const fingerprint = [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify({ n, chunks, date, repository }))))].map(byte => byte.toString(16).padStart(2, "0")).join("");
  const r = { id: id(n), published_at: date, trust: n % 2 ? "high" : "qualified",
    repository, fingerprint,
    classification: { arxiv: ["math.CO", "math.PR", "math.AP", "math.AG", "math.NT"], msc2020: ["05C10"] },
    summary: JSON.stringify({ id: id(n), published_at: date, trust: { level: n % 2 ? "high" : "qualified" }, title: "Test" }),
  };
  await operation({ op: "record", record: r }, rel, full);
  for (const [position, tokens] of chunks.entries()) await operation({ op: "chunk", id: id(n), position, tokens }, rel, full);
  await operation({ op: "complete", id: id(n), chunks: chunks.length }, rel, full);
}
async function query(parameters: Record<string, string> = {}) {
  const response = await queryResults(new Request("https://data.palomar-registry.org/api/v1/results?" + new URLSearchParams(parameters)), db);
  const text = await response.text();
  expect(new TextEncoder().encode(text).length).toBeLessThanOrEqual(RESPONSE_BYTES);
  return JSON.parse(text);
}
beforeAll(async () => {
  const migrations = JSON.parse((env as unknown as { QUERY_MIGRATIONS: string }).QUERY_MIGRATIONS) as string[];
  await db.batch(migrations.map(sql => db.prepare(sql)));
});
beforeEach(async () => {
  await db.batch([db.prepare("DELETE FROM query_meta"), db.prepare("DELETE FROM query_generations")]);
});

describe("registry query in the actual D1 runtime", () => {
  it("filters the whole index, ANDs across chunks, and paginates both ways", async () => {
    await operation({ op: "begin" });
    for (let n = 1; n <= 31; n++) await record(n);
    await operation({ op: "finish", results: 31 });
    const first = await query();
    expect(first.entries).toHaveLength(25);
    expect(first.totals).toEqual({ results: 31, projects: 1 });
    expect(first.previous).toBeNull();
    const second = await query({ cursor: first.next });
    expect(second.entries).toHaveLength(6);
    expect(second.next).toBeNull();
    expect(new Set([...first.entries, ...second.entries].map((r: { id: string }) => r.id)).size).toBe(31);
    expect((await query({ cursor: second.previous })).entries).toEqual(first.entries);
    expect((await query({ q: "finite elliot", trust: "high", msc: "05", from: "2026-08-01", to: "2026-08-31" })).entries).toHaveLength(16);
    expect((await query({ arxiv: "math.NT" })).entries).toHaveLength(25);
    expect((await query({ q: "missing" })).entries).toEqual([]);
    expect((await query({ id: id(1) })).entries.map((r: { id: string }) => r.id)).toEqual([id(1)]);
    expect((await query({ q: "the" })).message).toContain("searchable");
    await expect(query({ q: "different", cursor: first.next })).rejects.toMatchObject({ status: 400 });
    const response = await worker.fetch(new Request("https://data.palomar-registry.org/api/v1/results"), env);
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    const head = await worker.fetch(new Request("https://data.palomar-registry.org/api/v1/results", { method: "HEAD" }), env);
    expect(head.status).toBe(200);
    expect(await head.text()).toBe("");
    await db.prepare("UPDATE query_summaries SET summary=json_set(summary,'$.abstract',?)").bind("é".repeat(7800)).run();
    expect((await query()).entries).toHaveLength(25);

  }, 60000);

  it("never exposes an incomplete record and retries without drifting counters", async () => {
    await operation({ op: "begin" });
    await record(1);
    await operation({ op: "finish", results: 1 });
    const old = await query();
    await record(2, ["complete"], changed, false);
    expect((await query()).totals).toEqual({ results: 2, projects: 1 });
    const revision = (await query()).revision;
    await record(2, ["complete"], changed, false);
    expect((await query()).revision).toBe(revision);
    expect((await query()).totals.results).toBe(2);
    await operation({ op: "remove", id: id(2) }, changed, false);
    expect((await query()).totals.results).toBe(1);
    await operation({ op: "finish" }, changed, false);
    expect((await query()).release).toBe(changed);
    expect(old.revision).toBeLessThan((await query()).revision);
  });

  it("rejects incomplete rebuilds without replacing the current index", async () => {
    await operation({ op: "begin" });
    await record(1);
    await operation({ op: "finish", results: 1 });
    await operation({ op: "begin" }, changed);
    await expect(operation({ op: "finish", results: 1 }, changed)).rejects.toBeInstanceOf(QueryError);
    expect((await query()).entries[0].id).toBe(id(1));
    await operation({ op: "finish", results: 0 }, changed);
    expect((await query()).totals).toEqual({ results: 0, projects: 0 });
    await operation({ op: "collect" }, changed);
    expect((await db.prepare("SELECT count(*) AS n FROM query_results").first<{n: number}>())!.n).toBe(0);
  });

  it("refuses oversized inputs before database work and protects writes", async () => {
    await expect(query({ q: "x".repeat(4097) })).rejects.toMatchObject({ status: 400 });
    await expect(query({ from: "2026-02-30" })).rejects.toMatchObject({ status: 400 });
    await expect(query({ cursor: "x".repeat(1025) })).rejects.toMatchObject({ status: 400 });
    await expect(operation({ op: "record", padding: "x".repeat(256 * 1024) })).rejects.toMatchObject({ status: 413 });
    const unauthorized = await worker.fetch(new Request("https://data.palomar-registry.org/_operations/results", { method: "PUT", body: "{}" }), env);
    expect(unauthorized.status).toBe(401);
    const retired = await worker.fetch(new Request("https://data.palomar-registry.org/search/t/ring/head.json"), { ...env, PALOMAR_RETIRE_STATIC_SEARCH: "true" });
    expect(retired.status).toBe(410);
  });
});

it("refreshes a cursor when an updated result would move across it", async () => {
  await operation({ op: "begin" });
  for (let n = 1; n <= 26; n++) await record(n);
  await operation({ op: "finish", results: 26 });
  const before = await query();
  await operation({ op: "remove", id: id(1) }, changed, false);
  await record(1, ["replacement metadata"], changed, false, "2026-09-01T00:00:00Z");
  await expect(query({ cursor: before.next })).rejects.toMatchObject({ status: 409, code: "registry_changed" });
  expect((await query()).entries[0].id).toBe(id(1));
  expect((await query({ q: "elliot", id: id(1) })).entries).toEqual([]);
  expect((await query({ q: "replacement" })).entries.map((r: { id: string }) => r.id)).toEqual([id(1)]);
  await operation({ op: "complete", id: id(1), chunks: 1 }, changed, false);
  expect((await query()).totals.results).toBe(26);
});

it("updates distinct project totals atomically with a corrected result", async () => {
  await operation({ op: "begin" });
  await record(1);
  await record(2);
  await operation({ op: "finish", results: 2 });
  expect((await query()).totals).toEqual({ results: 2, projects: 1 });
  await record(2, ["corrected"], changed, false, "2026-08-27T12:00:00Z", "2".repeat(64));
  expect((await query()).totals).toEqual({ results: 2, projects: 2 });
  await record(2, ["corrected"], changed, false);
  expect((await query()).totals).toEqual({ results: 2, projects: 1 });
  await operation({ op: "remove", id: id(1) }, changed, false);
  await operation({ op: "remove", id: id(2) }, changed, false);
  expect((await query()).totals).toEqual({ results: 0, projects: 0 });
});

it("keeps responses bounded with 100,000 current results and broad words", async () => {
  await operation({ op: "begin" });
  // Bulk seeding measures the actual query engine without hundreds of thousands
  // of artificial HTTP publication requests in the scale test.
  await db.prepare(`WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000)
    INSERT INTO query_results(generation,id,published_at,trust,repository,fingerprint,expected_chunks)
    SELECT ?,printf('PALOMAR-2026-08-27-%06d',x),'2026-08-27T12:00:00Z','high',?,printf('%064x',x),1 FROM n`)
    .bind(`f-${release}`, "1".repeat(64)).run();
  await db.prepare(`INSERT INTO query_summaries SELECT row_id,json_object('id',id,'title','Complete result') FROM query_results`).run();
  await db.prepare(`INSERT INTO query_chunks(row_id,position,tokens)
    SELECT row_id,0,CASE WHEN id='PALOMAR-2026-08-27-000001' THEN 'finite group rareauthor' ELSE 'finite group' END FROM query_results`).run();
  await operation({ op: "finish", results: 100000 });
  const started = Date.now();
  const first = await query({ q: "finite group" });
  expect(first.totals).toEqual({ results: 100000, projects: 1 });
  expect(first.entries).toHaveLength(25);
  expect((await query({ q: "finite group", cursor: first.next })).entries).toHaveLength(25);
  expect((await query({ q: "rareauthor" })).entries.map((r: { id: string }) => r.id)).toEqual([id(1)]);
  expect((await query({ to: "2026-08-01" })).entries).toEqual([]);
  console.info(`100,000-result D1 query workload: ${Date.now() - started} ms, maximum 25 rows per response`);
  await operation({ op: "begin" }, changed);
  await operation({ op: "finish", results: 0 }, changed);
  await operation({ op: "collect" }, changed);
  expect((await db.prepare("SELECT count(*) AS n FROM query_results").first<{ n: number }>())!.n).toBe(99500);
  expect((await query()).totals.results).toBe(0);
}, 120000);
