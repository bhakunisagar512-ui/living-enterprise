# Security

## Secrets
- The Anthropic API key is read from the `ANTHROPIC_API_KEY` environment variable only. It is never in the code, the repo, a trace, a log line, an event sent to the browser or an output: everything that is written or displayed passes through `redact()`.
- `.env` files are git-ignored. See `.env.example`.

## Prompt injection (outside text trying to give the AI orders)
The request, the company documents and API responses are treated as **data, never instructions**. Defence in depth:
1. **Fencing:** every piece of outside text reaches an agent inside a `<<<UNTRUSTED ...>>>` block. Markers inside the text are neutralised so it cannot close the block early. Every agent is told that fenced text is data.
2. **Detection in code:** `scan_injection()` looks for known attack phrasing ("ignore previous instructions", "note to the AI", "mark as approved", "do not mention..."). Each finding becomes a warning that the human sees.
3. **Validator:** it receives the findings and must FAIL a draft that obeys them.
4. **Output check in code:** secrets are removed, and links or e-mail addresses that appear in no source are flagged.
5. **Human approval:** nothing is released without a person approving it. The safe default is Disapprove.
6. **Tested:** the `injection` sample request is an attack. It is covered by unit tests and by `python reliability.py injection 3`.

## Tools and data
- Documents: plain file names only (`contract.txt`), resolved inside `data/`. Path tricks are refused, and very long files are cut.
- Exchange-rate API: HTTPS only, size-limited, and validated for status, shape, type and range. Only a number and a sanitised date reach the AI.
- Plans can only use agents that exist (schema-checked), and anything doubtful in the Validator's output counts as a failure.

## Web server
- Local only: it binds to `127.0.0.1`, and requests with a non-local `Host` are refused (blocks DNS rebinding).
- Cross-site POSTs (a foreign `Origin`) are refused.
- Inputs are validated: known request names, bounded budgets, allowed choices, note length, and strict trace-file names.
- Strict headers are set (CSP, no framing, nosniff, no referrer, no caching of API data). The page renders all text with `textContent`, never as HTML.

## Limits
- The injection detector is a pattern list and can be evaded. That is why it is only one layer: the fencing, the Validator and the human stay in place.
- The web UI has no login, because it is meant for your own machine only. Do not expose it to a network as it is.

## Reporting
Please open a GitHub issue without exploit details, or contact the author directly.
