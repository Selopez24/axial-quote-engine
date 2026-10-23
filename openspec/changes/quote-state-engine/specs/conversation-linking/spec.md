# Spec Delta

## Purpose

Resolves which quote(s) a message belongs to by analyzing conversation context, handles cross-conversation references, and maintains the bidirectional relationship between conversations and quotes.

## ADDED Requirements

### Requirement: Quote resolution from message context
The system SHALL determine which quote(s) a new message belongs to by analyzing the message content, conversation history, and existing quote state for the entire customer (company), not just the current conversation or user.

#### Scenario: Different user from same company references existing quote
- **WHEN** a message arrives from a new user (different conversation_id) but same customer_id as existing quotes
- **THEN** the system SHALL load all open quotes for that customer_id and all conversations linked to those quotes, then analyze the message content to determine which quote it references (e.g., "Alice me pasó con ustedes, me confirmas qué lleva la cotización de las lonas?" → Q-001)

#### Scenario: Message clearly references existing quote
- **WHEN** a message contains explicit reference to an existing quote (e.g., "update quote Q-1001", "change the price in my last quote", "las lonas que cotizó Alice")
- **THEN** the system SHALL link the message to the referenced quote

#### Scenario: Message continues ongoing conversation
- **WHEN** a message is part of an ongoing conversation that is already linked to a quote
- **THEN** the system SHALL resolve a target-less revision to that conversation's single open quote, and when the conversation is linked to more than one open quote it SHALL create a pending review instead of guessing which one was meant

#### Scenario: Message starts new quote request
- **WHEN** a message contains a new product request with no reference to existing quotes (e.g., "aparte cotízame...")
- **THEN** the system SHALL create a new quote and link it to the conversation

### Requirement: Cross-conversation quote reference
The system SHALL detect when a message in one conversation references a quote from a different conversation.

#### Scenario: Customer references quote from different channel
- **WHEN** a message in conversation A references a quote that was created in conversation B
- **THEN** the system SHALL link the message to the referenced quote and establish a link between conversation A and that quote

#### Scenario: Multiple conversations linked to same quote
- **WHEN** a quote is referenced from multiple conversations
- **THEN** the system SHALL maintain links between all referencing conversations and the quote

### Requirement: Bidirectional quote-conversation navigation
The system SHALL support navigation from quote to conversations and from conversation to quotes.

#### Scenario: Get conversations for a quote
- **WHEN** requesting all conversations related to quote Q1
- **THEN** the system SHALL return all conversations that have been linked to Q1

#### Scenario: Get quotes for a conversation
- **WHEN** requesting all quotes from conversation C1
- **THEN** the system SHALL return all quotes that have been linked to C1

### Requirement: Conversation context retrieval for LLM
The system SHALL retrieve relevant conversation history to provide context for LLM-based quote resolution.

#### Scenario: Load conversation history for quote resolution
- **WHEN** processing a new message for quote resolution
- **THEN** the system SHALL retrieve the last 20 messages of each conversation linked to the customer's open quotes, plus the customer identity and the structure of every open quote

#### Scenario: Include current conversation even if unlinked
- **WHEN** a message arrives in a conversation not yet linked to any quote
- **THEN** the system SHALL include that conversation's recent messages in the context for quote resolution

### Requirement: Linkage persistence
The system SHALL persist all quote-conversation linkages in the database to survive service restarts.

#### Scenario: Linkage survives service restart
- **WHEN** the service restarts after establishing quote-conversation linkages
- **THEN** the system SHALL restore all previous linkages from the database

#### Scenario: New linkage created
- **WHEN** a new quote-conversation relationship is established
- **THEN** the system SHALL persist the linkage immediately to the database
