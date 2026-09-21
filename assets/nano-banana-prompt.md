# Image prompt for nano banana — yolo-dag process diagram

Paste the prompt in the fenced block below into nano banana (Gemini's image model) as-is. It's
written as one self-contained prompt — everything the model needs to know about the pipeline is
in it; it doesn't assume the model has read this repo.

Suggested use: generate it, then look specifically at (a) whether all six phase labels are legible
and correctly ordered left-to-right, and (b) whether the two loop callouts (review loop, task
rework loop) read as *bounded* loops (a small loop-back arrow with a round-cap label) rather than
open-ended cycles. Image models routinely mangle dense in-image text, and a richer, more polished
visual style (gradients, glow, depth) makes that worse before it makes it better — if labels come
out garbled or hard to read against a busy background, regenerate with "please make every text
label larger and simpler, one short phrase per box, on a solid high-contrast card behind the text"
or similar; it's fine to sacrifice a little visual flourish right around the text if that's what
it takes to keep it readable. Once you have a version you like, save it as `assets/workflow.png`
(or `.jpeg`) and swap it into the `![yolo-dag workflow ...]` image reference near the top of
`README.md` (search for `assets/workflow.jpeg` there).

---

```
Create a stunning, premium, modern tech-product infographic — a horizontal flowchart, wide aspect
ratio (landscape, roughly 16:9 or wider) — explaining how a software pipeline called "yolo-dag"
turns a plain-English request into a reviewed, executed, merged code branch. This should look like
the hero architecture graphic on a beautifully designed SaaS product site (think the polish of
Stripe, Linear, or Vercel's own diagrams) — not a corporate whiteboard sketch and not plain
technical documentation. Aim for genuinely eye-catching, portfolio-worthy visual quality.

STYLE: a deep, rich dark background — a subtle charcoal-to-navy gradient, almost black at the
edges — with soft ambient glow pooling behind the busiest parts of the diagram. Render each stage
as a sleek, slightly glassmorphic card: soft-rounded corners, a faint frosted-glass translucency,
a thin luminous border, and a gentle drop shadow that lifts it off the background for real depth.
Connect stages with smooth, gently curved lines (not rigid right angles) rendered in a glowing
gradient stroke, with small soft-glow arrowheads. Use a cohesive, vibrant accent-color gradient
that flows across the whole pipeline left to right — e.g. electric blue → violet → magenta →
warm amber — so the eye reads progress through the pipeline as a shift in color, not just position.
Typography: clean, modern, geometric sans-serif (Inter/Söhne-style), bright near-white text on
dark cards for strong contrast, with the largest text reserved for the eight stage titles. Add
tasteful ambient detail — soft particle/bokeh glow, faint circuit-like linework in the background
at low opacity — enough to feel high-production-value, without ever crowding or fighting the
labels. No photorealism, no literal photos or 3D renders of people/hardware, and nothing so busy
that a stage's label becomes hard to read — beautiful and legible, not beautiful instead of
legible.

OVERALL SHAPE: a left-to-right pipeline with eight stages in a row, connected by arrows, plus two
small annotated side-loops and one small footnote panel. Structure it exactly like this:

STAGE 1 — "brainstorm" (leftmost, the cool end of the accent gradient — electric blue):
a single rounded glass card labeled "brainstorm" with three small bullet icons/labels beneath it reading
"plan-only or full run?", "resolve ambiguity", "size the run". An arrow leads out of this box
labeled "hands off to orchestrator".

STAGE 2 — "Phase 1: Route & Fan-out": a box labeled "Route & Fan-out", with a small fan-out glyph
(one line splitting into several) pointing to a row of small pill-shaped labels representing
specialist domains: "architecture", "research", "test planning", "security", "data & schema",
"design", "ux copy", "cost". Show the first three in solid color (always selected) and the other
five in a lighter/outlined style (selected only when relevant) to visually communicate "always vs.
conditional".

STAGE 3 — "Phase 2: Build + Review Loop": a box labeled "Build + Review Loop (per specialist)".
Inside or beside it, draw a small self-contained loop diagram: one box "specialist drafts" →
arrow to three small parallel boxes labeled "reviewer: completeness", "reviewer: consistency",
"reviewer: feasibility" → arrow converging into one box "consolidator" → arrow labeled "revise"
looping back to "specialist drafts". On the loop-back arrow, add a small round badge that reads
"up to 3 rounds" to make clear this is a bounded loop, not an infinite cycle.

STAGE 4 — "Phase 3: Merge & Reconcile": a box labeled "Merge & Reconcile" showing several small
document icons (one per specialist) flowing into one larger combined document icon labeled "merged
spec", then into a small box labeled "reconciler" with a magnifying-glass icon, checking for
contradictions between the documents.

STAGE 5 — "Phase 4: Decompose": a box labeled "Decompose into Task DAG". Show a small node-graph
icon inside it: several small circles connected by arrows forming a directed acyclic graph (no
cycles, arrows all flowing one direction), with one pair of circles connected by a dashed line
labeled "shared contract" instead of a solid dependency arrow, to convey "independence first,
contracts over dependencies".

STAGE 6 — "Phase 5: Execute in Passes": a box labeled "Execute, Verify & Merge" showing three
small "worktree" boxes running in parallel (side by side), each with a small arrow to a small
"reviewer" box, each of those merging with a checkmark into one thick horizontal bar labeled
"integration branch". Add a small recurring icon of a numbered question mark ("how many tasks in
parallel?") above this stage to show the pass-by-pass pacing.

STAGE 7 — "Phase 6: Verify & Hand Off" (rightmost main stage): a box labeled "Verify & Hand Off"
with a small checklist icon and a shield/checkmark icon, arrow pointing to a final box labeled
"branch ready for review".

FOOTNOTE PANEL (small, bottom of the image, visually distinct — a thin-bordered box, not part of
the main flow): a short annotation reading "Every agent writes its own output directly to its run
file and hands the next agent a path — not a pasted copy — so large documents never have to travel
through a single chat message." Illustrate this with a tiny inset icon: a small robot/agent icon
with an arrow pointing straight down into a small file/document icon labeled with a small folder
tag, rather than an arrow pointing sideways into another robot icon (which would represent the
slower, old way) — show the sideways version crossed out faintly for contrast if space allows.

TOP LABEL: small text above the whole flow reading "yolo-dag: request → reviewed, executed branch".

Keep every card's internal label short and readable — prioritize legibility over density, even
inside a lush, glowing visual style. Use one consistent connector style throughout (smooth curved
gradient-glow lines with soft arrowheads, as described above) except for the two bounded-loop
callouts, which should curve back on themselves with a small luminous round badge showing the
round cap, and the shared-contract edge in Phase 4, which should be a dashed glowing line instead
of a solid one, so it still reads as visually distinct at a glance.
```

---

## If you want a second, simpler version

The prompt above is dense — it's aiming for something close to the detail level of the existing
`assets/workflow.svg`. If nano banana struggles to keep eight stages plus two side-loops legible
in one image, ask it to drop the two in-flow loop diagrams (Phase 2's review loop and the
footnote panel) and render *only* the eight-stage top-level flow first; then generate the loop
detail and the artifact-writing footnote as two small separate inset graphics you place around the
main flow when assembling the final image.
