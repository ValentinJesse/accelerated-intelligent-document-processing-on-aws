// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * What a rendered section says about being shown on the page.
 *
 * Requested as [#1327]. The locate control already existed and worked — measured
 * against a real run, six fields carrying geometry produced six controls — so the
 * gap was never the indicator. It was that the other four rows ended in nothing:
 * no count to set expectations, and no distinction between "the model extracted
 * nothing here" and "the value could not be found on the page", which have
 * different remedies.
 *
 * The fixture reproduces the **shape** that run produced — ten fields, six of them
 * located, the other four carrying no value — rather than the document, whose text
 * is not this repository's to publish.
 *
 * The claim worth pinning is the agreement: the count in the caption and the number
 * of locate controls are derived from the same structure, so they cannot disagree
 * on screen. A reader who is told "6 of 10" and can find seven magnifiers has been
 * given two facts and no way to choose between them.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import FormFieldRenderer from '../FormFieldRenderer';

const box = (page = 1) => [{ boundingBox: { left: 0.1, top: 0.2, width: 0.3, height: 0.04 }, page }];

/** Ten fields: six with a value and a box, four with neither. */
const INFERENCE_RESULT = {
  FormId: '1046835',
  FormType: 'NOTIFICATION',
  SubmitterName: 'None',
  SubmitterId: '887',
  SubmissionDate: '24 AUG 11',
  Department: '006',
  ProcessedBy: 'None',
  ProcessingDate: 'None',
  ApprovalStatus: 'None',
  Comments: 'Referred for review.',
};

const EXPLAINABILITY = [
  {
    FormId: { confidence: 0.98, geometry: box() },
    FormType: { confidence: 0.95, geometry: box() },
    SubmitterName: { confidence: 0.4 },
    SubmitterId: { confidence: 0.92, geometry: box() },
    SubmissionDate: { confidence: 0.9, geometry: box() },
    Department: { confidence: 0.88, geometry: box() },
    ProcessedBy: { confidence: 0.4 },
    ProcessingDate: { confidence: 0.4 },
    ApprovalStatus: { confidence: 0.4 },
    Comments: { confidence: 0.81, geometry: box() },
  },
];

const renderSection = (overrides: Record<string, unknown> = {}) =>
  render(
    <FormFieldRenderer
      fieldKey="Document Data"
      value={INFERENCE_RESULT}
      onChange={vi.fn()}
      isReadOnly={false}
      onFieldFocus={vi.fn()}
      path={[]}
      displayPath={[]}
      explainabilityInfo={EXPLAINABILITY}
      {...overrides}
    />,
  );

const locateControls = () => screen.queryAllByRole('button', { name: /Show .* on the page/i });
const reasonControls = () => screen.queryAllByRole('button', { name: /Why .* cannot be shown on the page/i });

describe('a section that can only partly be shown on the page', () => {
  it('offers a locate control for exactly the fields that have a box', () => {
    renderSection();

    expect(locateControls().map((b) => b.getAttribute('aria-label'))).toEqual([
      'Show FormId on the page',
      'Show FormType on the page',
      'Show SubmitterId on the page',
      'Show SubmissionDate on the page',
      'Show Department on the page',
      'Show Comments on the page',
    ]);
  });

  it('leaves no row with an empty slot, so an unlocatable field is not mistaken for a broken viewer', () => {
    renderSection();

    expect(reasonControls()).toHaveLength(4);
    expect(locateControls().length + reasonControls().length).toBe(Object.keys(INFERENCE_RESULT).length);
  });

  it('states the count, and the count matches the controls', () => {
    renderSection();

    // The agreement is the point: both come from explainability_info, so there is
    // one source of truth rather than a caption and a renderer that can drift.
    expect(screen.getByText(/6 of 10 fields can be shown on the page/)).toBeInTheDocument();
    expect(locateControls()).toHaveLength(6);
  });

  it('says the model extracted nothing, rather than that the value was not found', () => {
    renderSection();

    // The four here all carry the pipeline's stringified None. Telling the reader
    // to go looking for the value on the page would send them after something
    // that was never extracted.
    const reason = reasonControls()[0];
    expect(reason.getAttribute('aria-label')).toBe('Why SubmitterName cannot be shown on the page');
  });

  it('omits the count where there is no per-field data to count', () => {
    // "0 of 0" would assert a coverage figure for a section that was never
    // assessed, which is a different statement from one whose fields could not be
    // located.
    renderSection({ explainabilityInfo: null });

    expect(screen.queryByText(/can be shown on the page/)).not.toBeInTheDocument();
  });

  it('does not caption a nested group, which would read as the whole section', () => {
    // The caption is rendered at the root only. On a sub-object it would describe
    // the section while sitting under a group heading.
    renderSection({ path: ['Payer'], displayPath: ['Payer'] });

    expect(screen.queryByText(/can be shown on the page/)).not.toBeInTheDocument();
  });
});
