-- The index is derived public data. Staged generations are never queried.
CREATE TABLE query_generations (
  generation TEXT PRIMARY KEY,
  release TEXT NOT NULL,
  results INTEGER NOT NULL DEFAULT 0,
  projects INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE query_meta (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  generation TEXT NOT NULL REFERENCES query_generations(generation),
  revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE query_results (
  row_id INTEGER PRIMARY KEY,
  generation TEXT NOT NULL REFERENCES query_generations(generation) ON DELETE CASCADE,
  id TEXT NOT NULL,
  published_at TEXT NOT NULL,
  trust TEXT NOT NULL,
  repository TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  expected_chunks INTEGER NOT NULL DEFAULT -1,
  UNIQUE(generation,id)
);
CREATE INDEX query_updated ON query_results(generation,published_at DESC,id DESC);
CREATE INDEX query_registered ON query_results(generation,id DESC);
CREATE INDEX query_trust_updated ON query_results(generation,trust,published_at DESC,id DESC);
CREATE INDEX query_trust_registered ON query_results(generation,trust,id DESC);
CREATE TABLE query_summaries (
  row_id INTEGER PRIMARY KEY REFERENCES query_results(row_id) ON DELETE CASCADE,
  summary TEXT NOT NULL CHECK(length(CAST(summary AS BLOB))<=16384)
);
CREATE TABLE query_codes (
  row_id INTEGER NOT NULL REFERENCES query_results(row_id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  code TEXT NOT NULL,
  PRIMARY KEY(row_id,kind,code)
);
CREATE INDEX query_code_lookup ON query_codes(kind,code,row_id);
CREATE TABLE query_chunks (
  chunk_id INTEGER PRIMARY KEY,
  row_id INTEGER NOT NULL REFERENCES query_results(row_id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  tokens TEXT NOT NULL CHECK(length(CAST(tokens AS BLOB))<=65536),
  UNIQUE(row_id,position)
);
CREATE VIRTUAL TABLE query_fts USING fts5(tokens,content='query_chunks',content_rowid='chunk_id');
CREATE TRIGGER query_chunks_insert AFTER INSERT ON query_chunks BEGIN
  INSERT INTO query_fts(rowid,tokens) VALUES(new.chunk_id,new.tokens);
END;
CREATE TRIGGER query_chunks_delete AFTER DELETE ON query_chunks BEGIN
  INSERT INTO query_fts(query_fts,rowid,tokens) VALUES('delete',old.chunk_id,old.tokens);
END;
CREATE TABLE query_repositories (
  generation TEXT NOT NULL REFERENCES query_generations(generation) ON DELETE CASCADE,
  repository TEXT NOT NULL,
  results INTEGER NOT NULL,
  PRIMARY KEY(generation,repository)
);
CREATE TRIGGER query_results_insert AFTER INSERT ON query_results BEGIN
  INSERT INTO query_repositories VALUES(new.generation,new.repository,1)
    ON CONFLICT(generation,repository) DO UPDATE SET results=results+1;
  UPDATE query_generations SET results=results+1,
    projects=projects+(SELECT results=1 FROM query_repositories
      WHERE generation=new.generation AND repository=new.repository)
    WHERE generation=new.generation;
END;
CREATE TRIGGER query_results_delete AFTER DELETE ON query_results BEGIN
  UPDATE query_generations SET results=results-1,
    projects=projects-COALESCE((SELECT results=1 FROM query_repositories
      WHERE generation=old.generation AND repository=old.repository),0)
    WHERE generation=old.generation;
  UPDATE query_repositories SET results=results-1
    WHERE generation=old.generation AND repository=old.repository;
  DELETE FROM query_repositories WHERE generation=old.generation AND repository=old.repository AND results=0;
END;
