# Setup on Windows — step by step

## 1. Install Python

Download Python **3.11 or newer** from <https://www.python.org/downloads/>.

During installation tick **"Add python.exe to PATH"**, at the bottom of the
first screen. If you forget it no command will work, and reinstalling is faster
than fixing it by hand.

Check by opening **PowerShell** (Windows key, type `powershell`):

```powershell
python --version
```

It must answer `Python 3.11.x` or higher. If it says 3.10 or lower the game
will not start: it uses `.toml` files, which require 3.11.

## 2. Create the folder

Lay the files out exactly like this, for example in `C:\Users\YourName\agiko`:

```
agiko\
├── README.md
├── SETUP-WINDOWS.md
├── config.py
├── theme.py
├── world.py
├── orders.py
├── observation.py
├── agents.py
├── roster.py
├── run.py
├── tournament.py
├── serve.py
├── db.py
├── ui.html
├── test_diplomacy.py
├── test_fairness.py
├── roster\
│   ├── aggressor.toml
│   ├── expansionist.toml
│   ├── scout.toml
│   ├── fortress.toml
│   ├── local.toml
│   ├── merchant.toml
│   └── opportunist.toml
└── replays\
    ├── demo-betrayal.jsonl
    └── demo-ai-center.jsonl
```

The seven `.toml` files go in a **`roster` subfolder**. This is the most
common mistake.

## 3. Try it without spending anything

```powershell
cd C:\Users\YourName\agiko
python run.py --agents red=scripted,blue=scripted,green=scripted
```

Do you see the map moving in the terminal? The engine works. `Ctrl-C` to stop.

If you see odd characters like `←[91m`, you are using the old `cmd.exe`: open
**Windows Terminal** (free in the Microsoft Store) or add `--no-render`.

## 4. Set the API key

In PowerShell, **for this window only**:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-your-key"
```

To make it permanent (then **close and reopen** PowerShell):

```powershell
setx ANTHROPIC_API_KEY "sk-ant-your-key"
```

Check that it is there:

```powershell
echo $env:ANTHROPIC_API_KEY
```

If you use the old `cmd.exe` the syntax is `set ANTHROPIC_API_KEY=sk-ant-...`.

## 5. Start the interface

```powershell
python serve.py
```

The browser opens at `http://127.0.0.1:8765`.

If Windows asks for a firewall permission you can refuse: the server only
listens on your own computer and does not need network access.

## 6. First real game

In the **Game** tab put `aggressor` on red, `merchant` on blue, `fortress` on
green. Duration 800 ticks. Start.

It costs about half a dollar. When it ends go to **Replay**, pick the game
and read the **decision diary**.

---

## Common problems

**`python is not recognized`** — you did not tick "Add to PATH". Reinstall
Python and tick the box.

**`ModuleNotFoundError: No module named 'config'`** — you are running the
command from the wrong folder. `cd` into the `agiko` folder, where `run.py`
is.

**`Agent 'aggressor' not found`** — the `.toml` files are not in the `roster`
subfolder.

**`Agent files are TOML: use Python 3.11 or newer`** — you have an old
version of Python, or more than one installed. Try `py -3.11 run.py ...`.

**`environment variable ANTHROPIC_API_KEY is not set`** — if you used
`setx`, you need to close and reopen PowerShell.

**The game starts but one faction does nothing** — look at the log panel: if
it says `!! agent 'red' is not responding: HTTP 401`, the key is wrong.

**Antivirus or SmartScreen blocks the script** — there is nothing to install
and no executable: they are just plain-text `.py` files, which you can read.
