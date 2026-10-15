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
  /** Show the field on the page. Only called in the located state. */
  onLocate: () => void;
}

const FieldGeometryAffordance = ({ fieldKey, value, geometry, onLocate }: FieldGeometryAffordanceProps): React.JSX.Element => {
  const state = classifyFieldGeometry(value, Boolean(geometry));

  if (state === 'located') {
    return (
      <Button
        variant="inline-icon"
        iconName="search"
        ariaLabel={`Show ${fieldKey} on the page`}
        onClick={(e) => {
          // Stops the row's own click handler running a second locate.
          e.stopPropagation();
          onLocate();
        }}
      />
    );
  }

  return (
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
      <Button
        variant="inline-icon"
        iconName="status-info"
        ariaLabel={`Why ${fieldKey} cannot be shown on the page`}
        onClick={(e) => e.stopPropagation()}
      />
    </Popover>
  );
};

export default FieldGeometryAffordance;
