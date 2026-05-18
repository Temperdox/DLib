# DLib Library Overlay — browser extension

Overlays your DLib library state (DOWNLOADED / IN LIBRARY / RUNNING / FAVORITE / BAD)
on F95Zone and DLsite game links and pages. Talks to the local DLib server
at `http://127.0.0.1:8000`.

## Install — Chromium browsers (Opera GX, Chrome, Edge, Brave)

1. Make sure DLib is running.
2. Open `opera://extensions/` (or `chrome://extensions/`, `edge://extensions/`).
3. Toggle **Developer mode** on (top-right).
4. Click **Load unpacked** and pick this `extension/` folder.
5. The DLib icon appears in your toolbar. Click it to confirm the server
   is reachable.

Updates after editing files: click the circular **reload** button on the
extension's card in `…/extensions/`, then refresh any open F95/DLsite tabs.

## Install — Firefox

Two paths depending on whether you want it to survive a browser restart.

### Quick (temporary, gone after restart)

Works on stable Firefox 121+ unchanged.

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on…**.
3. Select `manifest.json` inside this `extension/` folder.
4. The extension runs until you close Firefox. Reload it the same way
   after a restart.

### Permanent (persistent install, no AMO)

Requires either Firefox **Developer Edition**, **Nightly**, or **ESR** —
stable Firefox release will reject unsigned extensions.

1. In `about:config`, set `xpinstall.signatures.required` to `false`.
2. Zip the contents of this `extension/` folder (the zip's root must
   contain `manifest.json` directly — not an `extension/` subfolder).
   Rename the `.zip` to `.xpi`.
3. Drag the `.xpi` onto Firefox and confirm the install prompt.
4. The extension persists across restarts.

If you want it on regular stable Firefox forever, the only path is to
submit a signed build through [AMO](https://addons.mozilla.org/) — that's
overkill for a personal local tool.

## Verify it works

1. Open the popup (toolbar icon) — should say *Connected — DLib v0.1.6*
   and show your library count.
2. Visit `https://f95zone.to/latest_alpha/#/cat=games` or any DLsite
   browse page — game cards in your library get a colored banner across
   the top of each row.
3. Visit a specific thread/product page for a game that's in your
   library — a sticky banner appears at the top of the page, colored by
   state (green for downloaded, blue for in-library-not-installed, red for
   bad, pastel rainbow for favorite). Top-right of the cover image has
   action buttons (Mark/Unmark favorite, Mark/Unmark bad, Add to library).

## How it stores data

* The **service worker** (`background.js`) owns an IndexedDB cache so
  pills survive DLib being offline.
* When DLib is unreachable, cached pills keep their colors but get a
  dashed border + "·" prefix so you know they're stale.
* The popup has a **Clear cache** button if you ever want to reset that
  state.

## Privacy

The extension makes HTTP calls **only to `http://127.0.0.1:8000`**. The
F95Zone / DLsite host permissions exist so the content script can read
the page DOM — no requests are sent to those origins by the extension
itself.
