# Offline multi-model chatbot

A local chat page at **http://127.0.0.1:8080**, powered by Ollama and a Python server. It uses no CDN, account, API key, or paid service. The runtime and model files are stored inside the project folder.

## First launch on Windows

1. Install **Python 3.11 or newer** with the Windows Python launcher (`py`). No pip packages are required.
2. Clone this repository or download and extract its source ZIP into a writable folder.
3. Connect to the internet and double-click **start.bat**. It downloads the portable Ollama runtime and the three compact models below, then opens the chat page. Allow several gigabytes of free disk space for the runtime, models, and download cache.

The repository contains source code; runtime executables and model weights are downloaded during setup. After setup completes, installed models run without internet.

## Use it

1. Double-click **start.bat**. This is the only batch launcher you need. It sets up missing runtime/models if necessary, starts the model runtime, and opens the chat page.
2. Choose an installed model from the sidebar and type a message.
3. Use **New chat** to start another conversation, or **Stop** to cancel a reply.
4. Run **start.bat -Stop** to stop this project's chatbot and runtime.

After setup completes, these steps work with internet disconnected. The first reply after selecting a model may take longer while it loads. The runtime loads one model at a time and uses a 4,096-token context to limit memory use. Performance depends on your CPU, RAM, and GPU.

## Models

| Model tag | Model | Notes |
| --- | --- | --- |
| `qwen3:1.7b` | Qwen 3 1.7B | Downloaded by setup; default compact general-purpose and multilingual chat. Thinking is disabled for faster replies. |
| `llama3.2:1b` | Llama 3.2 1B | Downloaded by setup; compact text chat from Meta. |
| `gemma3:1b` | Gemma 3 1B | Downloaded by setup; compact text chat from Google. |
| `qwen3-4b-thinking-local:latest` | Qwen 3 4B Thinking 2507 | Optional GGUF import. Always reasons before answering; slower. |

To add the optional Qwen 4B Thinking model, place your `qwen3-4b-thinking-2507.Q4_K_M.gguf` file in the project root, alongside `start.bat`, then run **start.bat -Setup**. The file is not provided by this repository. Setup imports it when present; a fresh clone runs with the three compact models without this file.

The imported thinking model has a 4,096-token output budget. If it reaches this limit while reasoning, try a shorter question or choose the compact Qwen model. Large prompts and long conversations can exceed the configured 4,096-token context; start a new chat if the model loses earlier context. These are text chat models; the page does not accept image uploads.

Setup preserves any existing GGUF or llamafile executable in the project folder. These local assets are excluded from Git and are not provided by this repository. `start-original.txt` preserves the original launcher command for reference.

## Setup or reinstall

Windows and Python 3.11 or newer are required. The scripts detect Python through `py -3` or a regular `python` executable.

Double-click **start.bat** while connected to the internet if setup is missing. To explicitly repair or reinstall, run **start.bat -Setup**. Setup downloads the official portable Ollama release, verifies its SHA-256 against GitHub release metadata, installs the three compact models, and imports the optional Qwen GGUF if supplied. Runtime and model downloads use resumable chunks to handle unstable connections. Every model blob is verified against the official registry's SHA-256 before installation, including its template and license files. Setup can be rerun; verified files are reused. Normal launches check the local installation and skip setup, so installed models work offline. Keep `start.bat`, the supporting source files, `.runtime/`, and `.models/` together in the project folder.

To install additional **local** Ollama models after setup, run this in PowerShell from the project folder:

```powershell
. .\scripts\common.ps1
Start-ProjectOllama
& $OllamaExe pull 'qwen3:4b'
```

Refresh the model list in the page after downloading. Downloading new models requires internet; using installed models does not. Larger models may use system RAM and run more slowly. The installed-model selector excludes cloud models and the runtime is launched with `OLLAMA_NO_CLOUD=1`.

## Files and privacy

- `.runtime/ollama/`: portable Ollama installation.
- `.models/`: downloaded and imported model files.
- `.runtime/logs/`: local runtime and web-server logs.
- `web/`: all page assets, served locally.
- `app.py`: Python standard-library web server; no pip packages needed.
- Chat history is stored in this browser's local storage. Use the page's history controls or browser site-data settings to remove it. Server chat prompts are not persisted by this application.

Both servers listen only on `127.0.0.1`; they are reachable from this computer. Startup scripts do not create a Windows startup task, change firewall rules, or change system environment variables.

## Troubleshooting

- **Runtime unavailable:** run `start.bat`, then refresh the page. Check `.runtime/logs/ollama.stderr.log`.
- **No models installed:** run `start.bat -Setup` online and wait for downloads to finish.
- **Port already occupied:** close the other program or use `start.bat -Port 8081`.
- **Another Ollama installation uses port 11434:** quit that installation before launching this project's portable runtime.
- **A model's first reply is slow:** allow it to finish loading. The compact models are the fastest options.
- **Python was not found with `python`:** use `py -3`; the launcher scripts find the actual Python executable automatically.

Run the backend checks with:

```powershell
py -3 -m unittest discover -s tests -v
```

## Official references

- [Ollama for Windows](https://docs.ollama.com/windows)
- [Importing GGUF models](https://docs.ollama.com/import)
- [Local-only mode and runtime settings](https://docs.ollama.com/faq)
- [Qwen 3](https://ollama.com/library/qwen3:1.7b), [Llama 3.2](https://ollama.com/library/llama3.2:1b), [Gemma 3](https://ollama.com/library/gemma3:1b)

Models retain their upstream licenses; downloading or importing a model does not change its license.
