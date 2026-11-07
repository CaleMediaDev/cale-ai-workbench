# Print Farm

Open **Print Farm** in Cale AI, click **Connect eBay account**, add each K1 SE printer and its JPEG bed camera, select its loaded PLA color, then start automatic printing. Keep Cale and the PC running. Each eBay connection has its own queue and Windows-protected device credential.

Paid, unfulfilled sales import every 60 seconds. A local vision model matches saved files that have completed-print history or an existing approved mapping. File confidence strictly above 50% permits automatic dispatch; 50% or below presents one **OK** button. A fresh independent camera check must see the complete bed clear before every start. Removal of a finished part allows the next queued job after that check. Pausing blocks new starts and leaves active prints running.

This driver supports Creality K1 SE printers using their LAN API and PLA color settings. It can copy verified identical G-code to another compatible K1 SE with the same color. Other printer models require their own driver. Configure a current JPEG camera URL and a local Ollama vision model (default `cale-vision:latest`). Offline, obscured, stale, or occupied camera views hold the queue.

Dispatch reservations, file hashes, printer identity checks, current eBay order checks, and persistent submission records prevent blind retries and duplicate prints. An uncertain submission or failed print needs inspection. An OK approves the selected file; it never bypasses an occupied-bed check. Historical sales imported before enabling automation require one review unless already covered by an approved mapping.

Private settings, cached G-code, credentials and queue databases live in `PrintFarm/`, excluded from releases. The existing owner's connection is configured only on that installation, never bundled for others.

## Hosted account connection

`Server/cale-print-connect.php` is the WordPress MU plugin for the owner's direct OAuth broker. Its private config and encrypted refresh tokens live outside the web root in `.cale-print-connect/`, with restricted filesystem permissions. Production credentials are not in this repository. The broker uses only the eBay fulfillment read scope. Desktop connections use a random device verifier protected by Windows DPAPI; access tokens expire normally and refresh through the broker.

## Validation

Run `python -m unittest -q Validation.test_print_farm Validation.test_long_tasks`. Tests cover the confidence boundary, single OK action, fresh unobstructed-bed gating, cancellation, account changes and pause during inference, durable reservation, duplicate prevention, and uncertain submissions.
