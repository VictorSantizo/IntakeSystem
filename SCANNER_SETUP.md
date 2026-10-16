# Scan Straight Into Klick Klack — Per-Computer Setup

One-time setup for each office computer with a ScanSnap. After this, scanning
into Klick Klack is: load pages → tap **Klick Klack** on the scanner's screen →
press **Scan**. The document appears in Klick Klack within seconds, already
read by the AI, with a suggested filename and filing folder you can adjust.

The **Scan** page in Klick Klack (top menu) shows this computer's readiness
live and walks through these same steps — start there if unsure.

## Step 1 — Connect this computer (once)

1. In Klick Klack, open the **Scan** page (top menu) and click
   **Download "Connect My Scanner"**. (The same file also lives on the server
   at `\\LACKSSERVER2021\File-Cabinet\IntakeSystem` — that full path always
   works; drive letters like `T:` differ between desks, so this guide avoids
   them.)
2. Run the downloaded file — it lands in **Downloads**. If Windows asks
   whether to run it, choose **Run**; it comes from Klick Klack's own server.
3. A black window says *Connected!* — close it. Done. (This starts the scan
   connector and makes it start again automatically every time you sign in.
   It only *reports* scanner status to Klick Klack; it never moves files.)

## Step 2 — Add the "Klick Klack" scan profile (once)

> **Shortcut for every machine after the first:** if a file named
> `Klick Klack Profile.*` already exists in
> `\\LACKSSERVER2021\File-Cabinet\IntakeSystem`, you don't need
> to build the profile by hand — in ScanSnap Home open the Scan window, click
> the **… menu → Import/Export profiles → Import from a file**, browse to that
> file, tick it, **Import**. Done — skip the rest of this step. (Connect My
> Scanner even opens the file for you when it sees the profile is missing.)
>
> **If the window says "Select data to import" and wants an `Export file
> (*.sswa)`** — that's the WRONG window: it's ScanSnap Home's whole-computer
> data-migration/restore wizard, not the profile importer (any version shows
> it, even the newest). Close it and take the exact path above: **Scan
> window → the small "…" button (top right, next to "Edit profiles") →
> Import/Export profiles → Share profiles… → Import profiles to the scanner
> from a file** — that browser reads `Information file of profiles (*.sswp)`
> and accepts our file. Still stuck? Build the profile by hand below —
> 2 minutes on any version.
>
> **And on the FIRST machine only:** after finishing this step, please also do
> **Edit → Import/Export profiles → Export**, select the Klick Klack profile,
> and save it as `\\LACKSSERVER2021\File-Cabinet\IntakeSystem\Klick Klack Profile` — that's what makes
> every other machine's setup a three-click import.

1. Open **ScanSnap Home** (Start menu → type "ScanSnap Home").
2. Click the **Scan** button (top-left of the main window).
3. Click **+** (Add profiles) — choose the **"Save documents"** template.
4. Change the profile name to exactly: **Klick Klack**
5. Under **Manage options / Save to**, change *Type* to
   **"PC (Scan to folder)"** and set the folder to:
   `\\LACKSSERVER2021\File-Cabinet\IntakeSystem\Unfiled Scans`
   (type the full path — drive letters differ between desks)
6. Leave Feed on **"Normal scan"** (Continuous merges separate batches into ONE
   file — not what we want). Color/quality defaults are fine, with one
   exception: **Detailed settings → Scanning → uncheck "Blank page removal"** —
   it has eaten real pages before, and the office writes/stamps the received
   date on the BACK of the last sheet, which the scanner may see as "blank".
7. Click **Add** to save. The profile appears as a tile on the iX1600's
   touchscreen within a few seconds.

> The profile must save into
> `\\LACKSSERVER2021\File-Cabinet\IntakeSystem\Unfiled Scans` — that's the
> folder the server watches. Anywhere else and the scan stays on this computer.

## Step 3 — Try it

1. In Klick Klack, open the **Scan** page — all four checks should be green.
2. Put a page in the scanner, make sure the **Klick Klack** tile is selected
   on the scanner's screen, press the blue **Scan** button.
   *(Scanner without a touchscreen — e.g. an iX500? Select **Klick Klack** in
   ScanSnap Home's Scan window once; the scanner's physical button then scans
   with whichever profile was last selected.)*
3. Within a few seconds the page jumps to the document's review form —
   check the details, adjust the **File as** name/folder if you want,
   **Confirm & Log**.

## If something's red on the Scan page

| Check | Fix |
|---|---|
| Scan connector | Re-run **Connect My Scanner** (Step 1). |
| ScanSnap plugged in | USB cable into this computer; open the feed tray so the scanner wakes. |
| ScanSnap Home running | Start **ScanSnap Home** once — its background part stays on after that. |
| Klick Klack profile | Do Step 2. The check may take a minute to notice a new profile. |

Scanner shared between desks? Each computer that the ScanSnap gets plugged
into needs Step 1 + Step 2 once. Documents always land in the same Review
Queue either way, whoever scans them.
