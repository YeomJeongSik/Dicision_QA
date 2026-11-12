# Unreal Auto QA

When the user asks to test Unreal gameplay, use the packaged Win64 Development game with GameQABridge enabled. Use the game executable the user supplied or the `executable` field in the matching project's `runs/game-builds/*.last-good.json`. If only an Unreal project is available, follow the README to build its QA package and use the returned executable path. Ask for the project or executable only when the target cannot be identified.

Run from this repository with Python 3.10 or later. Translate the request into a short gameplay objective and pass it as one argument.

```powershell
python -B qa.py ai-test "<game executable>" --objective "Verify movement and jumping separately." --check IA_Move --check IA_Jump
```

Use one `--check` per requested action. Join up to 12 action IDs with `+` for simultaneous input, as in `--check IA_Move+IA_Jump`. If action IDs are unknown, run `python -B qa.py list-actions "<game executable>"` to discover them without a paid call. Report unsupported objectives as unverified.

Decisions chooses among the pending checks that Unreal can currently execute. The default call cap equals the number of checks. Use the compact CLI output to report each check's verdict and any unverified checks. Open the detailed JSON report only when you need to investigate a result. Avoid repeating an unchanged test.

Use the local key registered by the user. If the CLI reports `API_KEY_MISSING`, ask the user to run `Register-OpenAI-Key.cmd`.
