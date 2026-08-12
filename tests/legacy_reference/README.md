# Legacy reference fixtures

`llm.py` in this directory is a frozen, byte-identical copy of the retired
`backend/llm.py`, retrieved from git history (baseline commit `a5d08bf`,
verified identical to the copy that existed immediately before the legacy
`backend/` tree was removed from the working tree — see commit `cdb8642`).

It exists solely as ground truth for `tests/test_p3_prompts.py`, which
executes real legacy functions (not just string constants) to catch subtle
prompt-formatting drift that a transcription-only comparison would miss —
see that file's own docstring for why live execution matters here.

**This is not application code.** Nothing under `app/` imports it, and it
must never be wired into anything that runs in production. If P3's frozen
contract (`docs/p3-contract.md`) is ever deliberately changed with the
owner's approval, this file's role as "what parity is measured against"
should be revisited at the same time.
