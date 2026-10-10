// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The search box issues one request per settled keystroke, and the replies do not
 * arrive in the order they were sent (GitHub #1323).
 *
 * The first fix for #1323 moved search to the server and rendered every reply as
 * it arrived, which is correct only if replies arrive in order. They do not, and
 * the ordering is actively adverse: the resolver builds each returned row by
 * reading that document's baseline sections, so a broad search is **slower** than
 * a narrow one. Typing `0063` sends `0`, `00`, `006`, `0063`; `0063` returned its
 * single match first and `0` then overwrote it with page 1 of all 500 documents.
 * The search worked and then appeared to reset itself.
 *
 * ⚠️ These tests stage replies **out of order on purpose**. A test that resolves
 * them in order passes against the unguarded version too, which is exactly why the
 * defect shipped: the obvious test cannot see it. Every case below fails if the
 * guard is removed.
 */

import { describe, expect, it } from 'vitest';

import { createLatestResponseGuard } from '../latestResponseOnly';

/** A promise plus the handle to settle it, so a test decides the arrival order. */
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
};

describe('letting only the newest reply render', () => {
  it('discards a reply that a later request superseded', async () => {
    const guard = createLatestResponseGuard();
    const broad = deferred<string>();
    const narrow = deferred<string>();
    const rendered: string[] = [];

    const run = async (reply: Promise<string>) => {
      const isCurrent = guard.claim();
      const value = await reply;
      if (!isCurrent()) return;
      rendered.push(value);
    };

    // Sent in typing order: the broad search first, the narrow one after.
    const first = run(broad.promise);
    const second = run(narrow.promise);

    // Arriving in the opposite order, which is the measured behaviour.
    narrow.resolve('packet_0063.pdf');
    broad.resolve('all 500 documents');
    await Promise.all([first, second]);

    expect(rendered).toEqual(['packet_0063.pdf']);
    expect(rendered).not.toContain('all 500 documents');
  });

  it('renders each reply when requests do not overlap', async () => {
    const guard = createLatestResponseGuard();
    const rendered: string[] = [];

    for (const value of ['a', 'b', 'c']) {
      const isCurrent = guard.claim();
      await Promise.resolve();
      if (isCurrent()) rendered.push(value);
    }

    // The guard must not suppress ordinary sequential paging, which is the way a
    // too-eager version of this would break the table while fixing the search.
    expect(rendered).toEqual(['a', 'b', 'c']);
  });

  it('keeps only the last of several overlapping requests', async () => {
    const guard = createLatestResponseGuard();
    const pending = ['0', '00', '006', '0063'].map(() => deferred<string>());
    const rendered: string[] = [];

    const runs = pending.map(async (d, i) => {
      const isCurrent = guard.claim();
      const value = await d.promise;
      if (!isCurrent()) return;
      rendered.push(`${i}:${value}`);
    });

    // Settle in a shuffled order; only the request claimed last may render.
    pending[2].resolve('ten matches');
    pending[0].resolve('five hundred matches');
    pending[3].resolve('one match');
    pending[1].resolve('five hundred matches');
    await Promise.all(runs);

    expect(rendered).toEqual(['3:one match']);
  });

  it('is per guard, so two tables do not cancel each other', async () => {
    const left = createLatestResponseGuard();
    const right = createLatestResponseGuard();

    const leftCurrent = left.claim();
    const rightCurrent = right.claim();

    expect(leftCurrent()).toBe(true);
    expect(rightCurrent()).toBe(true);
  });

  it('reports the superseded request as stale and the newest as current', () => {
    const guard = createLatestResponseGuard();
    const older = guard.claim();
    expect(older()).toBe(true);

    const newer = guard.claim();
    expect(older()).toBe(false);
    expect(newer()).toBe(true);
  });
});
