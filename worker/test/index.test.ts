import { describe, expect, it } from "vitest";

import worker, { PUBLIC_CACHE_CONTROL } from "../src/index";
import { FakeBucket, env, release } from "./fakes";

const request = (path: string) => new Request(`https://data.example${path}`);

describe("public data boundary", () => {
  it("streams an allowed object with revalidation and public CORS", async () => {
    const bucket = new FakeBucket();
    bucket.put(`snapshots/${release}/LICENSE`, "licence\n");
    const response = await worker.fetch(request("/LICENSE"), env(bucket));
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("licence\n");
    expect(response.headers.get("Cache-Control")).toBe(PUBLIC_CACHE_CONTROL);
    expect(response.headers.get("Access-Control-Allow-Origin")).toBe("*");
    expect(response.headers.get("X-Content-Type-Options")).toBe("nosniff");
  });

  it("does not expose pointers, releases, listings, traversal, or writes", async () => {
    const bucket = new FakeBucket();
    for (const [method, path] of [["GET", "/_current.json"], ["GET", `/snapshots/${release}/LICENSE`], ["GET", "/tombstones/"], ["GET", "/entries/%2e%2e/recent.json"], ["POST", "/recent.json"]]) {
      const response = await worker.fetch(new Request(`https://data.example${path}`, { method }), env(bucket));
      expect([404, 405]).toContain(response.status);
    }
  });

  it("does not serve a whole-registry index, wherever one might still be", async () => {
    // Nothing publishes it any more, but a release prefix built before this
    // change still holds one, and `public/` would hold one if anything ever
    // wrote it there. Being reachable is what would keep a reader on the one
    // served object whose size was the registry's, so the path is refused
    // outright and no key is looked up at all.
    const bucket = new FakeBucket();
    bucket.put(`snapshots/${release}/index.json`, '{"schema_version":3,"entries":[]}\n');
    bucket.put("public/index.json", '{"schema_version":3,"entries":[]}\n');

    const response = await worker.fetch(request("/index.json"), env(bucket));

    expect(response.status).toBe(404);
    expect(bucket.reads).toEqual([]);
  });

  it("serves what replaced it, and announces no date on any of it", async () => {
    // The replacements are the whole argument for removing the index, so a
    // change that took it away while one of them was unreachable is the
    // failure worth catching. Nothing here is on its way out, so nothing
    // carries a retirement header.
    const bucket = new FakeBucket();
    bucket.put("public/recent.json", '{"schema_version":1,"entries":[]}\n');
    bucket.put("public/feed.xml", "<rss/>\n");
    bucket.put("public/browse/index.json", '{"years":[]}\n');
    bucket.put("public/versions/PALOMAR-2026-08-07-383237.json", '{"entries":[]}\n');
    bucket.put("public/repositories/owner/repository.json", '{"registrations":[]}\n');
    bucket.put(`public/registration-identities/${"0".repeat(64)}.json`, '{"registration_id":null}\n');
    bucket.put("public/subjects/msc/11A05.json", '{"entries":[]}\n');
    for (const path of [
      "/recent.json",
      "/feed.xml",
      "/browse/index.json",
      "/versions/PALOMAR-2026-08-07-383237.json",
      "/repositories/owner/repository.json",
      `/registration-identities/${"0".repeat(64)}.json`,
      "/subjects/msc/11A05.json",
    ]) {
      const response = await worker.fetch(request(path), env(bucket));
      expect(response.status, path).toBe(200);
      expect(response.headers.get("Deprecation"), path).toBeNull();
      expect(response.headers.get("Sunset"), path).toBeNull();
    }
  });

  it("serves only canonical repository and identity lookup paths", async () => {
    const bucket = new FakeBucket();
    bucket.put("public/repositories/owner/repository.json", "{}\n");
    bucket.put(`public/registration-identities/${"a".repeat(64)}.json`, "{}\n");
    for (const path of [
      "/repositories/owner/repository.json",
      `/registration-identities/${"a".repeat(64)}.json`,
    ]) {
      expect((await worker.fetch(request(path), env(bucket))).status, path).toBe(200);
    }
    for (const path of [
      "/repositories/Owner/repository.json",
      "/repositories/owner/repository",
      "/repositories/./repository.json",
      `/registration-identities/${"A".repeat(64)}.json`,
      `/registration-identities/${"a".repeat(63)}.json`,
    ]) {
      expect((await worker.fetch(request(path), env(bucket))).status, path).toBe(404);
    }
  });

  it("returns 404 for absent public records and serves exact tombstones", async () => {
    const bucket = new FakeBucket();
    const id = "PALOMAR-2026-08-06-000001-v1";
    let response = await worker.fetch(request(`/entries/${id}.json`), env(bucket));
    expect(response.status).toBe(404);
    bucket.put(`public/tombstones/${id}.json`, '{"taken_down_at":"2026-08-06T00:00:00Z"}');
    response = await worker.fetch(request(`/tombstones/${id}.json`), env(bucket));
    expect(response.status).toBe(200);
  });

  it("fails closed when the pointer or selected release is unavailable", async () => {
    const bucket = new FakeBucket();
    bucket.objects.delete("_current.json");
    let response = await worker.fetch(request("/LICENSE"), env(bucket));
    expect(response.status).toBe(503);
    bucket.put(
      "_current.json",
      JSON.stringify({ schema_version: 2, release, publication_base: "b".repeat(64) }),
    );
    bucket.objects.delete(`snapshots/${release}/release-delta.json`);
    response = await worker.fetch(request("/healthz"), env(bucket));
    expect(response.status).toBe(503);
  });

  it("supports conditional and range requests", async () => {
    const bucket = new FakeBucket();
    bucket.put(`snapshots/${release}/LICENSE`, "0123456789");
    let response = await worker.fetch(
      new Request("https://data.example/LICENSE", { headers: { "If-None-Match": '"etag"' } }),
      env(bucket),
    );
    expect(response.status).toBe(304);
    expect(response.headers.get("Cache-Control")).toBe(PUBLIC_CACHE_CONTROL);
    response = await worker.fetch(
      new Request("https://data.example/LICENSE", {
        headers: { "If-Match": '"etag"', "If-None-Match": '"etag"' },
      }),
      env(bucket),
    );
    expect(response.status).toBe(304);
    expect(response.headers.get("Cache-Control")).toBe(PUBLIC_CACHE_CONTROL);
    response = await worker.fetch(
      new Request("https://data.example/LICENSE", { headers: { Range: "bytes=2-5" } }),
      env(bucket),
    );
    expect(response.status).toBe(206);
    expect(response.headers.get("Cache-Control")).toBe(PUBLIC_CACHE_CONTROL);
    expect(await response.text()).toBe("2345");
    expect(response.headers.get("Content-Range")).toBe("bytes 2-5/10");
  });

  it("keeps failed write and time preconditions unstorable", async () => {
    const bucket = new FakeBucket();
    bucket.put(`snapshots/${release}/LICENSE`, "licence\n");
    const failedPreconditions: Array<Record<string, string>> = [
      { "If-Match": '"some-other-etag"' },
      { "If-Unmodified-Since": "Wed, 05 Aug 2026 00:00:00 GMT" },
      { "If-Match": '"some-other-etag"', "If-None-Match": '"etag"' },
    ];
    for (const headers of failedPreconditions) {
      const response = await worker.fetch(
        new Request("https://data.example/LICENSE", { headers }),
        env(bucket),
      );
      expect(response.status, JSON.stringify(headers)).toBe(412);
      expect(response.headers.get("Cache-Control"), JSON.stringify(headers)).toBe("no-store");
      expect(await response.text(), JSON.stringify(headers)).toBe("");
    }
  });
});

describe("source availability updates", () => {
  const target = {
    source_repository: "owner/source",
    commit: "c".repeat(40),
    fork_repository: "PalomarArchive/source",
  };

  async function digest(value: unknown): Promise<string> {
    const bytes = new TextEncoder().encode(`${JSON.stringify(value)}\n`);
    const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
    return [...hash].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async function fixture(): Promise<{ bucket: FakeBucket; body: string }> {
    const bucket = new FakeBucket();
    await bucket.put("public/source-availability-targets.json", JSON.stringify({
      schema_version: 1,
      generated_at: "2026-08-13T00:00:00Z",
      database_commit: "d".repeat(40),
      targets: [target],
    }));
    const observation = {
      status: "available",
      checked_at: "2026-08-13T00:00:00Z",
      last_attempt_at: "2026-08-13T00:00:00Z",
      consecutive_missing: 0,
      last_error: null,
    };
    return {
      bucket,
      body: JSON.stringify({
        schema_version: 2,
        generated_at: "2026-08-13T00:00:00Z",
        targets_sha256: await digest([target]),
        coverage: {},
        repositories: [{ ...target, original: observation, archive: observation }],
        publication_revision: 1,
      }) + "\n",
    };
  }

  it("requires authentication and a conditional write", async () => {
    const { bucket, body } = await fixture();
    let result = await worker.fetch(new Request(
      "https://data.example/_operations/source-availability",
      { method: "PUT", body, headers: { "If-None-Match": "*" } },
    ), env(bucket));
    expect(result.status).toBe(401);
    result = await worker.fetch(new Request(
      "https://data.example/_operations/source-availability",
      { method: "PUT", body, headers: { Authorization: `Bearer ${"t".repeat(32)}` } },
    ), env(bucket));
    expect(result.status).toBe(428);
  });

  it("accepts the exact current target set and refuses a stale writer", async () => {
    const { bucket, body } = await fixture();
    const headers = {
      Authorization: `Bearer ${"t".repeat(32)}`,
      "Content-Type": "application/json",
      "If-None-Match": "*",
    };
    let result = await worker.fetch(new Request(
      "https://data.example/_operations/source-availability",
      { method: "PUT", body, headers },
    ), env(bucket));
    expect(result.status).toBe(204);
    expect(bucket.objects.get("public/source-availability.json")).toEqual(
      new TextEncoder().encode(body),
    );
    result = await worker.fetch(new Request(
      "https://data.example/_operations/source-availability",
      { method: "PUT", body, headers },
    ), env(bucket));
    expect(result.status).toBe(412);
  });

  it("refuses a manifest for any other target set", async () => {
    const { bucket, body } = await fixture();
    const altered = body.replace("owner/source", "owner/other");
    const result = await worker.fetch(new Request(
      "https://data.example/_operations/source-availability",
      {
        method: "PUT",
        body: altered,
        headers: {
          Authorization: `Bearer ${"t".repeat(32)}`,
          "If-None-Match": "*",
        },
      },
    ), env(bucket));
    expect(result.status).toBe(422);
  });
});

describe("query strings", () => {
  it("are refused on every public path, before anything is read", async () => {
    // Query strings are distinct requests and potential cache keys, while no
    // public document takes a parameter. Refusal also prevents any R2 read.
    const bucket = new FakeBucket();
    bucket.put("public/entries/PALOMAR-2026-08-07-383237-v1.json", "{}");
    bucket.put("public/recent.json", "{}");
    bucket.put(`snapshots/${release}/LICENSE`, "licence\n");
    for (const path of [
      "/recent.json?v=1",
      "/entries/PALOMAR-2026-08-07-383237-v1.json?v=1",
      "/versions/PALOMAR-2026-08-07-383237.json?v=1",
      "/repositories/owner/repository.json?v=1",
      `/registration-identities/${"0".repeat(64)}.json?v=1`,
      "/browse/index.json?v=1",
      "/subjects/msc/11A05.json?v=1",
      "/feed.xml?utm_source=elsewhere",
      "/feeds/msc/11A05.xml?v=1",
      "/source-availability.json?v=1",
      "/LICENSE?v=1",
      "/healthz?v=1",
    ]) {
      const response = await worker.fetch(request(path), env(bucket));
      expect(response.status, path).toBe(404);
    }
    expect(bucket.reads).toEqual([]);
  });

  it("does not refuse a URL that merely ends in a question mark", async () => {
    // An empty query is no query: `?` alone is what a form with nothing in it
    // produces, and refusing it would be refusing a path that is exactly right.
    const bucket = new FakeBucket();
    bucket.put("public/feed.xml", "<rss/>\n");
    const response = await worker.fetch(request("/feed.xml?"), env(bucket));
    expect(response.status).toBe(200);
  });
});

describe("the policy on a render", () => {
  const bundle =
    "/renders/PALOMAR-2026-08-07-383237-v1/" + "b".repeat(64) + "/index.html";

  it("sends the render policy as a header, not only as the bundle's meta tag", async () => {
    // The bundle carries the same policy in a `<meta>`, which binds only from
    // the moment the parser reaches it. A header binds before the first byte
    // is parsed, and a render is submitted material served on a subdomain of
    // the site's own registrable domain.
    const bucket = new FakeBucket();
    bucket.put(`public${bundle}`, "<!DOCTYPE html>\n");
    const response = await worker.fetch(request(bundle), env(bucket));
    const policy = response.headers.get("Content-Security-Policy") ?? "";
    expect(response.status).toBe(200);
    expect(policy).toContain("default-src 'none'");
    expect(policy).toContain("script-src 'self'");
    expect(policy).toContain("object-src 'none'");
    expect(policy).toContain("base-uri 'none'");
    expect(policy).toContain("form-action 'none'");
  });

  it("lets the registry frame a render and nobody else", async () => {
    // The site embeds this with `sandbox="allow-scripts"` and no
    // `allow-same-origin`, so the framed document has an opaque origin;
    // `frame-ancestors` is matched against the ancestor's URL, which is the
    // site's, so that iframe keeps working and a stranger's does not.
    const bucket = new FakeBucket();
    bucket.put(`public${bundle}`, "<!DOCTYPE html>\n");
    const response = await worker.fetch(request(bundle), env(bucket));
    expect(response.headers.get("Content-Security-Policy")).toContain(
      "frame-ancestors https://palomar-registry.org",
    );
  });

  it("puts the policy on a 304 as well, where a cache will keep it", async () => {
    // A cache updates its stored headers from the ones a 304 carries and
    // leaves the rest alone. A render fetched before this header existed
    // revalidates, gets a 304, and would go on having no policy for as long as
    // the entry lived; the entry is immutable, so that is as long as the cache
    // keeps it.
    const bucket = new FakeBucket();
    bucket.put(`public${bundle}`, "<!DOCTYPE html>\n");
    const response = await worker.fetch(
      new Request(`https://data.example${bundle}`, { headers: { "If-None-Match": '"etag"' } }),
      env(bucket),
    );
    expect(response.status).toBe(304);
    expect(response.headers.get("Content-Security-Policy")).toContain(
      "frame-ancestors https://palomar-registry.org",
    );
  });

  it("says nothing about content policy on the documents that are not documents", async () => {
    // A record, a feed and a search page are data the site fetches and checks
    // for itself. The render policy has nothing to say about them, so sending
    // it would be a header in every response answering a question nobody
    // asked.
    const bucket = new FakeBucket();
    bucket.put("public/entries/PALOMAR-2026-08-07-383237-v1.json", "{}\n");
    bucket.put("public/feed.xml", "<rss/>\n");
    bucket.put(`snapshots/${release}/LICENSE`, "licence\n");
    for (const path of [
      "/entries/PALOMAR-2026-08-07-383237-v1.json",
      "/feed.xml",
      "/LICENSE",
    ]) {
      const response = await worker.fetch(request(path), env(bucket));
      expect(response.status, path).toBe(200);
      expect(response.headers.get("Content-Security-Policy"), path).toBeNull();
    }
  });
});
