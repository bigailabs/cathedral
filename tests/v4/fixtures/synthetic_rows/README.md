# v4 Synthetic Corpus Fixtures (Test-Only)

These three JSON rows are **synthetic, operator-authored test
fixtures** that exercise the `CathedralEngine.load_task` path
hermetically. They are NOT the production v4 corpus.

The production v4 corpus rows live OUTSIDE the public repo at the
path declared by the `CATHEDRAL_V4_CORPUS_PATH` env var (or passed
explicitly to `CathedralEngine(corpus_path=...)`). Operator-only.

The fixtures here target the in-tree vault upstream base
`src/cathedral/v4/vault/python_fastapi_base/`, which IS public (it's
a clean upstream micro-repo, not a bug row). The bug patches in
these fixtures are trivially obvious (sign flips on toy arithmetic);
they exist to prove the loader + bundle + oracle wiring works
end-to-end, not to provide hard challenges.

Do NOT add real bug rows here. If you find yourself wanting to:

  * Add a row whose `issue_text` would tip off miners -- STOP.
    Put it under `$CATHEDRAL_V4_CORPUS_PATH`.
  * Add a row sourced from a real upstream commit -- STOP. Public
    PR/issue history is not allowed under the revised v4 spec.
