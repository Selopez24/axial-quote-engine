# Spec Delta

## Purpose

Provides reliable entry points for ingesting WhatsApp messages in a simplified format (as provided by the assignment). The system exposes two endpoints: POST /conversations for batch processing with full conversation history (testing/demonstration), and POST /messages for individual message ingestion (production use). Both accept pre-structured payloads without webhook parsing.

## ADDED Requirements

### Requirement: Message ingestion endpoint
The system SHALL expose a POST endpoint at /messages that accepts message payloads in the assignment's format (customer_id, conversation_id, customer_name, message_id, direction, timestamp, text) and processes them through the quote extraction pipeline. The endpoint handles both inbound and outbound messages.

#### Scenario: Inbound message processing
- **WHEN** an inbound message is received at POST /messages (direction: "inbound")
- **THEN** the system SHALL process the message through the extraction pipeline and return a 200 status with the processing result

#### Scenario: Outbound message recording
- **WHEN** an outbound message is recorded at POST /messages (direction: "outbound")
- **THEN** the system SHALL store the message in conversation history without triggering quote extraction

#### Scenario: Missing required fields
- **WHEN** a message payload is missing required fields (message_id, conversation_id, customer_id, direction, text)
- **THEN** the system SHALL return a 400 status with details about missing fields

### Requirement: Conversation batch ingestion (testing utility)
The system SHALL expose a POST endpoint at /conversations that accepts a conversation in the assignment's batch format (with conversation_history array) and processes each message sequentially through the standard pipeline. This is a utility endpoint for testing and demonstration, not the production flow.

#### Scenario: Batch conversation ingestion
- **WHEN** a conversation payload with conversation_history array is received at POST /conversations
- **THEN** the system SHALL process each message in order through the standard pipeline and return the resulting quote state

#### Scenario: Batch conversation preserves message order
- **WHEN** a conversation contains interleaved inbound and outbound messages
- **THEN** the system SHALL process them in timestamp order, maintaining full conversation context

### Requirement: Message deduplication
The system SHALL detect and skip duplicate message deliveries based on message_id to ensure idempotent processing.

#### Scenario: Duplicate message received
- **WHEN** a message with a previously processed message_id is received
- **THEN** the system SHALL return a 200 status indicating the message was already processed without re-processing

#### Scenario: New message received
- **WHEN** a message with a new message_id is received
- **THEN** the system SHALL process the message and record the message_id as processed

### Requirement: Pipeline orchestration
The system SHALL route validated messages through the quote extraction pipeline, coordinating extraction, validation, and state updates.

#### Scenario: Successful pipeline execution
- **WHEN** a valid inbound message enters the pipeline
- **THEN** the system SHALL execute extraction, validate results, update state, and return the processing outcome

#### Scenario: Outbound message short-circuits pipeline
- **WHEN** an outbound message enters the pipeline
- **THEN** the system SHALL store the message in conversation history and return without running extraction

#### Scenario: Pipeline processing failure
- **WHEN** the pipeline encounters an unrecoverable error
- **THEN** the system SHALL return a 500 status with error details and NOT update any state

### Requirement: Speaker identification in messages
The system SHALL support identifying which human participant sent each message by using role "user" with speaker names embedded in message content.

#### Scenario: Inbound message with speaker name
- **WHEN** an inbound message is received from a customer (direction: "inbound")
- **THEN** the system SHALL format the message with role "user" and embed the customer's name in the content (e.g., "Alice: Hola buenos dias")

#### Scenario: Outbound message with speaker name
- **WHEN** an outbound message is recorded from the sales rep (direction: "outbound")
- **THEN** the system SHALL format the message with role "user" and embed the sales rep's name in the content (e.g., "Izzy: Hola buen día")
