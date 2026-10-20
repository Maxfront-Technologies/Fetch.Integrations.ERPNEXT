# Fetch Einvoice 0.0.2: changes to ERPNext

This note lists everything the Fetch Einvoice app adds to or changes in an ERPNext v13 site. Read it before you install the app or update it.

The app connects ERPNext to FETCH, which transmits invoices to NRS (FIRS). It sends submitted Sales Invoices and customer payments to FETCH, and it stores the IRN that FETCH sends back.

## What the app leaves alone

- It posts no GL entries and moves no stock.
- It never changes an invoice's items, prices, taxes or totals.
- It edits no standard DocType definition. Standard DocTypes get custom fields only.
- It deletes no ERPNext records.
- Its checks run only while **Enabled** is ticked in FETCH Settings. If **Company** is set there, they run only for that company. With the app disabled, ERPNext behaves as it did before.

## New DocTypes

All five belong to the **Fetch Einvoice** module.

| DocType | What it holds | Who can edit | Who can read |
|---|---|---|---|
| FETCH Settings | The single settings record: Enabled, Environment (Production or Sandbox), Base URL, Company, API Key, API Secret, Callback URL (read-only) and Callback Signing Secret. The key and both secrets are stored as Password fields. | System Manager | System Manager |
| FETCH Outbox | One row for each call to FETCH, named `FOB-#######`. Each row holds its kind (Invoice or Payment), status, attempt count, next attempt time, response code, error, the request sent and the response received. Every field is read-only, and only the app changes the rows. | System Manager | Accounts Manager |
| FIRS Tax Category | A FIRS tax code such as `STANDARD_VAT`, a title, and an optional tax rate. | System Manager, Accounts Manager | Accounts User, Sales User, Item Manager |
| FIRS HSN Code | An HS or service code, a description, and whether it is a Product or a Service. | System Manager, Accounts Manager | Accounts User, Sales User, Item Manager |
| FIRS Quantity Code | A unit code, a title and a description. | System Manager, Accounts Manager | Accounts User, Sales User, Item Manager |

## New fields on standard DocTypes

The install adds these as Custom Fields. `bench migrate` applies them again each time. Running it twice changes nothing.

| DocType | Field | Type | Notes |
|---|---|---|---|
| Item | FIRS e-Invoicing | Section Break | Collapsible. Placed after Customs Tariff Number. |
| Item | FIRS HSN Code | Link to FIRS HSN Code | |
| Item | FIRS Tax Category | Link to FIRS Tax Category | |
| Item | FIRS Quantity Code | Link to FIRS Quantity Code | |
| Sales Invoice Item | FIRS HSN Code | Link | Copied from the Item when the line is empty. It can be changed on the line. |
| Sales Invoice Item | FIRS Tax Category | Link | Copied from the Item when the line is empty. When the line's Item Tax Template has a category, the template's category replaces it on save. |
| Sales Invoice Item | FIRS Quantity Code | Link | Copied from the Item when the line is empty. |
| Item Tax Template | FIRS Tax Category | Link | Placed after Title. |
| Customer | FIRS Business Description | Small Text | Placed after Tax ID. |
| Sales Invoice | FETCH e-Invoicing | Section Break | Collapsible. Placed after Amended From. |
| Sales Invoice | FETCH Status | Select: Pending, Submitted, Transmitted, Failed | Read-only. Available as a list filter. Not copied when an invoice is duplicated or amended. |
| Sales Invoice | FETCH IRN | Data | Read-only. Indexed for search. Not copied. |
| Sales Invoice | FETCH Invoice ID | Data | Read-only. Not copied. |

## Changes to how ERPNext behaves

### Sales Invoice

- **On save.** Each line with an Item Tax Template that has a FIRS Tax Category takes that category.
- **On submit, before anything is posted.** ERPNext refuses an invoice that FETCH would reject and lists every problem at once. An invoice needs all of these:
  - a customer Tax ID (TIN), email and mobile number,
  - a billing address with a street, a city and a country,
  - the three FIRS codes on every line, and a whole-number quantity on every line,
  - currency USD, NGN, GBP, EUR or CAD,
  - tax rows of type "On Net Total" only,
  - a FIRS Tax Category rate, where one is set, equal to the rate the invoice charges on that line.
- **Debit notes can't be submitted** while the app is enabled, because FETCH accepts only invoices and credit notes.
- **After submit.** The app adds a FETCH Outbox row, sets FETCH Status to Pending, and queues the send. A return goes to FETCH as a credit note. An amended invoice goes as a new invoice.
- **On cancel.** The app cancels every Outbox row that FETCH hasn't accepted yet, and adds a comment to the invoice that lists them. When FETCH has already accepted the invoice, the form asks for confirmation first, because the NRS document stays valid. The confirmation appears only when you cancel from the form, not from the list.
- **The form.** A submitted invoice shows its FETCH status, and its IRN once stored, at the top. A Failed invoice gets a **Resend to FETCH** button for users who can submit Sales Invoices.
- **Consolidated POS invoices** that ERPNext creates at POS closing are neither checked nor sent.

### Payment Entry

- **After submit.** A Payment Entry that receives money from a customer gets one Outbox row for each Sales Invoice it pays. Payments against returns are skipped, and so are payments for invoices submitted before the app was installed. A payment waits until its invoice has an IRN. If the invoice is Failed, the payment is marked Failed too, and it goes by itself once the resent invoice has its IRN. A payment whose invoice is cancelled is not sent.
- **On cancel.** Rows that haven't reached FETCH are cancelled. When FETCH has already recorded the payment, the form asks for confirmation first, because FETCH has no payment reversal.

### Tax rate sent to FETCH

Each line goes to FETCH with the tax rate the invoice charges on it. The rate comes from the invoice's Sales Taxes and Charges rows and from the line's Item Tax Template, the same way ERPNext works out the tax. The FIRS Tax Category's rate is only a check. A rate of 0 counts as no rate.

## What the app writes to existing records

- **Sales Invoice.** The app writes only FETCH Status, FETCH IRN and FETCH Invoice ID, directly to the database. These writes don't change the invoice's modified time. When the IRN arrives, the app adds an Info comment that quotes it.
- **Sales Invoice Item.** The app sets FIRS Tax Category from the Item Tax Template on save.
- **Error Log.** The app records failed payload builds, failed FIRS code syncs, unknown callback events and IRN mismatches here.

## Background jobs

| Job | When it runs | Queue | What it does |
|---|---|---|---|
| `fetch_einvoice.outbox.send` | After each submit, retry or resend | default | Sends one Outbox row to FETCH and records the answer. |
| `fetch_einvoice.outbox.sweep` | Every scheduler tick (every 4 minutes by default in v13) | default | Puts rows stuck in Sending for 10 minutes back to Pending. Then it queues every Pending row that is due. |
| `fetch_einvoice.outbox.wake` | When an IRN arrives | default | Sends the payments and credit notes that were waiting for that IRN. |
| `fetch_einvoice.api.run_firs_code_sync` | Daily, and from the **Sync FIRS Codes** button | long | Downloads the FIRS tax category, quantity code and HS code lists. It adds new codes and updates changed ones. It never deletes a code. |

Failed sends are retried after 1, 5, 15, 60 and 240 minutes. A row fails after 6 attempts.

The site needs the scheduler running. It also needs a worker on the `default` queue and a worker on the `long` queue. On Docker, every container has to run an image that includes the app.

## New API endpoints

| Method | Who can call it | Purpose |
|---|---|---|
| `fetch_einvoice.api.receive_fetch_irn` | Anyone (guest). Every call must carry a valid FETCH signature, and calls for the other environment are refused. | FETCH's IRN callback. It stores the IRN and sets FETCH Status to Transmitted. |
| `fetch_einvoice.api.resend_invoice` | Users who can submit the Sales Invoice | The **Resend to FETCH** button. |
| `fetch_einvoice.api.retry` | Users who can write the Outbox row | The **Retry** button on a Failed Outbox row. |
| `fetch_einvoice.api.payment_accepted_by_fetch` | Users who can read the Payment Entry | Used by the cancel confirmation. |
| `fetch_einvoice.api.sync_firs_codes` | System Manager | The **Sync FIRS Codes** button. |

## Outbound network calls

- `POST <Base URL>/api/v1/invoices/transmit` and `POST <Base URL>/api/v1/invoices/add-payment` go to FETCH, with the API key in the headers. The app doesn't follow redirects, and it waits 30 seconds for an answer.
- `GET https://einvoice1.firs.gov.ng/api/v1/invoice/resources/` goes to FIRS, for the code lists.

## Data patch

`fetch_einvoice.patches.snapshot_invoice_outstanding` runs once during `bench migrate`. It fills in the amount on Invoice Outbox rows that are still Pending or Failed, using each invoice's outstanding amount.

## Before you enable it

- Disable any older Frappe **Webhook** that sends invoices to FETCH. With both on, every invoice goes twice.
- Load the FIRS codes with **Sync FIRS Codes**. Then fill in the FIRS e-Invoicing section on each Item, or set FIRS Tax Category on your Item Tax Templates.
- Check that your Sales Taxes and Charges Templates charge the current VAT rate.
