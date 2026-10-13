// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Pricing lookup for the document cost table.
 *
 * This is a direct port of the backend's `_get_unit_cost`
 * (`lib/idp_common_pkg/idp_common/reporting/save_reporting_data.py`) and is
 * meant to stay in lock-step with it: the cost this panel shows and the cost
 * written to the `metering` reporting table are not allowed to disagree. It
 * lives in its own module, rather than inside DocumentPanel.tsx, so it can be
 * unit-tested without mounting the component.
 *
 * ⚠️ KNOWN DIVERGENCE, and it is in the direction that understates. The backend
 * no longer treats every unit a matched entry omits as free. It returns a NULL
 * cost unless a declaration in `idp_common.metering_units` says the unit costs
 * nothing — at service scope for units that are not a billing dimension of the
 * service at all, and per pricing key for the two prompt-cache units, since
 * whether a cache read is chargeable is a property of the model (GitHub issues
 * #1212 and #1206). Those declarations are Python data this module has no copy
 * of, so an omitted unit still reads as `0` here.
 *
 * What that means concretely: for a Bedrock row whose entry omits a cache rate
 * on a model AWS does publish one for, the reporting table records NULL with a
 * warning and this panel shows $0.00 with no mark. The rendering path for the
 * unpriced case already exists — `metering-cost.ts` uses it for an entry that
 * matches nothing — so closing this means getting the declarations in front of
 * the browser, not new UI. Until then, read a $0.00 unit cost here as "free or
 * unpriced" and the `metering` table as the authority on which.
 */

export interface PricingLookup {
  [serviceName: string]: {
    [unitName: string]: number;
  };
}

/**
 * Resolve the unit price for a `serviceApi`/`unit` pair.
 *
 * Resolution is by EXACT pricing key: `serviceApi` is tried first, then
 * progressively shorter `/`-delimited suffixes of it, and the longest match
 * wins. Within a matched entry the unit name must match exactly.
 *
 * Returns:
 * - the price, when the matched entry lists the unit;
 * - `0`, when an entry matches but does not list the unit. For `totalTokens` and
 *   `requests` on Bedrock that is correct and is what the backend answers too —
 *   both are metered on every call and neither is chargeable. For anything else
 *   it is the divergence described above, not a verified zero;
 * - `null`, when no entry matches at all, meaning genuinely unpriced. A related
 *   entry's price is never substituted.
 *
 * There is deliberately NO substring matching. This function used to match
 * case-insensitively and bidirectionally on both the service key and the unit
 * name, so `cacheReadInputTokens` bound to the matched entry's `inputTokens`
 * price — the first unit every Bedrock row lists. Cache reads cost about a tenth
 * of fresh input, so they were displayed at up to ~8x their real rate, while
 * `cacheWriteInputTokens` was understated by ~20%. See GitHub issue #926 and
 * PR #952.
 *
 * Lambda-hook rows resolve because the metering key carries the bare function
 * name (`lambda_hook/GENAIIDP-mistral-ocr-hook`) rather than the configured ARN.
 * An ARN delimits the function name with `:`, which this walk cannot split, so
 * an ARN-keyed metering row would be unpriceable. See
 * `bedrock.client.lambda_hook_metering_name`.
 */
export const lookupUnitPrice = (pricingData: PricingLookup, serviceApi: string, unit: string): number | null => {
  const parts = serviceApi.split('/');
  for (let start = 0; start < parts.length; start += 1) {
    const candidate = parts.slice(start).join('/');
    const serviceCosts = pricingData[candidate];
    if (serviceCosts !== undefined && serviceCosts !== null) {
      const price = serviceCosts[unit];
      return price !== undefined ? Number(price) : 0;
    }
  }

  return null;
};
