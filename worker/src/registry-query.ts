import { STOPWORDS } from "./search-words";

export const PAGE_SIZE = 25;
export const RESPONSE_BYTES = 512 * 1024;
export const SUMMARY_BYTES = 16 * 1024;
export const BODY_BYTES = 256 * 1024;
export const ID = /^PALOMAR-\d{4}-\d{2}-\d{2}-\d{6}$/;
const encoder = new TextEncoder();
export const byteLength = (s: string): number => encoder.encode(s).length;

export class QueryError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
export function json(value: unknown, status = 200): Response {
  const body = JSON.stringify(value);
  if (byteLength(body) > RESPONSE_BYTES) throw new Error("query response exceeds byte bound");
  return new Response(body, { status, headers: {
    "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*", "X-Content-Type-Options": "nosniff",
  } });
}
export function words(value: string): string[] {
  return [...new Set(value.normalize("NFKD").replace(/\p{Mn}/gu, "").toLowerCase()
    .split(/[^a-z0-9]+/).filter(word => word.length >= 2 && word.length <= 32))];
}
function bad(message: string): never { throw new QueryError(400, "invalid_query", message); }
function day(value: string): string {
  if (!value) return value;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(Date.parse(value)) ||
      new Date(value).toISOString().slice(0, 10) !== value) bad("Invalid date");
  return value;
}
type Cursor = { revision: number; query: string; direction: "next" | "previous"; date: string; id: string };
function encodeCursor(value: Cursor): string { return btoa(JSON.stringify(value)); }
function decodeCursor(value: string): Cursor {
  if (value.length > 1024) bad("Cursor is too long");
  try {
    const c = JSON.parse(atob(value)) as Cursor;
    if (!Number.isSafeInteger(c.revision) || c.revision < 1 || !/^[a-f0-9]{64}$/.test(c.query) ||
        !["next", "previous"].includes(c.direction) || !ID.test(c.id) ||
        !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(c.date)) bad("Invalid cursor");
    return c;
  } catch { return bad("Invalid cursor"); }
}
async function digest(value: string): Promise<string> {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value)))]
    .map(n => n.toString(16).padStart(2, "0")).join("");
}

export async function queryResults(request: Request, db: D1Database): Promise<Response> {
  const p = new URL(request.url).searchParams;
  const allowed = new Set(["q", "arxiv", "msc", "trust", "order", "from", "to", "cursor", "id"]);
  for (const key of p.keys()) if (!allowed.has(key) || p.getAll(key).length !== 1) bad("Unknown or repeated query parameter");
  const id = p.get("id") ?? "";
  if (id && !ID.test(id)) bad("Invalid result identifier");
  const q = p.get("q") ?? "";
  if (byteLength(q) > 4096) bad("Use at most 4096 bytes of search text");
  const asked = words(q);
  if (asked.length > 20) bad("Use at most 20 distinct search words");
  const terms = asked.filter(term => !STOPWORDS.has(term)).sort();
  const dropped = asked.filter(term => STOPWORDS.has(term));
  const arxiv = p.get("arxiv") ?? "";
  const msc = (p.get("msc") ?? "").toUpperCase();
  const trust = p.get("trust") || "all";
  const order = p.get("order") || "updated";
  const from = day(p.get("from") ?? "");
  const to = day(p.get("to") ?? "");
  if (arxiv && (arxiv.length > 32 || !/^[a-z]+(?:-[a-z]+)*(?:\.[A-Za-z-]+)?$/.test(arxiv))) bad("Invalid arXiv code");
  if (msc && !/^(?:\d{1,2}|\d{2}[A-Z-]|\d{2}[A-Z-]\d|\d{2}[A-Z-]\d{2})$/.test(msc)) bad("Invalid MSC prefix");
  if (!["all", "high", "qualified"].includes(trust)) bad("Invalid dependency filter");
  if (!["updated", "registered"].includes(order)) bad("Invalid order");
  if (from && to && from > to) bad("The date range ends before it begins");
  const query = await digest(JSON.stringify({ id, terms, arxiv, msc, trust, order, from, to, unsearchable: Boolean(q.trim() && !terms.length) }));
  const cursor = p.has("cursor") ? decodeCursor(p.get("cursor")!) : null;
  if (cursor && cursor.query !== query) bad("Cursor belongs to a different query");

  const clauses = ["r.generation=(SELECT generation FROM query_meta WHERE singleton=1)"];
  const bindings: (string | number)[] = [];
  if (q.trim() && !terms.length) clauses.push("0");
  let source = "query_results r";
  if (terms.length) {
    // Match each word independently, then intersect at RESULT level. Words
    // may occur in different chunks; repeated chunks cannot satisfy a word twice.
    // Drive lookups from matching IDs. An IN subquery lets SQLite scan the
    // date index all the way to an old rare match just to preserve ordering.
    source = `(SELECT c.row_id FROM json_each(?) term
      JOIN query_fts ON query_fts MATCH ('"' || term.value || '"')
      JOIN query_chunks c ON c.chunk_id=query_fts.rowid
      GROUP BY c.row_id HAVING count(DISTINCT term.value)=?) matches
      CROSS JOIN query_results r`;
    clauses.push("r.row_id=matches.row_id");
    bindings.push(JSON.stringify(terms), terms.length);
  }
  if (id) { clauses.push("r.id=?"); bindings.push(id); }
  if (trust !== "all") { clauses.push("r.trust=?"); bindings.push(trust); }
  for (const [kind, value] of [["arxiv", arxiv], ["msc", msc]]) {
    if (!value) continue;
    clauses.push(`EXISTS(SELECT 1 FROM query_codes code WHERE code.row_id=r.row_id AND code.kind=? AND ${kind === "msc" ? "code.code>=? AND code.code<?" : "code.code=?"})`);
    bindings.push(kind, value);
    if (kind === "msc") bindings.push(value + "\uffff");
  }
  if (from) {
    clauses.push(order === "updated" ? "r.published_at>=?" : "r.id>=?");
    bindings.push(order === "updated" ? from + "T00:00:00Z" : "PALOMAR-" + from + "-000000");
  }
  if (to) {
    clauses.push(order === "updated" ? "r.published_at<=?" : "r.id<=?");
    bindings.push(order === "updated" ? to + "T23:59:59Z" : "PALOMAR-" + to + "-999999");
  }
  const ascending = cursor?.direction === "previous";
  const direction = ascending ? "ASC" : "DESC";
  if (cursor) {
    const cmp = ascending ? ">" : "<";
    clauses.push(order === "updated" ? `(r.published_at,r.id)${cmp}(?,?)` : `r.id${cmp}?`);
    if (order === "updated") bindings.push(cursor.date);
    bindings.push(cursor.id);
  }
  const sort = order === "updated" ? `r.published_at ${direction},r.id ${direction}` : `r.id ${direction}`;
  // Summaries are read only AFTER the indexed selection and limit.
  const sql = `WITH selected AS MATERIALIZED (SELECT r.row_id,r.id,r.published_at
    FROM ${source} WHERE ${clauses.join(" AND ")} ORDER BY ${sort} LIMIT 26)
    SELECT selected.*,s.summary FROM selected JOIN query_summaries s USING(row_id)
    ORDER BY ${order === "updated" ? `published_at ${direction},` : ""} id ${direction}`;
  const [metadata, selected] = await db.batch<Record<string, unknown>>([
    db.prepare("SELECT m.revision,g.release,g.results,g.projects FROM query_meta m JOIN query_generations g USING(generation) WHERE singleton=1"),
    db.prepare(sql).bind(...bindings),
  ]);
  const meta = metadata.results[0];
  if (!meta) throw new QueryError(503, "index_unavailable", "Registry search is temporarily unavailable");
  if (cursor && cursor.revision !== meta.revision) throw new QueryError(409, "registry_changed", "The registry changed. Refresh these results to continue.");
  const more = selected.results.length > PAGE_SIZE;
  const rows = selected.results.slice(0, PAGE_SIZE);
  if (ascending) rows.reverse();
  const makeCursor = (row: Record<string, unknown>, direction: Cursor["direction"]) => encodeCursor({
    revision: Number(meta.revision), query, direction, date: String(row.published_at), id: String(row.id),
  });
  return json({
    schema_version: 1, revision: meta.revision, release: meta.release,
    totals: { results: meta.results, projects: meta.projects },
    entries: rows.map(row => JSON.parse(String(row.summary))),
    previous: rows.length && (ascending ? more : Boolean(cursor)) ? makeCursor(rows[0], "previous") : null,
    next: rows.length && (ascending ? Boolean(cursor) : more) ? makeCursor(rows[rows.length - 1], "next") : null,
    dropped, message: q.trim() && !terms.length ? "Enter a searchable word; common words are ignored." : null,
  });
}
