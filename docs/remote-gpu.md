# Remote GPU: use the graphics card of another computer

Edit on your laptop, let the gaming PC do the work. OpenFill (in GIMP or PhotoCraft) stays on the computer you edit on. ComfyUI and the models run on another computer of yours, the one with the good graphics card. Your picture goes to that computer and the result comes back, nothing else leaves your own machines.

In this guide the **GPU computer** is the one with the graphics card, and the **editor computer** is the one you edit on.

## Pick a way to connect

| Way | Encrypted | What you need | Use it when |
|---|---|---|---|
| **Tailscale** (recommended) | yes | Tailscale on both computers, free for personal use | almost always |
| **SSH** | yes | an SSH server on the GPU computer and an SSH key | you already use SSH, or you can't install Tailscale |
| Home network only | **no** | both computers on the same network | only if you understand the risk below |

Never open a ComfyUI port on your router or to the internet. ComfyUI has no password of its own, so anyone who can reach it can use your graphics card and read your pictures. OpenFill does not connect to an open ComfyUI over the network for that reason. It always goes through the **OpenFill GPU server** (it needs a pairing code) or through SSH.

## 1. Set up the GPU computer

1. Install ComfyUI and the models there. The easiest way is OpenFill for GIMP's **Download ComfyUI** and **Models** on that computer, or a ComfyUI you already have.
2. Get OpenFill's files there. Unzip the same `openfill-<version>.zip` you use on the editor computer. The GPU server is the file `gimp-openfill/openfill/gpu_server.py` inside it. Use the same OpenFill version on both computers.
3. Start the GPU server with ComfyUI's own Python. You don't need to install anything else. On Windows, with the ComfyUI portable build in `C:\AI\ComfyUI`:

   ```
   C:\AI\ComfyUI\ComfyUI_windows_portable\python_embeded\python.exe -s C:\path\to\gimp-openfill\openfill\gpu_server.py serve --comfy-dir C:\AI\ComfyUI\ComfyUI_windows_portable
   ```

   On Linux or a Mac, with ComfyUI in `~/ComfyUI`:

   ```
   ~/ComfyUI/venv/bin/python ~/openfill/gimp-openfill/openfill/gpu_server.py serve --comfy-dir ~/ComfyUI
   ```

4. The first time, it shows a **pairing code**, a line like `ofgpu://3fK...q9@100.101.102.103:8390`. Copy the whole line. It is shown only this once. Treat it like a password.
5. Leave the window open while you edit. Press Ctrl+C in it to stop the GPU server and ComfyUI.

What the GPU server does:

- It starts ComfyUI so that only that computer can reach it (`127.0.0.1`).
- It listens on your Tailscale address only. Without Tailscale it asks which address to use. It refuses "every network" (`0.0.0.0`) and internet addresses.
- It lets in only requests with the pairing code. After 8 wrong codes in a minute, it waits a minute before it accepts any from that computer.
- It passes on only what OpenFill needs: progress, image uploads up to 256 MB, and results. The rest of ComfyUI, including any extension's web pages, stays private.
- It keeps only a fingerprint (SHA-256) of the code, in `%APPDATA%\OpenFill\gpu-server` on Windows or `~/.config/openfill/gpu-server` elsewhere. Its log is there too.

Useful options:

- `--bind <address>`: listen on this address, for example `127.0.0.1` when you connect over SSH only.
- `--port <number>`: another port than 8390.
- `--comfy-url http://127.0.0.1:8188`: use a ComfyUI that already runs on that computer instead of starting one. Then it cannot restart ComfyUI after a node install. Add `--custom-nodes <ComfyUI>\custom_nodes` so it can see and install node packs.
- `--comfy-arg=--lowvram`: an extra ComfyUI option. Repeat it for more.
- `gpu_server.py pair`: make a new pairing code. The old one stops working at once. Use it if the code got out, or when you set up a new editor computer.
- `gpu_server.py addresses`: show the addresses it could listen on.

## 2. Set up the editor computer

**GIMP:** open Filters > OpenFill > Generative Fill Settings and expand **Connection and troubleshooting**. Set **Backend** to **Remote GPU** and paste the whole `ofgpu://` line into **Remote GPU pairing code**. Leave **Remote GPU over SSH** empty for Tailscale. Press **Check backend**. It should say which graphics card it found on the GPU computer.

**PhotoCraft:** open the gear, then **Engine**. Tick **Use a Remote GPU**, paste the line into **Remote GPU pairing code**, and press **Save**.

After you save, OpenFill keeps the code in its settings file, not in the address box. To pair again, paste the new line.

## Tailscale

Install Tailscale on both computers and sign in with the same account. That's all. The connection is encrypted, and only your own devices can reach the GPU computer.

On Windows, Tailscale's installer adds a firewall rule that lets traffic in on the Tailscale address. You don't need to change Windows Firewall, and Windows does not ask.

If OpenFill says the GPU computer does not answer, check that Tailscale shows both computers as connected and that the GPU computer is awake.

## SSH

OpenFill can open the SSH connection itself, using your computer's own `ssh` program and an SSH key. It never asks for a password.

1. On the GPU computer, turn on its SSH server. On Windows that's Settings > System > Optional features > OpenSSH Server.
2. On the editor computer, make a key if you have none (`ssh-keygen`), and put the public key on the GPU computer. Check that `ssh you@gpu-computer` works in a terminal without a password. Answer `yes` once to accept the computer's fingerprint.
3. Then pick one:
   - **With the GPU server (recommended):** start it with `--bind 127.0.0.1`. In OpenFill, paste its pairing line (it shows `@127.0.0.1:8390`) and put `you@gpu-computer` into **Remote GPU over SSH**. Node pack installs from the Models tab work.
   - **With ComfyUI alone:** start ComfyUI on the GPU computer as usual, on 127.0.0.1. In OpenFill, put `http://127.0.0.1:8188` as the address and `you@gpu-computer` into **Remote GPU over SSH**. Node packs then have to be installed on that computer by hand.

For another SSH port, write `you@gpu-computer:2222`. Names from your `~/.ssh/config` work too.

OpenFill closes its SSH connection when GIMP or PhotoCraft is done with it. If the editor was killed and left one behind, the next start ends it.

## Home network without Tailscale (not recommended)

Start the GPU server with `--bind` and the GPU computer's home network address, for example `--bind 192.168.1.20`. It works, but:

- **Nothing is encrypted.** Anyone on the same network (a shared flat, a café, a guest on your Wi-Fi) can see your pictures, and can read the pairing code and use it.
- Windows Firewall blocks it until you allow Python for private networks. Windows shows that question on the GPU computer the first time.

Prefer Tailscale or SSH. Never use this on a public or shared network.

## Models and node packs

- OpenFill asks the GPU computer's ComfyUI which models it has. The Models tab shows what is there, not what is on your editor computer.
- **Downloads happen on the GPU computer.** Run `gpu_server.py download <model id> --comfy-dir <ComfyUI folder>` there, for example `download lama-big`. FLUX models need a Hugging Face key in the environment variable `HF_TOKEN` (see [models.md](models.md)). Or download them with OpenFill for GIMP on that computer. Then press **Check backend** on the editor computer.
- **Node packs:** **Install nodes** in the Models tab asks the GPU server to install them on the GPU computer. It installs only the packs OpenFill's own list names for that model, at the pinned version, then restarts ComfyUI. It takes about a minute.
- Removing models and **Open models folder** don't work from the editor computer. Do that on the GPU computer.
- Model files you renamed yourself are not recognized from a distance yet. Keep the names from OpenFill's model list.

## When something is wrong

OpenFill never falls back to this computer's ComfyUI, or to a cloud service, when the GPU computer doesn't answer. It tells you what is wrong:

| OpenFill says | What to do |
|---|---|
| The GPU computer does not answer. It may be asleep... | Wake it up, check Tailscale or your network, and check that the GPU server runs there. Over Tailscale, Windows doesn't answer at all for a port nobody listens on, so a stopped GPU server looks the same as a sleeping computer. |
| ...the OpenFill GPU server is not running there | Start the GPU server on that computer. |
| The SSH connection works, but nothing answers behind it | Start the GPU server (or ComfyUI) on that computer. |
| ...did not accept the pairing code | Run `gpu_server.py pair` there and paste the new line. |
| ...refused several wrong pairing codes and waits a minute | Check the code, then wait a minute. |
| ...runs, but its ComfyUI does not | Look at the GPU server's window or `comfyui.log`. It starts ComfyUI again on the next request. |
| The connection to the GPU computer was lost while it was generating | The network dropped or the computer went to sleep. Your image was not changed. Run the command again. |
| SSH refused the login / does not know the computer yet | See the SSH steps above. |

If the network drops in the middle of a job, OpenFill gives up after about two minutes of silence. The GPU computer finishes the job anyway, and its result is simply not used.

## Good to know

- Speed: a 512 px LaMa removal adds well under a second for the trip there and back over Tailscale at home. The work itself takes as long as on that computer.
- One editor at a time works best. Cancel stops the job that is running on the GPU computer.
- The GPU server and the editor should run the same OpenFill version, so both use the same model list.
