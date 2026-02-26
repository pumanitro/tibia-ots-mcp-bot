# Crash Report Skill

Investigate a game crash, document it in the crash report, identify root cause, and attempt a fix.

## Step 1: Read Crash Logs

Read the following log files to gather crash data. Read ALL of them — crash info may be spread across multiple files:

```
dll/dbvbot_crash.txt     — DLL crash handler output (VEH entries, registers, stack)
dll/dbvbot_debug.txt     — DLL debug log (scan timing, creature counts, map validation)
dll_bridge_debug.txt     — Python DLL bridge log (HP updates, creature sync)
```

Focus on the **most recent entries** (bottom of files). Look for:
- `[CRASH]` entries with register dumps (EIP, ESP, EAX, etc.)
- VEH recovery entries
- Scan failures or map validation errors
- Disconnect indicators
- Timestamps to correlate events across logs

## Step 2: Analyze the Crash

Determine:

1. **Crash location**: Convert EIP to RVA (Relative Virtual Address — offset from game base). Game base is typically `0x00400000`. RVA = EIP - base.
2. **Which thread crashed**: Check ESP value:
   - `0x0019xxxx` range = main game thread
   - `0x105xxxxx` range = DLL scan thread
   - Other = unknown thread (possibly game worker thread)
3. **Crash type**:
   - Null pointer dereference (EAX=0, accessing [EAX+offset])
   - Wild pointer (EIP at nonsensical address like ASCII text)
   - Stack corruption (ESP outside normal range)
   - Access violation during tree walk (creature scan race condition)
4. **Root cause hypothesis**: Based on crash location, thread, and surrounding debug log context
5. **Was VEH recovery attempted?**: Check if `setjmp/longjmp` guard was active (`g_scan_recovery` state)

## Step 3: Check DLL Version

Check if the user is running the latest DLL:
- Read `dll/dbvbot.cpp` to see current version number
- Check crash log for DLL version indicators
- If old DLL is loaded, note this — many crashes are fixed in newer versions but require game restart to take effect

## Step 4: Document in Crash Report

Read the existing crash report at `memory/game_crash.md`, then append a new entry following the existing format:

```markdown
### Crash N — [Brief description] (YYYY-MM-DD ~HH:MM)

**Logs:**
- [Paste relevant crash/debug log excerpts]

**Analysis:**
- Thread: [main/scan/unknown]
- RVA: +0xNNNNNN
- Cause: [description]
- VEH recovery: [yes/no/not applicable]

**Fix N** (if applicable):
- [Description of code change]
- File: [file path]
- Status: [applied/pending game restart/needs investigation]
```

## Step 5: Attempt Fix (if possible)

If the crash has a clear root cause that can be fixed in the DLL or Python code:

1. Read the relevant source file (`dll/dbvbot.cpp` for DLL crashes, or Python files for proxy/action crashes)
2. Implement the fix
3. If DLL was modified, rebuild:
   ```bash
   PATH="/c/mingw32/bin:$PATH" g++ -shared -o dll/dbvbot.dll dll/dbvbot.cpp -lkernel32 -luser32 -static -s -O2 -std=c++17
   ```
4. Document the fix in the crash report
5. **IMPORTANT**: Remind the user they must close the game completely and restart to load the new DLL

If the crash cannot be fixed (e.g., unknown thread, unclear cause, or already fixed in newer DLL):
- Document this in the crash report
- Note whether the user needs to restart the game to pick up existing fixes

## Step 6: Summary

Provide a concise summary to the user:
- What crashed and why
- Whether a fix was applied
- Whether game restart is needed
- Any patterns noticed (e.g., floor changes triggering crashes, specific creature counts)
