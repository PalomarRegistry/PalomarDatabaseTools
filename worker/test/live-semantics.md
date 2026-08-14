R2 may populate `R2ObjectBody.range` with the full object even when no Range
header was supplied. Response status therefore depends on the request header,
not merely on the presence of that descriptor. The workerd suite in
`runtime/worker.test.ts` asserts ordinary GET/HEAD are 200 and an explicit byte
range is 206 against the local R2 implementation.
