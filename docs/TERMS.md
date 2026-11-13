# Project terms

These are the terms used in the dispatcher and in customer calling workflows.

| Term | Meaning |
|---|---|
| **Tenant** | A client company using the platform. Concurrency limits are applied per tenant. |
| **Workflow** | A business process run by voice agents, such as renewing a motor policy or collecting feedback after a purchase. Each workflow can define its own lead fields and dispositions. |
| **Campaign** | One outbound calling run for a tenant, including its leads, calling window, and retry policy. |
| **Lead** | A customer we intend to call. A lead belongs to a campaign and is identified by its campaign and lead ID. |
| **Batch** | One uploaded group of leads. Reports may group results by batch. |
| **Lead schema** | The definition of accepted lead columns, their types, required fields, and PII fields. |
| **PII** | Personally identifiable information, such as a person's name or phone number. It must be protected at rest. |
| **Call / interaction** | One attempt to reach a lead. It can fail at the carrier, go unanswered, or connect. |
| **Connectivity rate** | Connected calls divided by total calls. It is a basic campaign health metric. |
| **Disposition** | The outcome classified after a connected call, such as `renewed`, `callback_requested`, or `not_interested`. |
| **Terminal disposition** | A disposition that ends work on a lead. The lead is not called again. |
| **Success disposition** | A disposition that represents a business win for the client, such as `renewed`. |
| **Override** | A human correction to an AI disposition. It is recorded as a new event; the original event remains unchanged. |
| **Interaction ledger** | The append-only history of events for every call. Reports are derived from this history. |
| **Calling window** | The local hours and days when calls may start. Commercial calls in India are restricted to 09:00–21:00. |
| **DND** | “Do not disturb.” A customer who asks not to be called must not be called again. |
| **Callback** | A request from the customer to be called again at a particular time. |
| **Telephony provider** | The carrier API that places calls and sends back results through webhooks. |
| **Idempotency key** | A client-chosen ID, called `request_id` here, that makes safely retrying one provider request possible. |
