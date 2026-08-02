# UI Restyle: Bioluminescent Deep-Sea Theme

## Goal

Restyle `templates/index.html` (Brainwave Monitor dashboard) so it stops
reading as a generic AI-generated dashboard template, while leaving
**100% of functionality untouched**: same DOM element `id`s, same JS logic,
same Socket.IO event names/payloads, same SVG structure for the brain map.
This is a pure `<style>` (and minor cosmetic markup/class) change.

## Problem with current design

- Palette (dark navy `#0f0f1a` + flat neon green/purple/amber) is a common
  default for AI-generated dashboards.
- Card grid uses flat rounded rectangles with hard 1px borders — a
  recognizable templated pattern.
- Typography is a single generic monospace (`Courier New`) throughout,
  with no distinct visual mood tied to "brain / neural" subject matter.

## Direction: Bioluminescent Deep-Sea

**Palette** — background moves from navy to near-black teal
(`#050e0d`–`#081412`). Cards keep flat teal-black fill but replace hard
borders with soft glow edges (box-shadow based) and a subtle radial
gradient, evoking bioluminescent organisms suspended in dark water rather
than boxed UI widgets.

State colors keep their current semantic mapping but are re-rendered as
soft emitted glow instead of flat neon fill:
- Calm → teal-cyan
- Flow → warm amber-gold
- Tense → magenta-violet

**Typography** — two-track system:
- Headers/labels (page title, section labels, channel names, buttons):
  rounded organic sans-serif — `"Space Grotesk"` / `"Outfit"` with system
  sans fallback — looser letter-spacing, replacing the current tight
  uppercase mono look.
- Numeric/data readouts (Hz values, bpm, battery %, waveform canvases,
  state badge): refined monospace — `"JetBrains Mono"` with `Courier New`
  fallback — kept so data still reads as precise/technical.

**Structure** — layout skeleton is unchanged (header → divider → main
card → footer). Within existing cards: replace flat 1px borders with
soft glow-edge treatment, replace flat card fill with a subtle organic
gradient, give the state badge / spectrum track / brain map their own
glow signature consistent with the bioluminescent palette.

## Explicitly out of scope / must not change

- No element `id` renames.
- No changes to JS logic, Socket.IO event names, or payload handling.
- No changes to SVG brain-map node ids (`eq-AF7`, `halo-AF7`,
  `link-frontal`, etc.) or their functional attributes (only visual
  styling of fills/strokes may change, driven by the same JS that sets
  them today).
- No changes to `overlay_brainwave_visual.html`, `overlay_brain_art.html`,
  `overlay_mental_command.html` (out of scope for this pass).
- No new external network dependencies — if web fonts are used, provide a
  system-font fallback stack so the page still renders correctly offline
  (this app runs on a local Flask server, not guaranteed internet access).

## Testing / verification

Visual-only change — no automated tests apply. Verification is manual:
load `index.html` in a browser (via the running Flask/Socket.IO server),
confirm all interactive elements (BCI connect button, mute, cursor
control, mental command flashes, waveform rendering, brain map
electrode glow) still function exactly as before, only the visual
presentation differs.
