# Nightshift desktop design QA

Final result: passed for the native desktop adaptation.

## Evidence and scope

The user selected the first displayed concept, `exec-f16ff6bb-ec8b-4081-a0b6-646c2391a616.png`. It is a three-screen presentation board, not three full-size production viewports. Actual application widgets were exercised at 1440x1024 and 1050x740. Captures used the owned Tk window handle, not a desktop crop. The isolated fixture used a copied existing game capture and two temporary project rows; model calls, installers, microphone, downloads and external processes were disabled. These screenshots prove layout and navigation, not fresh model-generated task success.

## Findings fixed

- The first Create layout lost its action button on a short display. The composer/action now claim bottom space before history; both Home Send and Create Build it remain inside 1050x740.
- Narrow studio content exceeded the available page height. One outer auto-hiding Studio scrollbar keeps every control reachable without nested studio scrollers; the footer was reached in the actual window.
- A Home file selection could be inherited by a dedicated build/song request. Those requests explicitly exclude Home attachments and preserve the unsent Home selection/draft.
- A failed start could clear the build prompt. The shared pipeline now acknowledges acceptance; rejected/setup-blocked requests retain their text. Real pipeline failure and readiness-change regressions passed.
- Selecting another project could retain the first project's picture. The previous image is cleared on selection, missing image or decode failure. Finished HTML projects can request a capture while Create is visible. Hidden Create does not start preview browsers.
- A song launch acknowledgement could be overwritten by radio polling. Make a song now has a separate persistent success/error notice. Radio-only Vocals is hidden in song mode.

## Fidelity surfaces

- Typography: existing Segoe UI, clear display/body hierarchy and readable native controls. Body text is generally 11pt. Existing Windows font rendering is retained.
- Layout: graphite base, cyan/coral dual decks, common Home/Music Studio/Create navigation, chat companion/task rows and a dedicated creation preview. Navigation joins the brand bar on wide screens and wraps to one row on narrower PCs.
- Colors: dark graphite surfaces, cyan active navigation/deck B, coral Supercharge/deck A, quiet secondary text and visible focus states.
- Assets: real Cale logo and existing animated male beach portrait retained. Jogwheel is an Image Gen raster. Waveforms use actual PCM audio, never invented traces; unloaded/unsupported audio shows an explicit empty state.
- Copy/content: user-facing page names and actions are plain language. Accepted song requests point to Home/My tasks. Game/app/website requests use the existing task pipeline; preview opens the actual local project for interaction.

## Intentional adaptation and limits

Account/donation/readiness/provider controls and all five requested hardware gauges remain available. The existing male Cale portrait replaces the concept's illustrative female placeholder. Native controls are flatter than the glossy concept. Live EQ is unavailable and is labelled accordingly; master/deck levels, manual crossfading and Auto-DJ are real controls. The embedded view is an updating capture; clicking opens the working project. No claim of an embedded interactive browser, new music-engine quality, a new generated game, general task completion, or full fresh-PC installation is made.

## Verification

- Affected desktop/radio/preview/updater modules: 275 passed, 3 skipped, 6 subtests, 69.53 seconds before the final song-notice-only addition.
- Final song/vocals/callback selection: 9 passed, 1 Tk initialization skip; the author's final dedicated three Tk cases also passed individually.
- Real Windows owned two-deck near-silent MCI playback smoke passed, including overlap, manual gain and Auto-DJ continuation.
- Actual full desktop fixture navigation, project image isolation, finished preview scheduling and narrow action/footer reachability passed without external processes.

P3 follow-up: further native button/material polish and an independently tested real-time EQ backend. No unresolved P0/P1/P2 layout or core-action issue was found in the inspected states.
