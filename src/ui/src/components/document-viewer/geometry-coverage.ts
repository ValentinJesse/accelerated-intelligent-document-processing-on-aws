// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Why a field can or cannot be shown on the page, and how much of a section can.
 *
 * A field is located by matching its extracted value to the document — under the
 * default `extraction.geometry.mode: ocr_only` by matching it to a line of OCR
 * text — so a field the model returned nothing for cannot be located at all.
 * On a real run that is the whole of the difference: of ten fields in one form
 * section, the six carrying a value each had a box and the four carrying none had
 * no box ([#1327]).
 *
 * That rule is obvious once stated and invisible until then: the viewer offered a
 * locate control on some rows and nothing on the others, leaving the reader to
 * infer the rule or conclude the boxes were unreliable.
 */

/** A field's relationship to the page, as the viewer can determine it. */
export type FieldGeometryState =
  /** Has a box: the locate control is offered. */
  | 'located'
  /** Nothing was extracted, so there is nothing to point at. */
  | 'no-value'
  /** A value, but it could not be tied to anything on the page. */
  | 'unmatched';

/**
 * Values that mean "the model returned nothing", beyond the empty ones.
 *
 * ⚠️ `'None'` is here because the pipeline emits Python's `str(None)` for an
 * absent value rather than JSON `null`, so the field arrives as the four-character
 * string and renders as text. Recognising it is a display decision only — it
 * changes which sentence this file offers and nothing about the data. The
 * underlying artifact is worth fixing at its source, since such a value copied
 * into a test-set baseline becomes literal ground truth and would be scored as the
 * string `"None"` against a correctly-empty field.
 *
 * Deliberately a short, explicit list rather than a pattern: a document genuinely
 * containing the word "None" in a field is possible, and the cost of being wrong
 * is one sentence reading "no value was extracted" where "not found on the page"
 * was meant. A regex over anything null-ish would make that misread common.
 */
const ABSENT_VALUE_SENTINELS = new Set(['none', 'null', 'n/a', 'na', '-', '--']);

/** True when the model returned nothing for this field. */
export const isAbsentValue = (value: unknown): boolean => {
  if (value === null || value === undefined) return true;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed === '' || ABSENT_VALUE_SENTINELS.has(trimmed.toLowerCase());
  }
  // An empty array or object is the structural equivalent: nothing was extracted.
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === 'object') return Object.keys(value as object).length === 0;
  return false;
};

/**
 * What the viewer can say about one field.
 *
 * `hasGeometry` is passed in rather than derived, so this answers for exactly the
 * geometry the row was given — the alternative is a second traversal of
 * `explainability_info` that can disagree with the one driving the control.
 */
export const classifyFieldGeometry = (value: unknown, hasGeometry: boolean): FieldGeometryState => {
  if (hasGeometry) return 'located';
  return isAbsentValue(value) ? 'no-value' : 'unmatched';
};

/** One sentence per state, for the row that carries no locate control. */
export const describeFieldGeometry = (state: FieldGeometryState): string => {
  switch (state) {
    case 'no-value':
      return 'No value was extracted for this field, so there is nothing to locate on the page.';
    case 'unmatched':
      return 'This value could not be matched to anything on the page, so it cannot be shown there. Check the extracted value against the document.';
    default:
      return 'Show this field on the page.';
  }
};

export interface GeometryCoverage {
  /** Fields carrying a bounding box. */
  located: number;
  /** Fields the extraction reported at all, box or no box. */
  total: number;
}

/**
 * How much of a section can be shown on the page.
 *
 * Counted from `explainability_info` rather than from the value tree, because that
 * is the structure the per-field controls are driven from — counting the values
 * instead would mean a second way of deciding what a field is, and two counts that
 * can disagree on screen. A leaf is a node carrying `confidence`: that is what the
 * extraction writes per field, and it is present whether or not geometry is.
 *
 * Nested objects and arrays are walked, so a packet's sub-objects count once each
 * rather than as one field apiece.
 */
export const summariseGeometryCoverage = (explainabilityInfo: unknown): GeometryCoverage => {
  let located = 0;
  let total = 0;

  const walk = (node: unknown): void => {
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    if (!node || typeof node !== 'object') return;
    const record = node as Record<string, unknown>;
    if ('confidence' in record) {
      total += 1;
      const geometry = record.geometry;
      if (Array.isArray(geometry) ? geometry.length > 0 : Boolean(geometry)) located += 1;
      return;
    }
    Object.values(record).forEach(walk);
  };

  walk(explainabilityInfo);
  return { located, total };
};
