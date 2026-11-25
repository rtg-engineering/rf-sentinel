# Bounded investigation missions

A mission is an operator-authorized, persistent investigation. The operator sets an objective and a scope. RF Sentinel then monitors on a schedule: it assesses what changed, ranks follow-up observations, and asks for approval before anything beyond read-only analysis.

This is Phase 4 of the autonomous-investigation roadmap. **Execution is simulated.** The only executor shipped records what an approved action would do, and changes no receiver, scan schedule, or collection setting. Connecting a real executor is a separate, reviewed step.

## Lifecycle

```text
DRAFT -> SCOPE_REVIEW -> APPROVED -> BASELINING -> MONITORING
MONITORING -> CHANGE_DETECTED -> HYPOTHESIS_REVIEW -> NEXT_ACTION_DECISION
NEXT_ACTION_DECISION -> read-only follow-up -> MONITORING
                     -> AWAITING_APPROVAL -> approved: executed (simulated) | denied | expired -> MONITORING
                     -> no action in scope: finding reported -> MONITORING
Any active state -> PAUSED -> resume to the previous state;  -> CANCELLED;  -> FAILED
COMPLETED (assessment budget used) and TIMED_OUT (duration elapsed) end with a final report.
```

- **Nothing runs before scope approval.** A mission in `DRAFT` or `SCOPE_REVIEW` does no work. Scope can be rejected back to `DRAFT`.
- **Monitoring is unattended and read-only.** Each due tick captures a snapshot, assesses it against the last assessment, and records the result in [environment memory](analyst.md#environment-memory). Monitoring continues while a proposal awaits a decision.
- **Pause** stops all scheduled work, and approvals cannot be granted while paused. **Resume** returns to the previous state. **Cancel** ends the mission and voids any pending proposal.
- **Failures are recorded, not hidden.** An exception during a tick ends the mission as `FAILED`, with the reason.

## Scope

| Field | Default | Meaning |
|---|---|---|
| `allowed_actions` | none | Restricted actions the mission may *propose*: `start_passive_capture`, `modify_scan_schedule` |
| `sensors` | `all` | Sensors an action may target |
| `max_action_duration_s` | 600 | Longest action that may be proposed |
| `max_actions` | 3 | Executed actions per mission |
| `approval_ttl_s` | 3600 | How long an approval, or an unanswered request, stays valid |
| `assess_interval_s` | 300 | Time between assessments |
| `duration_s` | 86400 | Mission length; afterwards the mission ends `TIMED_OUT` |
| `max_assessments` | 48 | Assessment budget; when it is used, the mission ends `COMPLETED` |

## Policy and approval

The policy engine (`rf_platform/analyst/policy.py`) is plain code with no model involved. It decides every proposal:

- **allow:** read-only analysis tools, while the mission is active;
- **require approval:** a restricted action the scope permits, on an in-scope sensor, within the duration limit, with action budget left, while the mission is active;
- **deny:** everything else, with each failed check recorded.

An approval is issued by a **named operator** for the **pending proposal only**. It is bound to a hash of that proposal's action, parameters, and mission, so a changed proposal no longer matches. It expires after `approval_ttl_s` and can be used once. Execution re-runs both the policy check and the approval check immediately beforehand. Any failure is recorded as `execution_refused`, and the executor is never called.

## Choosing follow-ups

Change findings become proposals:

| Finding | Proposal |
|---|---|
| Missing entity | Passive capture on its sensor (restricted) |
| Low-confidence signal change | Passive capture on its sensor (restricted) |
| Sensor gap | Re-check sensor health (read-only); restart the sensor's assignment (restricted) |
| New entity or relationship change | Entity history (read-only) |

Proposals are ranked by **expected information gain per unit cost**:
- Gain is log₂ of the number of explanations still in play (those not weakened).
- Cost is 1 for read-only analysis, and 2 + duration/300 s for restricted actions.

This heuristic is documented, not calibrated. It prefers free, read-only checks over collection when they are equally informative.

After an executed action, the next assessment records an **outcome**: the finding is no longer reported, its explanations changed, or nothing changed.

## Audit

Mission state is never stored directly. Every transition is an event in the append-only `mission_events` table, and the current state is derived by replaying those events. Triggers reject edits and deletions. A mission's full history, including scope approval, every assessment, every proposal with its policy decision, and every approval, denial, refusal, execution, and outcome, comes from `mission status` or `Missions.report()`.

## Run it

Command line, against stored snapshots or a running backend (`--base-url`):

```bash
P="python3 -m rf_platform.analyst mission"
MID=$($P create --name "Lab watch" --objective "Report changes in the lab" --allow start_passive_capture --interval 300)
$P submit $MID
$P approve-scope $MID --operator ops-lead
$P start $MID --operator ops-lead --base-url http://127.0.0.1:8080
$P run $MID --base-url http://127.0.0.1:8080          # ticks until finished; prints pending proposals
$P approve $MID <proposal_id> --operator ops-lead     # or: deny ... --reason "..."
$P status $MID
```

End to end through the Docker demo, with an access point going quiet partway through:

```bash
python3 scripts/mission_demo.py --build                  # stops at the approval request
python3 scripts/mission_demo.py --approve-as ops-demo    # a named operator approves; execution is simulated
```

## Limits

- Execution is simulated. No collection changes are made.
- The information-gain ranking is a heuristic and is not calibrated against outcomes.
- Missions run the deterministic analyst. The optional LLM provider is not used for mission decisions.
- Missions are driven by `tick` (CLI `run`, or the demo script). The dashboard does not yet schedule or display missions.
