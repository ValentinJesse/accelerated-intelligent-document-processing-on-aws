// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Moving to the next document after a save.
 *
 * Reported as [#1321] — "after clicking 'Save and next Document in queue' the
 * document status doesn't update". Measured on a live stack, the status was
 * recorded correctly every time: each save appeared in the very next
 * `getAnnotationQueue` reply (2000 → 1999 → 1998 → 1997 → 1996 remaining, each
 * immediately after the corresponding baseline write). What was wrong was the
 * wait. `advanceToNext` awaited a full queue reload before changing the
 * selection, and that call measured 8.6–9.6s on a 2000-document set, because
 * `getAnnotationQueue` lists the whole `baseline/` prefix and then reads every
 * section of all 200 ranked documents from S3. So the editor sat on the document
 * it had just saved for the better part of ten seconds, which reads as a save
 * that did nothing.
 *
 * The queue in hand already names the next document, so the advance no longer
 * waits for anything. The two functions below are what it advances with, and the
 * source-level checks pin the two ways the fix can be undone — re-awaiting the
 * reload, and handing Cloudscape's click event to a function whose first
 * parameter is now a document key.
 *
 * Asserted at source level rather than by rendering, for the reason given in
 * AnnotationWorkspace.editGating.test.ts: the component needs the GraphQL
 * client, settings, role hooks, a router and a populated queue.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { applyReviewedToQueue, nextAvailableAfter, type QueueItem, type QueueState } from '../AnnotationWorkspace';

const SOURCE = readFileSync(join(__dirname, '..', 'AnnotationWorkspace.tsx'), 'utf-8');

const doc = (objectKey: string, overrides: Partial<QueueItem> = {}): QueueItem => ({
  objectKey,
  inputKey: `set/input/${objectKey}`,
  sectionCount: 1,
  claimedByMe: false,
  reviewed: false,
  available: true,
  labelSource: 'draft-machine',
  ...overrides,
});

const queueOf = (documents: QueueItem[], overrides: Partial<QueueState> = {}): QueueState => ({
  totalDocs: documents.length,
  reviewedDocs: 0,
  remainingDocs: documents.length,
  claimedByOthers: 0,
  documents,
  ...overrides,
});

describe('nextAvailableAfter', () => {
  it('does not reopen the document just finished, even though it is still first', () => {
    // The queue is ordered worst-first and the reply has not been refreshed, so
    // the document just saved is still at the head of the list. Taking the head
    // would hand the reviewer back the document they had finished.
    const documents = [doc('worst.pdf'), doc('next.pdf')];
    expect(nextAvailableAfter(documents, 'worst.pdf')).toBe('next.pdf');
  });

  it('skips documents the server says are not available', () => {
    // Reviewed, or claimed by another annotator: `available` already carries both.
    const documents = [doc('taken.pdf', { available: false }), doc('done.pdf', { available: false, reviewed: true }), doc('free.pdf')];
    expect(nextAvailableAfter(documents, null)).toBe('free.pdf');
  });

  it('reports no next document rather than falling back to an unavailable one', () => {
    // Null closes the editor pane, which is honest. Returning a claimed document
    // would open one whose save the server refuses.
    expect(nextAvailableAfter([doc('taken.pdf', { available: false })], null)).toBeNull();
    expect(nextAvailableAfter([], null)).toBeNull();
    expect(nextAvailableAfter([doc('only.pdf')], 'only.pdf')).toBeNull();
  });
});

describe('applyReviewedToQueue', () => {
  it('retires the reviewed document from the rail and moves the counters', () => {
    const before = queueOf([doc('done.pdf'), doc('todo.pdf')]);
    const after = applyReviewedToQueue(before, 'done.pdf', false);

    expect(after.documents.map((d) => d.objectKey)).toEqual(['todo.pdf']);
    expect(after.reviewedDocs).toBe(1);
    expect(after.remainingDocs).toBe(1);
  });

  it('ticks it in place instead of hiding it when reviewed documents are shown', () => {
    // Mirrors the server's own filter: it omits reviewed documents unless
    // includeCompleted was asked for, so the rail must behave the same whether
    // this update or the reply produced the rows.
    const after = applyReviewedToQueue(queueOf([doc('done.pdf')]), 'done.pdf', true);

    expect(after.documents).toHaveLength(1);
    expect(after.documents[0]).toMatchObject({ reviewed: true, available: false, labelSource: 'reviewed-human' });
    expect(after.reviewedDocs).toBe(1);
  });

  it('leaves the queue alone for a document it does not hold', () => {
    // The rail is capped at one page, so a document reviewed from a shared ?doc=
    // link need not be on it. Counting it here would report progress against rows
    // this queue cannot show.
    const before = queueOf([doc('todo.pdf')]);
    expect(applyReviewedToQueue(before, 'elsewhere.pdf', false)).toBe(before);
  });

  it('does not move the counters twice when an already-reviewed document is saved again', () => {
    // Reopening a confirmed label and saving it is supported, and it is not new
    // progress.
    const before = queueOf([doc('done.pdf', { reviewed: true, available: false, labelSource: 'reviewed-human' })], {
      reviewedDocs: 1,
      remainingDocs: 0,
    });
    expect(applyReviewedToQueue(before, 'done.pdf', true)).toBe(before);
  });

  it('floors remaining at zero rather than reporting a negative count', () => {
    // totalDocs counts the whole set while the rail is one page, so the two can
    // disagree about how much is left.
    const after = applyReviewedToQueue(queueOf([doc('last.pdf')], { remainingDocs: 0 }), 'last.pdf', false);
    expect(after.remainingDocs).toBe(0);
  });
});

describe('the advance does not wait for the queue reload', () => {
  const body = SOURCE.slice(
    SOURCE.indexOf('const advanceToNext = useCallback('),
    SOURCE.indexOf('[loadQueue, loadImpact, selectedKey, showReviewed]'),
  );

  it('is positioned in the source where this test expects to find it', () => {
    // Guards the two assertions below from silently passing on an empty slice.
    expect(body).toContain('setSelectedKey(nextAvailableAfter(');
  });

  it('refreshes in the background instead of awaiting it', () => {
    // `loadQueue(true, true)` — preserve the selection this function just made,
    // and do not touch isLoading. An `await` here is the #1321 regression: the
    // selection would once again change only after a ~9s round trip.
    expect(body).toContain('loadQueue(true, true)');
    expect(body).not.toContain('await loadQueue');
  });

  it('keeps the click event out of the reviewed-document parameter', () => {
    // `onClick={advanceToNext}` compiles and is wrong: Cloudscape passes a
    // CustomEvent, which would arrive as `reviewedKey` and retire the document
    // being skipped as though it had been reviewed.
    expect(SOURCE).toContain('onClick={() => advanceToNext()}');
    expect(SOURCE).not.toMatch(/onClick=\{advanceToNext\}/);
  });
});
