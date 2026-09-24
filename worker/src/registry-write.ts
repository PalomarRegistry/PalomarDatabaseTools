import { BODY_BYTES, ID, QueryError, SUMMARY_BYTES, byteLength, json } from "./registry-query";

type Operation = {
  op: string; release: string; full: boolean; build?: string; id?: string;
  record?: Record<string, unknown>; position?: number; tokens?: string;
  chunks?: number; results?: number; after?: string;
};
const hash = /^[a-f0-9]{64}$/;
const invalid = (message: string): never => { throw new QueryError(400, "invalid_update", message); };
const requireValue = (condition: unknown, message: string): void => { if (!condition) invalid(message); };
async function body(request: Request): Promise<Operation> {
  const reader = request.body?.getReader();
  if (!reader) return invalid("Missing update body");
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const next = await reader.read();
    if (next.done) break;
    size += next.value.length;
    if (size > BODY_BYTES) {
      await reader.cancel();
      throw new QueryError(413, "update_too_large", "Update body exceeds 256 KiB");
    }
    chunks.push(next.value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  try { return JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes)); }
  catch { return invalid("Invalid JSON"); }
}

/** One authenticated publisher streams bounded operations. Public readers see
 * only complete records. Ordinary writes need a temporary buffer for ONE result
 * whose complete search text need not fit in one request, not a release snapshot.
 */
export async function updateQuery(request: Request, db: D1Database): Promise<Response> {
  const input = await body(request);
  requireValue(input && hash.test(input.release) && typeof input.full === "boolean", "Invalid release");
  requireValue(["inspect", "export", "begin", "record", "chunk", "complete", "remove", "finish", "collect"].includes(input.op), "Unknown update operation");
  const { release, full } = input;
  const build = input.build ?? release;
  requireValue(hash.test(build), "Invalid build identifier");
  const active = await db.prepare("SELECT m.generation,m.revision,g.release FROM query_meta m JOIN query_generations g USING(generation) WHERE singleton=1").first<{generation: string; revision: number; release: string}>();
  const completeRelease = active?.release === release && (!full || active.generation === `f-${build}`);
  if (input.op === "inspect") {
    return json({ schema_version: 1, ...active, ...(await db.prepare("SELECT results,projects FROM query_generations WHERE generation=?").bind(active?.generation ?? "").first() ?? {}) });
  }
  if (input.op === "export") {
    requireValue(input.after === undefined || ID.test(input.after), "Invalid export position");
    const rows = await db.prepare(`SELECT id,fingerprint FROM query_results
      WHERE generation=? AND id>? ORDER BY id LIMIT 26`).bind(active?.generation ?? "", input.after ?? "").all();
    return json({ entries: rows.results.slice(0, 25), next: rows.results.length > 25 ? rows.results[24].id : null });
  }
  // A publisher retry after successful activation must not repopulate an old
  // buffer or erase records already made current by this exact release.
  if (completeRelease && input.op !== "collect") return json({ ok: true, release });
  if (!full && !active) throw new QueryError(409, "rebuild_required", "Populate the query index with a full publication first");
  const identifier = input.record?.id ?? input.id;
  if (["record", "chunk", "complete", "remove"].includes(input.op)) requireValue(typeof identifier === "string" && ID.test(identifier), "Invalid result identifier");
  const generation = full ? `f-${build}` : `u-${release}-${identifier}`;
  const prepare = (sql: string, ...args: (string | number)[]) => db.prepare(sql).bind(...args);

  if (input.op === "begin") {
    if (full) await prepare("INSERT INTO query_generations(generation,release) VALUES(?,?) ON CONFLICT DO NOTHING", generation, release).run();
    return json({ ok: true });
  }
  if (input.op === "remove") {
    if (active) await db.batch([
      prepare("DELETE FROM query_results WHERE generation=? AND id=?", active.generation, String(identifier)),
      prepare("UPDATE query_meta SET revision=revision+1 WHERE singleton=1"),
    ]);
    return json({ ok: true });
  }
  if (input.op === "record") {
    const r = input.record!;
    requireValue(typeof r.summary === "string" && byteLength(r.summary) <= SUMMARY_BYTES, "Summary exceeds byte bound");
    requireValue(typeof r.fingerprint === "string" && hash.test(r.fingerprint), "Invalid record fingerprint");
    requireValue(typeof r.repository === "string" && hash.test(r.repository), "Invalid repository identity");
    requireValue(["high", "qualified"].includes(String(r.trust)), "Invalid trust level");
    requireValue(typeof r.published_at === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(r.published_at), "Invalid registration instant");
    let summary;
    try { summary = JSON.parse(String(r.summary)); } catch { return invalid("Invalid summary JSON"); }
    requireValue(summary && summary.id === identifier && summary.published_at === r.published_at && summary.trust?.level === r.trust, "Summary identity does not match index row");
    const classification = r.classification as { arxiv: string[]; msc2020: string[] };
    requireValue(classification && Array.isArray(classification.arxiv) && classification.arxiv.length <= 8 && Array.isArray(classification.msc2020) && classification.msc2020.length <= 8, "Invalid classifications");
    const codes = [...classification.arxiv.map(code => ["arxiv", code]), ...classification.msc2020.map(code => ["msc", code])];
    requireValue(codes.every(([kind, code]) => typeof code === "string" && (kind === "msc" ? /^\d{2}[A-Z-]\d{2}$/.test(code) : code.length <= 32 && /^[a-z]+(?:-[a-z]+)*(?:\.[A-Za-z-]+)?$/.test(code))), "Invalid classification code");
    const statements = [
      prepare("INSERT INTO query_generations(generation,release) VALUES(?,?) ON CONFLICT DO NOTHING", generation, release),
      prepare("DELETE FROM query_results WHERE generation=? AND id=?", generation, String(identifier)),
      prepare("INSERT INTO query_results(generation,id,published_at,trust,repository,fingerprint) VALUES(?,?,?,?,?,?)", generation, String(identifier), String(r.published_at), String(r.trust), String(r.repository), String(r.fingerprint)),
      prepare("INSERT INTO query_summaries SELECT row_id,? FROM query_results WHERE generation=? AND id=?", String(r.summary), generation, String(identifier)),
      prepare("INSERT INTO query_codes SELECT r.row_id,json_extract(c.value,'$[0]'),json_extract(c.value,'$[1]') FROM query_results r,json_each(?) c WHERE r.generation=? AND r.id=?", JSON.stringify(codes), generation, String(identifier)),
    ];
    await db.batch(statements);
    return json({ ok: true });
  }
  if (input.op === "chunk") {
    requireValue(Number.isSafeInteger(input.position) && input.position! >= 0 && typeof input.tokens === "string" && byteLength(input.tokens) <= 65536 && /^(?:[a-z0-9]{2,32})(?: [a-z0-9]{2,32})*$/.test(input.tokens), "Invalid search chunk");
    const row = await prepare("SELECT row_id FROM query_results WHERE generation=? AND id=?", generation, String(identifier)).first<{row_id: number}>();
    if (!row) throw new QueryError(409, "missing_record", "Start the record before uploading chunks");
    await db.batch([
      prepare("DELETE FROM query_chunks WHERE row_id=? AND position=?", row.row_id, input.position!),
      prepare("INSERT INTO query_chunks(row_id,position,tokens) VALUES(?,?,?)", row.row_id, input.position!, input.tokens!),
    ]);
    return json({ ok: true });
  }
  if (input.op === "complete") {
    requireValue(Number.isSafeInteger(input.chunks) && input.chunks! >= 0, "Invalid chunk count");
    const row = await prepare(`SELECT r.row_id,r.fingerprint,count(c.chunk_id) AS chunks,
      COALESCE(max(c.position),-1) AS last FROM query_results r LEFT JOIN query_chunks c USING(row_id)
      WHERE r.generation=? AND r.id=? GROUP BY r.row_id`, generation, String(identifier)).first<{row_id: number; fingerprint: string; chunks: number; last: number}>();
    requireValue(row && row.chunks === input.chunks && row.last === input.chunks! - 1, "Search chunks are incomplete");
    if (full) {
      await prepare("UPDATE query_results SET expected_chunks=? WHERE row_id=?", input.chunks!, row!.row_id).run();
    } else {
      const prior = await prepare("SELECT fingerprint FROM query_results WHERE generation=? AND id=?", active!.generation, String(identifier)).first<{fingerprint: string}>();
      if (prior?.fingerprint !== row!.fingerprint) {
        await db.batch([
          prepare("DELETE FROM query_results WHERE generation=? AND id=?", active!.generation, String(identifier)),
          prepare(`INSERT INTO query_results(generation,id,published_at,trust,repository,fingerprint,expected_chunks)
            SELECT ?,id,published_at,trust,repository,fingerprint,? FROM query_results WHERE row_id=?`, active!.generation, input.chunks!, row!.row_id),
          prepare(`INSERT INTO query_summaries SELECT r.row_id,s.summary FROM query_results r,query_summaries s
            WHERE r.generation=? AND r.id=? AND s.row_id=?`, active!.generation, String(identifier), row!.row_id),
          prepare(`INSERT INTO query_codes SELECT r.row_id,c.kind,c.code FROM query_results r,query_codes c
            WHERE r.generation=? AND r.id=? AND c.row_id=?`, active!.generation, String(identifier), row!.row_id),
          prepare(`INSERT INTO query_chunks(row_id,position,tokens) SELECT r.row_id,c.position,c.tokens
            FROM query_results r,query_chunks c WHERE r.generation=? AND r.id=? AND c.row_id=?`, active!.generation, String(identifier), row!.row_id),
          prepare("UPDATE query_meta SET revision=revision+1 WHERE singleton=1"),
        ]);
      }
      // Retain the buffer until collection so retrying a lost complete response
      // can confirm the same fingerprint without rewriting the active record.
    }
    return json({ ok: true });
  }
  if (input.op === "finish") {
    if (full) {
      requireValue(Number.isSafeInteger(input.results) && input.results! >= 0, "Invalid result count");
      const counts = await prepare(`SELECT count(*) AS total,sum(expected_chunks<0) AS unfinished
        FROM query_results WHERE generation=?`, generation).first<{total: number; unfinished: number}>();
      requireValue(counts?.total === input.results && !counts?.unfinished, "Full index is incomplete");
      await db.batch([
        prepare("INSERT INTO query_meta(singleton,generation,revision) VALUES(1,?,1) ON CONFLICT(singleton) DO UPDATE SET generation=excluded.generation,revision=query_meta.revision+1", generation),
      ]);
    } else {
      await prepare("UPDATE query_generations SET release=? WHERE generation=?", release, active!.generation).run();
    }
    return json({ ok: true, release });
  }
  // Garbage collection is bounded per call, including stale upload buffers.
  // It never deletes the active generation. The publisher drains it after success.
  if (input.op === "collect") {
    const removed = await db.prepare(`DELETE FROM query_results WHERE row_id IN (
      SELECT row_id FROM query_results WHERE generation!=(SELECT generation FROM query_meta WHERE singleton=1) LIMIT 500
    )`).run();
    await db.prepare("DELETE FROM query_generations WHERE results=0 AND generation!=(SELECT generation FROM query_meta WHERE singleton=1)").run();
    return json({ more: removed.meta.changes > 0 });
  }
  return invalid("Unknown update operation");
}
