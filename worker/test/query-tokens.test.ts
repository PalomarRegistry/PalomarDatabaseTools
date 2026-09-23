import { readFileSync } from "node:fs";
import { expect, it } from "vitest";
import { words } from "../src/registry-query";
import { STOPWORDS } from "../src/search-words";

it("uses exactly the publisher's Unicode folding and stopwords", () => {
  const fixture = JSON.parse(readFileSync("../tests/fixtures/query-tokens.json", "utf8"));
  expect([...STOPWORDS].sort()).toEqual(fixture.stopwords);
  for (const sample of fixture.cases) expect(words(sample.text)).toEqual(sample.words);
});
