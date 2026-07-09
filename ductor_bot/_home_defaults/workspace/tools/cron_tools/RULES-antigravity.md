# Cron Tools (Antigravity Only)

Scripts for creating, editing, listing, and removing scheduled jobs.

## MANDATORY: Ask Before Creating Jobs

**When the user requests a new cron job, you MUST ask:**

1. **Which model?**
   - `Gemini 3.5 Flash (High)` - Fast, highly capable agentic model (recommended)
   - `Gemini 3.5 Pro (High)` - Most capable reasoning model
   - `Gemini 3.5 Flash (Low)` - Fast, low rate limit/cost model
   - `Gemini 3.5 Pro (Low)` - Capable reasoning, low rate limit/cost model

2. **Should this job respect quiet hours?**
   - Ask: "Should this job skip execution during specific hours (e.g., at night)?"
   - If YES: Ask for start/end hours (e.g., "Don't run between 22:00-08:00")
   - Explain: "Quiet hours prevent jobs from running during specified times (default: 21:00-08:00)"
   - Use `--quiet-start <hour>` and `--quiet-end <hour>` (0-23, supports wrap-around)

3. **Does this job share resources with other jobs?**
   - Ask: "Does this job use Chrome/browser, or compete for API rate limits/tokens?"
   - If YES: "Use a dependency name (e.g., `chrome_browser`) so jobs run one at a time"
   - Explain: "Jobs with the SAME dependency run sequentially. Different dependencies run in parallel."
   - Use `--dependency <name>` (e.g., `chrome_browser`, `api_rate_limit`, `database`)

**Present these options and wait for the user's choice!**

Do NOT suggest `--cli-parameters` proactively. Only mention it exists if the user asks.

## Mandatory Rules

1. Use these scripts for cron lifecycle actions.
2. Do not manually edit `cron_jobs.json` for normal operations.
3. Do not manually delete `cron_tasks/` folders.
4. Run `cron_list.py` before `cron_remove.py` and use exact job IDs.

## Timezone (Critical)

All job schedules use the timezone defined in the global config: `user_timezone`.
Verify the active time and offset before recommending cron schedules by running:
`python3 tools/cron_tools/cron_time.py`

## Commands

- `python3 tools/cron_tools/cron_add.py --name "TaskName" --schedule "*/5 * * * *" --provider antigravity --model "Gemini 3.5 Flash (High)" "Task description..."`
- `python3 tools/cron_tools/cron_edit.py JOB_ID --schedule "0 9 * * *"`
- `python3 tools/cron_tools/cron_remove.py JOB_ID`
- `python3 tools/cron_tools/cron_list.py`
