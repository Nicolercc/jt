# Today and Review usability trial

Open `today-review.html` in a Mac browser. The HTML uses synthetic data and
keeps changes in memory. Reset the demo between runs.

## Run the comparison

Give Nicole and two fresh testers the prompts below without showing them the
expected answers. For the first task, compare against a synthetic inbox and
SwiftBar fixture with equivalent content. In the current SwiftBar workflow,
Today is a menu, source and completion are menu actions, and reviewing a
proposal opens Terminal. Record which tools each tester actually uses. Use
equivalent messages with different names when repeating a task so recognition
of a prior answer does not inflate the speed result.

| Scenario | Read this to the tester | Observe |
| --- | --- | --- |
| One due action | Find what the recruiter needs, find the original request, and tell the tracker you took care of it. | Identifies Frontend Engineer and the 4 PM ET deadline, opens the source in one click after finding the action, and sees completion. |
| Two roles at one company | Use the email to decide which application this update belongs to. | Identifies Frontend Engineer on Design Systems, sees the proposed change, and confirms the right role. Neither role starts selected. |
| Empty day | Tell me whether you need to do anything today. | Recognizes the empty state without searching another screen. |

Capture time to identify the next action, total task time, wrong selections,
hesitations, and requests for help. Aim for about five seconds to identify the
action once Today is open and a faster full task than the baseline. Repeat the
prototype with the keyboard and record focus and status announcements with
VoiceOver. Check Escape closes the source email and restores focus to Open
email. Check Leave for later preserves the proposal and confirm requires a
role. Revise any interaction that needs coaching before signing off.

## Contract questions for PR 2

Map source email and application IDs to the existing read model. Verify repeat
completion, a lost write response followed by reload, and review approval
against stale application state using a disposable database. Confirm the
product wording for the backend's screen stage during the walkthrough.

## Verification

Starting revision: a07c936d11f808bab7a5e337bb421c7b12b5f8c7.

- Synthetic CLI demo completed.
- Prototype script smoke test covered due action, opening source, completion,
  deferral, role confirmation, and empty state with DOM stubs.
- Python suite: 273 passed and 10 setup errors because the sandbox blocks
  localhost listeners. Rerun the full suite on a Mac or in CI.
- Browser, VoiceOver, visual evidence and all participant timings are pending.

## Classifier labeling

Follow [the labeling guide](../EVAL_LABELING.md) on the private corpus.
Keep existing holdout assignments frozen. Keep offers and rejections in
review during the pilot. Use ambiguity, missed recruiter updates, and wrong
application matches to guide which examples to collect. Export only
aggregate results.
