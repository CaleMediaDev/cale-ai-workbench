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

## Live cameras

The Print Farm page shows continuous camera video at four frames per second when an optional RTSP or HTTP video URL is configured, and otherwise polls the JPEG snapshot URL. While the page is visible, a separate read-only AI observer scans available cameras in turn, including busy printers. It shows the observation, confidence and age. Enable **AI overlay** to see detected object boxes on the exact inspected frame, or choose **Enlarge** for a bigger view. Preview images continue refreshing while inference runs; inference is serialized with dispatch AI to avoid concurrent model calls. Frozen or unavailable feeds are visibly held. These single-frame observations do not supply bed-clear tickets or authorize motion; dispatch still requires its own fresh two-frame check.

Video decoders use the existing bundled FFmpeg and stop when the page closes or is hidden. The local relay protocol is documented in the [go2rtc API documentation](https://github.com/AlexxIT/go2rtc/blob/master/pkg/README.md). Detection boxes are approximate model observations and are drawn only on the inspected frame.

## Performance and layout

Camera cards and image buffers are reused across status updates. The page uses a scrollable, responsive one/two/four-column camera grid. Decode/preview resizing runs off the UI thread. Display-only AI scans adapt to image changes: changed scenes are eligible after 8 seconds, busy printers after 20 seconds, and unchanged idle scenes after 45 seconds. These are minimum intervals; actual timing depends on model availability. Pending queue inference takes priority over the next display scan. Hidden/minimized views stop video work. Every dispatch still requires an independent fresh two-frame bed check.

Completion reconciliation binds the new history entry seen while the exact job is running, so printers with an incorrect internal clock can still complete the queue entry. It never clears a bed lock without a new camera check.
