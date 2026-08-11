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

SMALL DETAILS (only if you care)
--------------------------------
- The updater uses the NEWEST posentine-*.zip in Downloads.
  If you have several, keep only the one you want installed.
- The bat refuses to run unless it is inside the live install
  folder: install\update_agent.ps1 AND config.json must be next
  to it. That refusal is a message, not a failure: it means the
  bat was double-clicked from the wrong place.
- Optional safety pin: in UPDATE_POSENTINE.bat you can paste
  the SHA-256 of the EXACT zip file on this machine into
  EXPECTED_SHA. If you do, the updater will REFUSE any zip
  that does not match it. To get the SHA of the downloaded
  file:
      Get-FileHash C:\Users\Techno\Downloads\posentine-xxxx.zip -Algorithm SHA256
  then paste the hash into the bat. IMPORTANT: every release
  is rebuilt, and every rebuilt file has a NEW SHA - so only
  paste the hash of the file that is actually on this machine.
  Leaving it empty is safe: the updater still verifies every
  file against the zip's own MANIFEST.txt before it installs.

HOW LONG DOES IT TAKE
---------------------
A few minutes. The updater deliberately waits for the Windows
scheduled task to fire one real cycle on its own (every 3
minutes) before it reports success - it never fakes that.
A SUCCESS screen means the new agent really ran once and
really confirmed with the cloud.

============================================================
