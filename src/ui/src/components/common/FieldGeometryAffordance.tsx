// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The per-field answer to "can this be shown on the page, and if not why".
 *
 * One component for all four of FormFieldRenderer's value branches (string,
 * number, boolean, array item), which carried four byte-identical copies of the
 * locate button. The duplication is why the absent case went unanswered for so
 * long: adding an explanation meant adding it four times, and a change to one
 * copy was invisible against the others.
 *
 * Both halves are **real controls rather than hover affordances**, for the reason
 * the locate button already was: a click target with no affordance is invisible to
 * sighted users and unreachable with a keyboard. So an unlocatable field costs one
 * tab stop and says why, instead of being silently inert.
 */

import React from 'react';
import { Button, Popover, Box } from '@cloudscape-design/components';

import { classifyFieldGeometry, describeFieldGeometry } from './geometry-coverage';

interface FieldGeometryAffordanceProps {
  /** Label the control names, so the announcement identifies the row. */
  fieldKey: string;
  /** The extracted value, which decides between the two reasons for having no box. */
  value: unknown;
  /** The box this row was given, if any. Passed rather than re-derived. */
  geometry?: unknown;
  /**
   * Whether anything in this document carries a box.
   *
   * False makes every field's own value irrelevant: with `geometry.mode: off`, or
   * on labels that predate geometry, no row can be located and the cause is the
   * same on all of them. Without this the reader is told on every row to check a
   * value against the page, for a configuration condition no amount of checking
   * would reveal.
   */
  geometryAvailable?: boolean;
  /**
   * Whether `explainability_info` holds an entry for this field at all.
   *
   * False means neither a confidence nor a box was recorded for it, which is a
   * different statement from a box that could not be found — and sending the
   * reader to check the value against the page could not reveal that cause.
   */
  hasFieldData?: boolean;
  /** Show the field on the page. Only called in the located state. */
  onLocate: () => void;
}

const FieldGeometryAffordance = ({
  fieldKey,
  value,
  geometry,
  geometryAvailable = true,
  hasFieldData = true,
  onLocate,
}: FieldGeometryAffordanceProps): React.JSX.Element => {
  const state = classifyFieldGeometry(value, Boolean(geometry), geometryAvailable, hasFieldData);

  if (state === 'located') {
    return (
      <Button
        variant="inline-icon"
        iconName="search"
        ariaLabel={`Highlight ${fieldKey} on the page image`}
        onClick={(e) => {
          // Stops the row's own click handler running a second locate.
          e.stopPropagation();
          onLocate();
        }}
      />
    );
  }

  return (
    /* ⚠️ The row's own click handler is stopped OUTSIDE the Popover, never on the
       trigger button.
       FormFieldRenderer puts `onClick={handleClick}` on the containing element, so
       a click here would otherwise also locate the field — but Cloudscape opens a
       `triggerType="custom"` popover from a handler on the wrapper it renders
       around this child, and its cancelable events forward `stopPropagation()` to
       the underlying React event. Calling it on the button therefore stops the
       click before the popover's own trigger ever sees it, which leaves a
       focusable, correctly-labelled control that does nothing at all — on click
       and on Enter/Space alike. Measured, not reasoned: the probe asserting the
       reason text is visible after a click is what distinguishes the two, and an
       assertion on the label cannot, since the label is the same either way. */

    <span role="presentation" onClick={(e) => e.stopPropagation()}>
      <Popover
        dismissButton={false}
        position="top"
        size="small"
        triggerType="custom"
        content={<Box variant="span">{describeFieldGeometry(state)}</Box>}
      >
        {/* `status-info`, not a warning or an error: neither state is a fault. A
            field the model found nothing for is an ordinary outcome, and one whose
            value is not on the page may simply have been inferred from elsewhere in
            the document. */}
        <Button variant="inline-icon" iconName="status-info" ariaLabel={`Why ${fieldKey} has no bounding box`} />
      </Popover>
    </span>
  );
};

export default FieldGeometryAffordance;
