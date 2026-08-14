/** R2 stand-ins shared by the boundary and layout suites. */

export const release = "a".repeat(64);

export class FakeObject {
  readonly key: string;
  readonly version = "1";
  readonly size: number;
  readonly etag = "etag";
  readonly httpEtag = '"etag"';
  readonly uploaded = new Date("2026-08-06T00:00:00Z");
  readonly httpMetadata: R2HTTPMetadata;
  readonly customMetadata = {};
  readonly range?: R2Range;
  readonly checksums = {} as R2Checksums;
  readonly storageClass = "Standard" as const;
  readonly ssecKeyMd5 = undefined;
  readonly body: ReadableStream;

  constructor(key: string, readonly bytes: Uint8Array, range?: R2Range) {
    this.key = key;
    this.size = bytes.length;
    this.range = range;
    // The same three answers `_content_type` in tools/publish_snapshot.py
    // gives for these suffixes, because what the Worker does with a response
    // depends on the type the publisher wrote onto the object.
    this.httpMetadata = {
      contentType: key.endsWith(".json")
        ? "application/json; charset=utf-8"
        : key.endsWith(".html")
          ? "text/html; charset=utf-8"
          : "text/plain",
      // Deliberately stale metadata: the current publisher writes no cache
      // policy, but old objects may retain one. Replaying it proves the Worker
      // still overwrites it and remains the response-policy owner.
      cacheControl: "no-store",
    };
    const selected = range && "offset" in range && "length" in range &&
      typeof range.offset === "number" && typeof range.length === "number"
      ? bytes.slice(range.offset, range.offset + range.length)
      : bytes;
    this.body = new ReadableStream({ start(controller) { controller.enqueue(selected); controller.close(); } });
  }

  writeHttpMetadata(headers: Headers): void {
    if (this.httpMetadata.contentType) headers.set("Content-Type", this.httpMetadata.contentType);
    if (this.httpMetadata.cacheControl) headers.set("Cache-Control", this.httpMetadata.cacheControl);
  }

  async arrayBuffer(): Promise<ArrayBuffer> { return await new Response(this.body).arrayBuffer(); }
  async text(): Promise<string> { return await new Response(this.body).text(); }
  async json<T>(): Promise<T> { return JSON.parse(await this.text()) as T; }
  async blob(): Promise<Blob> { return await new Response(this.body).blob(); }
}

export class FakeBucket {
  readonly reads: string[] = [];
  readonly objects = new Map<string, Uint8Array>();

  constructor(schema = 3) {
    this.put(
      "_current.json",
      JSON.stringify({ schema_version: schema, release, publication_base: "b".repeat(64) }),
    );
    this.put(`snapshots/${release}/release-delta.json`, "ready");
  }

  async put(
    key: string,
    value: string | Uint8Array,
    options?: R2PutOptions,
  ): Promise<R2Object | null> {
    const bytes = typeof value === "string" ? new TextEncoder().encode(value) : value;
    const onlyIf = options?.onlyIf;
    if (onlyIf instanceof Headers) {
      const exists = this.objects.has(key);
      const ifMatch = onlyIf.get("If-Match");
      const ifNoneMatch = onlyIf.get("If-None-Match");
      if ((ifMatch !== null && (!exists || ifMatch !== '"etag"')) ||
          (ifNoneMatch === "*" && exists)) return null;
    }
    this.objects.set(key, bytes);
    return new FakeObject(key, bytes) as unknown as R2Object;
  }

  async get(key: string, options?: R2GetOptions): Promise<R2ObjectBody | R2Object | null> {
    this.reads.push(key);
    const bytes = this.objects.get(key);
    if (!bytes) return null;
    const onlyIf = options?.onlyIf;
    if (onlyIf instanceof Headers) {
      const ifNoneMatch = onlyIf.get("If-None-Match");
      const ifMatch = onlyIf.get("If-Match");
      const modifiedSince = onlyIf.get("If-Modified-Since");
      const unmodifiedSince = onlyIf.get("If-Unmodified-Since");
      const uploaded = new Date("2026-08-06T00:00:00Z");
      const failed =
        ifNoneMatch === '"etag"' ||
        (ifMatch !== null && ifMatch !== '"etag"') ||
        (modifiedSince !== null && new Date(modifiedSince) >= uploaded) ||
        (unmodifiedSince !== null && new Date(unmodifiedSince) < uploaded);
      if (failed) {
        const object = new FakeObject(key, bytes);
        const { body: _body, ...withoutBody } = object;
        return withoutBody as unknown as R2Object;
      }
    }
    // R2 currently reports a full-object range descriptor even without a
    // Range request; response status must still remain 200 in that case.
    let range: R2Range | undefined = { offset: 0, length: bytes.length };
    if (options?.range instanceof Headers) {
      const header = options.range.get("Range");
      const match = header?.match(/^bytes=([0-9]+)-([0-9]+)$/);
      if (match) range = { offset: Number(match[1]), length: Number(match[2]) - Number(match[1]) + 1 };
    }
    return new FakeObject(key, bytes, range) as unknown as R2ObjectBody;
  }

  async head(key: string): Promise<R2Object | null> {
    this.reads.push(key);
    const bytes = this.objects.get(key);
    return bytes ? new FakeObject(key, bytes) as unknown as R2Object : null;
  }
}

export function env(bucket: FakeBucket): Env {
  return {
    DATA: bucket as unknown as R2Bucket,
    PALOMAR_AVAILABILITY_UPDATE_TOKEN: "t".repeat(32),
  };
}
