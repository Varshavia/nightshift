## Security upgrade: {package} {from_version} → {to_version}

Fixes {advisory_id}{cve_suffix} ({severity}).

> {advisory_summary}

### What broke, and what was changed

The upgrade broke {failing_test_count} test(s) in this repository. The failure was:

```
{failing_excerpt}
```

{repair_explanation}

<details>
<summary>Repair diff ({changed_file_count} file(s), {added_lines}+ / {removed_lines}−)</summary>

```diff
{repair_diff}
```

</details>

### Verification

| | |
|---|---|
| Baseline before upgrade | {baseline_status} |
| After upgrade, before repair | failing |
| After repair | {final_status} |
| Repair attempts used | {attempts} of {max_attempts} |
| Test command | `{test_command}` |

The test suite was **not modified**. Writes to test files are refused by the
policy engine, not merely discouraged — the suite is the evidence that this
change is safe, so the agent that makes the change cannot touch it.

---

🌙 Opened by **Nightshift**, an autonomous agent fleet.

**This pull request was written by an AI agent.** The dependency bump, the code
changes and the explanation above were produced without a human author. A human
has not reviewed it before it was opened. Please review it as you would any
change from an unfamiliar contributor — and if this repository would rather not
receive automated pull requests, say so on this thread and we will stop.

Run `{run_id}` · job `{job_id}` · {model}
