import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

/** Refuse any subrequest the runtime suite did not deliberately provide. */
function rejectOutbound(request: Request): Response {
  return new Response(
    `runtime tests must not reach ${new URL(request.url).origin}`,
    { status: 502 },
  );
}

export default defineConfig({
  plugins: [
    cloudflareTest({
      // Exercise the actual entry point, compatibility settings and DATA
      // binding deployed by the production environment. Miniflare supplies a
      // local, isolated R2 implementation; the production bucket is never
      // contacted.
      wrangler: {
        configPath: "./wrangler.jsonc",
        environment: "production",
      },
      // A future Wrangler binding must not silently turn cleanup against the
      // local bucket into cleanup against the production bucket.
      remoteBindings: false,
      miniflare: {
        outboundService: rejectOutbound,
      },
    }),
  ],
  test: {
    include: ["test/runtime/*.test.ts"],
    // Runner workers share the named local R2 bucket. A future second test
    // file must not clear another file's fixtures while it is executing.
    fileParallelism: false,
  },
});
