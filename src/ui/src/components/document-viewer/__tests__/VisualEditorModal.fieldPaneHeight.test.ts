// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The field pane's height is derived from the row that clips it.
 *
 * Reported while validating [#1327]: the field list "stops short" — the last rows
 * of a long document were unreachable, which on a document whose final rows are
 * nested list items reads as a missing control rather than as a cut-off view.
 *
 * The mechanism is a height that could exceed its container. The row holding both
 * panes is `height: calc(100vh - 300px)`, capped at 700px, and clips with
 * `overflow: hidden`. Between that row and the scrolling box sits a Cloudscape
 * `Container`, which is not a flex container — so `flex: 1` on the box resolves
 * against nothing and its effective height was a hard `550px`. Wherever the
 * Container's header plus 550px exceeded the row, the bottom of the box fell
 * inside the clipped region: it scrolled to its own end with those rows never
 * becoming visible.
 *
 * ⚠️ **This test cannot prove the pane scrolls.** jsdom performs no layout, so
 * `scrollHeight`, `clientHeight` and every computed height are zero there, and an
 * assertion about reachability would pass against the broken pane just as happily.
 * What it pins is narrower and still worth having: that the height is computed
 * from the row's own numbers rather than reintroduced as a literal, which is how
 * the defect would come back. Confirming the fix needs a browser — the `ux-test`
 * tier, or an eye on a deployed stack.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'VisualEditorModal.tsx'), 'utf-8');

describe("the field pane's height", () => {
  it('is derived from the row height and the pane chrome, not fixed', () => {
    // One named figure for the chrome, so the two heights cannot drift apart.
    expect(SOURCE).toMatch(/const FIELD_PANE_CHROME_PX = \d+;/);
    expect(SOURCE).toContain('maxHeight: `min(calc(100vh - ${300 + FIELD_PANE_CHROME_PX}px), ${700 - FIELD_PANE_CHROME_PX}px)`');
    expect(SOURCE).toContain('minHeight: `${450 - FIELD_PANE_CHROME_PX}px`');
  });

  it('subtracts from the same numbers the clipping row uses', () => {
    // If the row's own height changes, the derivation above is stale rather than
    // wrong-by-a-constant — so the three figures it subtracts from are asserted
    // to still be the row's.
    const row = SOURCE.slice(SOURCE.indexOf("flexDirection: 'row'"), SOURCE.indexOf("width: '100%',"));
    expect(row).toContain("height: 'calc(100vh - 300px)'");
    expect(row).toContain("maxHeight: '700px'");
    expect(row).toContain("minHeight: '450px'");
  });

  it('does not reintroduce a literal height for the scrolling box', () => {
    // The specific regression: a fixed figure that can exceed the row it sits in.
    const pane = SOURCE.slice(SOURCE.indexOf("overflowY: 'auto'"));
    expect(pane).not.toMatch(/maxHeight: '5\d\dpx'/);
  });
});
