# Don: what this investigation means

**The vision gate should not be the final art judge for this Bildbot set.** It combines real image inspection with guesses about unseen surfaces. We found defects in what it was asked to do, not proof that your approved back details were wrong.

## What was actually wrong

- The prompt included historical `_about` notes containing the old “no bolts” rule, alongside the new description allowing bolts.
- It then told the model that back/side head bolts absent from the front image were invented. That contradicted your back-face design.
- It did not calculate which corners or panels were visible. “Should be visible from east” and “no counterpart on the anchor” were partly stock phrases added by Warden, not geometric proof.
- A broken reply was correctly blocked, but the summary made it look like another artistic disagreement. Worse, an incomplete JSON object could be accepted as a clean match.
- Sampling settings were implicit. The old records also lack fingerprints proving that the new cache path was active. A long-running MCP process does not automatically reload an editable installation.

## What the proposed branch fixes

Historical metadata is excluded from design instructions. The applicable face description has explicit authority for surfaces the front cannot show. Front, back, profile and diagonal visibility are explained conservatively, without pretending to ray-cast the artwork. JSON errors/incomplete answers are unassessed, never approvals. Reported cost and failed-attempt history are retained without automatic retries. Sampling is explicit and request parameters/hashes are auditable. Unchanged completed comparisons can be reused; metadata-only edits no longer cause unnecessary model calls.

**These are offline-tested plumbing/prompt fixes, not proof that Gemini will now judge every image correctly.** No paid model experiment was made.

## The size setting

96×96 is the canvas. Your drawn body is approximately 59 pixels high. Warden's `size_tier` is a drawn-height policy: `48-64` accepts 40–64px, while `96` requires 72–96px. The pipeline says `48-64`, but the saved identity says `96`. Correcting that identity value while keeping the 96×96 canvas is the proposed data fix. It was not applied here; the check was not loosened.

## Recommended next step

Review the proposal before activating it. Keep pixel checks hard. Use your current neutral-background images and explicit decisions per view for completed artistic disagreements; do not chase a passing opinion by rerunning the critic. A malformed west reply cannot be accepted as evidence, and this report does not authorize a paid replacement assessment. Also, the genuinely wrong north-arm orientation is still an independent repair issue—it must not disappear under a blanket approval.

Once art/evidence is ready, remote publication still needs exact source/upload/target authorization and full-character pixel readback. No remote replacement or attack generation was performed here.

**Verification: 632 tests passed, 2 skipped, including browser tests. This session did not edit the game. PNG hashes are unchanged, but a separate no-vision re-gating action changed live records/pipeline/STATUS during the investigation; the report preserves that distinction. Investigation branch: `fix/vision-reference-face-authority`. This is the pre-integration summary; consult Git for current commit/merge status.**
