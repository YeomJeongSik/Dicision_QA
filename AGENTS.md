# Unreal Auto QA

When the user asks for Unreal gameplay QA with this tool, run `Project-QA.ps1` from this tool folder. It finds the parent Unreal project and its installed engine and prepares or reuses an isolated QA build.

1. Run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Project-QA.ps1 -ListActions` to get the action-to-key map without a Decisions call. The tool reuses the build's startup mappings. Use `-ListActions -RefreshActions` if the active mappings differ.
2. Match the user's gameplay objective to the listed action IDs. Run the test with a short objective and one check per requested behavior.

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -Command '& .\Project-QA.ps1 -Objective "Verify movement and jumping separately." -Check IA_Move,IA_Jump'
   ```

   Use the listed `Action@Key` variant for a specific key or movement direction. Join action IDs with `+` for simultaneous input. Report unsupported objectives as unverified.
3. Report each result from the compact CLI output. `INPUT_CONFIRMED` confirms input action activation, with gameplay outcome unverified. The default Decisions call cap equals the number of checks. Read the detailed report when diagnosing an error or inconclusive result.

If the tool reports `API_KEY_MISSING`, ask the user to run `Register-OpenAI-Key.cmd`. Never request the key in chat.
