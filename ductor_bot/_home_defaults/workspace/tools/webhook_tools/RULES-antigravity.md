# Webhook Tools

Scripts for managing incoming HTTP webhook endpoints.

## MANDATORY: Ask Before Creating cron_task Webhooks

**When creating a webhook in `cron_task` mode, you MUST ask:**

1. **Which model?**
   - `Gemini 3.5 Flash (High)` - Fast, highly capable agentic model (recommended)
   - `Gemini 3.5 Pro (High)` - Most capable reasoning model
   - `Gemini 3.5 Flash (Low)` - Fast, low rate limit/cost model
   - `Gemini 3.5 Pro (Low)` - Capable reasoning, low rate limit/cost model

2. **Should this webhook respect quiet hours?**
   - Ask: "Should this webhook skip execution during specific hours (e.g., at night)?"
   - If YES: Ask for start/end hours (e.g., "Don't run between 22:00-08:00")
   - Explain: "Quiet hours prevent webhooks from running during specified times (default: 21:00-08:00)"
   - Use `--quiet-start <hour>` and `--quiet-end <hour>` (0-23, supports wrap-around)

3. **Does this webhook share resources with other tasks?**
   - Ask: "Does this webhook use Chrome/browser, or compete for API rate limits/tokens?"
   - If YES: "Use a dependency name (e.g., `chrome_browser`) so tasks run one at a time"
   - Explain: "Tasks with the SAME dependency run sequentially. Different dependencies run in parallel."
   - Use `--dependency <name>` (e.g., `chrome_browser`, `api_rate_limit`, `database`)

**Present these options and wait for the user's choice!**

For `wake` mode webhooks, these parameters are not applicable (uses current main session).

Do NOT suggest `--cli-parameters` proactively. Only mention it exists if the user asks.

## Mandatory Rules

1. Use webhook tool scripts for create/list/edit/remove/test/rotate.
2. Do not manually edit `~/.ductor/webhooks.json` for normal operations.
3. Use exact hook IDs from `webhook_list.py` output.
4. Run tools with `python3`.

## Runtime Model (What Happens)

Endpoint pattern:

```text
POST /hooks/<hook-id>
```

Request validation order:
1. Validate client IP (if whitelist configured).
2. Validate `Authorization: Bearer <token>`.
3. Check quiet hours (unless webhook uses `--force`).
4. Check rate limits.
5. Check dependencies. If busy:
   - For `cron_task` webhooks: Queue execution, return `202 Accepted`.
   - For `wake` webhooks: Drop request, return `429 Too Many Requests`.

## Commands

- `python3 tools/webhook_tools/webhook_add.py --mode cron_task --provider antigravity --model "Gemini 3.5 Flash (High)" --name "AlertHandler" "Task description..."`
- `python3 tools/webhook_tools/webhook_list.py`
- `python3 tools/webhook_tools/webhook_remove.py HOOK_ID`
- `python3 tools/webhook_tools/webhook_rotate.py HOOK_ID`
- `python3 tools/webhook_tools/webhook_test.py HOOK_ID '{"key":"value"}'`
