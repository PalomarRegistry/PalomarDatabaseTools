/**
 * Which R2 key backs a public path.
 *
 * The public URL is promised permanent; the key behind it is not. Objects were
 * once keyed under the release that published them, which is why adding one
 * entry rewrote the whole dataset. They are keyed by mutability now, which is
 * also why a record costs one read instead of three.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import worker, {
  pathClass,
  PUBLIC_CACHE_CONTROL,
  snapshotKey,
  stableKey,
} from "../src/index";
import { FakeBucket, env, release } from "./fakes";

const fixture = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../../tests/path-classes.json", import.meta.url).href),
    "utf8",
  ),
) as { paths: Record<string, string> };

const request = (path: string) => new Request(`https://data.example${path}`);

describe("path classification", () => {
  it("agrees with the shared table, which the publisher will also be read against", () => {
    const classified = Object.fromEntries(
      Object.keys(fixture.paths).map((path) => [path, pathClass(path)]),
    );
    expect(classified).toEqual(fixture.paths);
  });

  it("treats an unknown path as a snapshot rather than as immutable", () => {
    // Getting this backwards would freeze a file that changes at a stable key.
    expect(pathClass("/something-invented-later.json")).toBe("snapshot");
  });
});

describe("object keys", () => {
  it("keys a record by itself and an aggregate by its release", () => {
    expect(stableKey("/entries/PALOMAR-2026-08-07-383237-v1.json"))
      .toBe("public/entries/PALOMAR-2026-08-07-383237-v1.json");
    expect(stableKey("/source-availability.json")).toBe("public/source-availability.json");
    expect(stableKey("/recent.json")).toBe("public/recent.json");
    expect(snapshotKey("/LICENSE", release)).toBe(`snapshots/${release}/LICENSE`);
    expect(snapshotKey("/feeds/msc/11A05.xml", release))
      .toBe(`snapshots/${release}/feeds/msc/11A05.xml`);
  });
});

describe("serving", () => {
  it("serves a record in one read, without consulting the pointer", async () => {
    // The read-side point of the whole change: three operations became one.
    const bucket = new FakeBucket();
    bucket.put("public/entries/PALOMAR-2026-08-07-383237-v1.json", '{"id":"x"}\n');
    const response = await worker.fetch(
      request("/entries/PALOMAR-2026-08-07-383237-v1.json"),
      env(bucket),
    );
    expect(response.status).toBe(200);
    expect(bucket.reads).toEqual(["public/entries/PALOMAR-2026-08-07-383237-v1.json"]);
  });

  it("serves availability in one read too", async () => {
    const bucket = new FakeBucket();
    bucket.put("public/source-availability.json", '{"repositories":[]}\n');
    const response = await worker.fetch(request("/source-availability.json"), env(bucket));
    expect(response.status).toBe(200);
    expect(bucket.reads).toEqual(["public/source-availability.json"]);
  });

  it("refuses a pointer from the layout that was removed", async () => {
    const bucket = new FakeBucket(1);
    bucket.put(`snapshots/${release}/LICENSE`, "licence\n");
    expect((await worker.fetch(request("/LICENSE"), env(bucket))).status).toBe(503);
  });

  it("serves an aggregate from its release, which needs the pointer", async () => {
    const bucket = new FakeBucket();
    bucket.put(`snapshots/${release}/LICENSE`, "licence\n");
    const response = await worker.fetch(request("/LICENSE"), env(bucket));
    expect(response.status).toBe(200);
    expect(bucket.reads).toContain(`snapshots/${release}/LICENSE`);
  });

  it("serves only the current entry schema", async () => {
    const bucket = new FakeBucket();
    bucket.put(`snapshots/${release}/schema-v2.json`, '{"title":"current"}\n');
    bucket.put(`snapshots/${release}/schema-v1.json`, '{"title":"obsolete"}\n');

    expect((await worker.fetch(request("/schema-v2.json"), env(bucket))).status).toBe(200);
    const readsAfterCurrent = [...bucket.reads];
    expect((await worker.fetch(request("/schema-v1.json"), env(bucket))).status).toBe(404);
    expect(bucket.reads).toEqual(readsAfterCurrent);
  });

  it("serves what is new in one read, like every other stable surface", async () => {
    // The landing page reads this on every visit, and it changes whenever the
    // registry does, so paying the pointer read an aggregate costs would be a
    // round trip on the most-requested object here.
    const bucket = new FakeBucket();
    bucket.put("public/recent.json", '{"schema_version":1,"entries":[]}\n');
    const response = await worker.fetch(request("/recent.json"), env(bucket));
    expect(response.status).toBe(200);
    expect(bucket.reads).toEqual(["public/recent.json"]);
  });

  it("serves a browse page and the two documents above it in one read each", async () => {
    // A browse page is read once per visit and changes only when a record on
    // it does, so it must not cost the pointer read an aggregate does.
    const bucket = new FakeBucket();
    bucket.put("public/browse/2026-08-07/3676.json", '{"day":"2026-08-07"}\n');
    bucket.put("public/browse/2026.json", '{"year":"2026","days":[]}\n');
    bucket.put("public/browse/index.json", '{"years":[]}\n');
    const keys = [
      "public/browse/2026-08-07/3676.json",
      "public/browse/2026.json",
      "public/browse/index.json",
    ];
    for (const key of keys) {
      const response = await worker.fetch(request(`/${key.slice("public/".length)}`), env(bucket));
      expect(response.status, key).toBe(200);
    }
    expect(bucket.reads).toEqual(keys);
  });

  it("serves a subject page in one read, under the feed's own name grammar", async () => {
    const bucket = new FakeBucket();
    bucket.put("public/subjects/msc/11A05.json", '{"code":"11A05","entries":[]}\n');
    bucket.put("public/subjects/msc/11A05/2026.json", '{"year":"2026","days":[]}\n');
    bucket.put("public/subjects/msc/11A05/2026-08-07/1.json", '{"day":"2026-08-07"}\n');
    const keys = [
      "public/subjects/msc/11A05.json",
      "public/subjects/msc/11A05/2026.json",
      "public/subjects/msc/11A05/2026-08-07/1.json",
    ];
    for (const key of keys) {
      const response = await worker.fetch(request(`/${key.slice("public/".length)}`), env(bucket));
      expect(response.status, key).toBe(200);
    }
    expect(bucket.reads).toEqual(keys);
    for (const path of [
      "/subjects/msc2020/11A05.json",
      "/subjects/msc/11A05.xml",
      "/subjects/11A05.json",
      "/subjects/msc/../../recent.json",
      "/subjects/msc/11A05/2026-08-07/01.json",
      "/subjects/msc/11A05/2026-08-07.json",
    ]) {
      expect((await worker.fetch(request(path), env(bucket))).status, path).toBe(404);
    }
  });

  it("serves only the pages the year documents can name", async () => {
    // Those documents supply paths to a client, so a hostile one must not be
    // able to name a key outside the grammar and have it answered. A page
    // number with a leading zero is refused for a second reason: it would be a
    // second public name for one document.
    const bucket = new FakeBucket();
    for (const path of [
      "/browse/71.json",
      "/browse/2026-08-07/0.json",
      "/browse/2026-08-07/01.json",
      "/browse/2026-08-07/1a.json",
      "/browse/2026-8-7/1.json",
      "/browse/index.json.bak",
      "/browse/",
      "/browse/msc/11A05.json",
    ]) {
      const response = await worker.fetch(request(path), env(bucket));
      expect(response.status, path).toBe(404);
    }
  });

  it("is 404 for an object the ready release does not hold", async () => {
    const bucket = new FakeBucket(2);
    const response = await worker.fetch(request("/feeds/msc/11A05.xml"), env(bucket));
    expect(response.status).toBe(404);
  });

  it("fails closed when the release is not ready", async () => {
    const bucket = new FakeBucket();
    bucket.objects.delete(`snapshots/${release}/release-delta.json`);
    expect((await worker.fetch(request("/LICENSE"), env(bucket))).status).toBe(503);
    expect((await worker.fetch(request("/healthz"), env(bucket))).status).toBe(503);
  });

  it("refuses a pointer it does not understand", async () => {
    for (const pointer of [
      { schema_version: 4, release, publication_base: "b".repeat(64) },
      { schema_version: 3, release, publication_base: "b".repeat(64), generated_at: "2026-08-07T00:00:00Z" },
      { schema_version: 3, release: "not-a-digest", publication_base: "b".repeat(64) },
      { schema_version: 3, release, publication_base: "not-a-digest" },
    ]) {
      const bucket = new FakeBucket();
      bucket.put("_current.json", JSON.stringify(pointer));
      const response = await worker.fetch(request("/LICENSE"), env(bucket));
      expect(response.status, JSON.stringify(pointer)).toBe(503);
    }
  });

  it("bounds successful responses and answers a 304 with the same policy", async () => {
    // Every object can be withdrawn. A short freshness window is useful, but
    // after it must-revalidate refuses stale use and the origin's 404 wins.
    const bucket = new FakeBucket();
    bucket.put("public/entries/PALOMAR-2026-08-07-383237-v1.json", "{}");
    const fresh = await worker.fetch(
      request("/entries/PALOMAR-2026-08-07-383237-v1.json"), env(bucket),
    );
    expect(fresh.headers.get("Cache-Control")).toBe(PUBLIC_CACHE_CONTROL);
    expect(fresh.headers.get("Vary")).toBe("Accept-Encoding");
    expect(fresh.headers.get("ETag")).toBeTruthy();

    const again = await worker.fetch(
      new Request("https://data.example/entries/PALOMAR-2026-08-07-383237-v1.json", {
        headers: { "If-None-Match": '"etag"' },
      }),
      env(bucket),
    );
    expect(again.status).toBe(304);
    expect(again.headers.get("ETag")).toBe('"etag"');
    expect(again.headers.get("Cache-Control")).toBe(PUBLIC_CACHE_CONTROL);
    expect(again.headers.get("Vary")).toBe("Accept-Encoding");
  });

  it("uses the same finite policy for every withdrawal-sensitive path class", async () => {
    const bucket = new FakeBucket();
    const entry = "/entries/PALOMAR-2026-08-07-383237-v1.json";
    const render =
      "/renders/PALOMAR-2026-08-07-383237-v1/" + "b".repeat(64) + "/index.html";
    const paths = [entry, render, "/recent.json", "/source-availability.json", "/LICENSE"];
    bucket.put(`public${entry}`, "{}\n");
    bucket.put(`public${render}`, "<!doctype html>\n");
    bucket.put("public/recent.json", '{"entries":[]}\n');
    bucket.put("public/source-availability.json", '{"repositories":[]}\n');
    bucket.put(`snapshots/${release}/LICENSE`, "licence\n");

    for (const path of paths) {
      const response = await worker.fetch(request(path), env(bucket));
      expect(response.status, path).toBe(200);
      expect(response.headers.get("Cache-Control"), path).toBe(PUBLIC_CACHE_CONTROL);
      expect(response.headers.get("Cache-Control"), path).not.toContain("immutable");
    }
  });

  it("keeps errors and the absent unstorable", async () => {
    // There is nothing worth revalidating, and a stored 503 would be actively
    // unhelpful to a reader who came back a second later.
    const bucket = new FakeBucket();
    const missing = await worker.fetch(request("/LICENSE"), env(bucket));
    expect(missing.status).toBe(404);
    expect(missing.headers.get("Cache-Control")).toBe("no-store");

    bucket.objects.delete("_current.json");
    const unavailable = await worker.fetch(request("/LICENSE"), env(bucket));
    expect(unavailable.status).toBe(503);
    expect(unavailable.headers.get("Cache-Control")).toBe("no-store");

    for (const invalid of [
      request("/not-public"),
      new Request("https://data.example/recent.json", { method: "POST" }),
    ]) {
      const refused = await worker.fetch(invalid, env(bucket));
      expect([404, 405]).toContain(refused.status);
      expect(refused.headers.get("Cache-Control")).toBe("no-store");
    }

    bucket.put(
      "_current.json",
      JSON.stringify({ schema_version: 3, release, publication_base: "b".repeat(64) }),
    );
    bucket.put(`snapshots/${release}/release-delta.json`, "{}\n");
    const healthy = await worker.fetch(request("/healthz"), env(bucket));
    expect(healthy.status).toBe(200);
    expect(healthy.headers.get("Cache-Control")).toBe("no-store");
  });

  it("keeps every internal prefix unreachable from outside", async () => {
    const bucket = new FakeBucket();
    bucket.put("public/entries/PALOMAR-2026-08-07-383237-v1.json", "{}");
    for (const path of [
      "/_current.json",
      "/index.json",
      `/releases/${release}/LICENSE`,
      `/snapshots/${release}/LICENSE`,
      "/public/entries/PALOMAR-2026-08-07-383237-v1.json",
      "/release-delta.json",
      // The scores that decided a version, and their schema. Nothing ever
      // writes these keys, but the record used to be stripped on the way out
      // and a projection is only as good as the last person to remember it.
      // Refusing the path is the guarantee; not writing it is a coincidence.
      "/scores/PALOMAR-2026-08-07-383237-v1.json",
      "/scores-v1.json",
    ]) {
      const response = await worker.fetch(request(path), env(bucket));
      expect(response.status, path).toBe(404);
    }
  });
});

describe("the search postings grammar", () => {
  /**
   * A client builds these paths from what somebody typed, with no dictionary
   * to check the word against first -- that is the whole point of the shape,
   * and it means the grammar here is the only thing standing between a hostile
   * query and a key in this bucket.
   */
  it("serves a head and a page in one read, without consulting the pointer", async () => {
    const bucket = new FakeBucket();
    bucket.put("public/search/t/ring/head.json", '{"pages":1}\n');
    bucket.put("public/search/t/ring/0.json", '{"postings":[]}\n');
    for (const path of ["/search/t/ring/head.json", "/search/t/ring/0.json"]) {
      const response = await worker.fetch(request(path), env(bucket));
      expect(response.status, path).toBe(200);
    }
    expect(bucket.reads).toEqual([
      "public/search/t/ring/head.json",
      "public/search/t/ring/0.json",
    ]);
  });

  it("serves the words the indexer drops, which is the one list there is", async () => {
    // Not a term dictionary and unable to become one: a fixed editorial choice
    // of a few hundred function words, so it is the same size at a hundred
    // thousand results. A reader without it cannot tell a word nothing carries
    // from a word the indexer drops, and answers "the ring" against a record
    // whose text happens not to contain "the" by saying "the" is unfindable.
    const bucket = new FakeBucket();
    bucket.put("public/search/stopwords.json", '{"stopwords":["the"]}\n');
    const response = await worker.fetch(request("/search/stopwords.json"), env(bucket));
    expect(response.status).toBe(200);
    expect(bucket.reads).toEqual(["public/search/stopwords.json"]);
  });

  it("answers a word nothing carries with 404 rather than with a list of words", async () => {
    // A reader distinguishes "no result carries this" from "this is not a word
    // the indexer keeps" nowhere, because a document that could tell them
    // apart is a term dictionary, and a term dictionary grows with the
    // registry and is rewritten on every publication.
    const response = await worker.fetch(
      request("/search/t/quasicoherent/head.json"),
      env(new FakeBucket()),
    );
    expect(response.status).toBe(404);
  });

  it("refuses anything a term cannot be, before it reaches the bucket", async () => {
    const store = new FakeBucket();
    for (const path of [
      "/search/t/a/head.json",
      "/search/t/Ring/head.json",
      "/search/t/ring!/head.json",
      "/search/t/ring/head.txt",
      "/search/t/ring/-1.json",
      "/search/t/ring/00000.json",
      "/search/t/ring/0.json/",
      "/search/t/ring/sub/0.json",
      "/search/t/../entries/x.json",
      "/search/t/ring/",
      "/search/t/",
      "/search/stopwords.txt",
      "/search/t/stopwords.json",
      `/search/t/${"r".repeat(33)}/head.json`,
    ]) {
      const response = await worker.fetch(request(path), env(store));
      expect(response.status, path).toBe(404);
    }
    expect(store.reads).toEqual([]);
  });
});
