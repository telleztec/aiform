---
name: error-messages
description: Standard for error strings in the aiform CLI and any API it exposes. Use whenever you write or change an error message, exception text, parser.error() call, stderr line or API error body.
---

# error-messages

Applies to new and changed messages. Do not rewrite untouched messages in a feature PR.

## Format

- `main()` adds `Error: ` when it prints a caught exception. Never put it in exception text.
- A direct `print(..., file=sys.stderr)` in `aiform/cli.py` writes `Error: ` itself. Keep it there.
- Write one short sentence: the problem, then the offending value.
- Follow with at most one action line.
- Start lowercase after `Error: `. No trailing period on a one-line message.
- Use plain words. Do not blame the user.
- Do not use the word "illegal".

## The four rules

1. Say what happened. Name the problem and quote the offending value, flag or resource.
2. Say what to do. Give the exact flag or command where one exists. If the user can do nothing, call it a bug and point at the log.
3. Put non-actionable detail in the log (`.aiform/logs/`). Never put stack traces, internals or secrets in user text.
4. Write like a person: a plain statement, no filler.

## Checklist

Author and reviewer both walk this list for every new or changed message.

- Names the offending value, quoted.
- Says what to do next.
- One sentence plus at most one action line.
- No stack trace, internals or secrets on stderr.
- Exit code matches the CLI section below.
- Detail the user cannot act on is logged.
- No banned phrase.

## Banned phrases

The left column is quoted bad text. Never write it.

| Banned | Write instead |
|---|---|
| `Oops!`, `Something went wrong` | the symptom |
| `It seems that`, `appears to` | the flat fact, or what was checked |
| `Unfortunately`, `I apologize` | delete |
| `Successfully`, `simply`, `just` | delete |
| `please` before an instruction | drop it |
| `Failed to X` | `cannot X: reason` |
| `Invalid X` | `X 'val' is not allowed: rule` |
| `!` and dash flourishes | a full stop or a colon |

## CLI

- Write errors to stderr. Do not treat stderr as a log.
- Exit 2 for usage and operational errors.
- Exit 1 for a verdict: a declined gate, an unhealthy resource.
- Optional: a short error id printed on stderr and written to the log.

## API

Shape after RFC 9457:

- `type`: stable machine code.
- `title`: short summary.
- `detail`: the message, following the rules above.
- `status`: HTTP status.
- `instance`: the request or resource path.
- Extensions: `param` or `errors[]`, `request_id`, `doc_url`.
- Keep the English text separate from the stable code. Clients match on `type`, never on text.
- Send tracebacks, request params and upstream response bodies to the log, never in the body.

## Examples

Before/after pairs below are illustrations, not applied changes. "Before" strings are quoted from the repo. Each pair says whether it shows exception text (no prefix) or a printed line (with prefix).

Filler verb, raw upstream text, no action (exception text, `aiform/exceptions.py`, `DriverExecutionError`):

- Before, rendered with the upstream error filled in: `digitalocean.compute driver failed during create: <upstream error>`
- After: `cannot create digitalocean.compute 'web': <reason>` then `  details: .aiform/logs/`

Rule not stated as an action, no flag named (exception text, `aiform/exceptions.py`, `DeploymentMismatchError`):

- Before, first line of three: `this state file belongs to deployment 'a', not 'b'.`
- After: `state file belongs to deployment 'a', not 'b'` then `  pass --deployment a, or point --state-file at another file` then `  Nothing was read from the provider and nothing was changed.`

Python list repr leaks into user text (printed line, `aiform/cli.py`, `_cmd_init`):

- Before: `Error: unsupported provider 'aws'; supported: ['digitalocean']`
- After: `Error: unsupported provider 'aws': use digitalocean`

"invalid X" with no rule (exception text, `aiform/models.py`, `parse_dependency_key`):

- Before: `malformed dependency key 'Y.compute.web': invalid provider 'Y'`
- After: `provider 'Y' in dependency key 'Y.compute.web' is not allowed: use lowercase letters, digits and '_', starting with a lowercase letter`

Already compliant, copy the shape (`aiform/state.py`, `validate_deployment_name`):

- `invalid deployment name 'X': use 1 to 63 lowercase letters, digits, '-' or '_', starting with a letter or digit`
