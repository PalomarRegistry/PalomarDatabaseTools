const POINTER_KEY = "_current.json";
const POINTER_LIMIT = 1024;
const AVAILABILITY_KEY = "public/source-availability.json";
const AVAILABILITY_TARGETS_KEY = "public/source-availability-targets.json";
const AVAILABILITY_OPERATION = "/_operations/source-availability";
const AVAILABILITY_LIMIT = 5 * 1024 * 1024;
const RELEASE_RE = /^[0-9a-f]{64}$/;
const IDENTIFIER = "PALOMAR-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}";
const VERSIONED = `${IDENTIFIER}-v[1-9][0-9]*`;
/**
 * One bounded policy for every public representation response, including a
 * 304 that refreshes one. Records and render bundles are byte-stable, while
 * feeds and pages change in place, but all can disappear after a withdrawal.
 * A one-minute freshness window permits reuse without leaving any cached copy
 * usable indefinitely; `must-revalidate` forbids stale use after that.
 *
 * Missing, invalid, unavailable, health and failed-precondition responses use
 * `no-store` separately.
 */
export const PUBLIC_CACHE_CONTROL = "public, max-age=60, must-revalidate";
/**
 * A year document and the pages of one day, which is the shape both browsing
 * and each classification code's archive use. A page number has no leading
 * zero, so one key spells one page: `/1.json` and `/01.json` would otherwise be
 * two public names for one document.
 */
const DAY_PAGES = "(?:[0-9]{4}\\.json|[0-9]{4}-[0-9]{2}-[0-9]{2}/[1-9][0-9]*\\.json)";
const PUBLIC_PATHS = [
  // No whole-registry index. `index.json` is a staging intermediate the other
  // surfaces are derived from, is not part of a release, and is not served:
  // it was the one served object whose size was the registry's.
  /^\/(?:feed\.xml|recent(?:-renders)?\.json|source-availability(?:-targets)?\.json|LICENSE)$/,
  /^\/schema-v3\.json$/,
  new RegExp(`^/entries/${VERSIONED}\\.json$`),
  new RegExp(`^/tombstones/${VERSIONED}\\.json$`),
  // Every version of one result. An entry page reads this instead of the whole
  // index, which is the difference between four hundred bytes and the registry.
  new RegExp(`^/versions/${IDENTIFIER}\\.json$`),
  // Intake lookups are bounded derived documents: one exact identity digest,
  // or at most five hundred registrations for one repository.
  /^\/registration-identities\/[0-9a-f]{64}\.json$/,
  /^\/repositories\/[a-z0-9_.-]+\/[a-z0-9_.-]+\.json$/,
  // Browsing: a head naming the years, a document per year naming its days,
  // and a page per day and band of two hundred serials. The grammar is exactly
  // what those documents can name, so a head that named anything else would be
  // pointing a reader at a path this Worker does not serve.
  new RegExp(`^/browse/(?:index\\.json|${DAY_PAGES})$`),
  /^\/feeds\/(?:arxiv|msc)\/[A-Za-z0-9.-]+\.xml$/,
  // One postings sequence per indexed word: a constant-size head naming how
  // many pages there are, and the pages. A client builds this path from typed
  // text with no dictionary to check it against, which is why the term grammar
  // is this narrow -- anything a word cannot be is not a request this Worker
  // will make of the bucket.
  /^\/search\/t\/[a-z0-9]{2,32}\/(?:head|[0-9]{1,4})\.json$/,
  // The words the indexer drops, so a reader can drop them from a query too
  // rather than reading a missing head as "nothing carries this".
  /^\/search\/stopwords\.json$/,
  // Under one classification code: the front page of the newest results, which
  // is the same set the feed for that code carries and the same grammar for
  // its name, and the archive of the rest, paged exactly as browsing is.
  /^\/subjects\/(?:arxiv|msc)\/[A-Za-z0-9.-]+\.json$/,
  new RegExp(`^/subjects/(?:arxiv|msc)/[A-Za-z0-9.-]+/${DAY_PAGES}$`),
  new RegExp(`^/renders/${VERSIONED}/[0-9a-f]{64}/[A-Za-z0-9._/-]+$`),
  new RegExp(`^/evidence/${VERSIONED}/[0-9a-f]{64}/[A-Za-z0-9._/-]+$`),
];

/**
 * An object is keyed by how it changes. Records that never change sit at a
 * stable key and are written once; only the aggregates that change on every
 * publication stay under a release, where the pointer still makes them one
 * consistent set.
 *
 * Objects were once keyed under the release that published them, which meant
 * adding one entry rewrote the whole dataset. That layout is gone, and so is
 * reading the pointer to find out which one is in use.
 */
type PathClass = "immutable" | "stable" | "snapshot";

const IMMUTABLE_PREFIXES = ["/entries/", "/renders/", "/evidence/"];
/**
 * Also at a key that does not depend on the release, but rewritten in place
 * rather than written once. A feed is a notification channel and is allowed to
 * run briefly ahead of or behind the rest of the release, which is what an RSS
 * reader assumes anyway; in exchange, one new entry rewrites the handful of
 * categories it belongs to instead of every category under a fresh release
 * prefix. `recent.json` is the same bargain for the landing page, and
 * `recent-renders.json` is rewritten with it. Tombstones
 * also stay at stable keys, but are written only by a full takedown/restoration
 * release; ordinary accepted releases leave historical tombstones untouched.
 */
const STABLE_PATHS = [
  "/source-availability.json",
  "/source-availability-targets.json",
  "/feed.xml",
  "/recent.json",
  "/recent-renders.json",
];
const STABLE_PREFIXES = [
  "/browse/",
  "/feeds/",
  "/registration-identities/",
  "/repositories/",
  "/search/",
  "/subjects/",
  "/tombstones/",
  "/versions/",
];

/**
 * Anything unrecognised is deliberately a snapshot path. A public path someone
 * forgets to classify then becomes release-keyed, which is only more expensive;
 * calling it immutable would silently freeze a file that changes. The publisher
 * has to make the same decision, so the table it will be checked against lives
 * outside both of them, in tests/path-classes.json.
 */
export function pathClass(pathname: string): PathClass {
  if (
    STABLE_PATHS.includes(pathname) ||
    STABLE_PREFIXES.some((prefix) => pathname.startsWith(prefix))
  ) {
    return "stable";
  }
  return IMMUTABLE_PREFIXES.some((prefix) => pathname.startsWith(prefix))
    ? "immutable"
    : "snapshot";
}

/** Where anything not made consistent by the pointer lives. */
export function stableKey(pathname: string): string {
  return `public${pathname}`;
}

/** Where an aggregate lives, which is the only thing the release decides. */
export function snapshotKey(pathname: string, release: string): string {
  return `snapshots/${release}${pathname}`;
}

/**
 * A release is complete when its delta is there, because the publisher writes
 * it last. That is what tells a genuine 404 apart from a release still being
 * uploaded, and it is why nothing else may be written after it.
 */
function manifestKey(release: string): string {
  return `snapshots/${release}/release-delta.json`;
}

/**
 * The exact policy `tools/render_validation.py` requires inside every render
 * bundle, as `RENDER_CSP` there. That file is the source of truth and the
 * validator is what refuses a bundle carrying anything else; this is a copy,
 * because a Worker cannot import a Python constant, and
 * `tests/test_render_csp.py` fails if the two stop agreeing. Copied verbatim,
 * including `navigate-to`, which browsers no longer implement: the value of
 * this list is being the same list, not being a curated one.
 *
 * Sent as a header as well as carried in the bundle's own `<meta>`, and that is
 * the point of it being here. A `<meta>` policy binds from the moment the
 * parser reaches it, so whatever precedes it in the document is covered by
 * nothing: a response truncated before the tag, markup the browser recovers
 * from differently than the validator's parser did, a tag the validator saw in
 * a place the browser treats as content. A header binds before the first byte
 * is parsed. These directives are the same ones, so a browser enforcing both
 * enforces one policy; the header additionally carries `frame-ancestors`, which
 * a `<meta>` policy cannot express.
 */
const RENDER_CSP = [
  "default-src 'none'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "connect-src 'self'",
  "font-src 'self'",
  "object-src 'none'",
  "frame-src 'none'",
  "child-src 'none'",
  "worker-src 'none'",
  "manifest-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
  "navigate-to 'self'",
].join("; ");

/**
 * Who may frame a render, which the bundle's `<meta>` cannot say at all:
 * `frame-ancestors` is ignored in a meta policy and means something only in a
 * header.
 *
 * One origin, because there is one site. The registry's pages embed a render
 * with `sandbox="allow-scripts"` and deliberately without `allow-same-origin`,
 * and that iframe keeps working: `frame-ancestors` is matched against each
 * ancestor document's origin, which for the embedding page is the site's, and
 * not against the framed document's own opaque one.
 *
 * What this refuses is a browser anywhere else putting a submitter's rendered
 * Lean inside its own chrome, and a page on `localhost` framing production
 * renders during development. Local work uses the fixture server in
 * PalomarWeb's `tests/`, which serves its own renders, so the second is a
 * refusal rather than a cost. It is a framing rule and nothing more: the bytes
 * stay fetchable, navigable and copyable by anyone, which is what a public
 * registry is for.
 *
 * `X-Frame-Options` is deliberately not set beside this. Its only value that
 * permits anything is `SAMEORIGIN`, which is false here: the one legitimate
 * framer is a different origin from this one by design.
 */
const RENDER_FRAME_ANCESTORS = "frame-ancestors https://palomar-registry.org";

/**
 * A render bundle is the only HTML this Worker serves, and it is submitted
 * material. The rest is JSON, XML and plain text, which the render policy has
 * nothing to say about, so sending it on those would be a header in every
 * response that answers a question nobody asked.
 *
 * Decided from the media type rather than the path, because that header is what
 * decides whether a browser builds a document out of the bytes at all, and
 * `nosniff` above makes it the only thing that decides it. Compared as a media
 * type too: the essence, lowercased, without its parameters, since `text/html`
 * and `Text/HTML; charset=utf-8` are one type and `text/html-summary` is not.
 */
function isHtmlDocument(contentType: string | undefined): boolean {
  return (contentType ?? "").split(";", 1)[0].trim().toLowerCase() === "text/html";
}

function applyHtmlPolicy(headers: Headers, contentType: string | undefined): void {
  if (!isHtmlDocument(contentType)) return;
  headers.set("Content-Security-Policy", `${RENDER_CSP}; ${RENDER_FRAME_ANCESTORS}`);
}

function commonHeaders(cache = "no-store"): Headers {
  return new Headers({
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": cache,
    // The origin negotiates encoding, and answers with a weak validator for
    // the compressed variant and a strong one for identity. Without this a
    // shared cache could hand a brotli body to a client that asked for none.
    // Harmless while nothing is stored; wrong the moment anything is.
    Vary: "Accept-Encoding",
    "X-Content-Type-Options": "nosniff",
  });
}

function response(status: number, message: string): Response {
  const headers = commonHeaders();
  headers.set("Content-Type", "text/plain; charset=utf-8");
  return new Response(message, { status, headers });
}

async function authenticated(request: Request, token: string): Promise<boolean> {
  const supplied = request.headers.get("Authorization") ?? "";
  const expected = `Bearer ${token}`;
  const [left, right] = await Promise.all([
    crypto.subtle.digest("SHA-256", new TextEncoder().encode(supplied)),
    crypto.subtle.digest("SHA-256", new TextEncoder().encode(expected)),
  ]);
  if (token.length < 32) return false;
  if (typeof crypto.subtle.timingSafeEqual === "function") {
    return crypto.subtle.timingSafeEqual(left, right);
  }
  // Node's Web Crypto used by the fast fake-binding tests has not yet picked
  // up the Workers extension. Production always takes the primitive above;
  // keep the tests and non-Workers development runtime behaviorally exact.
  const a = new Uint8Array(left);
  const b = new Uint8Array(right);
  let difference = 0;
  for (let position = 0; position < a.length; position += 1) {
    difference |= a[position] ^ b[position];
  }
  return difference === 0;
}

async function readBoundedBody(request: Request): Promise<Uint8Array | null> {
  const declared = Number(request.headers.get("Content-Length"));
  if (Number.isFinite(declared) && declared > AVAILABILITY_LIMIT) return null;
  if (request.body === null) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.byteLength;
    if (length > AVAILABILITY_LIMIT) {
      await reader.cancel("source availability body exceeds limit");
      return null;
    }
    chunks.push(value);
  }
  const result = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

type AvailabilityTarget = {
  source_repository: string;
  commit: string;
  fork_repository: string;
};

function targetKey(target: AvailabilityTarget): string {
  return `${target.source_repository.toLowerCase()}\0${target.commit}\0${target.fork_repository.toLowerCase()}`;
}

function targetsOf(value: unknown, field: string): AvailabilityTarget[] {
  if (!Array.isArray(value)) throw new Error(`${field} is not an array`);
  const targets = value.map((candidate, position) => {
    if (typeof candidate !== "object" || candidate === null) {
      throw new Error(`${field}[${position}] is not an object`);
    }
    const row = candidate as Record<string, unknown>;
    const target = {
      source_repository: row.source_repository,
      commit: row.commit,
      fork_repository: row.fork_repository,
    };
    if (
      typeof target.source_repository !== "string" ||
      typeof target.commit !== "string" ||
      typeof target.fork_repository !== "string" ||
      !/^[0-9a-f]{40}$/.test(target.commit) ||
      Object.keys(row).sort().join(",") !== "commit,fork_repository,source_repository"
    ) throw new Error(`${field}[${position}] is malformed`);
    return target as AvailabilityTarget;
  });
  const keys = targets.map(targetKey);
  if (new Set(keys).size !== keys.length || keys.join("\n") !== [...keys].sort().join("\n")) {
    throw new Error(`${field} is duplicated or not canonical`);
  }
  return targets;
}

async function targetsDigest(targets: AvailabilityTarget[]): Promise<string> {
  const bytes = new TextEncoder().encode(`${JSON.stringify(targets)}\n`);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return [...digest].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function validObservation(value: unknown): boolean {
  if (typeof value !== "object" || value === null) return false;
  const row = value as Record<string, unknown>;
  return (
    Object.keys(row).sort().join(",") ===
      "checked_at,consecutive_missing,last_attempt_at,last_error,status" &&
    ["available", "missing", "unknown"].includes(String(row.status)) &&
    (row.checked_at === null || typeof row.checked_at === "string") &&
    (row.last_attempt_at === null || typeof row.last_attempt_at === "string") &&
    Number.isInteger(row.consecutive_missing) && Number(row.consecutive_missing) >= 0 &&
    (row.last_error === null || typeof row.last_error === "string")
  );
}

async function validateAvailabilityBody(
  bytes: Uint8Array,
  bucket: R2Bucket,
): Promise<boolean> {
  let value: unknown;
  try {
    value = JSON.parse(
      new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes),
    );
  } catch {
    return false;
  }
  if (typeof value !== "object" || value === null) return false;
  const manifest = value as Record<string, unknown>;
  if (
    Object.keys(manifest).sort().join(",") !==
      "coverage,generated_at,publication_revision,repositories,schema_version,targets_sha256" ||
    manifest.schema_version !== 2 ||
    typeof manifest.generated_at !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(manifest.generated_at) ||
    !Number.isInteger(manifest.publication_revision) ||
    Number(manifest.publication_revision) < 1 ||
    typeof manifest.coverage !== "object" || manifest.coverage === null ||
    typeof manifest.targets_sha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(manifest.targets_sha256)
  ) return false;

  let rows: AvailabilityTarget[];
  try {
    if (!Array.isArray(manifest.repositories)) return false;
    rows = targetsOf(manifest.repositories.map((candidate) => {
      if (typeof candidate !== "object" || candidate === null) return candidate;
      const row = candidate as Record<string, unknown>;
      if (
        Object.keys(row).sort().join(",") !==
          "archive,commit,fork_repository,original,source_repository" ||
        !validObservation(row.original) || !validObservation(row.archive)
      ) throw new Error("availability row is malformed");
      return {
        source_repository: row.source_repository,
        commit: row.commit,
        fork_repository: row.fork_repository,
      };
    }), "repositories");
  } catch {
    return false;
  }

  const targetObject = await bucket.get(AVAILABILITY_TARGETS_KEY);
  if (targetObject === null || !("body" in targetObject) || targetObject.size > AVAILABILITY_LIMIT) {
    return false;
  }
  try {
    const targetDocument = await targetObject.json<Record<string, unknown>>();
    if (
      Object.keys(targetDocument).sort().join(",") !==
        "database_commit,generated_at,schema_version,targets" ||
      targetDocument.schema_version !== 1
    ) return false;
    const targets = targetsOf(targetDocument.targets, "targets");
    const expectedDigest = await targetsDigest(targets);
    return (
      expectedDigest === manifest.targets_sha256 &&
      rows.map(targetKey).join("\n") === targets.map(targetKey).join("\n")
    );
  } catch {
    return false;
  }
}

async function updateAvailability(request: Request, env: Env): Promise<Response> {
  if (!(await authenticated(request, env.PALOMAR_AVAILABILITY_UPDATE_TOKEN))) {
    return response(401, "Unauthorized\n");
  }
  const ifMatch = request.headers.get("If-Match");
  const ifNoneMatch = request.headers.get("If-None-Match");
  if (ifMatch === null && ifNoneMatch !== "*") {
    return response(428, "An R2 write precondition is required\n");
  }
  const bytes = await readBoundedBody(request);
  if (bytes === null) return response(413, "Payload too large\n");
  if (!(await validateAvailabilityBody(bytes, env.DATA))) {
    return response(422, "Invalid source availability manifest\n");
  }
  const stored = await env.DATA.put(AVAILABILITY_KEY, bytes, {
    onlyIf: request.headers,
    httpMetadata: { contentType: "application/json; charset=utf-8" },
  });
  if (stored === null) return response(412, "Source availability changed; retry\n");
  const headers = commonHeaders();
  headers.set("ETag", stored.httpEtag);
  return new Response(null, { status: 204, headers });
}

function publicPath(pathname: string): boolean {
  return (
    !pathname.includes("%") &&
    !pathname.includes("\\") &&
    !pathname.includes("//") &&
    !pathname.split("/").includes(".") &&
    !pathname.split("/").includes("..") &&
    PUBLIC_PATHS.some((pattern) => pattern.test(pathname))
  );
}

async function currentRelease(bucket: R2Bucket): Promise<string | null> {
  const pointer = await bucket.get(POINTER_KEY);
  if (pointer === null || pointer.size > POINTER_LIMIT) return null;
  const value: unknown = await pointer.json();
  if (
    typeof value !== "object" ||
    value === null ||
    Object.keys(value).sort().join(",") !== "publication_base,release,schema_version"
  ) {
    return null;
  }
  const typed = value as {
    schema_version?: unknown;
    release?: unknown;
    publication_base?: unknown;
  };
  if (typeof typed.release !== "string" || !RELEASE_RE.test(typed.release)) return null;
  if (
    typeof typed.publication_base !== "string" ||
    !RELEASE_RE.test(typed.publication_base)
  ) return null;
  return typed.schema_version === 3 ? typed.release : null;
}

async function releaseReady(bucket: R2Bucket, release: string): Promise<boolean> {
  return (await bucket.head(manifestKey(release))) !== null;
}

function conditionalStatus(request: Request, etag: string, uploaded: Date): number {
  // R2 returns the same bodyless object for every failed condition. Work out
  // which HTTP answer that means in conditional-header precedence order.
  const ifMatch = request.headers.get("If-Match");
  if (ifMatch !== null) {
    const matches = ifMatch.trim() === "*" || ifMatch.split(",").some(
      (candidate) => candidate.trim() === etag,
    );
    if (!matches) return 412;
  } else {
    // If-Match suppresses If-Unmodified-Since. Invalid dates are ignored.
    const value = request.headers.get("If-Unmodified-Since");
    const instant = value === null ? Number.NaN : Date.parse(value);
    if (
      !Number.isNaN(instant) &&
      Math.floor(uploaded.getTime() / 1000) > Math.floor(instant / 1000)
    ) return 412;
  }
  // With the write preconditions satisfied, the failed condition was a GET or
  // HEAD cache validator (If-None-Match or If-Modified-Since).
  return 304;
}

function contentRange(object: R2ObjectBody): string | null {
  const range = object.range;
  if (
    range === undefined ||
    !("offset" in range) ||
    !("length" in range) ||
    typeof range.offset !== "number" ||
    typeof range.length !== "number"
  ) return null;
  return `bytes ${range.offset}-${range.offset + range.length - 1}/${object.size}`;
}

/** The response for a key, or null when the bucket does not hold it. */
async function serve(request: Request, bucket: R2Bucket, key: string): Promise<Response | null> {
  const options: R2GetOptions = { onlyIf: request.headers };
  const requestedRange = request.headers.has("Range");
  if (requestedRange) options.range = request.headers;
  const object = await bucket.get(key, options);
  if (object === null) return null;
  // Read before narrowing, because both shapes carry these and only one of
  // them survives the check below.
  const etag = object.httpEtag;
  const uploaded = object.uploaded;
  const contentType = object.httpMetadata?.contentType;
  if (!("body" in object)) {
    const status = conditionalStatus(request, etag, uploaded);
    // A 304 updates a reusable representation and carries its normal policy.
    // A 412 is a client-specific precondition failure, not a representation
    // another request can reuse, and is deliberately unstorable.
    const headers = commonHeaders(status === 304 ? PUBLIC_CACHE_CONTROL : "no-store");
    headers.set("ETag", etag);
    if (status === 304) {
      // A cache updates its stored headers from a 304 and leaves the rest as
      // they were, so the render policy has to be refreshed too. The type
      // comes from the stored object; this response correctly has no body.
      applyHtmlPolicy(headers, contentType);
    }
    return new Response(null, { status, headers });
  }

  const headers = commonHeaders(PUBLIC_CACHE_CONTROL);
  object.writeHttpMetadata(headers);
  // After writeHttpMetadata, which replays whatever cache metadata was stored
  // on the object when it was written. What this Worker serves is decided
  // here, not by whatever the publisher happened to record.
  headers.set("Cache-Control", PUBLIC_CACHE_CONTROL);
  headers.set("ETag", etag);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("X-Content-Type-Options", "nosniff");
  // After the content type is settled, since that is what decides whether this
  // response is a document a policy can apply to.
  applyHtmlPolicy(headers, headers.get("Content-Type") ?? undefined);
  const range = requestedRange ? contentRange(object) : null;
  if (range !== null) {
    const returnedLength = object.range && "length" in object.range ? object.range.length : undefined;
    headers.set("Content-Range", range);
    headers.set("Content-Length", String(returnedLength));
    headers.set("Accept-Ranges", "bytes");
  } else {
    headers.set("Content-Length", String(object.size));
  }
  return new Response(request.method === "HEAD" ? null : object.body, {
    status: range === null ? 200 : 206,
    headers,
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const started = Date.now();
    const url = new URL(request.url);
    if (url.pathname === AVAILABILITY_OPERATION && request.method === "PUT") {
      try {
        return await updateAvailability(request, env);
      } catch (error) {
        console.error(JSON.stringify({
          event: "source_availability_update_failed",
          error: error instanceof Error ? error.name : "unknown",
        }));
        return response(503, "Source availability update failed\n");
      }
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return response(405, "Method not allowed\n");
    }
    // Refused, not ignored. A query string is a distinct request, and becomes
    // a distinct cache key anywhere caching is configured. Nothing served here
    // takes a parameter, so there is no second spelling to accept; refusing it
    // before routing also costs no R2 read.
    if (url.search !== "") {
      return response(404, "Not found\n");
    }
    if (url.pathname !== "/healthz" && !publicPath(url.pathname)) {
      return response(404, "Not found\n");
    }
    try {
      if (url.pathname === "/healthz") {
        // The only route that still proves the whole chain: a pointer this
        // Worker understands, naming a release whose manifest is there.
        const release = await currentRelease(env.DATA);
        if (release === null || !(await releaseReady(env.DATA, release))) {
          return response(503, "Public data temporarily unavailable\n");
        }
        const headers = commonHeaders();
        headers.set("Content-Type", "application/json; charset=utf-8");
        return new Response('{"ok":true}\n', { status: 200, headers });
      }

      // A record, a feed and the availability manifest are at a key that does
      // not depend on the release, so serving one costs a single read. This is
      // what the layout was changed for, on the read side.
      if (pathClass(url.pathname) !== "snapshot") {
        const served = await serve(request, env.DATA, stableKey(url.pathname));
        return served ?? response(404, "Not found\n");
      }

      const release = await currentRelease(env.DATA);
      if (release === null) return response(503, "Public data temporarily unavailable\n");
      const served = await serve(request, env.DATA, snapshotKey(url.pathname, release));
      if (served !== null) return served;
      // Absent from a release that is there is a 404; absent because the
      // release is half-written is not. Only the miss pays for that answer.
      return (await releaseReady(env.DATA, release))
        ? response(404, "Not found\n")
        : response(503, "Public data temporarily unavailable\n");
    } catch (error) {
      console.error(
        JSON.stringify({
          event: "public_data_request_failed",
          method: request.method,
          path: url.pathname,
          duration_ms: Date.now() - started,
          error: error instanceof Error ? error.name : "unknown",
        }),
      );
      return response(503, "Public data temporarily unavailable\n");
    }
  },
} satisfies ExportedHandler<Env>;
