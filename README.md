# Structured-extraction middleware for Open WebUI

A local stack of three services. Open WebUI talks to a FastAPI proxy instead of talking
to OpenAI directly; the proxy injects a system prompt that turns the model into a
structured data extractor, then streams the answer back.

```
Browser ──▶ Open WebUI 0.6.5 ──▶ middleware (FastAPI) ──▶ OpenAI
                   │                    │
                   └──▶ Postgres        └── injects the prompt in SYSTEM_PROMPT.md
```

Paste an email, a receipt, a job listing, or a medical note into the chat and the reply
is a single JSON block: what kind of document it is, every field extracted under fixed
conventions, and an explicit list of the values the model is unsure about. Ask a
follow-up question instead and it answers in prose, citing the fields it drew on.

- The prompt and the reasoning behind it: [SYSTEM_PROMPT.md](SYSTEM_PROMPT.md)
- What it actually did when run against `gpt-4o-mini`, including what failed:
  [the live-runs section](SYSTEM_PROMPT.md#what-the-live-runs-actually-showed)

---

## Running it

Requires Docker with Compose, and an OpenAI API key with credit on it.

```bash
cp .env.example .env
# edit .env and set OPENAI_API_KEY
docker compose up -d
./scripts/verify.sh
```

`verify.sh` is the fastest way to know it works — it checks each hop in order and tells
you which one broke. On success, open <http://localhost:3000>, create the first account
(it becomes the admin), pick `gpt-4o-mini` in the model selector, and paste something
messy.

`.env` is the only file you need to touch. It is gitignored; `.env.example` documents
every variable and holds only placeholders.

### What verify.sh proves

| Check | What it rules out |
| --- | --- |
| 1. `/healthz` | The middleware process is not running |
| 2. `/readyz` | Missing API key, or `SYSTEM_PROMPT.md` not readable in the container |
| 3. `/v1/models` | An empty model selector in Open WebUI |
| 4. Extraction | The prompt is not reaching the model, or the envelope contract broke |
| 5. Streaming | Responses are buffered, or the stream never terminates |
| 6. Injection scoping | Open WebUI's own title/tag calls are being corrupted |
| 7. Open WebUI `/health/db` | Open WebUI cannot reach Postgres |

Its exit codes are distinct, because two failures look identical in a browser and
confusing them wastes the most time: `1` a hop is broken, `2` the stack is not running,
`3` **the chain works but OpenAI refused you** — out of quota, unpaid, or a bad key.

### Running the tests

```bash
cd middleware
uv venv --python 3.11 && uv pip install -e ".[dev]"
.venv/bin/python -m pytest
```

89 tests, no network access — the OpenAI upstream is faked with `httpx2.MockTransport`,
including the streams that fail halfway through.

### Useful commands

```bash
docker compose logs -f middleware          # structured JSON, one event per line
docker compose ps                          # health of all three services
docker compose exec postgres psql -U openwebui -d openwebui
docker compose down                        # stop, keep data
docker compose down -v                     # stop and wipe Postgres + Open WebUI state
```

---

## Design decisions

### The output contract splits a fixed envelope from a document-shaped body

The brief asks for a *consistent* JSON block and extraction of *all* key data from *any*
text. Those pull against each other: a schema strict enough to cover both a medical
report and a shipping label would either be enormous or throw most of each document
away.

So the eight envelope keys never vary — `document_type`, `confidence`, `language`,
`summary`, `fields`, `uncertain_fields`, `unextracted`, `warnings` — and everything
document-specific lives inside `fields` under fixed conventions (snake_case, ISO 8601
dates, money as amount plus ISO 4217 currency, `null` rather than omission). Consistency
comes from the envelope and the conventions rather than from one universal schema. Full
argument in [SYSTEM_PROMPT.md](SYSTEM_PROMPT.md#why-the-envelope-is-fixed-and-fields-is-not).

### Uncertainty is a separate list, and it took measurement to make it work

Wrapping every value as `{value, confidence}` was the first design. It roughly doubles
output tokens on a response the user watches stream, and it buries the one 0.41 among
forty 0.98s, so `uncertain_fields` collects only the exceptions.

The instruction "flag anything you hold below 0.90" then turned out not to work at all —
on live runs it produced an empty array for every document that did not resemble the
worked example, including a job listing saying "Equity maybe". Nine concrete triggers
replaced it (hedge words, ranges, bounds, ambiguous dates, missing units, likely
misreads, inferred values, contradictions, ambiguous field assignment). Same documents,
zero flags became two apiece. This is written up with the before and after in
SYSTEM_PROMPT.md.

### Open WebUI's internal calls must not be injected into

Open WebUI issues its own completions to name each chat, tag it, propose search queries
and pick an emoji. Those expect small JSON objects of their own, so injecting the
extraction contract turns every chat title in the sidebar into an extraction envelope.

Open WebUI does label these calls — `metadata.task` in the request body — but its OpenAI
router runs `metadata = payload.pop("metadata", None)` **before** forwarding upstream, so
the label never reaches a proxy standing where this one stands. Detection has to match
the prompt template text instead: a lone user message beginning `### Task:` (or the
emoji template's opening). Deliberately narrow, so pasting a document that mentions
`### Task:` further down is still treated as a real turn. Check 6 of `verify.sh` is the
live proof this works.

### Streaming failures are reported differently before and after the first byte

Once `200` and `text/event-stream` are sent, the status line cannot be revised. So the
proxy pulls the **first chunk before** constructing the streaming response: every
failure up to that point — a refused connection, a 401, a `context_length_exceeded` —
still gets a real HTTP status and an OpenAI-shaped error body. It costs no latency,
because the chunk is forwarded as soon as it arrives.

After the first byte the only channel left is the stream itself, so the proxy emits one
`data:` event carrying an error object and then `data: [DONE]`. Open WebUI shows a
message rather than a silently truncated one. The cost is that a mid-stream failure is
an HTTP `200`; the log line and the error event are the signal.

### Chunks are forwarded as opaque bytes

The proxy never decodes SSE frames. Parsing would allow logging token counts and
completion text, but a re-encoding bug would corrupt every response, and Open WebUI
sends `stream_options.include_usage`, whose final chunk has `"choices": []` — a shape
that crashes naive `chunk["choices"][0]` handling. Response-side logging is therefore
limited to byte counts and timing. That is the accepted trade.

Relatedly, response headers on the SSE path are an allowlist rather than a passthrough:
Open WebUI copies the middleware's headers verbatim onto the response it sends the
browser, so relaying an upstream `content-encoding` would describe a body that no longer
matches, and the message would never render. The upstream request asks for `identity`
encoding so the situation cannot arise.

### The prompt document is the runtime source of truth

`SYSTEM_PROMPT.md` is not a copy of the prompt — the service parses the block between
two HTML-comment markers out of that file at startup. The document a reviewer reads and
the bytes sent to OpenAI cannot drift apart. A failed load does not crash the process:
`/readyz` reports it and the chat route returns `503`, which is more diagnosable than a
restart loop.

### Liveness and readiness answer different questions

`/healthz` is dependency-free and stays `200` while the process can serve. `/readyz`
checks local preconditions — prompt loaded, key configured — and returns `503` with a
per-check breakdown naming what is wrong.

`/readyz` deliberately does **not** call OpenAI. A probe that spends a completion every
few seconds costs real money, and letting an upstream blip mark every replica unready
would withdraw the service exactly when it should be returning `502`s and logging why.

The container healthcheck uses `/healthz`, not `/readyz`, for a related reason: Open
WebUI waits on the middleware being healthy, and gating that on readiness would turn
"forgot to fill in `.env`" into "Open WebUI never starts" instead of a `503` that names
the missing key.

### Structured logs cover the full lifecycle, including cancellation

Every line is a single JSON object whose message is an event name, with the request id
attached from a contextvar. The lifecycle middleware is raw ASGI rather than
`BaseHTTPMiddleware`, because the latter hands back control as soon as the response
*starts* — which would report a few milliseconds for a response that streamed for
thirty seconds. Wrapping `send` gives both time-to-first-byte and the true total.

Cancellation needed explicit handling. A client that disconnects mid-stream never
produces a final body message, and `asyncio.CancelledError` is a `BaseException` that an
`except Exception` arm cannot catch — so pressing stop used to log a request that looked
permanently open. There is now always a terminal event.

An inbound `X-Request-ID` is reused rather than replaced, and echoed on the response.

### Configuration and key handling

The real key reaches the middleware only. Open WebUI gets a literal placeholder, because
sharing the env file would make the real credential the one Open WebUI presents to this
proxy and would expose it in Open WebUI's admin UI in the browser. The proxy discards
the inbound `Authorization` header entirely and authenticates upstream with its own
configured key.

Timeouts are per-operation with no overall deadline. httpx applies a single value to
each operation separately, so a total budget would abort a stream that is still
legitimately producing tokens: connect stays short to fail fast on a dead upstream, and
read bounds the gap *between* chunks.

`ENABLE_PERSISTENT_CONFIG=false` is set on Open WebUI. It otherwise writes
`OPENAI_API_BASE_URL` into Postgres on first boot and ignores the environment
thereafter, so correcting a typo'd base URL appears to do nothing. `docker compose down
-v` is the reset.

Postgres publishes no host port — only Open WebUI talks to it, and 5432 is commonly
already taken by a local install.

---

## Deliberate omissions

Not oversights; each is a decision.

- **No authentication on the proxy.** It is bound to a local stack and the real key
  lives in its own environment, so a string comparison against a dummy bearer token
  would buy nothing.
- **No retries or circuit breaking.** Retrying a completion risks double-billing, a
  streamed request cannot be replayed once bytes have shipped, and a silently retrying
  proxy makes its own latency logs lie. Upstream `429`s and `5xx`s pass through and the
  user can regenerate.
- **No rate limiting.** OpenAI's own `429` is the backstop.
- **No metrics or tracing.** The structured logs meet the brief; a Prometheus exporter
  would be scope creep here.
- **No OpenAI endpoints beyond chat completions and model listing.** Nothing else is
  needed for chat; Open WebUI's default embedding engine is local.
- **`/v1/models` is served from configuration, not proxied.** It keeps the selector
  populated when OpenAI is unreachable and keeps the endpoint deterministic under test.
  The cost: it does not reflect the account's real entitlements, so a model listed in
  `EXPOSED_MODELS` that the key cannot reach fails at first chat rather than at
  selection.
- **No validation of the model's output.** The envelope is a prompt-level contract, not
  an enforced one. Enforcing it would mean buffering and parsing every response, which
  the byte-passthrough decision rules out.

## Known limitations

- **A prior extraction truncated mid-JSON still gets cited as though complete.** Stop a
  response mid-envelope, then ask a follow-up, and the answer names a field path that
  was never produced — the value is read back out of the source text and attributed to
  a field. Two prompt escalations did not shift it. The adjacent case is fixed: a field
  absent from a *complete* extraction now correctly reports the extraction as
  incomplete. Details in
  [SYSTEM_PROMPT.md](SYSTEM_PROMPT.md#what-the-live-runs-actually-showed).
- **Uploading a file behaves differently from pasting text.** Open WebUI routes uploads
  through retrieval, so the middleware sees a RAG template containing retrieved chunks
  rather than the document. Paste the text to exercise the extractor.
- **`OPENAI_API_BASE_URL` must not end in a slash.** Open WebUI builds calls as
  `f"{url}/models"`, so a trailing slash produces `//models` and a 404.

---

## Layout

```
├── SYSTEM_PROMPT.md          the prompt, its rationale, and the live-run results
├── docker-compose.yml        Postgres + Open WebUI 0.6.5 + middleware
├── scripts/verify.sh         end-to-end verification, one check per hop
├── .env.example              every variable, placeholders only
└── middleware/
    ├── Dockerfile            python:3.11-slim, non-root, build context = repo root
    └── src/extractor_proxy/
        ├── config.py         environment-driven settings
        ├── prompt.py         prompt loading, injection, internal-task detection
        ├── upstream.py       the OpenAI hop, streaming and failure mapping
        ├── observability.py  JSON logs and the request-lifecycle middleware
        ├── routes/health.py  liveness and readiness
        └── routes/openai_compat.py  /v1/models and /v1/chat/completions
```
