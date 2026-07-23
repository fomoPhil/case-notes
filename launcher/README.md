# Debrief launcher

Turns Debrief into a double-clickable Mac app with a real Dock icon. One click
boots everything and opens the UI in its own chromeless window.

## What's here

- `debrief-launch.sh`: the brain. Runs the friendly startup checks, opens the window, then shows an optional one-time Obsidian nudge.
- `build_app.sh`: builds `Debrief.app` at the repo root (a thin wrapper around the launcher).
- `Debrief.icns`: the app icon (generated from `design/app-icon.png`).
- `server.log`: where the app server's output goes when the launcher starts it.

## What the app does when you open it

1. **Checks the AI engine (LM Studio).** If it isn't running, starts it and waits.
   If LM Studio isn't installed at all, a dialog offers an **Open Download Page**
   button (takes you to lmstudio.ai) or **Not Now**.
2. **Checks the AI model.** Makes sure `gemma-4-12b-it-qat` is loaded with a big
   enough memory (64k context). Loads or reloads it if needed (~a minute). If the
   model can't load (usually because it hasn't been downloaded), a dialog offers a
   **Get the Model** button that opens LM Studio and tells you exactly what to
   search for, or **Not Now**.
3. **Checks the Debrief app server.** If it's already running, it is left
   completely alone. If not, it starts it in the background.
4. **Opens the window.** A clean, chromeless Chrome app window pointed at Debrief
   (falls back to your default browser if Chrome isn't installed).
5. **Optional Obsidian nudge (after the window is open).** If Obsidian isn't
   installed, a gentle, non-blocking dialog mentions it as an optional way to
   browse your vault visually, with an **Open Download Page** button (obsidian.md)
   or **Skip**. This never blocks Debrief from opening, and it's only shown once
   ever.

If a required step can't complete, you get a calm, plain-language dialog telling
you what to do, and where it helps, a button that takes you straight to the right
place (the LM Studio download page, LM Studio's model search, or the Obsidian
download page). You never see a raw technical error.

## First run: the in-app setup wizard

On first launch, Debrief opens a short **setup wizard** in the window itself. It
checks the AI model server, shows where your vault lives, and walks you through
the three macOS permissions (**Calendar**, **Mail**, **Screen Recording**) with a
**Grant** button that shows the system prompt so you can click **Allow**. The
launcher no longer explains permissions; the wizard owns that. You can reopen it
any time from **Setup** in the sidebar, and `debrief-doctor` in the Terminal
prints the same model and vault checks.

Screen Recording needs the app to be quit and reopened after you allow it. The
wizard says so on that step.

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
