# Hand-authored feature — not compiled from adcp-req.
#
# LOCALLY-ADDED (survives BR-*.feature regeneration).
# Upstream gap: no storyboard scenario drives the A2A protocol methods
# `tasks/get` / `tasks/cancel` against a task another principal owns. That is
# the surface the in-memory ownership gate defends (#1702): a task_id is
# unguessable but not secret once known (webhook payloads, logs, support
# channels), so serving or cancelling an in-memory hit for anyone who learned
# the id leaks — and mutates — another buyer's task. The denial must be
# indistinguishable from an unknown id (no existence oracle). Reconcile
# upstream in adcp-req, then retire this file for the regenerated scenarios.
#
# @a2a: these are protocol methods, not AdCP skills — there is no MCP or REST
# equivalent to parametrize across. The denial oracle grades the handler-
# exception shape mirrored to today's v0.3 JSON-RPC body (CoreInternalError /
# -32603), not a live adapter capture and not a two-layer AdCP envelope. Live
# JSON-RPC wire serialization is proven in tests/unit/test_a2a_task_identity_wire.py
# (#1720).
Feature: A2A in-memory task ownership for tasks/get and tasks/cancel (local)

  Background:
    Given an in-memory A2A task "task-owned-1" owned by the owning principal

  @T-A2A-TASK-OWNERSHIP-owner-get @a2a
  Scenario: The owner retrieves their own task via tasks/get
    When the "owner" calls tasks/get for task "task-owned-1"
    Then the A2A task response should carry task "task-owned-1" in state WORKING

  @T-A2A-TASK-OWNERSHIP-sibling-get @a2a
  Scenario: A sibling principal in the same tenant is denied via tasks/get
    When the "sibling" calls tasks/get for task "task-owned-1"
    Then the A2A task response should be a JSON-RPC task-not-found error for "task-owned-1"

  @T-A2A-TASK-OWNERSHIP-other-tenant-get @a2a
  Scenario: A principal in another tenant is denied via tasks/get
    When the "other_tenant" calls tasks/get for task "task-owned-1"
    Then the A2A task response should be a JSON-RPC task-not-found error for "task-owned-1"

  @T-A2A-TASK-OWNERSHIP-unknown-get @a2a
  Scenario: An unknown task id is denied in the same shape as an ownership miss
    When the "owner" calls tasks/get for task "task-never-created"
    Then the A2A task response should be a JSON-RPC task-not-found error for "task-never-created"

  @T-A2A-TASK-OWNERSHIP-sibling-cancel @a2a
  Scenario: A denied tasks/cancel does not mutate the owner's task
    When the "sibling" calls tasks/cancel for task "task-owned-1"
    Then the A2A task response should be a JSON-RPC task-not-found error for "task-owned-1"
    And the stored task "task-owned-1" should be in state WORKING

  @T-A2A-TASK-OWNERSHIP-other-tenant-cancel @a2a
  Scenario: A principal in another tenant is denied via tasks/cancel
    When the "other_tenant" calls tasks/cancel for task "task-owned-1"
    Then the A2A task response should be a JSON-RPC task-not-found error for "task-owned-1"
    And the stored task "task-owned-1" should be in state WORKING

  @T-A2A-TASK-OWNERSHIP-owner-cancel @a2a
  Scenario: The owner cancels their own task
    When the "owner" calls tasks/cancel for task "task-owned-1"
    Then the A2A task response should carry task "task-owned-1" in state CANCELED
    And the stored task "task-owned-1" should be in state CANCELED
