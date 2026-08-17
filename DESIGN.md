---
name: "Technicat Automation"
description: "An evidence-led revision takeoff desk for optional estimator edits and direct pricing."
colors:
  technical-blue: "#137EAE"
  technical-blue-hover: "#0F709C"
  technical-blue-pressed: "#0A5E84"
  technical-blue-deep: "#143E52"
  drafting-canvas: "#F2F4F3"
  work-surface: "#FAFBFA"
  white-surface: "#FFFFFF"
  muted-work-surface: "#F5F7F6"
  header-fill: "#E8ECEE"
  graphite: "#20282D"
  graphite-strong: "#182126"
  graphite-muted: "#5C686E"
  rule: "#BFC7CA"
  rule-strong: "#AEB8BC"
  selection-blue: "#C9E3EE"
  approval-fill: "#E7F4EA"
  approval-text: "#27683D"
  warning-fill: "#FFF3D3"
  warning-text: "#795300"
  conflict-fill: "#FFE8B0"
  exclusion-fill: "#ECEFF1"
  danger-fill: "#FFF5F3"
  danger-text: "#9A3C2D"
  disabled-fill: "#D6DCDE"
  disabled-text: "#7B858A"
  sidebar: "#20292F"
  sidebar-text: "#C3CDD2"
typography:
  display:
    fontFamily: "Segoe UI, sans-serif"
    fontSize: "24px"
    fontWeight: 700
    letterSpacing: "-0.2px"
  title:
    fontFamily: "Segoe UI, sans-serif"
    fontSize: "15px"
    fontWeight: 700
  body:
    fontFamily: "Segoe UI, sans-serif"
    fontSize: "13px"
    fontWeight: 400
  label:
    fontFamily: "Segoe UI, sans-serif"
    fontSize: "11px"
    fontWeight: 700
    letterSpacing: "0.6px"
  mono:
    fontFamily: "Cascadia Mono, Consolas, monospace"
    fontSize: "11px"
    fontWeight: 400
rounded:
  tight: "2px"
  control: "3px"
spacing:
  micro: "4px"
  compact: "8px"
  control: "10px"
  section: "14px"
  panel: "18px"
  page: "28px"
components:
  button-primary:
    backgroundColor: "{colors.technical-blue}"
    textColor: "{colors.white-surface}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "7px 13px"
    height: "34px"
  button-primary-hover:
    backgroundColor: "{colors.technical-blue-hover}"
    textColor: "{colors.white-surface}"
  button-primary-active:
    backgroundColor: "{colors.technical-blue-pressed}"
    textColor: "{colors.white-surface}"
  button-primary-disabled:
    backgroundColor: "{colors.disabled-fill}"
    textColor: "{colors.disabled-text}"
    rounded: "{rounded.control}"
    padding: "7px 13px"
  button-secondary:
    backgroundColor: "{colors.work-surface}"
    textColor: "{colors.graphite}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "7px 13px"
  input:
    backgroundColor: "{colors.white-surface}"
    textColor: "{colors.graphite}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "6px 8px"
  card:
    backgroundColor: "{colors.work-surface}"
    textColor: "{colors.graphite}"
    rounded: "{rounded.control}"
    padding: "18px"
  status-chip:
    backgroundColor: "{colors.header-fill}"
    textColor: "{colors.graphite-muted}"
    typography: "{typography.label}"
    rounded: "{rounded.tight}"
    padding: "4px 9px"
---

# Design System: Technicat Automation

## Overview

**Creative North Star: "The Revision Takeoff Desk"**

Technicat Automation looks and behaves like an estimator's marked-up review surface: cool drafting-white workplanes, graphite rules, dense schedules, and compact controls. The visual hierarchy favors evidence, editable quantities, source cross-check notes, and pricing scope over decoration. Technical blue identifies the current task and primary action; amber marks source discrepancies; exclusion gray marks rows omitted from pricing.

The interface is an Operate-mode Windows desktop tool. Familiar Qt navigation, persistent context rails, dense tables, and direct pricing actions carry the experience. The system is planar and structural: depth comes from adjacent tones and one-pixel rules, not floating-card shadows. State changes are immediate, with no authored decorative animation.

**Key Characteristics:**

- Drafting-white work surfaces bounded by cool-gray and graphite rules.
- Dense, scan-first tables with compact labels and persistent engineering context.
- Technical blue reserved for selection, focus, the active workflow stage, and primary actions.
- Amber, green, and exclusion gray paired with explicit text or decision labels.
- Square-to-gently-curved geometry, mostly two- and three-pixel radii.
- Segoe UI for the workbench; Cascadia Mono or Consolas for IDs, evidence, measurements, and logs.

## Colors

The palette is a cool drafting neutral system with one technical-blue interaction voice and tightly scoped engineering status colors.

### Primary

- **Technical Blue:** Drives the active workflow stage, primary actions, focus borders, and current selection. Darker implemented variants supply hover and pressed feedback.
- **Deep Technical Blue:** Anchors selected navigation and other dark active surfaces without competing with the brighter action blue.

### Secondary

- **Ready Green:** Appears only for valid, ready-to-price state and completed export.
- **Cross-check Amber:** Marks source discrepancies and dirty save state without implying a blocker or a changed value.
- **Exclusion Gray:** Separates intentionally excluded rows from included rows.
- **Danger Red:** Supports explicit exclusion or destructive intent; it is not a general accent.

### Neutral

- **Drafting Canvas:** The application background behind work areas.
- **Work Surface:** The dominant panel, table, toolbar, and footer surface.
- **Muted Work Surface:** Side rails and secondary structural regions.
- **Header Fill:** Table headers, tabs, and neutral status chips.
- **Graphite:** Default body copy and strong technical structure.
- **Muted Graphite:** Secondary explanations and metadata.
- **Cool Rules:** One-pixel boundaries distinguish adjacent regions without shadows.
- **Dark Sidebar:** A fixed navigation anchor with light text and a narrow blue active rule.

**The One Technical Voice Rule.** Technical blue identifies interaction and current position; it is not decorative fill.

**The Scope Color Rule.** Neutral means included, amber means a source cross-check note is present, and gray means excluded; every use is reinforced by text in the Pricing scope column or detail rail.

## Typography

**Display Font:** Segoe UI (with sans-serif fallback)  
**Body Font:** Segoe UI (with sans-serif fallback)  
**Label/Mono Font:** Cascadia Mono (with Consolas and monospace fallbacks)

**Character:** The typography is workmanlike and native to Windows. Compact labels and tabular metadata keep the interface legible at engineering-tool density without turning it into a terminal.

### Hierarchy

- **Display** (700, 24px, slightly tightened): Page titles such as the workbench overview and database manager.
- **Title** (700, 15px): Decision titles, tile titles, and other local hierarchy anchors.
- **Body** (400, 13px): Controls, forms, table cells, and working copy.
- **Supporting copy** (400, 14px): Page subtitles and explanatory framing.
- **Label** (700-800, 11-12px, 0.5-0.8px tracking): Uppercase rail, table, workflow, status, and section labels.
- **Mono** (400, 10-12px): Project references, evidence identifiers, entity handles, measurements, and extraction logs.

**The Evidence Type Rule.** Monospace is for identifiers, measurements, evidence, and logs; prose and controls stay in Segoe UI.

## Layout

The main shell opens at 1360×860 with a fixed 216px dark sidebar and a flexible stacked work area. Primary pages use 24-40px outer margins, 18-24px section spacing, and bordered panels. The project intake surface keeps form configuration and the extraction log side by side at a 5:6 proportion; the home surface uses horizontal stat and action rows.

The panel-review dialog opens at 1460×900 and enforces a 1080×680 minimum. Its horizontal splitter starts at approximately 235px / 930px / 295px for panel navigation, BOM work area, and decision rail. The center contains a vertical splitter for the editable BOM and the terminal/accessory/editable-offer tabs; selecting the offer expands it to roughly half the work area. Splitters are user-adjustable, the offer table uses per-row manufacturer dropdowns, the decision rail explains automatic constrained-AI review before source evidence, and dense tables retain horizontal scrolling when columns exceed the available width.

This is an implemented desktop layout, not a mobile stack. Below the review minimum, the window does not collapse to a phone composition. Qt layouts redistribute flexible space, while fixed rails, horizontal control groups, and tables preserve the workstation workflow. Windows display scaling is supported through native Qt layout and font metrics; new screens must avoid absolute positioning that breaks that behavior.

**The Three-Plane Review Rule.** Panel context stays left, the active schedule stays central, and evidence plus decision actions stay right.

## Elevation & Depth

The system uses no authored drop shadows. Hierarchy comes from adjacent neutral fills, one-pixel rules, the dark sidebar, selected-row tint, and inset table headers. Cards are structural containers on the drafting canvas, not floating objects.

**The Flat-by-Default Rule.** Add depth with tonal separation and rules; do not introduce floating-card shadows, glass, blur, or gradient elevation.

## Shapes

The form language is square and technical. Major panels, inputs, buttons, progress tracks, and action tiles use compact two- or three-pixel radii. Dividers and table cells stay straight. Status chips are small bordered rectangles rather than pills, and the active sidebar item uses a narrow two-pixel left rule.

**The Drafting Edge Rule.** Reusable controls stay within the implemented 2-3px corner vocabulary; rounded capsules and oversized squircles do not belong in the workbench.

## Components

### Buttons

- **Shape:** Compact rectangular controls with gently curved corners (3px), 7px vertical and 13px horizontal padding, semibold 13px text, and a 20px content-height floor.
- **Primary:** Technical-blue fill, white text, and a darker blue one-pixel border. Hover and pressed states darken; disabled state becomes neutral gray with muted graphite text.
- **Secondary:** Work-surface fill, graphite text, and a cool-gray border. Hover increases tonal and border contrast.
- **Ghost:** Transparent with technical-blue text; hover adds a pale blue field.
- **Scope actions:** Include uses a neutral secondary action and exclusion uses pale red. Labels state the pricing effect explicitly.
- **Focus:** Keyboard focus replaces the normal control edge with a clearly visible two-pixel technical-blue border.

### Status Chips

- **Style:** Compact bordered rectangles (2px radius) with uppercase 11px semibold labels and 4px × 9px padding.
- **State:** Neutral, running, complete, and blocked states change fill, text, and border together. Their words remain the primary status cue.

### Cards / Containers

- **Corner Style:** Gently curved structural panels (3px).
- **Background:** Work-surface fill on the drafting canvas; secondary rails use a slightly muted work surface.
- **Shadow Strategy:** None.
- **Border:** One-pixel cool-gray rule.
- **Internal Padding:** Compact work panels generally use 14-20px; page-level surfaces use 24-40px outer margins.

### Inputs / Fields

- **Style:** White field, graphite text, one-pixel cool-gray border, 3px radius, and 6px × 8px internal padding.
- **Focus:** Two-pixel technical-blue border. Text selection uses pale blue.
- **Error / Disabled:** Validation is expressed through explicit Qt messages and actionable copy; disabled controls retain readable muted contrast.

### Tables

- **Style:** Dense work-surface tables with 11px bold headers, compact 6-8px cell/header padding, cool-gray grid rules, interactive column widths, and visible row selection.
- **Pricing scope:** BOM rows use neutral included, amber note, or exclusion gray. The Pricing scope column, tooltips, and detail rail provide non-color confirmation.
- **Editing:** Review tables support row selection, multi-selection where actions apply in bulk, double-click/Edit-key editing, and read-only derived schedules.

### Navigation and Workflow

- **Sidebar:** Fixed dark graphite rail; hovered items receive a darker surface and selected items receive a deep-blue field plus a two-pixel bright-blue left rule.
- **Workflow strip:** Drawings → Extract → Review → Price → Export appears as a connected numbered sequence. Complete, active, and pending stages are labeled and color-coded.
- **Detail rail:** Selecting a BOM row exposes its title, manufacturer, source confidence, pricing scope, evidence handles, render preview, source cross-check notes, and include/exclude actions in one scrollable place.
- **Pricing readiness:** The footer states included, excluded, and cross-check-note counts. Pricing is immediately available whenever at least one valid row is included.
- **Long-running pricing:** Matching and Excel generation stay off the UI thread. The footer reports the current catalog item, protects edits while the saved snapshot is processed, and keeps a dedicated cancel action in a stable layout position.
- **Import terminal:** Price-list imports use a window-modal terminal surface with an indeterminate progress track, copyable live output, explicit running/complete/failed text, and a close action that unlocks only after the database transaction finishes.
- **Preview performance:** Drawing previews are deferred, decoded near their display size, and cached per source revision. Lower schedule tables load only when their tab is opened.

### Accessibility and Motion

- Native Qt controls preserve keyboard traversal and platform semantics. Implemented shortcuts include Alt+D, Alt+O, Alt+A, Alt+R, Alt+E, Ctrl+S, and Ctrl+P; key filters and editable fields also carry accessible names where provided.
- Focus is visible through a two-pixel technical-blue border. Status never depends on hue alone: text labels, counts, table decisions, and disabled/enabled action state reinforce meaning.
- The shipped interface defines no decorative transitions, parallax, or animated rearrangement. State changes are immediate, so reduced-motion users receive the same complete interaction model without a separate animated variant.

## Do's and Don'ts

### Do:

- **Do** keep evidence, source cross-check notes, source confidence, manufacturer, and pricing scope adjacent in review flows.
- **Do** use dense ruled tables for schedules and editable engineering data.
- **Do** reserve technical blue for current position, focus, selection, and primary action.
- **Do** pair every approval, warning, exclusion, error, and blocked state with words or another non-color cue.
- **Do** preserve visible keyboard focus, native tab order, shortcuts, and Qt scaling behavior.
- **Do** keep pricing directly available while making edits and exclusions easy to understand.

### Don't:

- **Don't** introduce floating-card shadows, glassmorphism, gradients, decorative blobs, or soft SaaS-style visual effects.
- **Don't** replace the desktop workbench with generic metric-card dashboards or a console-first workflow.
- **Don't** use green for generic success decoration or amber for neutral emphasis; these colors carry decision meaning.
- **Don't** hide dense engineering data behind hover-only affordances or color-only status.
- **Don't** add decorative motion; preserve the immediate state-change model and reduced-motion safety.
- **Don't** round controls into pills or expand the established 2-3px radius vocabulary without a functional reason.
