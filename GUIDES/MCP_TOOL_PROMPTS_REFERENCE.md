# Atlas MCP server—tool prompts reference

The Atlas Model Context Protocol (MCP) server (`platform-atlas-webui --mcp-server`) exposes
16 read-only tools that an MCP client—such as a Gateway 5 FlowAI agent or Claude—can call to
answer compliance questions in natural language, using the sessions already stored in `~/.atlas`. This reference gives you an example prompt for each tool, so you can confirm every
tool responds correctly and learn how to phrase requests that reliably reach the tool you want.

## How to use this reference

Each tool answers a specific kind of question. Some tools are scoped to a single environment;
others look across your whole fleet. Only name your environment when the tool actually accepts
one—naming an environment for a fleet-wide tool doesn't change its result, and it can nudge the
agent into picking the wrong tool.

The examples below use `local` as a concrete environment name. Swap it for whichever environment
you're testing. Replace `<rule>` with a real rule number, name, or path, and `<second-environment>`
with another configured environment's name.

---

## Environment-scoped tools

These tools accept an `environment` argument and report on one environment's latest audit data.

| Tool | Answers | Example prompt |
|---|---|---|
| `list_sessions` | Recent sessions for one environment | "Show me the most recent sessions for the local environment." |
| `get_compliance_summary` | Current pass/fail/skip numbers | "What's the current compliance summary for local?" |
| `session_history_trend` | Whether compliance is improving or declining over time | "Is local's compliance improving or getting worse over its last 10 audits?" |
| `flaky_rules` | Rules that flip between pass and fail | "Are any rules in local flipping between pass and fail instead of just failing once?" |
| `diff_sessions` | What changed between two sessions | "Compare the two most recent validated sessions for local—what changed?" |
| `fleet_severity_breakdown` | Failure counts by severity | "What's the severity breakdown of failures in local right now?" |
| `rule_category_health` | Which rule category is weakest | "Which rule category is weakest in local—gateway, mongo, redis, or platform?" |
| `skip_reason_breakdown` | Why rules were skipped | "Why were rules skipped in local—deliberate exceptions, or data that couldn't be collected?" |
| `explain_rule` | One rule's status in one environment | "Look up rule `<rule>` and show its status in local." |

## Two-environment comparison

| Tool | Answers | Example prompt |
|---|---|---|
| `compare_environments` | Rules where two environments disagree | "Compare local against `<second-environment>`—which rules disagree between the two?" |

## Fleet-wide tools

These tools look across every configured environment and return the same answer regardless of
which environment you mention, so leave the environment name out of the prompt.

| Tool | Answers | Example prompt |
|---|---|---|
| `list_environments` | Every environment and its tier and status | "What environments does Atlas have configured, and what's each one's audit status?" |
| `fleet_top_fix` | Failures recurring across the most environments | "What are the top 3 rule failures showing up across the most environments fleet-wide?" |
| `rule_fleet_distribution` | Whether a failure is systemic or a one-off | "Is rule `<rule>` failing everywhere, or just in one environment?" |
| `stale_environments` | Environments that haven't been audited recently | "Which environments haven't been audited in the last 30 days, or ever?" |
| `fleet_tier_coverage` | Environment counts and pass rate by tier | "How many environments are on each tier, and what's the average pass rate per tier?" |
| `fleet_regressions_since_last_audit` | What got worse since each environment's last audit | "What got worse, anywhere in the fleet, since each environment's last audit?" |

---

## Tips for reliable results

- **Use a real rule identifier.** `explain_rule` and `rule_fleet_distribution` need an actual rule number, name, or path—the agent can't guess one.
- **Don't over-specify fleet-wide tools.** Adding an environment name to a fleet-wide prompt can cause the agent to try, and fail, to pass an `environment` argument the tool doesn't accept.
- **Ask one question at a time.** Each prompt above maps cleanly to one tool call. Combining several questions in one prompt makes it more likely the agent picks the wrong tool or only answers part of your question.

> **Important:** `diff_sessions`, `session_history_trend`, `flaky_rules`, and `compare_environments`
> all require an `environment` argument with no default value. As of 2026-08-24, calls to these
> four tools are failing before they reach Atlas—Gateway 5 is wrapping them in a device-inventory
> filter (`gateway_manager` domain, HTTP 400) that Atlas's tool schema never requested or requires.
> If prompts for these four tools fail while the others succeed, that's this known issue, not a
> problem with your prompt.

---

## Related information

- The `platform-atlas` README's **Atlas MCP server** section gives an overview of what the server does and how to start it.
- The `platform-atlas-webui` README's **Atlas MCP Server** section covers launching the server, registering it with Gateway 5 via `iagctl`, and the full 16-tool list with descriptions.
