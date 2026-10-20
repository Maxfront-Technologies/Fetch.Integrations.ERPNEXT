# The frappe/erpnext image with this app inside, for frappe_docker. Every container (backend, workers, scheduler,
# websocket, frontend, configurator) must run this image: each one imports the apps listed in sites/apps.txt.
ARG ERPNEXT_IMAGE=frappe/erpnext:v13.52.15
FROM ${ERPNEXT_IMAGE}

COPY --chown=frappe:frappe . /home/frappe/frappe-bench/apps/fetch_einvoice

# The configurator service rewrites sites/apps.txt from this folder listing on every start.
RUN cd /home/frappe/frappe-bench \
	&& env/bin/pip install --no-cache-dir -e apps/fetch_einvoice \
	&& ls -1 apps > sites/apps.txt
