# Spec Delta

## Purpose

Transforms unstructured conversational text from WhatsApp messages into structured quote data with line items, SKUs, and quantities through LLM-based extraction.

## ADDED Requirements

### Requirement: Quote extraction from conversation
The system SHALL extract structured quote data from unstructured conversational text, identifying product requests, quantities, and customer intent.

#### Scenario: Successful extraction of product request
- **WHEN** a customer message contains product request information
- **THEN** the system SHALL extract product descriptions, quantities, and any specifications mentioned

#### Scenario: Multiple products in single message
- **WHEN** a customer message requests multiple products
- **THEN** the system SHALL extract each product as a separate line item

### Requirement: SKU mapping with catalog validation
The system SHALL validate extracted product descriptions against the SKU catalog and reject quotes containing invalid SKUs.

#### Scenario: Valid SKU match found
- **WHEN** an extracted product description matches a catalog SKU
- **THEN** the system SHALL map the product to the corresponding SKU code

#### Scenario: No matching SKU found
- **WHEN** an extracted product description does not match any catalog SKU
- **THEN** the system SHALL reject the extraction and create a pending review for human intervention

### Requirement: Quantity extraction and validation
The system SHALL extract quantities from conversational text and validate they are positive integers.

#### Scenario: Valid quantity extracted
- **WHEN** a quantity is mentioned in the conversation
- **THEN** the system SHALL extract the quantity as a positive integer

#### Scenario: Invalid quantity detected
- **WHEN** an extracted quantity is zero, negative, or non-numeric
- **THEN** the system SHALL reject the extraction and create a pending review

### Requirement: Intent classification
The system SHALL classify the customer's intent as either new_quote, revision, reference, other, or unclear to guide downstream processing.

#### Scenario: New quote request detected
- **WHEN** the conversation indicates a new quote request
- **THEN** the system SHALL classify intent as new_quote

#### Scenario: Revision to existing quote detected
- **WHEN** the conversation indicates modifications to an existing quote
- **THEN** the system SHALL classify intent as revision

#### Scenario: Question about an existing quote, no changes
- **WHEN** a message asks about or confirms an existing quote without modifying it (e.g., Event 2: "me confirmas qué lleva la cotización de las lonas?")
- **THEN** the system SHALL classify intent as reference, apply no line item changes, and still link the arriving conversation to that quote

#### Scenario: Ambiguous intent
- **WHEN** the intent cannot be determined with confidence
- **THEN** the system SHALL classify intent as unclear and create a pending review

### Requirement: Non-quote content passes through inertly
The system SHALL classify messages that carry no quote action — greetings, thanks, or availability-only questions about products that appear on no open quote — as intent other, without modifying state and without creating a pending review. Intent unclear is reserved for messages that demand a quote decision the system cannot resolve, such as a firm order of an off-catalog product or a cancellation while multiple quotes are open.

#### Scenario: Greeting or pure availability question
- **WHEN** "una consulta, si cuentan con manguera de 1 pulgada?" or "muchas gracias, buen fin de semana!" arrives with no firm order
- **THEN** the system SHALL process it with intent other, writing no line items, no quote, no conversation link, and no pending review

#### Scenario: Inconsistent other
- **WHEN** an extraction claims intent other yet attaches line items or a target quote
- **THEN** the validator SHALL reject it (inconsistent_extraction) and route the message to the review queue rather than silently applying either reading
