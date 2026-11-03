# TriAgent Studio: test report

Last updated 2026-10-31 (round 4: project management & folder access).
Machine: Windows 11 Pro 10.0.26300, Python 3.14.5, PySide6 6.12.0, Claude Code 2.1.316,
Codex CLI 0.166.0.

## Round 4: project management and folder access (latest)

**Automated tests: 101 passed, 0 failed** (`docs/pytest_results.txt`). 18 new tests are in
`tests/test_projects_ux.py`, plus 1 ACL unit test:

| Requirement | Test(s) |
|---|---|
| First-launch default folder creation (`Documents\TriAgent Studio Projects`, QStandardPaths) | `test_first_launch_creates_default_projects_folder`, `test_documents_resolved_with_qstandardpaths` |
| New project: named folder, registered, selected; no reuse of non-empty folders; safe names; app folder refused | `test_new_project_creates_named_folder_and_selects_it`, `test_new_project_refuses_application_folder` |
| Last-project restore (folder, prompts, counts, history) without starting agents | `test_last_project_restored_without_starting_agents` |
| Switching and the recent dropdown | `test_switch_project_and_recent_dropdown` |
| Persistence after restart | `test_persistence_after_restart` |
| Open Project / Output / Application folder (absolute native file URLs, never cwd) | `test_folder_buttons_open_correct_locations`, `test_open_path_uses_native_file_url`, `test_settings_about_opens_application_folder` |
| Invalid workspace recovery (default folder, banner, the app folder stays openable, missing folder recreated, Start offers recovery) | `test_invalid_workspace_recovery_use_default`, `test_invalid_workspace_banner_and_app_folder_still_open`, `test_missing_workspace_recreate`, `test_start_with_invalid_workspace_offers_recovery` |
| Migration of existing projects (old schema, transcripts kept, never moved) | `test_existing_project_migration_keeps_everything`, `test_projects_survive_rebuild_location` |
| Packaged exe path resolution (frozen, `_MEIPASS`, `_internal`, cwd-independent) | `test_packaged_executable_path_resolution` |
| Links to generated files for completed tasks | `test_task_artifact_links` |

**Real packaged-app smoke test: 16/16 passed** (`scripts/packaged_smoke.py` →
`docs/packaged_smoke_result.json`).

* The rebuilt `dist\TriAgentStudio\TriAgentStudio.exe` ran with an isolated data folder and
  projects folder, on the real desktop. It clicked the real dashboard buttons.
* The Windows Shell COM API confirmed that new Explorer windows opened at the project folder,
  `<project>\output`, and `dist\TriAgentStudio`, the folder containing the exe. The test then
  closed only those windows.
* A second launch restored the same project instead of creating a new one.
* The first smoke attempt failed one check. This was a test problem, not an app problem: it
  clicked the buttons milliseconds apart, and Explorer reused the project-folder window to show
  the `output` subfolder. The test now verifies each button in a separate launch.

**Bug found in this round:** an edit to `safety.py` had accidentally removed the
`_BROAD_PRINCIPALS` constant used by the Windows ACL check. The full suite caught it (the
Diagnostics page failed to render). It is restored and now has a direct unit test.

**Your existing data:**

* The project "calculator" is unchanged: its database, prompts, history and transcripts are
  intact, and it was **not moved**.
* Its folder is still `dist\TriAgentStudio`, so on the next launch it opens with the recovery
  banner.
* The recovered Claude-authored files remain in `recovered\calculator\calculator_app\`.
* The Documents projects folder is created on the next launch of the app. The tests and smoke
  test used temporary folders, so your Documents folder was not touched.

## Round 3: permissions and approvals

### R3.1 CLI mechanisms verified before use (real calls)

| Probe | Result |
|---|---|
| Claude `--restricted --tools Read,Glob,Grep,Edit,Write,Bash --permission-mode auto --settings {deny…}` | DESIGN.md + `src/calc.py` created and `python` ran **without prompts**. A write to `../outside_probe.txt` was **denied** ("outside …permprobe"); reading `.env` was **denied** (deny rule); `git push` was **denied** (deny rule). |
| Claude `--input-format stream-json --permission-prompts host` | *without* `--permission-prompt-tool stdio`: no control request, auto-denied. *With* it: a `can_use_tool` control request arrived; host `allow` → `git status` ran; host `deny` → "Rejected by the user in TriAgent Studio". |
| Codex `--approve-for-me` | `--sandbox` cannot be combined with it (it implies workspace-write). Creating `app/calc.py` and running Python **worked**; writing `../outside_codex.txt` was **denied** by the OS sandbox; `git push` was **not** blocked by any permission (it failed only for git reasons). |
| Codex project rules `<ws>/.codex/rules/triagent.rules` | `codex execpolicy check`: `forbidden` for `git push`, no match for `git status`. A real `codex exec` session **rejected** `git push` with the TriAgent justification, even through its PowerShell wrapper. |
| `codex exec` approval forwarding | No supported input channel. Codex approvals are therefore implemented only as a pre-launch grant; mid-run refusals mark the task BLOCKED with a manual path. |

### R3.2 Automated tests: 82 passed, 0 failed

`docs/pytest_results.txt`. New in this round: `tests/test_permissions.py` (15 tests).

* CLI flags for every mode × access level (Claude: `--restricted`, `auto` vs `default`,
  host prompts, deny/ask settings file; Codex: `--approve-for-me`, read-only, one-time grant).
* The Codex rules file; the task access matrix; risk classification.
* Trust resolution: a new workspace must be decided; previously trusted projects default to
  Full; moving the workspace means asking again.
* Live Approve and Reject round trips through the real control protocol (mock CLI process).
* A Codex launch grant: BLOCKED, then approved once / rejected, and the grant is consumed.
* The manual-action path: BLOCKED, then retry; the run never completes while items are pending.
* Missing-artifact rejection, with no review scheduled.
* Read Only changes nothing.
* Offline decision helpers.
* A Codex refusal parsed from **real captured output**.
* GUI: Approvals tab counter, notification, Approve Once button, and history; the access mode
  persists across a restart; Start is refused until a mode is chosen.

### R3.3 Real Claude + Codex workers: 35/35 checks passed (`scripts/real_permissions.py`)

Results: `docs/real_permissions_result.json`.

| Phase | What ran (real CLIs, subscription auth) | Result |
|---|---|---|
| 1. Calculator workflow, Full Workspace Access, auto coordinator, Claude 2 + Codex 2 | plan (claude-1) → **DESIGN.md** (claude-2) → calculator.py (codex-1) ∥ test_calculator.py (codex-2) → verify (codex-1) → 2 cross-provider reviews (Claude, both APPROVE) → synthesis | 8/8 tasks completed on the first attempt. **DESIGN.md, calculator.py and test_calculator.py exist** in the canonical workspace. Each was verified before completion. Codex prompts referenced DESIGN.md and Codex transcripts show it **read DESIGN.md**. **0 approval requests and 0 permission denials** for normal development work. Independent `python -m unittest -v`: 4/4 OK. `python calculator.py 2 + 3` → `5`. |
| 2. Claude live approval | 2 tasks each ran `git push origin HEAD` (high-risk ask rule) | Both requests arrived as real `can_use_tool` prompts. After **Approve Once**, git actually ran (it failed on the dummy remote: *Could not resolve host*). After **Reject**, Claude received "Rejected by the user in TriAgent Studio: not authorized in test" and nothing ran. |
| 3. Codex Manual Approval | Codex write task "notes/codex_note.md" | The task was **BLOCKED** with no process started until approval. After Approve Once it launched with `--sandbox workspace-write`; the file was created and verified; the grant was consumed. |
| 4. Codex forbidden command (Full mode) | Codex asked to `git push` | Rejected by the TriAgent rules. The task became **BLOCKED** with a manual-approval entry ("codex exec cannot forward approval requests…"). The run waited instead of completing, and the user then canceled the task. |

### R3.4 Bugs found and fixed in this round

* **Stop/resume race.** `_finalize_stop` persisted `stopped` before the engine's `finished`
  flag was set, so an immediate Resume failed with "A run is already active". This was the
  cause of the one-off GUI test timeout reported as *unexplained* in round 2. The persisted
  status is now always written last, for stop and for completion, and there is a regression
  assertion.
* **stdin pipe leak.** Interactive workers that were killed left their stdin pipe open. It is
  now closed when the output ends.
* **Codex refusal detection.** Codex execpolicy rejections appear only in message text; they
  are now parsed into permission events.

### R3.5 INCIDENT: user project files deleted by a rebuild (caused during this round)

Your existing project **"calculator"** had its workspace at
`C:\Users\Burakk\Desktop\TriAgentStudio\dist\TriAgentStudio`, the PyInstaller output
folder. Its two runs (17:08 and 17:25) created `calculator_app/`, `calculator_build/`,
`BUILD.md`, `build_exe.ps1` and `run_calculator.py` there. My rebuild at 18:00
(`pyinstaller --noconfirm`) **erased that folder**. PyInstaller deletes directly; nothing went
to the Recycle Bin.

* **Recovered:** the 5 Claude-authored files, reconstructed by replaying the stored Write and
  Edit tool calls from TriAgent's transcripts (`scripts/recover_from_transcripts.py`). All 5
  are **byte-exact** against the sizes recorded in the artifact registry: `DESIGN.md` (5207 B),
  `TEST_PLAN.md` (22977 B), `tests/__init__.py`, `tests/test_core.py` (21635 B) and
  `tests/test_cli.py` (11252 B). They are in
  `C:\Users\Burakk\Desktop\TriAgentStudio\recovered\calculator\calculator_app\`.
* **Not yet recovered:** the Codex-authored files (`calculator.py`, `__init__.py`, `cli.py`,
  `__main__.py`, `run_calculator.py`, `build_exe.ps1`, `BUILD.md`, `verify.ps1`, `report.md`)
  and the built `Calculator.exe`. TriAgent's Codex transcripts record only their paths. Codex's
  own session logs (`~\.codex\sessions`) normally contain the full patches; they were **not
  read** pending your permission.
* **Also:** before rebuilding (this round and earlier ones) I ran
  `taskkill /F /IM TriAgentStudio.exe`, which would also have closed any TriAgent Studio window
  you had open. Run data is persisted, so nothing else was lost.
* **Prevention, now in the code:**
  * `workspace_problem()` refuses workspaces inside the application's own install/build
    folders (`dist`, `build`, `.venv`, the frozen exe folder). The "calculator" project
    therefore cannot start until you choose a new workspace.
  * `build.ps1` runs `scripts/check_build_safety.py` and aborts if any project's workspace lies
    inside `dist`/`build`.
  * Test instances are now stopped by PID, never by image name.

## Round 2 (integration hardening)

## 1. Authentication modes (observed, not assumed)

| Provider | Auth mode reported by the official CLI | How workers authenticate | Verified how |
|---|---|---|---|
| **Claude Code** | `claude auth status`: `loggedIn: true`, `authMethod: claude.ai`, `apiProvider: firstParty`, `subscriptionType: max` | Claude subscription login; `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` removed from the worker env (neither was set on this machine) | env-var **names** of live `claude.exe` workers inspected via psutil: no API-key variables present |
| **Codex CLI** | `codex login status`: `Logged in using ChatGPT` (same with and without `OPENAI_API_KEY`, and with `-c forced_login_method=chatgpt`) | ChatGPT subscription login, **pinned** with `-c forced_login_method=chatgpt`; `OPENAI_API_KEY` / `CODEX_API_KEY` removed from the worker env | live `codex.exe` workers: no API-key variables, and the command line contains the pin |
| **Antigravity** | not queried | **UNSUPPORTED**: automated integration disabled; nothing is executed | unit test proves the adapter starts no process; engine test proves no worker is ever launched even if settings are tampered with |

* `OPENAI_API_KEY` **is set** in this machine's user environment. It is removed only from the
  Codex and Claude *child process* environments. The user's environment is unchanged; the smoke
  test checks this (`auth_host_environment_unchanged`).
* `forced_login_method` is a recognized, validated Codex 0.166 config field:
  * `codex exec --strict-config -c bogus_key_xyz=…` → `unknown configuration field`;
  * `-c forced_login_method=bogus` → `unknown variant 'bogus', expected 'chatgpt' or 'api'`;
  * `-c forced_login_method=chatgpt` → accepted, and a real request succeeded.
* No credentials were read, printed or logged. Login status output is redacted before use, and
  only the method name is kept. The Claude email/org fields are excluded from the reports.
* Paid API billing was **not** enabled at any point.

## 2. Automated tests: 64 passed, 0 failed (final run)

Command: `$env:QT_QPA_PLATFORM="offscreen"; .venv\Scripts\python -m pytest -q` → `64 passed in 94.77s`.

New or changed in this round:

| Test | What it proves |
|---|---|
| `test_codex_command` | the Codex worker command contains `-c forced_login_method=chatgpt` and no `OPENAI_API_KEY` in its env |
| `test_codex_api_billing_opt_in_removes_login_pin` | the pin and stripping change only after the explicit opt-in |
| `test_worker_env_is_a_copy_and_user_env_preserved` | `OPENAI_API_KEY`, `CODEX_API_KEY` and `ANTHROPIC_API_KEY` are absent from Claude and Codex worker envs; other variables are preserved; `os.environ` is unchanged |
| `test_sanitized_env_removes_billing_keys` | the input mapping is never mutated; unrelated variables (e.g. `GITHUB_TOKEN`) pass through |
| `test_codex_detect_redacts_login_status` | an API-key login is reported as AUTHENTICATED (not READY); a key in the status output never reaches stored info; detection uses the pin |
| `test_antigravity_unsupported_never_executes` | detect, test-connection and launch start no process (`subprocess.run`/`Popen` patched to fail) |
| `test_mock_mode_keeps_antigravity_unsupported`, `test_settings_force_antigravity_off` | there is no route to enable Antigravity |
| `test_antigravity_unsupported_is_never_launched` | even with tampered settings, Antigravity agents are BLOCKED (UNSUPPORTED) and no worker process exists |
| `test_stop_all_never_relaunches_killed_tasks` | regression for the bug found by the real smoke test (section 4) |
| GUI tests | the Antigravity panel is visible, badge UNSUPPORTED, controls disabled, config forced to 0 agents |

Flakiness note: in one full-suite run during this round (which took 207 s instead of the usual
~90 s), `test_stop_all_and_resume_from_gui` timed out once. It then passed in 8 consecutive
isolated runs and in the final full run. The cause was not identified; it is reported here
rather than hidden.

## 3. Genuine 1 Claude + 1 Codex smoke test: 31/31 checks passed

`scripts/real_smoke.py` → `docs/real_smoke_result.json` (runs `run-107032c0` and `run-a9fc6ef7`).

**Phase A: collaboration (real processes)**

```
claude-1  real claude.exe PID 30624  exit 0 after 7.3 s
  -> message #1 to team:codex (handoff) containing token BUS-9085046F
  -> delivered to codex-1 in its next real invocation
codex-1   real codex.exe  PID 17212  exit 0 after 8.8 s
  -> output contains BUS-9085046F; replied "BUS-9085046F ACK" to team:claude
PASS A_run_completed, A_{claude,codex}_task_completed, A_{claude,codex}_real_process,
     A_handoff_sent_by_claude, A_handoff_delivered_to_codex, A_codex_output_contains_token,
     A_codex_replied_via_bus
```

**Phase B: persistence (fresh database connection, as after a restart).** Run, tasks,
messages, deliveries, events and the prompts snapshot all reloaded from disk; output,
transcript and prompt files exist. (6 checks PASS)

**Phase C: Stop All.** Two real workers were alive (claude.exe PID 752 with 2 processes in its
tree; codex.exe PID 10392 with 9 processes in its tree). Then Stop All:

```
PASS C_two_real_workers_alive_before_stop     PASS C_agents_canceled
PASS C_run_stopped (0.6 s)                    PASS C_worker_records_marked_killed
PASS C_all_worker_process_trees_terminated    PASS C_no_relaunch_after_stop_all
PASS C_tasks_returned_to_queue_resumable      PASS C_no_orphans_after_restart
```

**Auth hygiene (live processes).** 9 checks PASS, among them: no API-key variables in any
Claude or Codex worker, Codex pinned to the ChatGPT login, no bypass flags, and the host
environment unchanged.

## 4. Defect found in this round (fixed)

The first run of the new smoke test **passed all its checks but its event log showed a bug**.
After Stop All killed both workers, the engine relaunched the two tasks (new PIDs) and stopped
them again 0.1 s later. Cause: Stop All killed the processes immediately but delivered the stop
command through the engine's queue. The worker exits were processed first, the tasks were
requeued, and the dispatcher started them again before reading the stop.

**Fix:** a halt flag that blocks dispatch is set *before* any process is killed, for both Stop
and Stop All. It is covered by a regression test and by the new smoke check
`C_no_relaunch_after_stop_all`. The re-run showed exactly 2 kills and no relaunch.

## 5. Earlier results (still valid, from the first round)

* Automatic-mode acceptance run (`scripts/real_acceptance.py`, Claude 2 + Codex 2):
  * the run completed in 105 s with 7 tasks: plan → 2 parallel implementations →
    2 cross-provider reviews → verification → final synthesis;
  * an independent `unittest` run on the files the agents produced: 3/3 OK.
  * At that time Antigravity was "unavailable" (not installed); it is now unsupported by design.
* Windows ACL issue (owner-only folders + Codex sandbox): fixed and detected; see README section 9.
* Packaged exe: builds and launches, and a full 12-task mock run used the packaged exe as the
  worker. It was rebuilt after this round and still launches.

## 6. Remaining limitations

* **Antigravity**: intentionally unsupported; no automated integration and no workaround.
  Editor D in the prompt editor is kept but its text is never sent.
* **Codex quota**: `codex exec --json` exposes no quota or usage-limit information. The app
  shows *Unknown* plus the token counts Codex reports per turn.
* **Claude quota**: shown only from Claude's own `rate_limit_event` (five-hour and seven-day
  utilization).
* **The pin protects Codex only.** The Claude CLI has no equivalent command-line option to pin
  the auth method. Claude workers rely on API-key variables being removed, plus the detection
  requirement `authMethod = claude.ai`. Separately, if an `apiKeyHelper` were configured in the
  user's Claude settings files, Claude Code could still use it. TriAgent Studio does not modify
  those files; none was found to be in effect (`authMethod: claude.ai`).
* **GUI with real providers**: the GUI was tested end to end with mock providers. Real-provider
  runs went through the same `Engine` headlessly (scripts), not by clicking through the GUI.
* **Flaky test**: one unexplained timeout under heavy load (see section 2).
