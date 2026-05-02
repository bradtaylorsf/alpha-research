# Sample markdown document

This is a small markdown fixture used by the local corpus indexer tests.

## Background

The indexer treats markdown as plain UTF-8 text — there is no special parsing
beyond the chunking step. That keeps the contract simple and avoids paying a
markdown-AST cost for documents that are mostly prose anyway.

## Findings

- Chunk boundaries land on whitespace tokens, so headings and list items
  are not split mid-word.
- Each chunk is content-addressed via sha256 so re-running the indexer on
  the same corpus is a no-op for unchanged files.
