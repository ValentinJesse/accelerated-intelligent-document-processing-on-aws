# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Delete document resolver Lambda function.

This function handles document deletion via GraphQL API, using the shared
delete_documents implementation from idp_common for consistent behavior
across CLI, SDK, and Web UI.
"""

import json
import logging
import os
from typing import List

import boto3
from idp_common.delete_documents import delete_single_document
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")


def _caller_in_groups(event, allowed):
    """Defense-in-depth RBAC check against the caller's Cognito groups.

    The schema restricts this field via @aws_cognito_user_pools(cognito_groups),
    but we also enforce the group server-side so the operation is never reachable
    by an unauthorized caller even if the schema directive is missing or
    misconfigured (e.g. the prior @aws_auth directive, which AppSync silently
    ignores on a multi-auth API).
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(set(allowed).intersection(groups))


def handler(event, context):
    """Handle document deletion requests from AppSync."""
    logger.info(f"Delete document resolver invoked with event: {json.dumps(sanitize_event_for_logging(event))}")

    try:
        # Defense-in-depth: deleteDocument is an Admin+Author operation.
        if not _caller_in_groups(event, ("Admin", "Author")):
            raise PermissionError(
                "Unauthorized: deleteDocument requires Admin or Author group"
            )

        object_keys: List[str] = event["arguments"]["objectKeys"]

        # Validate input
        if not object_keys or not isinstance(object_keys, list):
            raise ValueError("objectKeys must be a non-empty list")

        tracking_table = dynamodb.Table(os.environ["TRACKING_TABLE_NAME"])
        input_bucket = os.environ["INPUT_BUCKET"]
        output_bucket = os.environ["OUTPUT_BUCKET"]

        logger.info(f"Preparing to delete {len(object_keys)} documents: {object_keys}")

        deleted_count = 0
        failures: List[str] = []

        # Delete each document using the shared idp_common implementation
        for object_key in object_keys:
            logger.info(f"Processing deletion for document: {object_key}")

            result = delete_single_document(
                object_key=object_key,
                tracking_table=tracking_table,
                s3_client=s3,
                input_bucket=input_bucket,
                output_bucket=output_bucket,
            )

            if result["success"]:
                deleted_count += 1
                logger.info(f"Successfully deleted document: {object_key}")
            else:
                errors = result.get("errors", [])
                failures.append(f"{object_key}: {'; '.join(str(e) for e in errors)}")
                logger.error(f"Failed to delete document {object_key}: {errors}")

        logger.info(
            f"Completed deletion: {deleted_count} successful, {len(failures)} failed"
        )

        # ⚠️ **A failed delete RAISES; it does not return a falsy value.** Returning
        # `failed_count == 0` was an HTTP 200 whose body was `false`, and nothing
        # between here and the user looked at it: the dispatcher returns a 200 for any
        # value a resolver yields, and the UI only logged the result at debug level. So
        # a delete that cleared a document's version rows, could not clear its list row
        # and kept the document record answered "OK" to the browser, with the two
        # underlying faults reaching only the resolver's own log (#1366). A raise is the
        # only outcome the transport distinguishes — the dispatcher maps it to a non-200
        # carrying this message, which the UI surfaces.
        #
        # The loop above still attempts every document before this point, so one
        # document's failure does not abandon the rest of the selection, and the message
        # names each one that failed and why rather than a bare count: the cause is
        # usually a deployment-level fault (a missing permission, a rejected
        # expression) that is identical for every document and actionable only if it is
        # said out loud. The operation is Admin/Author-gated.
        if failures:
            raise RuntimeError(
                f"Deleted {deleted_count} of {len(object_keys)} document(s); "
                f"{len(failures)} failed — " + " | ".join(failures)
            )
        return True

    except Exception as e:
        logger.error(f"Error in delete_document resolver: {str(e)}", exc_info=True)
        raise e