============================================================
 POSentine - ONE-CLICK UPDATE (operator instructions)
============================================================

WHAT THIS IS
------------
UPDATE_POSENTINE.bat updates the POSentine agent on this
machine to a new version. You do not need to know anything
about scheduled tasks, Python, or the code.

WHERE THE UPDATER MUST LIVE (first time only)
---------------------------------------------
UPDATE_POSENTINE.bat and the install\ folder must be placed
INSIDE the live install folder - the folder that contains
config.json and state.json. The bat looks for
install\update_agent.ps1 NEXT TO ITSELF, so a copy of the
bat sitting loose in Downloads (or anywhere else) cannot
work: it stops immediately with a clear message and changes
nothing.

To get the updater onto the machine:
  1. Put the new release zip into C:\Users\Techno\Downloads\
     and extract it (Windows: right-click > Extract All).
     You get a posentine\ folder.
  2. Copy these three files from the extracted posentine\
     folder INTO the live install folder:
         UPDATE_POSENTINE.bat      -> the live folder itself
         UPDATE_README.txt         -> the live folder itself
         install\update_agent.ps1  -> the live folder's install\
  3. Delete the extracted posentine\ folder you just made.
     It is only a delivery box - never run the agent from it.

WHAT YOU DO (every update after that)
-------------------------------------
1. Put the new zip file into:
       C:\Users\Techno\Downloads\
   The file looks like:  posentine-xxxxxxxxxxxxxxxxxxxx.zip
   (This folder is the only place the updater looks for zips.)

2. Double-click UPDATE_POSENTINE.bat - the one INSIDE the
   live install folder.

3. Wait. The updater shows what it is doing as it goes:
   checking the zip, backing up, stopping the old agent,
   installing the new code, testing it, starting the agent,
   waiting for one real cycle, confirming with the cloud.

4. Read the final screen:
       UPDATE SUCCESS
     - Done. The agent is running the new version.
     - Nothing else to do. You can close the window.

       UPDATE FAILED
     - Do NOT touch anything. Do NOT close windows you were
       not told to close.
     - Send this file to support:
           logs\updater.log
     - A copy of the previous code was saved in:
           _backup\
       Support may ask you to send that too.

WHAT IT NEVER TOUCHES
---------------------
The updater NEVER overwrites or deletes these, on purpose:
   config.json   (the connection settings)
   state.json    (where the agent remembers how far it read)
   agent.log     (the agent's diary)
   logs\         (install records, including updater.log)
   the POS database and the cashier software

So updating does NOT lose any settings, history, or data.
You do not need to back anything up yourself, and you must
not delete or rename the install folder during an update.

YOUR BACKUPS AND config.json (how secrets are handled)
------------------------------------------------------
Every update saves a snapshot of the previous code into
_backup\<timestamp>\, so a failed update can put the machine
exactly back. Your connection settings file config.json is
NEVER copied into that backup: it holds the SQL password and
the agent token, and those must not sit at rest in a folder.
Instead the updater records only the file's fingerprint
(config.json.sha256) and checks during the update that the
file has not changed. If it has, the update stops - loudly,
never silently. The settings themselves stay where they are.

SMALL DETAILS (only if you care)
--------------------------------
- The updater uses the NEWEST posentine-*.zip in Downloads.
  If you have several, keep only the one you want installed.
- The bat refuses to run unless it is inside the live install
  folder: install\update_agent.ps1 AND config.json must be next
  to it. That refusal is a message, not a failure: it means the
  bat was double-clicked from the wrong place.

THE RELEASE PIN (EXPECTED_SHA) AND HOW RELEASES ARE BUILT
---------------------------------------------------------
UPDATE_POSENTINE.bat carries one optional knob:
   EXPECTED_SHA  - the SHA-256 of the EXACT zip the operator
                   downloaded, pasted into the bat. If set,
                   the updater REFUSES any zip that does not
                   match it - before anything is touched.

The release chain, so the numbers always line up:

   SOURCE (this repository)
     -> BUILD   (python make_ship.py --zip)
     -> ZIP     (posentine-<commit>.zip)
     -> SHA-256 (of that exact zip)
     -> EXPECTED_SHA pinned in UPDATE_POSENTINE.bat
     -> verified by the updater before it stops the agent

How the pin is set when a release is built:
   1. Build the release:      python make_ship.py --zip
   2. Hash the built zip:     Get-FileHash posentine-<commit>.zip -Algorithm SHA256
   3. Paste that hash into    EXPECTED_SHA=  in UPDATE_POSENTINE.bat
   4. Commit the pinned bat together with the release notes.

One honest subtlety: UPDATE_POSENTINE.bat is itself a file
INSIDE the zip, so the zip cannot contain a hash of its own
bytes - pinning the bat changes the zip. Therefore the pin
names the release artifact that was BUILT and VERIFIED, and a
REBUILD of the same source produces a new zip with a new SHA.
That is expected. If you (or an operator) ever rebuild or
re-zip the release by hand, the old pin will (correctly)
refuse it: re-run steps 1-4 with the new file. The same rule
applies on the machine: only paste the SHA of the file that
is ACTUALLY on that machine.

Leaving EXPECTED_SHA empty is still safe: the updater then
verifies every file the zip carries against the zip's own
MANIFEST.txt before installing anything - the stronger gate
of the two.

WHY THE AGENT PAUSES WHEN THE TILL IS LOGGED OUT
------------------------------------------------
The scheduled task runs "only while this user is logged on"
- that is a deliberate design choice (it needs no stored
password and no administrator rights). If the till is logged
out or asleep, cycles stop and resume at the next login. No
data is lost - the watermark only ever moves forward - but
the next shift report will say the shift's data is INCOMPLETE
instead of pretending it is complete. That orange INCOMPLETE
banner is the system being honest, not a fault: incomplete
data must never be presented as complete.

HOW LONG DOES IT TAKE
---------------------
A few minutes. The updater deliberately waits for the Windows
scheduled task to fire one real cycle on its own (every 3
minutes) before it reports success - it never fakes that.
A SUCCESS screen means the new agent really ran once and
really confirmed with the cloud.

============================================================
