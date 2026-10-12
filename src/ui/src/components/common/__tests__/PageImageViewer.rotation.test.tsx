// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A page can be rotated in the viewer, and the overlays rotate with it.
 *
 * Scanned pages arrive sideways, which made them unreadable in the Test Studio
 * ground-truth editor — the viewer offered zoom and pan but no rotation
 * (issue #1320).
 *
 * The geometry is the part worth pinning. Bounding-box overlays are
 * axis-aligned divs positioned from the image's on-screen rect, and that rect
 * reports a quarter-turned image's extent already transposed. So the overlay
 * needs its own corner remap, and an off-by-one-quarter-turn there is not
 * visible in a diff: it puts a highlighted field somewhere plausible but wrong
 * on the page. The invariants below (corner mapping, extent swap, four turns
 * returning to the start, staying inside the page) are what distinguish the
 * four cases from each other.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

vi.mock('../generate-s3-presigned-url', () => ({ default: vi.fn(() => Promise.resolve('https://example.test/signed')) }));
// Stable identities on purpose: the viewer's image-load effect is keyed on
// these, so a fresh object per render re-enters loading forever.
const APP_CONTEXT = { currentCredentials: {} };
const VERSION_CONTEXT = { isHistorical: false, runId: null, versionIdForUri: () => undefined };
vi.mock('../../../contexts/app', () => ({ default: () => APP_CONTEXT }));
vi.mock('../../../contexts/document-version', () => ({ useDocumentVersion: () => VERSION_CONTEXT }));

import PageImageViewer, { rotateBoundingBox } from '../PageImageViewer';
import type { NormalisedBox } from '../PageImageViewer';

/** A box in the page's upper-left quadrant, deliberately not square. */
const BOX = { left: 0.1, top: 0.2, width: 0.3, height: 0.4 };

/**
 * Compare boxes approximately: the remap subtracts normalised sums, so exact
 * equality would be asserting against float noise (0.2 + 0.4 is not 0.6) for a
 * difference far below one device pixel.
 */
const expectBox = (actual: NormalisedBox, expected: NormalisedBox) => {
  (['left', 'top', 'width', 'height'] as const).forEach((key) => {
    expect(actual[key]).toBeCloseTo(expected[key], 10);
  });
};

describe('rotateBoundingBox', () => {
  it('leaves the box alone when the page is not rotated', () => {
    expect(rotateBoundingBox(BOX, 0)).toEqual(BOX);
    expect(rotateBoundingBox(BOX, 360)).toEqual(BOX);
  });

  it('sends the top-left corner of the page to the top-right at 90°', () => {
    // A strip along the page's top-left corner...
    const corner = { left: 0, top: 0, width: 0.2, height: 0.1 };
    // ...ends up against the right edge, running down from the top.
    expect(rotateBoundingBox(corner, 90)).toEqual({ left: 0.9, top: 0, width: 0.1, height: 0.2 });
  });

  it('sends the top-left corner of the page to the bottom-left at 270°', () => {
    const corner = { left: 0, top: 0, width: 0.2, height: 0.1 };
    expect(rotateBoundingBox(corner, 270)).toEqual({ left: 0, top: 0.8, width: 0.1, height: 0.2 });
  });

  it('transposes the extent on a quarter turn and preserves it on a half turn', () => {
    expect(rotateBoundingBox(BOX, 90)).toMatchObject({ width: BOX.height, height: BOX.width });
    expect(rotateBoundingBox(BOX, 270)).toMatchObject({ width: BOX.height, height: BOX.width });
    expect(rotateBoundingBox(BOX, 180)).toMatchObject({ width: BOX.width, height: BOX.height });
  });

  it('reflects the box through the page centre at 180°', () => {
    expectBox(rotateBoundingBox(BOX, 180), { left: 0.6, top: 0.4, width: 0.3, height: 0.4 });
  });

  it('distinguishes all four quarter turns', () => {
    const seen = [0, 90, 180, 270].map((deg) => JSON.stringify(rotateBoundingBox(BOX, deg)));
    expect(new Set(seen).size).toBe(4);
  });

  it('returns to the original box after four right turns', () => {
    let box = BOX;
    for (let i = 0; i < 4; i += 1) box = rotateBoundingBox(box, 90);
    expectBox(box, BOX);
  });

  it('composes: two right turns are a half turn, three are a left turn', () => {
    expectBox(rotateBoundingBox(rotateBoundingBox(BOX, 90), 90), rotateBoundingBox(BOX, 180));
    expectBox(rotateBoundingBox(rotateBoundingBox(BOX, 180), 90), rotateBoundingBox(BOX, 270));
  });

  it('reads a left turn and its negative spelling the same way', () => {
    // The viewer keeps rotation in 0–359, but nothing stops a caller passing
    // -90, and the two must not disagree.
    expectBox(rotateBoundingBox(BOX, -90), rotateBoundingBox(BOX, 270));
    expectBox(rotateBoundingBox(BOX, -360), BOX);
    expectBox(rotateBoundingBox(BOX, 450), rotateBoundingBox(BOX, 90));
  });

  it('agrees with a pixel-level rotation of the page', () => {
    // An independent model of the same thing, to catch a reversed direction —
    // the failure mode the cases above cannot see, since they encode the same
    // assumption the implementation does. Rotate the page's *pixels* instead:
    // on a clockwise quarter turn a pixel at (x, y) of a W×H page lands at
    // (H - y, x) of the H×W result. Then renormalise and compare.
    const W = 400;
    const H = 600;
    // Page dimensions are a parameter because the turn swaps them: chaining two
    // turns has to feed the second one an H×W page.
    const rotateByPixels = (b: NormalisedBox, w: number, h: number): NormalisedBox => {
      const x1 = b.left * w;
      const x2 = (b.left + b.width) * w;
      const y1 = b.top * h;
      const y2 = (b.top + b.height) * h;
      // Corners after the turn, in the rotated page's pixel space (h wide, w tall).
      const left = h - y2;
      const right = h - y1;
      return { left: left / h, top: x1 / w, width: (right - left) / h, height: (x2 - x1) / w };
    };

    // A fixed spread of boxes: corners, edges, a thin strip, the whole page.
    const boxes: NormalisedBox[] = [
      BOX,
      { left: 0, top: 0, width: 1, height: 1 },
      { left: 0, top: 0, width: 0.1, height: 0.1 },
      { left: 0.9, top: 0.9, width: 0.1, height: 0.1 },
      { left: 0.25, top: 0, width: 0.5, height: 0.02 },
      { left: 0.98, top: 0.4, width: 0.02, height: 0.2 },
    ];
    boxes.forEach((b) => expectBox(rotateBoundingBox(b, 90), rotateByPixels(b, W, H)));
    // Two pixel-level right turns must likewise agree with the half turn, and
    // three with the left turn.
    boxes.forEach((b) => expectBox(rotateBoundingBox(b, 180), rotateByPixels(rotateByPixels(b, W, H), H, W)));
    boxes.forEach((b) => expectBox(rotateBoundingBox(b, 270), rotateByPixels(rotateByPixels(rotateByPixels(b, W, H), H, W), W, H)));
  });

  it('keeps the box inside the page at every rotation', () => {
    [0, 90, 180, 270].forEach((deg) => {
      const r = rotateBoundingBox(BOX, deg);
      expect(r.left).toBeGreaterThanOrEqual(0);
      expect(r.top).toBeGreaterThanOrEqual(0);
      expect(r.left + r.width).toBeLessThanOrEqual(1);
      expect(r.top + r.height).toBeLessThanOrEqual(1);
    });
  });
});

const ONE_PAGE_IDS = ['1'];
const ONE_PAGE = [{ Id: '1', ImageUri: 'blob:page-1' }];
const TWO_PAGE_IDS = ['1', '2'];
const TWO_PAGES = [
  { Id: '1', ImageUri: 'blob:page-1' },
  { Id: '2', ImageUri: 'blob:page-2' },
];

/** Render the viewer over one client-side page image (no presigning needed). */
const renderViewer = () => render(<PageImageViewer pageIds={ONE_PAGE_IDS} documentPages={ONE_PAGE} />);

const pageImage = () => screen.getByAltText('Page 1');

describe('PageImageViewer rotation controls', () => {
  it('turns the page right, wrapping back to 0° on the fourth click', async () => {
    renderViewer();
    await waitFor(() => expect(pageImage()).toBeInTheDocument());

    const right = screen.getByTitle('Rotate Right 90°');
    expect(screen.getByText('0°')).toBeInTheDocument();

    right.click();
    await waitFor(() => expect(screen.getByText('90°')).toBeInTheDocument());
    expect(pageImage().style.transform).toContain('rotate(90deg)');

    right.click();
    right.click();
    await waitFor(() => expect(screen.getByText('270°')).toBeInTheDocument());

    right.click();
    await waitFor(() => expect(screen.getByText('0°')).toBeInTheDocument());
  });

  it('turns the page left without ever showing a negative angle', async () => {
    renderViewer();
    await waitFor(() => expect(pageImage()).toBeInTheDocument());

    screen.getByTitle('Rotate Left 90°').click();
    await waitFor(() => expect(screen.getByText('270°')).toBeInTheDocument());
    expect(screen.queryByText('-90°')).not.toBeInTheDocument();
  });

  it('clears the rotation on reset, along with zoom and pan', async () => {
    renderViewer();
    await waitFor(() => expect(pageImage()).toBeInTheDocument());

    screen.getByTitle('Rotate Right 90°').click();
    screen.getByTitle('Zoom In').click();
    await waitFor(() => expect(screen.getByText('90°')).toBeInTheDocument());

    screen.getByTitle('Reset View').click();
    await waitFor(() => expect(screen.getByText('0°')).toBeInTheDocument());
    expect(screen.getByText('100%')).toBeInTheDocument();
  });

  it('keeps the rotation applied while navigating pages', async () => {
    // A misoriented scan is usually misoriented on every page, so rotation is
    // sticky across page navigation the way zoom and pan already are.
    render(<PageImageViewer pageIds={TWO_PAGE_IDS} documentPages={TWO_PAGES} />);
    await waitFor(() => expect(pageImage()).toBeInTheDocument());

    screen.getByTitle('Rotate Right 90°').click();
    await waitFor(() => expect(screen.getByText('90°')).toBeInTheDocument());

    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument();

    // The only real <button>s here are the two page chevrons; every zoom, pan
    // and rotate control is a span carrying role="button".
    const chevrons = document.querySelectorAll('button');
    expect(chevrons).toHaveLength(2);
    (chevrons[1] as HTMLButtonElement).click();

    await waitFor(() => expect(screen.getByText('Page 2 of 2')).toBeInTheDocument());
    expect(screen.getByText('90°')).toBeInTheDocument();
    expect(screen.getByAltText('Page 2').style.transform).toContain('rotate(90deg)');
  });
});
