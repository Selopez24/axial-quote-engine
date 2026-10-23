# Spec Delta

## Purpose

Detects when the system cannot confidently determine quote intent or linkage, creates pending reviews for human intervention, and provides APIs for reviewing and resolving ambiguous cases.

## ADDED Requirements

### Requirement: Ambiguity detection
The system SHALL detect when message intent, quote reference, or product extraction cannot be determined with sufficient confidence.

#### Scenario: Ambiguous quote reference
- **WHEN** a message references a quote but the reference is unclear (e.g., "cancel the last one" when multiple quotes exist)
- **THEN** the system SHALL flag the message as ambiguous and create a pending review

#### Scenario: Ambiguous product extraction
- **WHEN** a product description cannot be matched to any SKU with high confidence
- **THEN** the system SHALL flag the extraction as ambiguous and create a pending review

#### Scenario: Ambiguous intent
- **WHEN** the system cannot determine if a message is a new request, revision, or clarification
- **THEN** the system SHALL flag the intent as ambiguous and create a pending review

### Requirement: Pending review creation
The system SHALL create a pending review record containing the original message, the LLM extraction results, the resolution issues that made the message unassignable, and the candidate quotes it might refer to. The record carries facts, not suggested actions: the system proposes nothing a human might mistake for a decision it already made.

#### Scenario: Pending review created for ambiguous message
- **WHEN** a message is flagged as ambiguous
- **THEN** the system SHALL create a pending review with status 'pending', storing the original message, LLM extraction, identified issues, and candidate quotes

#### Scenario: Pending review includes follow-up messages
- **WHEN** additional messages arrive for a conversation with an open pending review
- **THEN** the system SHALL append these messages to the pending review's follow_up_messages array

#### Scenario: Inbound follow-up retries the parked request (self-heal)
- **WHEN** an inbound message arrives for a conversation with an open pending review
- **THEN** the system SHALL retry the parked request: the follow-up is classified with the review's original message and its issues surfaced in context, and when it clarifies that request the combined result is emitted — a follow-up is never a state change on its own
- **AND IF** the retry passes validation the system SHALL apply the change attributed to the original message and mark the review `self_resolved` with the triggering message id, keeping it closed to human resolution

#### Scenario: Still-ambiguous reviews wait for a human
- **WHEN** the self-heal retry fails validation (e.g., "mejor cancelalo", whose cancellation is not an intent the engine writes — retries confirm unclear by design)
- **THEN** the system SHALL keep the review pending, store the latest attempt's extraction and issues on the record, and await POST /pending-reviews/{id}/resolve

### Requirement: Pending review resolution API
The system SHALL provide endpoints for retrieving and resolving pending reviews.

#### Scenario: List pending reviews
- **WHEN** a request is made to GET /pending-reviews
- **THEN** the system SHALL return all pending reviews with status 'pending'

#### Scenario: Get specific pending review
- **WHEN** a request is made to GET /pending-reviews/{id}
- **THEN** the system SHALL return the pending review details including original message, extraction, and candidates

#### Scenario: Resolve pending review
- **WHEN** a request is made to POST /pending-reviews/{id}/resolve with resolution data
- **THEN** the system SHALL apply the resolution, update the quote state accordingly, and mark the review as 'resolved'

### Requirement: Quote resolution after ambiguity
The system SHALL apply the resolved decision from a pending review to update the appropriate quote state.

#### Scenario: Resolve to specific quote
- **WHEN** a pending review is resolved with a specific quote ID
- **THEN** the system SHALL apply the extracted changes to that quote

#### Scenario: Resolve by creating new quote
- **WHEN** a pending review is resolved by creating a new quote
- **THEN** the system SHALL create a new quote with the extracted data and link it to the conversation

#### Scenario: Resolve by discarding message
- **WHEN** a pending review is resolved by discarding the message
- **THEN** the system SHALL mark the review as 'discarded' and make no changes to quote state

### Requirement: Pending review persistence
The system SHALL persist all pending reviews in the database to survive service restarts.

#### Scenario: Pending review survives restart
- **WHEN** the service restarts with pending reviews in the database
- **THEN** the system SHALL restore all pending reviews with their original state
