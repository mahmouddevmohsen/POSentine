============================================================
 POSentine - ONE-CLICK UPDATE (operator instructions)
============================================================

WHAT THIS IS
------------
UPDATE_POSENTINE.bat updates the POSentine agent on this
machine to a new version. You do not need to know anything
about scheduled tasks, Python, or the code.

WHAT YOU DO
-----------
1. Put the new zip file into:
       C:\Users\Techno\Downloads\
   The file looks like:  posentine-xxxxxxxxxxxxxxxxxxxx.zip
   (This folder is the only place the updater looks.)

2. Double-click UPDATE_POSENTINE.bat.

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
not delete or rename the posentine folder during an update.

SMALL DETAILS (only if you care)
--------------------------------
- The updater uses the NEWEST posentine-*.zip in Downloads.
  If you have several, keep only the one you want installed.
- On the FIRST update after the updater itself is installed,
  put UPDATE_POSENTINE.bat and the install\ folder next to
  agent.py (in the posentine folder), like the other scripts.
- Optional safety pin: in UPDATE_POSENTINE.bat you can paste
  the SHA-256 of the EXACT zip file on this machine into
  EXPECTED_SHA. If you do, the updater will REFUSE any zip that
  does not match it. To get the SHA of the downloaded file:
      Get-FileHash C:\Users\Techno\Downloads\posentine-xxxx.zip -Algorithm SHA256
  then paste the hash into the bat. IMPORTANT: every release is
  rebuilt, and every rebuilt file has a NEW SHA - so only paste
  the hash of the file that is actually on this machine (the
  first build of this release was b28a5f57...; that value matches
  only that one file).
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
