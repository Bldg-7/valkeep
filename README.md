# Valkeep

**English** | [한국어](README_KO.md)

**Read-only Valkey inspection for AI assistants, with explicit targets, scoped access, and query budgets.**

Valkeep is a local [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server built on the [official Valkey Python client](https://github.com/valkey-io/valkey-py). Each target runs a dedicated read-only Valkeep worker. No Redis MCP dependency or arbitrary command console is exposed. Redis is available as an explicitly configured compatibility mode.

The default preset provides metadata inspection and connection checks. String previews and key discovery are separate, opt-in features.

```text
AI client
  → Valkeep: target selection, tool permissions, key scopes, and request budgets
    ├─ catalog-cache → dedicated Valkeep worker → ACL user → Valkey A
    └─ job-queue     → dedicated Valkeep worker → ACL user → Valkey B
```

## Features

- **Multiple targets:** Up to 16 targets, each with its own process, credentials, policy, rate limits, and audit log.
- **Explicit routing:** Every Redis operation requires `target_id`. No implicit default or automatic fallback to another target.
- **Metadata-first defaults:** Inspect key types, TTLs, and lengths without retrieving values.
- **Bounded String previews:** Read a prefix with `GETRANGE`, rather than fetching the entire value and truncating it afterward.
- **Budgeted key discovery:** One SCAN call per request, single-use continuation tokens, and per-session call, time, and observation budgets.
- **ACL assistance:** Generate a least-privilege permission plan and check an administrator-provided ACL export without changing Redis permissions.
- **Privacy-conscious audit logs:** Raw keys, values, passwords, and discovery tokens are not written to audit logs.

## Tools

| Tool | Purpose | Default |
|---|---|---|
| `list_targets` | List configured target IDs, environments, allowed tools, and discovery scope IDs | Available |
| `inspect_key` | Inspect type, TTL in milliseconds, and type-specific length or count | Enabled |
| `list_length` | Get the number of elements in a List | Enabled |
| `connection_status` | Check connectivity and authentication with PING | Enabled |
| `preview_key` | Preview a bounded prefix of a String value | Opt-in |
| `find_keys` | Discover one page of keys within an approved prefix | Opt-in |

`inspect_key` supports length/count metadata for String, List, Hash, Set, Sorted Set, and Stream. Unsupported module types return type and TTL information with a null length. Metadata observations are not atomic snapshots.

String previews default to 1,024 bytes and can be configured up to 4,096 bytes. Non-UTF-8 data, including a preview cut through a multibyte character, is returned as base64. Collection value previews are intentionally unsupported: limiting the number of elements does not bound their byte size.

## Requirements

- Python 3.12 or newer
- macOS or Linux
- [uv](https://docs.astral.sh/uv/)
- Standalone **Valkey 8.1 or newer**, or Redis 7.2+ in compatibility mode
- A dedicated ACL user with administrator-reviewed permissions

Live coverage includes **Valkey 8.1.6 and 9.1.2**, both plaintext and TLS, plus a local Redis compatibility regression suite. This is not a guarantee for every version or managed service. Cluster, hosted HTTP transport, and per-user authorization are outside the current supported scope.

## Quick start

### 1. Install and inspect the example policy

```bash
git clone https://github.com/Bldg-7/valkeep.git
cd valkeep
uv sync --locked

uv run --locked valkeep --config config/policy.example.toml --check-config
uv run --locked valkeep --config config/policy.example.toml --acl-plan
```

These checks **do not connect to Redis or apply permissions**. The example uses placeholder local targets and requires no credentials for these two commands.

### 2. Configure your targets

```bash
cp config/policy.example.toml config/policy.local.toml
```

Edit the [example policy](config/policy.example.toml) for your approved targets:

1. Set each target’s ID, engine, environment, host, port, username, and TLS settings. `engine = "valkey"` is the default; existing Redis targets must explicitly set `engine = "redis"`.
2. Specify the key prefixes or exact keys that the AI client may inspect.
3. Keep `preset = "production-safe"` unless you need an explicitly configured custom policy.
4. Have an administrator review and apply the generated ACL plan.
5. Supply each password through the environment variable named by that target's `password_env`, using your secret-management mechanism.

Do not commit passwords or local configuration. `config/*.local.toml`, `.env*`, and `*.jsonl` are ignored by default.

Each authenticated connection validates `HELLO 2` before sending data commands: engine identity, minimum version, and standalone mode must match the policy. An engine mismatch is rejected rather than treated as generic protocol compatibility. HELLO is a connection-handshake command; the ACL plan does not broadly grant `+@connection` for this check.

The `production-safe` preset defaults to one MCP request per second and one in-flight request per target. To enable optional features, edit the **existing** `[targets.features]` table for the intended target:

```toml
[targets.features]
value_preview = true
key_discovery = true
```

Discovery defaults to a COUNT hint of 100, a 10-call and 60-second session budget, an observation budget of 500 keys, and at most four active sessions per target. Observations are counted after receiving a page, so the final page can exceed the observation budget. Redis `SCAN COUNT` is a hint, not a strict response-size limit.

Development and production targets require TLS with certificate and hostname verification. Plaintext connections are allowed only for explicitly configured local loopback targets.

### 3. Check connectivity and run

After reviewing the policy and supplying credentials:

```bash
uv run --locked valkeep --config config/policy.local.toml \
  --check-connections --target catalog-cache

uv run --locked valkeep --config config/policy.local.toml
```

The connection check **does contact Redis**. Omitting `--target` checks every configured target. PING success proves connectivity and authentication, not that the account has exactly the intended read-only permissions.

In server mode, Valkeep waits for MCP requests over stdio. Register the public Valkeep server—not its internal worker—in your MCP client.

A client configuration using the common `mcpServers` format looks like this; replace both absolute paths:

```json
{
  "mcpServers": {
    "valkeep": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/valkeep",
        "run", "--locked", "valkeep",
        "--config", "/absolute/path/to/valkeep/config/policy.local.toml"
      ]
    }
  }
}
```

Supply the configured password environment variables through your MCP client's secure environment or secret configuration. Client configuration formats vary.

## Example tool calls

The following examples illustrate MCP tool names and arguments; they are not shell commands.

```text
list_targets({})
inspect_key({"target_id": "catalog-cache", "key": "example:catalog:42"})
list_length({"target_id": "job-queue", "key": "example:jobs:pending"})
connection_status({"target_id": "catalog-cache"})
preview_key({"target_id": "catalog-cache", "key": "example:catalog:42"})
find_keys({"target_id": "catalog-cache", "scope_id": "prefix-0"})
find_keys({"target_id": "catalog-cache", "scope_id": "prefix-0", "token": "previous_next_token"})
```

Preview and discovery calls require the corresponding feature to be enabled. Use `list_targets` to find available discovery scope IDs. Continuation tokens are single-use and cannot be reused across targets or scopes. Partial or truncated discovery results are explicitly marked; Valkeep does not silently scan the entire database.

## ACL planning and verification

```bash
uv run --locked valkeep --config config/policy.local.toml \
  --acl-plan --target catalog-cache

uv run --locked valkeep --config config/policy.local.toml \
  --verify-acl /secure/path/acl-export.json --target catalog-cache
```

- **Planning:** Produces a JSON argument list for administrator review. It does not run `ACL SETUSER` or include a password. **The proposed `reset` rule removes existing permissions and passwords**; do not apply it blindly to an existing account.
- **Verification:** Checks an administrator-provided `valkey-cli --json ACL GETUSER <user>` export (`redis-cli` for Redis targets) against the configured policy. It rejects excess permissions, unexpected selectors, channels, and mismatched key scopes.
- **Limits:** Export verification is offline. It cannot establish the file's provenance, freshness, or the current permissions of a live account. `readonly_acl_confirmed = true` is an operator declaration, not an automated ACL check.

ACL exports contain sensitive information, including password hashes. Handle them securely and do not commit them. Valkeep does not print the raw export or password hashes.

## Safety model and limitations

- **Redis ACLs are the write-protection boundary.** The proxy also rejects unapproved tools and key access, but it does not create or enforce Redis account permissions by itself.
- **Read-only is not zero-impact.** Reads consume Redis CPU, memory, and network resources. Validate the limits against a representative workload before production use.
- **Key discovery is not strict resource isolation.** SCAN may traverse a broad keyspace, and ACL key patterns alone do not isolate key-name enumeration. Disable discovery if you require a strict per-command response bound.
- **Timeout is not server-side cancellation.** Valkeep retains the in-flight slot until execution ends and terminates a stuck child process after its cleanup deadline. This cannot guarantee cancellation of a command already executing in Redis.
- **Budgets are local.** Limits are per target and per Valkeep process, not shared across multiple instances or different aliases of the same physical Redis.
- **Access is shared by the local MCP user.** Every configured target is available to that user. This is not a multi-user authorization service.
- **Protect data sent to AI clients.** Read-only access can still expose sensitive data. Enable previews and discovery only for approved key scopes, and treat returned values as untrusted data.

## Development and testing

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
uv run --locked pytest -q -m live
uv build
```

The test suite includes unit tests, isolated MCP worker integration, and real Valkey/Redis tests. By default, live tests use installed `valkey-server` or `redis-server` binaries and OpenSSL; missing optional executables cause skips. Tests never accept external server URLs.

For required Valkey coverage using Docker, run the pinned 8.1.6 and 9.1.2 image matrix:

```bash
uv run --locked pytest -q --require-valkey \
  --valkey-image valkey/valkey@sha256:1f84517eca8e0dd9585ed57c572796f32839e1b0e7ce07ad28e6bce098468371 \
  --valkey-image valkey/valkey@sha256:418652cfb58ef879d4978c33553735d7147016032d5aefaa14c828e611eb9dfd
```

`--require-valkey` fails instead of silently skipping missing Valkey/TLS prerequisites. Docker tests create temporary configs, credentials, and TLS certificates, publish random ports on loopback only, and remove their containers afterward. GitHub Actions is configured to run the same pinned Valkey matrix. Verification is not certification of every managed service.

The official Valkey client (`valkey==6.2.0`), MCP Python SDK 1.30.0, and transitive dependencies are pinned through `pyproject.toml` and `uv.lock`. Version 0.4 removes `redis-mcp-server` and `redis-py`; `socket_timeout_seconds` replaces `redis_timeout_seconds` (the old input name is accepted for migration).

## Documentation

The detailed documents below are currently in Korean.

| Document | Contents |
|---|---|
| [Korean README](README_KO.md) | Korean overview and quick start |
| [Implementation and operations](docs/implementation.md) | Configuration, presets, ACL workflows, tools, and limitations |
| [Requirements](docs/requirements.md) | Functional and non-functional requirements and acceptance criteria |
| [Design](docs/design.md) | Architecture, policies, process lifecycle, and discovery safety |
