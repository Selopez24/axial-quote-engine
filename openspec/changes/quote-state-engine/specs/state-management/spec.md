# Spec Delta

## Purpose

Persists and retrieves quote state, conversation state, and the many-to-many relationships between quotes and conversations using PostgreSQL with JSONB for flexible document storage.

## ADDED Requirements

### Requirement: Quote state persistence
The system SHALL persist quote state including quote_id, customer_id, status, line_items, and metadata in PostgreSQL.

#### Scenario: Quote created
- **WHEN** a new quote is generated from extracted data
- **THEN** the system SHALL store the quote with all its attributes in the quotes table

#### Scenario: Quote updated
- **WHEN** a quote is revised with new line items or status changes
- **THEN** the system SHALL update the existing quote record with the new state

### Requirement: Conversation state persistence
The system SHALL persist conversation state including conversation_id, customer_id, customer identity metadata, and message history in PostgreSQL using JSONB for flexible message storage.

#### Scenario: Conversation created
- **WHEN** a message arrives in a new conversation
- **THEN** the system SHALL create a new conversation record storing the conversation_id, customer_id, any available customer identity metadata (customer_name), and the message in JSONB format

#### Scenario: Message appended to conversation
- **WHEN** a new message arrives in an existing conversation
- **THEN** the system SHALL append the message to the conversation's message history

#### Scenario: Customer identity updated
- **WHEN** a message arrives with customer identity information (e.g., customer_name) that was not previously stored
- **THEN** the system SHALL update the stored customer identity metadata for that customer

### Requirement: Customer identity persistence
The system SHALL persist customer identity information (customer_id, customer_name, and other available metadata) so it is available across conversations and survives service restarts.

#### Scenario: Customer identity stored on first encounter
- **WHEN** a message arrives with a customer_id and customer_name not previously seen
- **THEN** the system SHALL create a customer record with the available identity information

#### Scenario: Customer identity reused for subsequent messages
- **WHEN** subsequent messages arrive with a customer_id that already exists
- **THEN** the system SHALL use the existing customer record without requiring customer_name to be provided again

#### Scenario: Customer identity used in context
- **WHEN** building conversation context for LLM extraction
- **THEN** the system SHALL include stored customer identity information to help the LLM understand who is speaking and their organizational context

### Requirement: Quote-conversation many-to-many relationship
The system SHALL maintain a many-to-many relationship between quotes and conversations using a join table, allowing one conversation to spawn multiple quotes and one quote to be managed across multiple conversations.

#### Scenario: Conversation linked to existing quote
- **WHEN** a message in conversation A refers to quote Q1
- **THEN** the system SHALL create a link between conversation A and quote Q1 in the join table

#### Scenario: Multiple conversations linked to same quote
- **WHEN** messages from conversation A and conversation B both refer to quote Q1
- **THEN** the system SHALL maintain links between both conversations and quote Q1

#### Scenario: Multiple quotes spawned from same conversation
- **WHEN** a conversation spawns quote Q1 and later spawns quote Q2
- **THEN** the system SHALL maintain separate links between the conversation and each quote

### Requirement: State retrieval by customer
The system SHALL retrieve all quotes for a given customer_id with their associated conversations and line items.

#### Scenario: Get customer quotes
- **WHEN** a request is made for all quotes of customer C1
- **THEN** the system SHALL return all quotes for C1 with their conversation links and line items

#### Scenario: Customer has no quotes
- **WHEN** a request is made for quotes of a customer with no quotes
- **THEN** the system SHALL return an empty list

### Requirement: State retrieval by quote
The system SHALL retrieve a specific quote with its full state including all linked conversations and line items.

#### Scenario: Get quote by ID
- **WHEN** a request is made for quote Q1
- **THEN** the system SHALL return the quote with all linked conversations and line items

#### Scenario: Quote does not exist
- **WHEN** a request is made for a non-existent quote ID
- **THEN** the system SHALL return a 404 status
