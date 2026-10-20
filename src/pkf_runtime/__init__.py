"""pkf_runtime — the code that runs inside Airflow tasks: credentials, sources, destinations, staging, and the
package operations transformations are built from. Installed on every Airflow worker; never imports `pkf`."""
VERSION = "0.8.2"
