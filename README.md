# Fetch Einvoice

Fetch Einvoice is an ERPNext v13 app that sends your Sales Invoices and customer payments to FETCH, which transmits them to NRS (FIRS) for e-invoicing. When NRS accepts an invoice, FETCH calls back with the IRN and the app stores it on the Sales Invoice.

The app does four things:

- When you submit a Sales Invoice, it sends the invoice to FETCH. A return goes as a credit note, and an amended invoice goes as a new invoice.
- When you submit a Payment Entry that receives money from a customer, it sends one payment to FETCH for each Sales Invoice the entry pays.
- It receives FETCH's IRN callback and stores the IRN on the Sales Invoice.
- It retries calls that time out or hit a FETCH outage, and records every call in the **FETCH Outbox** list.

## Before you start

You need:

- ERPNext and Frappe v13, on Python 3.7 or later.
- The scheduler and background workers running. Check with `bench --site <site> doctor`, and turn the scheduler on with `bench --site <site> enable-scheduler`.
- A FETCH API key for the environment you will use (Production or Sandbox), and FETCH's API base URL. Ask FETCH for the URL. It is not in this repository.
- A public HTTPS address for your site on port 443 that answers without a redirect. FETCH posts the IRN callback to it.

## Install the app

`bench get-app` needs a git URL. If this folder is in a git repository you can reach, run:

```bash
bench get-app <git-url-of-the-repository>
bench --site <site> install-app fetch_einvoice
```

Otherwise copy the folder into the bench:

1. Copy `ThirdParty/erpnext/fetch_einvoice` to `frappe-bench/apps/fetch_einvoice`.
2. From `frappe-bench`, install the Python package with `./env/bin/pip install -e apps/fetch_einvoice`. On benches that have it, `bench pip install -e apps/fetch_einvoice` does the same.
3. Add the app to `sites/apps.txt` on a line of its own: `echo fetch_einvoice >> sites/apps.txt`. First make sure the file ends with a newline.
4. Install it on the site: `bench --site <site> install-app fetch_einvoice`.
5. Restart the bench so the web and worker processes load the app. Use `bench restart` in production, or restart `bench start` in development.

The install creates the FIRS doctypes, the FETCH doctypes, and the custom fields on Item, Sales Invoice Item, Customer and Sales Invoice. `bench migrate` re-applies the custom fields each time, and running it twice changes nothing.

## Turn off any old FETCH webhook

If you sent invoices to FETCH with a Frappe **Webhook** before, open **Webhook** and disable it before you enable this app. Frappe v13 runs webhooks after this app's document events, so with both on, every invoice goes to FETCH twice and the second copy gets a 400 error.

## Configure FETCH Settings

Open **FETCH Settings** from the search bar and fill in:

- **Environment.** Choose Production or Sandbox to match your API key. Callbacks from the other environment are refused.
- **Base URL.** FETCH's API address. It must start with `https://`. The app removes a trailing slash.
- **Company.** Optional. Set it to send only that company's invoices. Leave it empty to send every company's invoices.
- **API Key** and **API Secret.** Your FETCH API key. Fill in the secret only if your key has one.
- **Enabled.** Tick it when everything else is filled in.

Save. The **Callback URL** field now shows the address FETCH must call, built from your site URL. If it shows an internal address such as `http://10.0.0.5:8000`, set your public URL with `bench --site <site> set-config host_name https://erp.example.com` and reload the form.

## Connect FETCH's IRN callback

1. In FETCH, open **Settings › ERP API** and select the same environment.
2. Paste the **Callback URL** from FETCH Settings.
3. Generate the callback signing secret and copy it. FETCH shows it only once.
4. In ERPNext, paste it into **Callback Signing Secret** in FETCH Settings and save.
5. In FETCH, enable callbacks.

FETCH only calls an `https` URL on port 443 with a public DNS name, and it never follows a redirect. Paste the final address. A URL that redirects from `http` to `https`, or from `example.com` to `www.example.com`, never receives a callback. FETCH waits 30 seconds for an answer, then retries after 1, 5, 15 and 60 minutes.

Only invoices submitted while callbacks are enabled in FETCH get a callback. If you rotate the secret in FETCH, callbacks pause. Paste the new secret into FETCH Settings, then enable callbacks again in FETCH.

## Load the FIRS codes

Every invoice line needs three FIRS codes. They live in three lists:

- **FIRS Tax Category** holds a code such as `STANDARD_VAT`, a title, and the tax rate.
- **FIRS HSN Code** holds the HS or service code, a description, and whether it is a Product or a Service.
- **FIRS Quantity Code** holds the unit code, a title, and a description.

The **Sync FIRS Codes** button on FETCH Settings starts a background job that downloads all three lists from FETCH and creates or updates the records. Today FETCH refuses API keys on its code-list endpoints, so the job stops with an "HTTP 401" message. Until FETCH accepts API keys there, enter the codes by hand or with **Data Import**.

Then open each Item and fill in the **FIRS e-Invoicing** section. Sales Invoice lines copy the codes from the Item, and you can change them on a line before you submit. The tax rate of the FIRS Tax Category must equal the rate the invoice charges, because FETCH adds the tax to each line at that rate.

## What each invoice needs

While the app is enabled, ERPNext refuses to submit an invoice that FETCH would reject and lists every problem at once. An invoice needs:

- A customer **Tax ID** (TIN).
- A customer email, from the invoice's contact or the Customer.
- A customer mobile number. A Nigerian number such as `0803 123 4567` becomes `+2348031234567`.
- A customer billing address with a street, a city and a country.
- The three FIRS codes on every line, and a whole-number quantity.
- Currency USD, NGN, GBP, EUR or CAD.
- Tax rows of type "On Net Total" only.

Debit notes cannot be submitted while the app is enabled, because FETCH accepts only invoices and credit notes.

## Follow an invoice

The **FETCH e-Invoicing** section of a Sales Invoice shows **FETCH Status**, **FETCH IRN** and **FETCH Invoice ID**, and the form shows the status at the top. You can filter the Sales Invoice list by FETCH Status.

| FETCH Status | Meaning |
|---|---|
| Pending | Waiting to be sent, or waiting for a retry. |
| Submitted | FETCH accepted the invoice and is transmitting it to NRS. |
| Transmitted | NRS accepted it, and the IRN is stored. |
| Failed | FETCH rejected it. The reason is in the FETCH Outbox row. |

To resend a Failed invoice, fix the problem and click **Resend to FETCH** on the invoice. You need permission to submit Sales Invoices.

**FETCH Outbox** has one row per call to FETCH, with the request, the response and the last error. A row is Pending, Sending, Accepted, Failed or Cancelled. A System Manager can retry a Failed row with its **Retry** button. Timeouts, HTTP 429 and server errors are retried after 1, 5, 15, 60 and 240 minutes, and the row fails after 6 attempts.

Payments wait until their invoice has its IRN. Payments for one invoice go oldest first, and a Failed payment holds back the later ones for that invoice until you retry it or cancel its Payment Entry. A retried payment sends exactly what was sent the first time. A return waits until the original invoice has its IRN, unless the original was submitted before this app was installed.

Cancelling an invoice or payment that FETCH has accepted shows a warning and then goes ahead. The NRS document stays valid, so issue a credit note if one is needed. The amended invoice goes to FETCH as a new invoice. Cancelling stops every call that has not reached FETCH yet, and adds a comment that lists them.

## Known gaps

- Only Payment Entries are sent. Journal Entries, Payment Reconciliation and advances allocated later are not.
- A Payment Entry in a different currency from its invoice fails, with a message on the FETCH Outbox row.
- FETCH sends no callback when NRS rejects an invoice. Such an invoice stays Submitted. Find it with the FETCH Status filter and retransmit it from FETCH. A later success still sends the callback.
- FETCH's PAID rule ignores credit notes, so the final payment on an invoice that has a credit note gets HTTP 409 until FETCH fixes it.
- If you cancel an earlier Payment Entry after a later one settled the invoice in ERPNext, the later payment still goes to FETCH as PAID for FETCH's whole balance. Check the balance in FETCH.
- An amended invoice named `…-00001-1` and another named `…-000011` on the same date produce the same IRN in FETCH. ERPNext's default 5-digit naming series avoids this.
- The cancel warning appears only when you cancel from the form. Cancelling from the list view skips it.
- The FIRS code sync waits on FETCH accepting API keys on its code-list endpoints.
- Every line goes to FETCH as a Service, so FETCH's catalogue lists goods as services. FETCH stock-checks Product lines, and the items it creates for ERPNext have no stock, so goods lines sent as Products would be refused. NRS never sees this field.
- If FETCH answers HTTP 403 (plan limit) it has already stored the invoice but will not transmit it. Resending gets "already exists" and is recorded as Accepted, so transmit that invoice from FETCH once the plan allows it.
- FETCH refuses a return dated after today in UTC. A return posted between midnight and 01:00 Lagos time fails until it is resent after 01:00.

## Run the tests

The payload and signature tests need no bench:

```bash
cd ThirdParty/erpnext/fetch_einvoice
python -m unittest fetch_einvoice.tests.test_core
```

The integration tests need a v13 bench with ERPNext set up on a disposable site. They mock FETCH, and they commit, so their records stay on that site:

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app fetch_einvoice
```

## License

MIT
