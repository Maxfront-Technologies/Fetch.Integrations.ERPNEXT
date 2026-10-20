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

The app's folder in the bench must be named `fetch_einvoice`. `bench get-app` names the folder after the repository. When the repository name contains dots, as in `Fetch.Integrations.ERPNEXT`, bench cuts the name at the first dot and fails with `No such file or directory: apps/Fetch/setup.py`. Clone the repository into the right folder instead:

```bash
cd ~/frappe-bench
git clone https://github.com/Maxfront-Technologies/Fetch.Integrations.ERPNEXT apps/fetch_einvoice
./env/bin/pip install -e apps/fetch_einvoice
grep -qx fetch_einvoice sites/apps.txt || echo fetch_einvoice >> sites/apps.txt
bench --site <site> install-app fetch_einvoice
bench restart
```

- Before appending to `sites/apps.txt`, make sure the file ends with a newline (`tail -c1 sites/apps.txt | od -c`). If it doesn't, run `echo >> sites/apps.txt` first.
- In development, restart `bench start` instead of running `bench restart`.
- If a failed `bench get-app` left `apps/Fetch.Integrations.ERPNEXT` behind, delete it first with `rm -rf apps/Fetch.Integrations.ERPNEXT`.

If the repository is renamed to `fetch_einvoice`, a plain `bench get-app` works:

```bash
bench get-app https://github.com/Maxfront-Technologies/fetch_einvoice
bench --site <site> install-app fetch_einvoice
```

To update later, see [Update the app](#update-the-app).

The install creates the FIRS doctypes, the FETCH doctypes, and the custom fields on Item, Sales Invoice Item, Customer and Sales Invoice. `bench migrate` re-applies the custom fields each time, and running it twice changes nothing.

## Install on Docker (frappe_docker)

In frappe_docker every service (backend, frontend, websocket, the workers, the scheduler and the configurator) runs from the image, and the configurator rewrites `sites/apps.txt` from the image's `apps` folder each time it starts. An app cloned into one running container is missing from the others, and drops out of `apps.txt` on the next start. Build an image that contains the app instead. The `Dockerfile` in this repository does that:

```bash
git clone https://github.com/Maxfront-Technologies/Fetch.Integrations.ERPNEXT fetch_einvoice
cd fetch_einvoice
docker build -t fetch-erpnext:v13.52.15 --build-arg ERPNEXT_IMAGE=frappe/erpnext:v13.52.15 .
```

Set `ERPNEXT_IMAGE` to the `frappe/erpnext` image your compose file uses today. Then edit the compose file:

- Replace that image with `fetch-erpnext:v13.52.15` in every service that uses it.
- Make a worker consume the `default` queue. Frappe v13 puts scheduled jobs and this app's calls to FETCH on `default`, and a stack with only `short` and `long` workers never runs them. Change the short worker's command to `bench worker --queue short,default`.

Apply it from the compose file's folder:

```bash
docker compose up -d
docker compose exec backend bench --site <site> install-app fetch_einvoice
docker compose exec backend bench --site <site> migrate
docker compose restart frontend
```

- Skip `install-app` if the app is already installed on the site.
- `docker compose up -d` reruns the configurator, which now lists `fetch_einvoice` in `sites/apps.txt`.
- The frontend restart is needed because its nginx looks up the backend's address only when it starts. See [the 502 entry under Troubleshooting](#nginx-502-connect-failed-111-connection-refused-on-docker).

To update, pull the new code, rebuild the image with the same command, then run the four commands above without `install-app`.

## Update the app

Run these from `frappe-bench`:

```bash
cd ~/frappe-bench
bench --site <site> backup
git -C apps/fetch_einvoice pull
./env/bin/pip install -e apps/fetch_einvoice
bench --site <site> migrate
bench restart
```

- `bench --site <site> backup` saves the database to `sites/<site>/private/backups` so you can roll back.
- `git pull` fetches the new code. If it refuses because of local changes in `apps/fetch_einvoice`, run `git -C apps/fetch_einvoice status` and decide whether to keep them (`git stash`) or discard them before pulling again.
- `pip install -e` picks up any new Python dependencies. When `requirements.txt` hasn't changed it does nothing harmful.
- `bench migrate` updates the doctypes, runs any patches, re-applies the custom fields and clears the cache. Run it on every site that has the app, or use `bench --site all migrate`.
- `bench restart` makes the web and background workers load the new code. Until you restart, the workers that send to FETCH keep running the old code. In development, restart `bench start` instead.

Check the update took effect with `bench version`, which lists `fetch_einvoice` with its version number. Users with a Sales Invoice or Payment Entry form open should reload the page to get the new form scripts.

FETCH Settings, the FETCH Outbox and the FIRS codes are stored in the database, so an update keeps them.

To roll back, check out the previous version and migrate again:

```bash
git -C apps/fetch_einvoice log --oneline -5
git -C apps/fetch_einvoice checkout <previous-commit>
bench --site <site> migrate
bench restart
```

If the newer version changed data, restore the backup with `bench --site <site> restore <path-to-backup.sql.gz>`. To go back to tracking the latest code, run `git -C apps/fetch_einvoice checkout main`.

If you installed with `bench get-app`, `bench update --pull` also updates the app, but it updates Frappe and ERPNext too. Use the steps above to update only this app.

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

Payments wait until their invoice has its IRN. Payments for one invoice go oldest first, and a Failed payment holds back the later ones for that invoice until you retry it or cancel its Payment Entry. If FETCH never answered, a retried payment resends exactly what was sent before. If FETCH refused it, the retry works out the amount and status again. A return waits until the original invoice has its IRN, unless the original was submitted before this app was installed.

Cancelling an invoice or payment that FETCH has accepted shows a warning and then goes ahead. The NRS document stays valid, so issue a credit note if one is needed. The amended invoice goes to FETCH as a new invoice. Cancelling stops every call that has not reached FETCH yet, and adds a comment that lists them.

## Troubleshooting

### `KeyError: 'fetch_einvoice'` when opening FETCH Settings

The web server's list of apps doesn't include `fetch_einvoice`, although the app is installed in the database. The error report's **App Versions** then lists only `erpnext` and `frappe`. Frappe builds that list from `sites/apps.txt` and caches it.

1. From `frappe-bench`, run `cat -A sites/apps.txt`. Each app must be on its own line ending in `$`, and `fetch_einvoice` must be one of them. If it's missing, or joined to another app's line, fix the file.
2. Run `bench --site <site> migrate`. In v13, `bench clear-cache` does not refresh the cached app list, but `migrate` does.
3. Run `bench restart`, then reload the browser page. **App Versions** in an error report, or `bench --site <site> execute frappe.utils.change_log.get_versions`, now lists `fetch_einvoice`.

If the line disappears again, something rewrites `sites/apps.txt`. In a Docker setup based on frappe_docker, the configurator rewrites it from the image's `apps` folder each time the containers start, so the app has to be built into the image. See [Install on Docker](#install-on-docker-frappe_docker).

### Nginx 502 "connect() failed (111: Connection refused)" on Docker

The frontend's nginx looks up the `backend` address once, when it starts. When the backend container restarts and comes back on a different address, nginx keeps calling the old one and every page returns 502. Compare the backend's current address with the `upstream:` address in the nginx error:

```bash
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' <backend container>
```

If they differ, restart only the frontend with `docker restart <frontend container>`. If the backend container itself keeps restarting, read `docker logs --tail 50 <backend container>` instead.

### The scheduler or a worker container keeps restarting

Read its log with `docker logs --tail 50 <container>`. `ModuleNotFoundError: No module named 'fetch_einvoice'` means `sites/apps.txt` lists the app but that container's image doesn't contain it. Build the app into the image as described in [Install on Docker](#install-on-docker-frappe_docker).

## Known gaps

- Only Payment Entries are sent. Journal Entries, Payment Reconciliation and advances allocated later are not.
- Consolidated POS invoices, the ones ERPNext creates itself at POS closing, are neither checked nor sent. They are usually for walk-in customers without the TIN and contact details FETCH needs, and refusing them would stop POS closing.
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
