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
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import FormFieldRenderer from '../../document-viewer/FormFieldRenderer';

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
    expect(screen.getByText(/^6 of 10 fields can be shown on the page$/)).toBeInTheDocument();
    expect(locateControls()).toHaveLength(6);
  });

  it('says the model extracted nothing, rather than that the value was not found', async () => {
    renderSection();

    // Clicked, not inspected. The label is identical in both states — "Why
    // <field> cannot be shown on the page" — so an assertion on it cannot see
    // which reason is given, and cannot see a control that opens nothing at all.
    // The four fields here carry the pipeline's stringified None, and telling the
    // reader to go looking on the page would send them after a value that was
    // never extracted.
    await userEvent.click(screen.getByRole('button', { name: /Why SubmitterName cannot be shown on the page/i }));

    expect(screen.getByText(/No value was extracted for this field/i)).toBeInTheDocument();
  });

  it('distinguishes a value that could not be found from one that was never extracted', async () => {
    // The other reason, which has a different remedy: there IS a value, so the
    // thing to check is the value against the document. Reached by giving a field
    // a value and no geometry.
    renderSection({
      value: { ...INFERENCE_RESULT, SubmitterName: '24 AUG 11' },
    });

    await userEvent.click(screen.getByRole('button', { name: /Why SubmitterName cannot be shown on the page/i }));

    expect(screen.getByText(/could not be matched to anything on the page/i)).toBeInTheDocument();
  });

  it('does not also locate the field when the explanation is opened', async () => {
    // The row itself carries a click handler, so the explanation has to stop the
    // click reaching it — while still reaching the popover. Getting that wrong in
    // either direction is silent: stop it too early and the control does nothing,
    // too late and asking why a field cannot be shown moves the page image.
    const onFieldFocus = vi.fn();
    renderSection({ onFieldFocus });

    await userEvent.click(screen.getByRole('button', { name: /Why SubmitterName cannot be shown on the page/i }));

    expect(screen.getByText(/No value was extracted for this field/i)).toBeInTheDocument();
    expect(onFieldFocus).not.toHaveBeenCalled();
  });

  it('states the count even when nothing was recorded, rather than going silent', () => {
    // The denominator is the fields displayed, which is known whatever the
    // assessment recorded — so "0 of 10" is both true and more use than no line
    // at all. Silence here is indistinguishable from a viewer that does not do
    // bounding boxes, which is the reading this caption exists to prevent.
    renderSection({ explainabilityInfo: null });

    expect(screen.getByText(/0 of 10 fields can be shown on the page/)).toBeInTheDocument();
  });

  it('omits the count when the section displays no fields at all', () => {
    // Nothing to describe. "0 of 0" would read as a finding about a section that
    // has no rows to find anything about.
    renderSection({ value: {} });

    expect(screen.queryByText(/can be shown on the page/)).not.toBeInTheDocument();
  });

  it('does not caption a nested group, which would read as the whole section', () => {
    // The caption is rendered at the root only. On a sub-object it would describe
    // the section while sitting under a group heading.
    renderSection({ path: ['Payer'], displayPath: ['Payer'] });

    expect(screen.queryByText(/can be shown on the page/)).not.toBeInTheDocument();
  });
});
