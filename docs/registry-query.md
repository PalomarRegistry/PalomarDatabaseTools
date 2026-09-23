# Bounded registry queries

`GET /api/v1/results` on `data.palomar-registry.org` lists current active
versions, one row per Palomar ID. It accepts `q`, `arxiv`, `msc` (a code
prefix), `trust=all|high|qualified`, `order=updated|registered`, inclusive
`from`/`to` dates, exact `id` lookup, and an opaque `cursor`. Filters combine
before pagination.
Text searches title, abstract, authors, theorem names and source repository.
Words are ANDed, accents folded, and common function words ignored; there is
no stemming. Words are ASCII alphanumeric, 2–32 characters. Empty text browses; text with no searchable words returns no rows
and an explanation. Invalid parameters are HTTP 400 before database queries.

Each response has at most 25 rows, each summary at most 16 KiB, and the whole
JSON body at most 512 KiB uncompressed. Query text is at most 4096 UTF-8 bytes
and 20 distinct normalized words, including dropped words. Cursors are at most
1024 bytes and bind the normalized filters, order and index revision. HTTP 409
means the registry changed: restart at the first page with the same filters.
Responses are `no-store`, CORS `*`, with exact registry-wide result/project
counts but no filtered total. Following `next` or `previous` costs one request.

The response includes `schema_version: 1`, `revision`, `release`, `totals`,
`entries`, `next`, `previous`, `dropped`, and `message`. Entries project the
recent-row fields plus `abbreviated`, `source_omitted`, and `preview` (artifact
hash and rendering version, including the original version for a correction).
Large author/theorem lists and abstracts may be abbreviated for display;
indexing always uses their complete canonical text. Oversized source controls
are omitted as a unit, never truncated into different URLs. Complete records
and preserved copies remain reachable from the entry page.

## Storage and publication

D1 is a derived index, not the canonical record store. The migration in
`worker/migrations` defines indexed metadata, bounded summaries, classification
codes, and an FTS5 index of bounded search-text chunks. AND intersection is at
the result level so words in different chunks still match. Selection and LIMIT
precede summary reads. Keyset pagination uses registration time/ID or ID; no
OFFSET scan or whole-index browser download is involved.

`stage-public` writes private `query-input.jsonl` alongside its staged files.
It is never an R2 served object. `publish-snapshot --query-api ORIGIN` streams
it to authenticated `PUT /_operations/results` with the dedicated
`PALOMAR_QUERY_UPDATE_TOKEN`. Requests are at most 256 KiB, token chunks at most
64 KiB; responses are bounded too. The token belongs only in the Worker and
credentialed publication and reconciliation steps.

Ordinary publication buffers each changed result until its complete search
text is present, then atomically upserts it. Retries are idempotent and database
triggers maintain counters in the same transaction. Full rebuilds populate an
inactive generation, then swap it into service. Failed uploads leave the old
index readable. Buffers and superseded generations are collected in bounded
batches of at most 500 result deletions after success. Publication jobs must remain serialized.

New canonical records are uploaded and verified before they become searchable.
In-place metadata replacements are unlisted before overwrite. Full activation
excludes withdrawals before the publisher deletes their canonical objects.
There is no per-query R2 pointer check and no global query-disable switch.
Weekly reconciliation compares bounded pages of ID/fingerprint pairs and both
counters against a canonical rebuild, and compares the index release with the
published R2 release. Moderation checks the exact current indexed version
through public `?id=...`, without any write credential.

## Rollout

1. `worker/wrangler.jsonc` binds the provisioned `palomar-query-staging` and
   `palomar-query` databases. Their initial schema is applied. The deployment
   workflow applies pending migrations before deploying both Worker environments;
   its Actions token needs D1 write access. The staging Worker remains inert and
   its database empty apart from its schema. Deploy the Worker before Web.
2. Set a fresh token of at least 32 characters as Worker secret
   `PALOMAR_QUERY_UPDATE_TOKEN` and the same Database Actions secret. Do not
   print it or put it in a shell argument. Build/release this tools version and
   update Database's immutable wheel pin before enabling its query publisher.
3. Under the existing `public-data` publication serialization, stage the exact
   current database with `stage-public --full`. Backfill with
   `publish-query --site STAGING --release CURRENT_R2_RELEASE --api ORIGIN`.
   This only writes D1; it does not retire static search objects or change R2.
   Obtain the release from the authenticated publisher's current-base command
   (its log names the release). Reconcile with the same command plus
   `--reconcile`, and check old-result text/date queries and cursor navigation.
   The local and remote 100,000-result checks are described below.
4. Deploy Web and its machine documentation, then enable Database publication
   using the query-aware wheel/workflow. Confirm that listing/search requests use
   only the bounded API, including hover previews, and reach older entries.
5. Set `PALOMAR_RETIRE_STATIC_SEARCH=true` on the production Worker. The old
   `/search/t/...` and `/search/stopwords.json` URLs now return 410 with the
   replacement API path. The next full publication deletes their old R2 keys.
   No compatibility search engine or old posting builder is retained. Web keeps
   a tiny retired-module notice so a cached app can still render entry pages.

Recovery is a full rebuild from canonical entries through the same publisher.
Keep canonical records and the current index serving while that rebuild runs.

## Scale validation

On 2026-09-23, the query SQL was exercised against a disposable remote D1
in ENAM/EWR with 100,000 current results, a 1 KiB abstract per summary, both
classification kinds, and twenty words shared by every result. One oldest
result additionally carried a unique author term. The database occupied
384 MB. Measured SQL execution times (not HTTP or CLI latency) were:

| Query | SQL time | Rows returned to Worker |
| --- | ---: | ---: |
| Browse | 0.5 ms | 26 |
| Two common words | 823 ms | 26 |
| Twenty common words | 1,638 ms | 26 |
| Unique author on the oldest result | 0.4 ms | 1 |
| Text + MSC prefix + date + trust | 500 ms | 26 |
| Next page, common words | 298 ms | 26 |

The Worker uses the extra row only to determine whether another page exists;
public pages contain at most 25 rows. The largest SQL result in this fixture
was 31,643 bytes. Independently, the local workerd suite enforces the response
byte cap and exercises the query implementation with 100,000 results. These
are synthetic measurements, not a latency guarantee for arbitrary metadata
or load.
