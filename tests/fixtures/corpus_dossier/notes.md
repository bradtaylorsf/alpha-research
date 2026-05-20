# Corpus dossier smoke fixture (Markdown)

This file exists so the dossier-mode ingestion smoke test
(issue #354) can prove that the per-page ingestion pipeline handles
Markdown files. Every chunk written for this file should carry
`metadata.parent_file` pointing at this file's URI and leave
`metadata.page_no` set to `None`.

## Why a separate fixture?

The PDF fixture exercises the per-page path through
`pdf.extract_pages_sync()`. The HTML fixture exercises the existing
bs4 / unstructured extractor. This Markdown fixture exercises the
simplest path: read the file, normalise, chunk by whitespace tokens.

## Keywords for assertion stability

alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu
nu xi omicron pi rho sigma tau upsilon phi chi psi omega.
