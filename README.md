# extractor-proxy

An OpenAI-compatible HTTP proxy that sits between Open WebUI and OpenAI and injects a
system prompt, turning any chat model into a structured data extractor. Paste unstructured
text into the chat; get back a JSON object describing it.

Ships with a Docker Compose stack: Postgres, Open WebUI 0.6.5, and this proxy.

- Prompt and its design: [SYSTEM_PROMPT.md](SYSTEM_PROMPT.md)
- Measured behaviour against a live model: [live-run results](SYSTEM_PROMPT.md#what-the-live-runs-actually-showed)

---

## Requirements

- Docker with Compose v2
- An OpenAI API key with credit
- Ports 3000 and 8000 free on localhost (configurable)

Python 3.11 is only needed to run the test suite outside the container.

## Quick start

```bash
cp .env.example .env          # set OPENAI_API_KEY
docker compose up -d
./scripts/verify.sh
```

First boot takes about a minute — Open WebUI runs migrations and loads embedding models.
When `verify.sh` exits 0, open <http://localhost:3000>, register (the first account becomes
admin), select `gpt-4o-mini`, and paste some text.

---

## Usage

### From the browser

Paste any unstructured text. The reply is a fenced JSON block. Ask a follow-up question
instead of pasting, and the reply is prose citing the extracted fields.

### From the API

The proxy is OpenAI-compatible, so any OpenAI client works against it.

```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"inv A-4491 total 82.10"}]}'
```

Authentication is not required — the proxy uses its own configured key and ignores any
`Authorization` header you send. It is bound to localhost only.

Add `"stream": true` for a Server-Sent Events response.

### Output format

Every extraction returns these eight keys, always present and always in this order:

| Key | Type | Contents |
| --- | --- | --- |
| `document_type` | string | `invoice`, `receipt`, `email`, … or `unknown` |
| `confidence` | number | 0.00–1.00, for the `document_type` call only |
| `language` | string | ISO 639-1, or `mul` / `und` |
| `summary` | string | one sentence, ≤ 25 words |
| `fields` | object | the extracted data; shape follows the document |
| `uncertain_fields` | array | every field held below 0.90, with a path and a reason |
| `unextracted` | array | source content that mapped to no field |
| `warnings` | array | conditions affecting the extraction |

Inside `fields` the shape varies by document, under fixed conventions: `lower_snake_case`
keys, ISO 8601 dates, money as `{"amount": …, "currency": …}` with an ISO 4217 code,
and `null` for a field the document does not supply rather than an omitted key.

Entries in `uncertain_fields` look like:

```json
{ "path": "fields.totals.total.currency", "confidence": 0.3,
  "reason": "no currency symbol or code anywhere in the document" }
```

Full specification, including the rules for follow-up questions and untrusted source
text, is in [SYSTEM_PROMPT.md](SYSTEM_PROMPT.md).

---

## How it works

```
browser ──▶ Open WebUI ──▶ extractor-proxy ──▶ api.openai.com
                 │                │
                 ▼                └── prepends SYSTEM_PROMPT.md to the messages
              Postgres
```

Open WebUI is configured with `OPENAI_API_BASE_URL=http://middleware:8000/v1`, so it
sends every chat completion here instead of to OpenAI. The proxy prepends the extraction
prompt and forwards the request upstream using its own key. Responses are relayed
unchanged — bytes for streams, verbatim JSON otherwise.

Neither Open WebUI nor the model is modified.

### Request path

```mermaid
flowchart TD
  A[POST /v1/chat/completions] --> B{body size}
  B -->|over MAX_REQUEST_BYTES| B1[413]
  B --> C{valid JSON object?}
  C -->|no| C1[400]
  C --> D{prompt loaded?}
  D -->|no| D1[503 prompt_unavailable]
  D --> E{Open WebUI internal task?}
  E -->|yes| F[forward unchanged]
  E -->|no| G[prepend extraction prompt]
  F --> H{stream requested?}
  G --> H
  H -->|no| I[await upstream, relay bytes]
  H -->|yes| J[pull first chunk]
  J -->|failed before first byte| J1[502 / 504 / upstream status]
  J -->|first chunk arrived| K[200 text/event-stream, relay chunks]
  K -->|upstream dies mid-stream| K1[error event + DONE, status stays 200]
  K -->|client disconnects| K2[release upstream, log abandoned]
```

Two branches carry most of the design:

**Internal-task detection.** After every message, Open WebUI asks the model to name the
chat, tag it and suggest searches, using its own prompt templates. Those calls must not
receive the extraction prompt, or chat titles become JSON. Open WebUI strips its own
`metadata.task` label before forwarding, so the proxy matches the template text instead: a
final user message, optionally preceded only by system messages, carrying a recognised
template opening. All eight of Open WebUI 0.6.5's templates are covered.

**The first-chunk pull.** Once `200` and `text/event-stream` are sent, the status code
cannot be changed. So the proxy waits for the first chunk before committing the response:
failures up to that point get a real HTTP status, failures after it are reported as an SSE
error event followed by `[DONE]`. Open WebUI's frontend recognises an `error` key in a
frame and ends the stream cleanly.

### Endpoints

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/healthz` | `200` always, while the process can serve |
| `GET` | `/readyz` | `200` ready, or `503` with a per-check breakdown |
| `GET` | `/v1/models` | the configured model list; never calls OpenAI |
| `POST` | `/v1/chat/completions` | the completion, streaming or not |
| `GET` | `/docs`, `/openapi.json` | FastAPI's generated API docs |

`/healthz` checks nothing but the process. `/readyz` checks that the prompt document
loaded and an API key is configured — it deliberately does not call OpenAI, so an upstream
outage does not mark the container unready.

### Status codes on `/v1/chat/completions`

| Code | Condition |
| --- | --- |
| `200` | success, or a mid-stream failure reported in-band |
| `400` | body is not valid JSON, or not a JSON object |
| `413` | body exceeds `MAX_REQUEST_BYTES` |
| `502` | OpenAI unreachable, empty stream, or a non-JSON upstream body |
| `503` | `SYSTEM_PROMPT.md` failed to load |
| `504` | upstream timed out |
| *upstream's* | any other upstream status, relayed with its body intact |

Errors use OpenAI's shape, plus a `request_id` that matches the `x-request-id` response
header and the logs:

```json
{ "error": { "message": "Could not reach OpenAI: …", "type": "upstream_unavailable",
             "code": null, "request_id": "3f2a…" } }
```

---

## Configuration

All settings are environment variables, read from `.env`. Only `OPENAI_API_KEY` has no
usable default.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | upstream credential; never sent to Open WebUI or the browser |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | upstream base; must not embed credentials |
| `EXPOSED_MODELS` | `gpt-4o-mini` | comma-separated list served by `/v1/models` |
| `MAX_REQUEST_BYTES` | `8388608` | request body ceiling |
| `CONNECT_TIMEOUT_SECONDS` | `10` | connect timeout |
| `READ_TIMEOUT_SECONDS` | `90` | gap allowed *between* streamed chunks |
| `LOG_LEVEL` | `INFO` | root log level |
| `SERVICE_NAME` | `extractor-proxy` | name reported by `/healthz` and in every log line |
| `SYSTEM_PROMPT_PATH` | discovered | set explicitly in the container |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | bind address inside the container |

Timeouts are per-operation, not a total deadline, so a long stream is never cut short as
long as chunks keep arriving.

Compose-only variables: `MIDDLEWARE_PORT` (default `8000`) and `OPEN_WEBUI_PORT`
(default `3000`) set the host ports, both bound to `127.0.0.1`; `POSTGRES_USER` /
`POSTGRES_PASSWORD` / `POSTGRES_DB` configure the database; `MIDDLEWARE_API_KEY` is the
placeholder Open WebUI presents to the proxy; `WEBUI_SECRET_KEY` signs Open WebUI's
session tokens.

> `HOST` and `PORT` are generic names and will pick up any ambient variable of that name.

---

## Logs

One JSON object per line. Every line from a request shares a `request_id`, which also
appears in the `x-request-id` response header and inside any error body.

```bash
docker compose logs -f middleware
docker compose logs middleware | grep '"request_id":"3f2a"'   # one request, start to end
docker compose logs middleware | grep service.starting        # effective configuration
```

| Event | Level | Meaning |
| --- | --- | --- |
| `service.starting` | INFO | every effective setting; the key appears only as a length |
| `chat.request` | INFO | model, streaming, request size, message count, whether injected |
| `chat.rejected` | WARN | body over the size limit |
| `chat.refused` | ERROR | prompt document unavailable |
| `chat.completed` | INFO | `finish_reason`, token usage, `truncated` |
| `chat.stream.finished` | INFO | chunks and bytes forwarded; logged on disconnect too |
| `prompt.injection.applied` | INFO | the prompt was prepended |
| `prompt.injection.skipped` | INFO/WARN | and why — `open_webui_task`, `no_messages`, … |
| `upstream.response` | INFO/WARN | what OpenAI answered |
| `upstream.request.failed` | WARN | could not reach OpenAI |
| `upstream.stream.interrupted` | WARN | upstream died mid-stream |
| `upstream.stream.unterminated` | WARN | stream ended without `[DONE]`; one was appended |
| `upstream.stream.empty` | WARN | 2xx with no body |
| `upstream.stream.abandoned` | INFO | consumer disconnected; upstream released |
| `http.request.start` / `.end` | INFO | lifecycle, with duration and byte count |
| `http.request.cancelled` | WARN | ended without a normal response |
| `http.request.failed` | ERROR | unhandled exception, with traceback |

Values shaped like API keys are masked in the output regardless of where they came from.

---

## Verifying and testing

`scripts/verify.sh` checks each hop in order and stops at the first failure.

| Check | Proves |
| --- | --- |
| 1–2 | the process is up and configured |
| 3 | `/v1/models` is populated, so the selector will not be empty |
| 4 | a messy document produces the eight-key envelope in order |
| 5 | streaming delivers multiple frames and terminates |
| 6 | Open WebUI's own task calls are *not* injected into |
| 7 | the real key is not in the Open WebUI container; ports are loopback-bound |
| 8 | Open WebUI can reach Postgres |

| Exit | Meaning |
| --- | --- |
| `0` | verified |
| `1` | a hop is broken |
| `2` | the stack is not running |
| `3` | the chain works; OpenAI refused the request (quota, billing, bad key) |

Unit tests:

```bash
cd middleware
uv venv --python 3.11 && uv pip install -e ".[dev]"
.venv/bin/python -m pytest
```

131 tests, no network access. The upstream is faked with `httpx2.MockTransport`, including
streams that fail mid-flight, end without a terminator, and are abandoned by a cancelled
consumer.

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Model dropdown is empty | middleware down or `EXPOSED_MODELS` empty — run `verify.sh` |
| Chats are named with JSON | injection scoping broke; see `verify.sh` check 6 |
| Replies stop halfway | look for `truncated: true` in `chat.completed` |
| Changed a setting, nothing happened | Open WebUI caches config in Postgres; `docker compose down -v` |
| `verify.sh` exits 3 | the OpenAI account, not this stack |
| `/readyz` returns 503 | the response body names the failing check |
| Port already in use | set `MIDDLEWARE_PORT` / `OPEN_WEBUI_PORT` in `.env` |

Inspect the database with `docker compose exec postgres psql -U openwebui -d openwebui`.
Postgres is not published to the host.

---

## Limitations

- A prior extraction truncated mid-JSON may be cited in a follow-up as though complete.
- Follow-up prose can assert a value the extraction recorded as `null`; the JSON is
  authoritative.
- Source text can attempt to redirect the extraction. It is answered with a rule and a
  warning, but nothing validates the model's output.
- Editing an Open WebUI task template, or bumping its version, can break injection
  scoping silently. `verify.sh` check 6 is the tripwire.
- File uploads go through Open WebUI's retrieval pipeline, so the proxy sees retrieved
  chunks rather than the document. Paste text instead.
- The injected prompt costs roughly 2,900 prompt tokens per request.
- No retries, no rate limiting, no inbound authentication.

## Layout

```
SYSTEM_PROMPT.md         the prompt and its design notes
docker-compose.yml       postgres + open-webui 0.6.5 + middleware
scripts/verify.sh        end-to-end checks
.env.example             every variable
middleware/
  Dockerfile             python:3.11-slim, non-root
  src/extractor_proxy/
    main.py              app factory, lifespan, error handlers
    config.py            settings
    prompt.py            prompt loading, injection, task detection
    upstream.py          the OpenAI client and streaming failure handling
    observability.py     JSON logging and request lifecycle
    errors.py            error envelope
    routes/health.py     /healthz, /readyz
    routes/openai_compat.py  /v1/models, /v1/chat/completions
  tests/
```
