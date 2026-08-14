import { env, exports } from "cloudflare:workers";
import { beforeEach, describe, expect, it } from "vitest";

import worker from "../../src/index";

const RELEASE = "a".repeat(64);
const ENTRY = "PALOMAR-2026-08-08-000001-v1";
const ENTRY_PATH = `/entries/${ENTRY}.json`;
const ENTRY_KEY = `public${ENTRY_PATH}`;
const BODY = '{"identifier":"PALOMAR-2026-08-08-000001","version":1}\n';
const PUBLIC_CACHE_CONTROL = "public, max-age=60, must-revalidate";

async function clearBucket(): Promise<void> {
  let cursor: string | undefined;
  do {
    const page = await env.DATA.list({ cursor });
    if (page.objects.length > 0) {
      await env.DATA.delete(page.objects.map((object) => object.key));
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor !== undefined);
}

async function seedHealthyRelease(): Promise<void> {
  await env.DATA.put(
    "_current.json",
    JSON.stringify({ schema_version: 3, release: RELEASE, publication_base: "b".repeat(64) }),
    { httpMetadata: { contentType: "application/json; charset=utf-8" } },
  );
  await env.DATA.put(`snapshots/${RELEASE}/release-delta.json`, "ready\n");
}

beforeEach(async () => {
  await clearBucket();
});

describe("deployed Database Worker in workerd", () => {
  it("refuses outbound traffic at the shared runtime boundary", async () => {
    const response = await fetch("https://example.invalid/forgotten-subrequest");

    expect(response.status).toBe(502);
    expect(await response.text()).toBe(
      "runtime tests must not reach https://example.invalid",
    );
  });

  it("proves the complete health chain and keeps it unstorable", async () => {
    await seedHealthyRelease();

    const response = await exports.default.fetch(
      new Request("https://data.palomar-registry.org/healthz"),
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ ok: true });
    expect(response.headers.get("content-type")).toBe("application/json; charset=utf-8");
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("access-control-allow-origin")).toBe("*");
    expect(response.headers.get("x-content-type-options")).toBe("nosniff");
  });

  it("streams a cacheable object from the real R2 binding", async () => {
    await env.DATA.put(ENTRY_KEY, BODY, {
      // A stale publisher policy must not override the Worker's policy.
      httpMetadata: {
        contentType: "application/json; charset=utf-8",
        cacheControl: "no-store",
      },
    });

    const response = await exports.default.fetch(
      new Request(`https://data.palomar-registry.org${ENTRY_PATH}`),
    );

    expect(response.status).toBe(200);
    expect(await response.text()).toBe(BODY);
    expect(response.headers.get("content-type")).toBe("application/json; charset=utf-8");
    expect(response.headers.get("content-length")).toBe(String(BODY.length));
    expect(response.headers.get("cache-control")).toBe(PUBLIC_CACHE_CONTROL);
    expect(response.headers.get("etag")).toMatch(/^"[0-9a-f]+"$/);

    // A direct call keeps the HEAD response inside this workerd isolate. The
    // loopback Fetcher used by `exports` reports a transport-level canceled
    // pump for every deliberately bodyless HEAD response.
    const head = await worker.fetch(new Request(
      `https://data.palomar-registry.org${ENTRY_PATH}`,
      { method: "HEAD" },
    ), env);
    expect(head.status).toBe(200);
    expect(await head.text()).toBe("");
    expect(head.headers.get("content-length")).toBe(String(BODY.length));
    expect(head.headers.get("cache-control")).toBe(PUBLIC_CACHE_CONTROL);
  });

  it("honours R2 validators and byte ranges", async () => {
    const stored = await env.DATA.put(ENTRY_KEY, BODY, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
    });

    const notModified = await exports.default.fetch(new Request(
      `https://data.palomar-registry.org${ENTRY_PATH}`,
      { headers: { "If-None-Match": stored.httpEtag } },
    ));
    expect(notModified.status).toBe(304);
    expect(await notModified.text()).toBe("");
    expect(notModified.headers.get("etag")).toBe(stored.httpEtag);
    expect(notModified.headers.get("cache-control")).toBe(PUBLIC_CACHE_CONTROL);

    const failedMatch = await exports.default.fetch(new Request(
      `https://data.palomar-registry.org${ENTRY_PATH}`,
      { headers: { "If-Match": '"not-the-stored-etag"' } },
    ));
    expect(failedMatch.status).toBe(412);
    expect(await failedMatch.text()).toBe("");
    expect(failedMatch.headers.get("cache-control")).toBe("no-store");

    const staleWrite = await exports.default.fetch(new Request(
      `https://data.palomar-registry.org${ENTRY_PATH}`,
      { headers: { "If-Unmodified-Since": "Thu, 01 Jan 1970 00:00:00 GMT" } },
    ));
    expect(staleWrite.status).toBe(412);
    expect(await staleWrite.text()).toBe("");
    expect(staleWrite.headers.get("cache-control")).toBe("no-store");

    const matchingWriteAndCacheValidators = await exports.default.fetch(new Request(
      `https://data.palomar-registry.org${ENTRY_PATH}`,
      {
        headers: {
          "If-Match": stored.httpEtag,
          "If-None-Match": stored.httpEtag,
          Range: "bytes=2-8",
        },
      },
    ));
    expect(matchingWriteAndCacheValidators.status).toBe(304);
    expect(await matchingWriteAndCacheValidators.text()).toBe("");
    expect(matchingWriteAndCacheValidators.headers.get("etag")).toBe(stored.httpEtag);
    expect(matchingWriteAndCacheValidators.headers.get("cache-control")).toBe(
      PUBLIC_CACHE_CONTROL,
    );

    const ranged = await exports.default.fetch(new Request(
      `https://data.palomar-registry.org${ENTRY_PATH}`,
      { headers: { Range: "bytes=2-8" } },
    ));
    expect(ranged.status).toBe(206);
    expect(await ranged.text()).toBe(BODY.slice(2, 9));
    expect(ranged.headers.get("content-range")).toBe(`bytes 2-8/${BODY.length}`);
    expect(ranged.headers.get("content-length")).toBe("7");
    expect(ranged.headers.get("accept-ranges")).toBe("bytes");
  });

  it("answers a missing object and a missing pointer differently", async () => {
    let response = await exports.default.fetch(
      new Request(`https://data.palomar-registry.org${ENTRY_PATH}`),
    );
    expect(response.status).toBe(404);
    expect(await response.text()).toBe("Not found\n");
    expect(response.headers.get("cache-control")).toBe("no-store");

    response = await exports.default.fetch(
      new Request("https://data.palomar-registry.org/LICENSE"),
    );
    expect(response.status).toBe(503);
    expect(await response.text()).toBe("Public data temporarily unavailable\n");
    expect(response.headers.get("cache-control")).toBe("no-store");
  });
});
