// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which fields can be shown on the page, and why the rest cannot.
 *
 * Requested as [#1327]: the viewer offered a locate control on some rows and
 * nothing on the others, with no statement of the rule and no count, so a reader
 * could not tell whether the missing boxes were a limitation of their
 * configuration or a fault.
 *
 * The shape here is the one a real run produces. In a ten-field form section from
 * a packet processed under the default `ocr_only` mode, six fields carried a value
 * and a box and four carried neither — so "has a box" and "has a value" coincided
 * exactly. The fixture reproduces that split rather than the document, whose text
 * does not belong in this repository.
 */

import { describe, expect, it } from 'vitest';

import { classifyFieldGeometry, describeFieldGeometry, isAbsentValue, summariseGeometryCoverage } from '../geometry-coverage';

/** The geometry shape the viewer can draw: a box and the page it is on. */
const box = () => [{ boundingBox: { left: 0.1, top: 0.2, width: 0.3, height: 0.04 }, page: 1 }];

/** A located field: confidence and a geometry entry. */
const located = (confidence = 0.9) => ({
  confidence,
  geometry: [{ boundingBox: { left: 0.1, top: 0.2, width: 0.3, height: 0.04 }, page: 1 }],
});

/** A reported field with no box. */
const unlocated = (confidence = 0.9) => ({ confidence });

describe('isAbsentValue', () => {
  it('treats the empty forms as absent', () => {
    expect(isAbsentValue(null)).toBe(true);
    expect(isAbsentValue(undefined)).toBe(true);
    expect(isAbsentValue('')).toBe(true);
    expect(isAbsentValue('   ')).toBe(true);
    expect(isAbsentValue([])).toBe(true);
    expect(isAbsentValue({})).toBe(true);
  });

  it("treats the pipeline's stringified None as absent", () => {
    // The pipeline emits Python's str(None) rather than JSON null, so the field
    // arrives as this four-character string and renders as text. A reader seeing
    // "None" with no box needs to be told the model returned nothing, not that
    // the value could not be found on the page.
    expect(isAbsentValue('None')).toBe(true);
    expect(isAbsentValue('none')).toBe(true);
    expect(isAbsentValue('null')).toBe(true);
  });

  it('does not guess at sentinels with no producer', () => {
    // A single hyphen is an ordinary extracted cell value and "NA" is a real
    // value in several document vocabularies. Treating them as absent would make
    // the row claim nothing was extracted while displaying the value that was.
    expect(isAbsentValue('-')).toBe(false);
    expect(isAbsentValue('--')).toBe(false);
    expect(isAbsentValue('NA')).toBe(false);
    expect(isAbsentValue('N/A')).toBe(false);
  });

  it('does not treat a real value as absent, including falsy ones', () => {
    // Zero and false are extracted values. Reporting them as "nothing was
    // extracted" would explain away a box that is genuinely missing.
    expect(isAbsentValue(0)).toBe(false);
    expect(isAbsentValue(false)).toBe(false);
    expect(isAbsentValue('0')).toBe(false);
    expect(isAbsentValue('887')).toBe(false);
    expect(isAbsentValue('Above identified via arrest report.')).toBe(false);
  });

  it('does not treat a value that merely contains a sentinel as absent', () => {
    // The list is matched whole, so a sentence mentioning one is still a value.
    expect(isAbsentValue('None of the above')).toBe(false);
    expect(isAbsentValue('NA 12345')).toBe(false);
  });
});

describe('classifyFieldGeometry', () => {
  it('reports a field with a box as located, whatever its value', () => {
    expect(classifyFieldGeometry('887', true)).toBe('located');
    // Geometry wins: if the viewer has a box it can show it, and arguing with
    // that from the value would hide a working control.
    expect(classifyFieldGeometry('None', true)).toBe('located');
  });

  it('separates "nothing was extracted" from "could not be found"', () => {
    // The two have different remedies — one is an extraction result to look at,
    // the other is a value to check against the document — so they must not
    // share a message.
    expect(classifyFieldGeometry('None', false)).toBe('no-value');
    expect(classifyFieldGeometry('', false)).toBe('no-value');
    expect(classifyFieldGeometry('24 AUG 11', false)).toBe('unmatched');
  });
});

describe('classifyFieldGeometry when the document has no geometry at all', () => {
  it('blames the document rather than the field', () => {
    // With geometry.mode: off, or on labels predating geometry, no row can be
    // located and the cause is identical on every one. Reporting each field's own
    // value would send the reader checking values against the page for a
    // configuration condition no checking would reveal.
    expect(classifyFieldGeometry('24 AUG 11', false, false)).toBe('none-produced');
    expect(classifyFieldGeometry('', false, false)).toBe('none-produced');
  });

  it('still reports a field that does have a box', () => {
    // Belt and braces: a located field is located whatever the document-level
    // flag says, so a wrong flag cannot hide a working control.
    expect(classifyFieldGeometry('x', true, false)).toBe('located');
  });
});

describe('describeFieldGeometry', () => {
  it('names the cause rather than restating the absence', () => {
    expect(describeFieldGeometry('no-value')).toMatch(/No value was extracted/);
    expect(describeFieldGeometry('unmatched')).toMatch(/could not be matched to anything on the page/);
    expect(describeFieldGeometry('none-produced')).toMatch(/No bounding boxes were produced for this document/);
  });

  it('gives every state a sentence', () => {
    // A state with no wording renders an empty tooltip, which reads as a bug in
    // the viewer rather than an explanation.
    (['located', 'no-value', 'unmatched', 'none-produced'] as const).forEach((state) => {
      expect(describeFieldGeometry(state).length).toBeGreaterThan(10);
    });
  });
});

describe('summariseGeometryCoverage', () => {
  it('counts the real six-of-ten split', () => {
    const explainability = [
      {
        FormId: located(),
        FormType: located(),
        SubmitterName: unlocated(),
        SubmitterId: located(),
        SubmissionDate: located(),
        Department: located(),
        ProcessedBy: unlocated(),
        ProcessingDate: unlocated(),
        ApprovalStatus: unlocated(),
        Comments: located(),
      },
    ];
    expect(summariseGeometryCoverage(explainability)).toEqual({ located: 6, total: 10 });
  });

  it('walks nested objects and arrays so a packet is not counted as one field', () => {
    const explainability = [
      {
        Payer: { Name: located(), Address: unlocated() },
        LineItems: [{ Amount: located() }, { Amount: located() }],
        Total: located(),
      },
    ];
    expect(summariseGeometryCoverage(explainability)).toEqual({ located: 4, total: 5 });
  });

  it('counts only geometry the viewer can actually draw', () => {
    // One definition of "has a box", shared with the per-field control: the
    // renderer builds a highlight only from an entry carrying both boundingBox
    // and page. Counting an entry missing either would report a field as
    // locatable whose row offers no way to locate it.
    expect(summariseGeometryCoverage([{ Field: { confidence: 0.5, geometry: [] } }])).toEqual({ located: 0, total: 1 });
    expect(summariseGeometryCoverage([{ Field: { confidence: 0.5, geometry: [{ page: 1 }] } }])).toEqual({ located: 0, total: 1 });
    expect(summariseGeometryCoverage([{ Field: { confidence: 0.5, geometry: [{ boundingBox: { left: 0 } }] } }])).toEqual({
      located: 0,
      total: 1,
    });
  });

  it('reports nothing rather than zero-of-zero when there is no explainability at all', () => {
    // Distinguishable by the caller: total 0 means "no per-field data", which is
    // a different statement from "no field could be located".
    expect(summariseGeometryCoverage(undefined)).toEqual({ located: 0, total: 0 });
    expect(summariseGeometryCoverage([])).toEqual({ located: 0, total: 0 });
    expect(summariseGeometryCoverage([{}])).toEqual({ located: 0, total: 0 });
  });

  it('does not mistake a confidence-bearing leaf for a container', () => {
    // A leaf carrying both `confidence` and nested-looking keys is still one
    // field; recursing into it would double-count.
    expect(summariseGeometryCoverage([{ Field: { confidence: 0.9, geometry: box(), extra: { confidence: 0.1 } } }])).toEqual({
      located: 1,
      total: 1,
    });
  });
});
