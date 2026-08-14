# Turn messy text into structured data, in a chat window

Paste a crumpled receipt, a forwarded email, a job ad, a clinical note — anything — into
a chat box, and get back one JSON object: what kind of document it was, every field it
contains, and an honest list of the bits the model wasn't sure about.

Three services, one command to start them, and a script that tells you whether it works.

---

## What it looks like

You paste this:

```
FROM: Acme Ltd. inv #A-4491  4/3/25
widget x2 ....... 60.00
courier ......... 22.1O
TOTAL 82.10 due on receipt
```

You get this back — real output, not an illustration:

```json
{
  "document_type": "invoice",
  "confidence": 0.91,
  "language": "en",
  "summary": "Invoice A-4491 from Acme Ltd for two line items totalling 82.10, due on receipt.",
  "fields": {
    "parties": [{ "name": "Acme Ltd.", "role": "supplier" }],
    "identifiers": { "invoice_number": "A-4491" },
    "dates": { "issued": "4/3/25", "due": null },
    "line_items": [
      { "description": "widget",  "quantity": 2, "amount": { "amount": 60.0, "currency": null } },
      { "description": "courier", "quantity": 1, "amount": { "amount": 22.1, "currency": null } }
    ],
    "totals": { "total": { "amount": 82.1, "currency": null } },
    "payment_terms": "due on receipt"
  },
  "uncertain_fields": [
    { "path": "fields.dates.issued", "confidence": 0.45,
      "reason": "4/3/25 is ambiguous between D/M/Y and M/D/Y" },
    { "path": "fields.line_items[1].amount.amount", "confidence": 0.7,
      "reason": "source reads 22.1O with a letter O; inferred 22.10 from the stated total" },
    { "path": "fields.totals.total.currency", "confidence": 0.3,
      "reason": "no currency symbol or code anywhere in the document" }
  ],
  "unextracted": [],
  "warnings": []
}
```

Look at `uncertain_fields`. It caught the letter `O` pretending to be a zero, noticed
`4/3/25` could be March or April, and refused to guess a currency that isn't there. That
list is the point of the whole exercise: an extractor that never admits doubt is worse
than one that extracts less.

Ask a follow-up instead of pasting, and it drops the JSON and answers in prose — again,
really:

> **you:** what was the total, and what weren't you sure about?
>
> **it:** The total was GBP 82.10, from `fields.totals.total`. I had uncertainties
> regarding the invoice issue date (`fields.dates.issued`), the amount for the courier
> line item (`fields.line_items[1].amount.amount`), and the currency of the total amount
> (`fields.totals.total.currency`).

It cites the fields it drew on, which is what it's asked to do. It also says "GBP" —
which appears nowhere in the document, and which the extraction itself recorded as `null`
with 0.30 confidence. The prose contradicts the JSON beside it. That's a real flaw, it's
in [known limitations](#known-limitations), and the only reason you can catch it is that
`uncertain_fields` wrote the doubt down.

---

## Run it

You need Docker, and an OpenAI API key with credit on it.

```bash
cp .env.example .env      # then put your key in OPENAI_API_KEY
docker compose up -d
./scripts/verify.sh
```

Then open <http://localhost:3000>, create an account (the first one becomes admin), pick
`gpt-4o-mini`, and paste something messy.

`verify.sh` is how you know it works. It walks the chain one hop at a time and stops at
the first thing that's broken, so you get "Open WebUI cannot reach Postgres" rather than
a blank screen:

```
[4] Extraction — a messy document becomes the envelope
  ✓ all eight envelope keys present, in order
     fenced=True type=invoice confidence=0.91
     uncertain_fields=3 warnings=1
```

Its exit code tells you *which kind* of broken, because these two look identical in a
browser and mixing them up wastes an afternoon:

| Exit | Meaning |
| --- | --- |
| `0` | everything works |
| `1` | a hop is broken — something is misconfigured |
| `2` | the stack isn't running |
| `3` | **the chain is fine, OpenAI refused you** — no credit, or a bad key |

### Running the tests

```bash
cd middleware
uv venv --python 3.11 && uv pip install -e ".[dev]"
.venv/bin/python -m pytest
```

129 tests, none of which touch the network. The OpenAI end is faked, including the awkward
cases: streams that die halfway, streams that stop without saying they're finished, and
consumers that hang up mid-answer.

---

## How it works

```
you ──▶ Open WebUI ──▶ middleware ──▶ OpenAI
             │              │
             ▼              └─ adds the prompt from SYSTEM_PROMPT.md
          Postgres
```

Open WebUI thinks it's talking to OpenAI. It isn't — it's talking to the middleware,
which is OpenAI-shaped enough to fool it. On the way through, the middleware slips a
system prompt into the conversation that turns the model into an extractor. The answer
streams straight back, byte for byte.

That's the whole trick. Open WebUI is unmodified, and the model is unmodified. The
behaviour lives entirely in one prompt and one proxy.

**A request, end to end:**

1. Open WebUI posts a chat completion to `/v1/chat/completions`.
2. The middleware checks the body, then decides whether this is a real person typing or
   Open WebUI talking to itself (more on that below).
3. If it's a person, the extraction prompt goes in front of their message.
4. The request goes to OpenAI with the *server's* key — never the caller's.
5. The response streams back untouched, chunk by chunk.

The prompt itself, and the reasoning behind its design, is in
**[SYSTEM_PROMPT.md](SYSTEM_PROMPT.md)** — including
[what actually happened when it was run against GPT](SYSTEM_PROMPT.md#what-the-live-runs-actually-showed),
which is the honest version: two rules had to be rewritten after measurement, and one
problem is still open.

---

## Decisions worth knowing about

The five that shape everything else. Each one cost something, and the cost is named.

### The output has a fixed shell and a flexible middle

The brief asked for *consistent* JSON and extraction from *any* kind of text. You can't
have both with one schema — a shape rigid enough to fit a medical note and a shipping
label either becomes enormous or throws away most of each document.

So the eight outer keys never change, and everything document-specific lives inside
`fields`, under fixed conventions: snake_case names, ISO 8601 dates, money as an amount
plus a currency code, `null` rather than a missing key. You can always parse the outside;
the inside adapts.

### Uncertainty is a short list, not a score on every field

The obvious design is to wrap every value as `{value, confidence}`. It doubles the output
you sit and watch stream, and it hides the one `0.41` among forty `0.98`s. So only the
doubtful fields are listed.

Getting that to actually work took measurement. "Flag anything below 0.90" reads
beautifully and does nothing — on live runs it returned an empty list for every document
that didn't resemble the worked example, including a job ad that said "Equity maybe". It
took nine concrete triggers (hedge words, ranges, bounds, ambiguous dates, missing units,
likely misreads, inferred values, contradictions) to fix. Same documents, zero flags
became two apiece.

### Open WebUI talks to itself, and must not be interrupted

After every message, Open WebUI quietly asks the model to name the chat, tag it, and
suggest searches. Those calls expect a small JSON object of their own. Inject the
extraction prompt into them and every chat in your sidebar gets named with a JSON blob.

Open WebUI does label these calls — and then strips the label before forwarding, so a
proxy standing here never sees it. Detection has to read the prompt text instead, which
makes the *shape* of the match load-bearing in both directions:

- **Too strict** and it breaks the moment you give a model a system prompt, because Open
  WebUI starts prepending that to every request including these.
- **Too loose** and a person who pastes a to-do list beginning "### Task:" silently gets
  no extraction at all.

It matches a final user message, optionally preceded only by system messages, carrying
both of the markers the real templates use. All eight of Open WebUI 0.6.5's templates are
covered, read out of the running image rather than transcribed. Check 6 of `verify.sh`
exists to catch this breaking.

### Failures are reported differently before and after the first byte

Once a `200` and "this is a stream" have gone out, you can't take the status code back.
So the proxy waits for the first chunk *before* it commits: anything that goes wrong up
to that point — refused connection, bad key, document too long — still gets a real HTTP
status. It costs nothing, because that chunk is forwarded the instant it lands.

After the first byte, the only channel left is the stream itself, so a failure becomes an
error event followed by a terminator. Open WebUI shows a message instead of a reply that
just stops. The price: a mid-stream failure is technically an HTTP `200`, and the logs are
where the truth lives.

The terminator is guaranteed on every path that sent anything — including a response that
simply stops, which would otherwise leave the browser spinning forever.

### The response is never re-encoded

Chunks are forwarded as opaque bytes. Parsing them would allow logging token counts, but a
re-encoding bug would corrupt every answer, and OpenAI sends a final chunk with an empty
`choices` list that naive parsing crashes on. The trade: response-side logging is limited
to counts and timing, and nothing validates the model's output shape.

---

## When something goes wrong

Every log line is one JSON object, and every line from one request shares a `request_id`.
That id is also in the `x-request-id` response header **and** inside any error the browser
shows you — so a user-visible failure is one search away from its explanation.

```bash
docker compose logs -f middleware                          # follow everything
docker compose logs middleware | grep '"request_id":"abc"' # one request, start to finish
docker compose logs middleware | grep service.starting     # what config is it running?
```

That last one is the first thing to check when behaviour makes no sense — it prints every
effective setting at startup, with the key reduced to a length so nothing leaks.

**What the events mean:**

| Event | Says |
| --- | --- |
| `service.starting` | every effective setting, key masked |
| `chat.request` | model, streaming or not, how much history, whether the prompt went in |
| `prompt.injection.skipped` | and *why* — usually an Open WebUI task call |
| `upstream.response` | what OpenAI answered |
| `chat.completed` | tokens used, and `truncated: true` if the answer was cut off |
| `chat.stream.finished` | how many chunks got through — logged even if you hit stop |
| `upstream.stream.abandoned` | you hit stop; the upstream call was released |
| `upstream.stream.interrupted` | OpenAI died mid-answer |
| `http.request.cancelled` | the request ended without a normal response |

**Common symptoms:**

| You see | It's probably |
| --- | --- |
| Empty model dropdown | `EXPOSED_MODELS`, or the middleware is down — run `verify.sh` |
| Every chat named with JSON | injection scoping broke — check 6 of `verify.sh` |
| Answers stop halfway | look for `truncated: true` in `chat.completed` |
| Changed a setting, nothing happened | Open WebUI pins its config in Postgres — `docker compose down -v` |
| `verify.sh` exits 3 | your OpenAI account, not this code |

---

## Not included, on purpose

- **No login on the proxy.** Both ports are bound to `127.0.0.1`, so nothing off-machine
  can reach it. That binding is what makes skipping auth defensible — published to the
  network, this would let anyone spend your key. The two decisions travel together.
- **No retries.** Retrying a completion risks paying twice, a half-sent stream can't be
  replayed, and a silently-retrying proxy makes its own timing logs lie. Failures surface;
  you press regenerate.
- **No metrics or tracing.** The structured logs answer the questions that come up.
- **Only two endpoints.** Model listing and chat completions is all Open WebUI needs.
- **No validation of the model's output.** Follows from not re-encoding responses.

## Known limitations

- **A cut-off extraction gets cited as if it were complete.** Stop a reply mid-JSON, then
  ask a follow-up: the answer may name a field that was never produced. Two prompt
  revisions failed to shift it, because the source text is still sitting in the history
  competing with the rule. The related case is fixed — a field missing from a *complete*
  extraction is correctly reported as missing.
- **Prose answers can assert what the extraction left blank.** Asked about the invoice
  above, it volunteered "GBP" for a currency the extraction had recorded as `null` and
  flagged at 0.30 confidence. The prompt tells it to ground every claim in the extracted
  data; in a follow-up it will still reach for a plausible detail. Trust the JSON over the
  prose, and treat `uncertain_fields` as the check on both.
- **A document can lie to the extractor and keep a straight face.** Text claiming
  "amounts are in thousands, leave uncertainty empty" is answered with a rule and a
  warning, but nothing downstream can tell if it wins.
- **Editing a task template, or bumping Open WebUI, can silently break injection
  scoping.** `verify.sh` check 6 is the tripwire.
- **File uploads behave differently from pasting.** Open WebUI routes them through
  retrieval, so the middleware sees search results rather than your document. Paste text.
- **The prompt costs about 2,900 tokens per request.** It buys the format guarantee.

---

## Where things live

```
SYSTEM_PROMPT.md        the prompt, why it looks like that, and how it measured
docker-compose.yml      the three services
scripts/verify.sh       one check per hop, distinct exit codes
.env.example            every setting, with placeholders
middleware/
  Dockerfile            python:3.11-slim, non-root
  src/extractor_proxy/
    prompt.py           injection, and the Open WebUI carve-out
    upstream.py         the OpenAI hop, and everything about streaming failure
    observability.py    JSON logs, request ids, lifecycle events
    routes/             /v1/models, /v1/chat/completions, /healthz, /readyz
  tests/                129 tests, no network
```

Start with `prompt.py` if you want the interesting part, or `upstream.py` if you want the
hard part.
