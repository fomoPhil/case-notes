# Debrief launcher

Turns Debrief into a double-clickable Mac app with a real Dock icon. One click
boots everything and opens the UI in its own chromeless window.

## What's here

- `debrief-launch.sh` — the brain. Runs four friendly startup checks and opens the window.
- `build_app.sh` — builds `Debrief.app` at the repo root (a thin wrapper around the launcher).
- `Debrief.icns` — the app icon (generated from `design/app-icon.png`).
- `server.log` — where the app server's output goes when the launcher starts it.

## What the app does when you open it

1. **Checks the AI engine (LM Studio).** If it isn't running, starts it and waits.
2. **Checks the AI model.** Makes sure `gemma-4-12b-it-qat` is loaded with a big
   enough memory (64k context). Loads or reloads it if needed (~a minute).
3. **Checks the Debrief app server.** If it's already running, it is left
   completely alone. If not, it starts it in the background.
4. **Opens the window.** A clean, chromeless Chrome app window pointed at Debrief
   (falls back to your default browser if Chrome isn't installed).

If any step can't complete, you get a calm, plain-language dialog telling you what
to do (for example: "Debrief could not find its AI model. Open LM Studio, then try
again."). You never see a raw technical error.

## First run: a one-time permission step (important)

The very first time the **Debrief.app icon** starts the app server itself, macOS
will show up to **three permission prompts** — for **Calendar**, **Mail**, and
**Screen Recording**. This is normal and only happens once. Just click **OK** on
each. macOS asks because the app has its own identity, separate from the Terminal.

**How to avoid the prompts entirely on demo day:** start the server once from the
Terminal (see below). The launcher detects an already-running server and will
**not** restart it, so the app keeps using the Terminal's already-granted
permissions and no prompts fire.

## Recommended demo-day flow

1. **In Terminal, once**, start the server so it inherits your proven permissions:

   ```bash
   cd /Users/philwoolley/Projects/gemma4hackathon-casenotes
   HF_HUB_OFFLINE=1 .venv/bin/python app.py
   ```

   (Leave that Terminal window running.)

2. **Double-click `Debrief.app`** (in the repo root, or drag it to the Dock).
   It sees the server is already up, does the AI checks, and opens the window.
   No permission prompts, no surprises.

That's it: the Terminal holds the permissions, the Dock icon gives you the clean
one-click experience.

## Rebuilding

If you regenerate the icon or edit the launcher, rebuild the bundle:

```bash
bash launcher/build_app.sh
```

The bundle identifier is stable (`com.meora.debrief`), so your one-time macOS
permission grants survive rebuilds.
