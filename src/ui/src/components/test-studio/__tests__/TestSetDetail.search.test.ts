// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The set detail table's search covers the whole set, not the page (GitHub #1323).
 *
 * This table is the one list in Test Studio that paginates on the *server*: it asks
 * for 50 rows and an opaque token, and replaces its rows on every page change, so the
 * browser holds one page and nothing else. Its search box used to filter those rows,
 * which answers a question about 50 documents in the voice of a question about the
 * set — a document two pages along came back as "No documents matching". The sibling
 * lists (`TestResultsList`, `TestResults`, `AddProcessedDocumentsModal`) are not
 * affected because `useCollection` filters the whole fetched array before paginating
 * it; that ordering is what is reproduced here, with the filtering moved to the only
 * place that can see the whole set.
 *
 * ⚠️ The direction that matters is the *absence* of the client-side filter. Adding a
 * server `search` argument while leaving the local `.filter` in place passes every
 * positive assertion and still narrows the result to the page, because the page is
 * then filtered twice. So the negative assertions below are the load-bearing ones.
 *
 * Asserted at source level, following the sibling tests in this directory — the page
 * needs the GraphQL client, settings, role and generator hooks to render. What the
 * server does with `search` is pinned in
 * `lib/idp_common_pkg/tests/unit/test_test_set_resolver.py`
 * (`TestDocumentSearchCoversTheWholeSet`), against real S3.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const HERE = join(__dirname, '..');
const ROOT = join(HERE, '..', '..', '..', '..', '..');
const DETAIL = readFileSync(join(HERE, 'TestSetDetail.tsx'), 'utf-8');
const SCHEMA = readFileSync(join(ROOT, 'nested', 'api-resolvers', 'src', 'api', 'schema.graphql'), 'utf-8');
const OPERATION = readFileSync(join(ROOT, 'src', 'ui', 'src', 'graphql', 'operations', 'queries', 'GetTestSetDocuments.graphql'), 'utf-8');

describe('searching a test set from its detail page', () => {
  it('sends the search to the server instead of filtering the rows it holds', () => {
    expect(DETAIL).toMatch(/search: searchText \|\| undefined/);
  });

  /**
   * The regression itself. A `.filter` over `documents` re-narrows a page the server
   * already searched, and on a set larger than one page that is indistinguishable
   * from the bug: both report "no match" for a document that is in the set.
   */
  it('does not filter the page client-side', () => {
    expect(DETAIL).not.toMatch(/documents\.filter\([^)]*objectKey/);
    expect(DETAIL).not.toMatch(/filterText\s*\?\s*documents\.filter/);
  });

  /**
   * The placeholder is the only promise the control makes before it is used, and it
   * said "on this page" — accurate for the old behaviour, so it has to move with it.
   * The empty state is the other half: it is what a user reads when the search is the
   * thing that failed them.
   */
  it('says the search covers the set, in the box and in the empty state', () => {
    expect(DETAIL).toMatch(/filteringPlaceholder="Find documents in this set"/);
    expect(DETAIL).not.toMatch(/Find documents on this page/);
    expect(DETAIL).toMatch(/No document in this test set matches/);
  });

  /**
   * A count beside a search box reads as "this is everything that matches", so under
   * a search it has to be the set's match count and not the row count. The server is
   * the only source for it.
   */
  it('counts matches across the set rather than the rows on screen', () => {
    expect(OPERATION).toMatch(/matchCount/);
    expect(OPERATION).toMatch(/searchTruncated/);
    expect(DETAIL).toMatch(/setMatchCount\(page\?\.matchCount \?\? null\)/);
    expect(DETAIL).toMatch(/matching\)/);
  });

  /**
   * A truncated scan makes `matchCount` a floor. Rendering it as a total would state
   * a number the server explicitly declined to claim, so the '+' and the note that
   * explains it are both pinned.
   */
  it('marks a truncated search as a lower bound', () => {
    expect(DETAIL).toMatch(/searchTruncated \? '\+' : ''/);
    expect(DETAIL).toMatch(/too large to search in full/);
  });

  /**
   * A new search cannot keep the old one's page tokens: under a search a token is an
   * offset into *that* search's matches, so replaying one would page into a match
   * list that no longer exists. `fetchPage` closing over `searchText` is what resets
   * them, via the effect's identity.
   */
  it('restarts paging when the search changes', () => {
    expect(DETAIL).toMatch(/\[testSetId, searchText\]/);
    expect(DETAIL).toMatch(/setTimeout\(\(\) => setSearchText\(filterText\.trim\(\)\), SEARCH_DEBOUNCE_MS\)/);
  });

  /**
   * The sorting note sat in the same sentence as a filtering claim that is no longer
   * true. Left alone it would keep telling the user the search was page-scoped,
   * which is the bug restated as copy.
   */
  it('no longer claims filtering is page-scoped', () => {
    expect(DETAIL).not.toMatch(/Sorting and filtering order this page/);
    expect(DETAIL).toMatch(/Sorting orders this page/);
  });

  /**
   * The guard's own behaviour is tested in `latestResponseOnly.test.ts`, with
   * replies staged out of order. What that cannot check is whether this page
   * consults it — a control that exists and is never called is indistinguishable
   * from no control, and reads as protection in review. So: claimed before the
   * await, and checked before anything is rendered.
   */
  it('renders only the newest reply, and consults the guard to decide', () => {
    expect(DETAIL).toMatch(/createLatestResponseGuard\(\)/);
    const fetchStart = DETAIL.indexOf('const fetchPage = useCallback(');
    const fetchBody = DETAIL.slice(fetchStart, DETAIL.indexOf('const fetchPageRef', fetchStart));
    expect(fetchBody).toMatch(/query: getTestSetDocuments/); // the slice really is the fetch
    // Claim precedes the await: ordering has to be decided by when each request
    // started, not by when it finished.
    expect(fetchBody.indexOf('responseGuard.claim()')).toBeLessThan(fetchBody.indexOf('await client.graphql'));
    // And the reply is gated before any of it reaches state.
    expect(fetchBody.indexOf('if (!isCurrentFetch()) return;')).toBeLessThan(fetchBody.indexOf('setDocuments('));
  });

  /**
   * The same defect by a second route. The arrival and generation polls build a
   * `setInterval` once, so a captured `fetchPage` would keep using the search in
   * force when the interval started — a search typed during an add was discarded
   * by the poll that followed it.
   */
  it('refetches through the current fetchPage from the polls, not a captured one', () => {
    expect(DETAIL).toMatch(/fetchPageRef\.current\(1, \[null\]\)/);
    // Inside the two setInterval bodies there must be no direct call.
    const intervals = DETAIL.split('setInterval(').slice(1);
    expect(intervals.length).toBeGreaterThan(0);
    for (const body of intervals) {
      const upToEnd = body.slice(0, body.indexOf('}, '));
      expect(upToEnd).not.toMatch(/[^.]\bfetchPage\(1, \[null\]\)/);
    }
  });

  it('declares the argument and the two fields on the schema', () => {
    expect(SCHEMA).toMatch(/getTestSetDocuments\(testSetId: String!.*search: String\)/);
    expect(SCHEMA).toMatch(/^\s*matchCount: Int$/m);
    expect(SCHEMA).toMatch(/^\s*searchTruncated: Boolean$/m);
  });
});
