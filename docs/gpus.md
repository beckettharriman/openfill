# Graphics cards

OpenFill runs on ComfyUI, and ComfyUI runs on whatever PyTorch can use. This page says which cards work how, and which of those I have actually seen work. I build and test on NVIDIA cards, so almost everything else here is **untested** until someone tries it. If you do, please tell me how it went on [Issues](https://github.com/beckettharriman/openfill/issues), good or bad, with the card, the system and the model.

Status means:

- **tested**: I or a user ran Fill, Remove, Expand and Create on it with OpenFill.
- **untested**: ComfyUI or the card's maker documents it, and OpenFill sets it up the documented way, but nobody has tried it with OpenFill yet.
- **not supported**: OpenFill does not set it up, and I don't recommend it.

Checked on November 6, 2026.

| Vendor | Card | System | How | Status |
|---|---|---|---|---|
| NVIDIA | RTX 20 series and newer | Windows | Download ComfyUI (ComfyUI's NVIDIA build, CUDA 13) | tested (RTX 4070 Laptop, 8 GB) |
| NVIDIA | RTX 20 series and newer | Linux | Your own ComfyUI with PyTorch for CUDA | tested (RTX 2070 Max-Q, Fedora 44) |
| NVIDIA | GTX 10 series and older | Windows | Download ComfyUI picks ComfyUI's CUDA 12.6 build | untested (made for [issue 2](https://github.com/beckettharriman/openfill/issues/2)) |
| Apple | M1 and newer | macOS | Your own ComfyUI (PyTorch MPS) | tested (M3 MacBook Air) |
| AMD | RX 7000, RX 9000, Ryzen AI 300 and Max | Windows | Download ComfyUI picks ComfyUI's AMD build (ROCm 7.2) | tested by a user (RX 7800 XT, [issue 3](https://github.com/beckettharriman/openfill/issues/3)) |
| AMD | RX 7000, RX 9000 | Linux | Your own ComfyUI with PyTorch for ROCm | untested |
| AMD | RX 6800, 6900 (gfx1030) | Linux | Your own ComfyUI with PyTorch for ROCm | untested |
| AMD | RX 6700, 6600 (gfx1031, gfx1032) | Linux | PyTorch for ROCm from pytorch.org, OpenFill sets `HSA_OVERRIDE_GFX_VERSION=10.3.0` | untested |
| AMD | RX 6700, 6600 (gfx1031, gfx1032) | Linux | AMD's own PyTorch with the card's kernels (`device-gfx1031`, `device-gfx1032`), OpenFill sets no override | untested |
| AMD | RX 6000 | Windows | Your own ComfyUI with AMD's PyTorch for ROCm 10 (see [AMD on Windows](install.md#amd-on-windows)) | untested |
| AMD | Radeon 780M, 760M (gfx1103) | Linux | PyTorch for ROCm, OpenFill sets `HSA_OVERRIDE_GFX_VERSION=11.0.0` | untested |
| AMD | any | Windows | DirectML (`torch-directml`) | not supported |
| AMD | any | Windows | ZLUDA (ComfyUI-Zluda fork) | not supported |
| Intel | Arc A and B series (A770, A750, B580...) | Windows | Download ComfyUI picks ComfyUI's Intel build (PyTorch 2.14 XPU) | untested |
| Intel | Core Ultra with Arc graphics (Meteor Lake, Lunar Lake, Arrow Lake H, Panther Lake) | Windows | Download ComfyUI picks ComfyUI's Intel build | untested |
| Intel | Arc A and B series, Core Ultra with Arc graphics | Linux | Your own ComfyUI with PyTorch for Intel (XPU) | untested |
| Intel | UHD, Iris Xe, HD graphics | any | none, runs on the processor | not supported |
| Intel | any | any | Intel Extension for PyTorch (IPEX) | not supported (Intel retired it) |
| any | any | any | Vulkan | not supported (ComfyUI has no Vulkan path) |
| none | no graphics card | any | ComfyUI on the processor (`--cpu`) | works, but minutes per fill |

## AMD

The steps are in the install guide: [AMD on Windows](install.md#amd-on-windows) and [AMD on Linux](install.md#amd-on-linux).

**RX 6000 on Linux.** ComfyUI's README installs PyTorch from pytorch.org (`--index-url https://download.pytorch.org/whl/rocm7.2`). Those builds carry code for the RX 6800 and 6900 (gfx1030) but not the 6700 or 6600 (gfx1031, gfx1032). For those, ComfyUI's README says to start it with `HSA_OVERRIDE_GFX_VERSION=10.3.0`, and OpenFill does that by itself when it starts ComfyUI, unless you set it already. It writes that in its log.

AMD's own PyTorch packages (the `whl-next` index in ComfyUI's Windows instructions, which has Linux packages too) do have kernels for the 6700 and 6600 (`torch[device-gfx1031]`, `torch[device-gfx1032]`). If your ComfyUI's venv has AMD's PyTorch (OpenFill checks that an `amd_torch_device_gfx*` package matches the installed torch version, so one left over from an earlier try doesn't count), OpenFill leaves the override off, since it would point PyTorch at gfx1030 kernels that may not be installed. That holds with a Ryzen's Radeon graphics next to the card too. With PyTorch from pytorch.org, OpenFill decides by the card with the most memory, not the Ryzen's built-in graphics.

**RX 6000 on Windows.** ComfyUI's AMD download only runs on RX 7000 and newer. AMD's PyTorch for ROCm 10 lists RDNA 2 (RX 6000) as supported on Windows, and ComfyUI's [instructions for AMD on Windows](https://github.com/Comfy-Org/ComfyUI#amd-gpus-windows-rocm-100) use it. You can swap `device-all` for `device-gfx1031` (RX 6700, 6750) or `device-gfx1032` (RX 6600) to download less. Note that AMD's own compatibility table still doesn't list the consumer RX 6000 cards, so this is the newest and least proven of the AMD paths.

Known problems people have reported on RX 6000 cards with ComfyUI (not with OpenFill):

- A 6700 XT freezing unless ComfyUI starts with `--disable-dynamic-vram` ([ComfyUI issue 16878](https://github.com/Comfy-Org/ComfyUI/issues/16878)). You can add that in Extra ComfyUI options.
- `HIPBLAS_STATUS_INVALID_VALUE` on a gfx1031 with ROCm on Windows ([ComfyUI issue 17422](https://github.com/Comfy-Org/ComfyUI/issues/17422)).
- Slow attention on gfx1030 ([ComfyUI issue 17500](https://github.com/Comfy-Org/ComfyUI/issues/17500)).

**Why not DirectML or ZLUDA.** ComfyUI itself warns that torch-directml "barely works, is very slow, has not been updated in over 1 year". Its last release (September 2024) pins PyTorch 2.4.1 and stops at Python 3.12, and Microsoft has put DirectML in maintenance mode. ZLUDA runs through a separate fork of ComfyUI with an older PyTorch. Now that AMD ships PyTorch for ROCm on Windows, both are a step back.

## Intel Arc and Core Ultra

ComfyUI makes a Windows build for Intel graphics, `ComfyUI_windows_portable_intel.7z` (Python 3.13, PyTorch 2.14 for Intel XPU). With an Intel Arc card or a Core Ultra chip with Arc graphics, **Download ComfyUI** gets it instead of the NVIDIA one: about 1.4 GB (about 5 GB unpacked), from the same ComfyUI release as the others and checked against its SHA-256. Keep the Intel graphics driver up to date; PyTorch's current Intel builds ask for driver 32.0.101.9033 or newer.

PyTorch lists these as supported for its Intel builds: Arc A series, Arc B series, and Core Ultra chips with Arc graphics (Meteor Lake H, Lunar Lake, Arrow Lake H, and Panther Lake, Core Ultra series 3). OpenFill only picks the Intel build when Windows names the chip "Arc", as Arc cards and the Core Ultra chips above do ("Intel(R) Arc(TM) Graphics", "Arc(TM) 140V GPU", "Arc(TM) B390 GPU"). Older Intel graphics (UHD, Iris Xe, HD) and chips that just say "Intel(R) Graphics" keep the NVIDIA download on the processor, which is slow but known to work. If you have an Arc card next to a Ryzen's Radeon graphics, OpenFill goes by the Arc, since it has memory of its own.

On Linux, install ComfyUI with PyTorch for Intel, as [ComfyUI's instructions](https://github.com/Comfy-Org/ComfyUI#intel-gpus-windows-and-linux) say:

```sh
git clone https://github.com/comfyanonymous/ComfyUI.git ~/ComfyUI
cd ~/ComfyUI
python3 -m venv venv
venv/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/xpu
venv/bin/pip install -r requirements.txt opencv-python-headless
```

Then choose `~/ComfyUI` in the panel (PhotoCraft) or run `sh install.sh --comfy-dir ~/ComfyUI` (GIMP). PyTorch lists Panther Lake on Linux for Ubuntu 26.04 only, so on other distributions it may need a newer Intel graphics stack.

Built-in graphics (Core Ultra) have no memory of their own; they share the computer's memory. OpenFill reads only the small slice Windows reserves for them, so the model manager may say models are too big when they would run. Go by your computer's memory: with 16 GB, LaMa and SD 1.5 are the safe choices.

Intel Extension for PyTorch (IPEX) is not needed and not supported: Intel archived it, and plain PyTorch now has Intel support built in.

## Vulkan

There is no Vulkan path for ComfyUI. ComfyUI is built on PyTorch, and PyTorch has no Vulkan backend for this. stable-diffusion.cpp runs FLUX.1 Fill over Vulkan, but it is a different program that doesn't run ComfyUI workflows or nodes, so OpenFill can't use it.

## Models on other cards

- **LaMa and MAT (Remove)** load as plain PyTorch through comfyui-inpaint-nodes, with nothing tied to NVIDIA. OpenFill's own LaMa node splits a big photo into pieces that fit the free graphics memory on NVIDIA, AMD (PyTorch for ROCm calls the card cuda) and Intel (XPU) alike; on a Mac and on the processor it uses a fixed limit. They need about 1 GB of graphics memory (my estimate; the LaMa file is 206 MB). I haven't found a report of them failing on AMD or Intel.
- **SD 1.5, SDXL and the Fooocus inpaint patch** are plain PyTorch too.
- **FLUX.1 Fill GGUF** goes through ComfyUI-GGUF. There are open reports of noise-only images on an RX 7900 with ROCm ([ComfyUI-GGUF issue 300](https://github.com/city96/ComfyUI-GGUF/issues/300)) and of running out of memory on an Arc B580 ([issue 282](https://github.com/city96/ComfyUI-GGUF/issues/282)). On an AMD card the model manager marks the GGUF models and leaves them out of Recommended.
  - Q4_0 (6.8 GB file) plus the T5 GGUF (about 3 GB) needs ComfyUI to move part of it to the computer's memory on an 8 GB card (RX 6600, Arc A750): on NVIDIA that works but is slow, about 3 minutes a fill on my 8 GB laptop card.
  - On a 12 GB card (RX 6700 XT, Arc B580) Q4_0 fits, and Q6_K (9.9 GB file) fits with a little moved out.

## Sources

- ComfyUI README, install sections and the "For AMD cards not officially supported by ROCm" note: https://github.com/Comfy-Org/ComfyUI#manual-install-windows-linux
- ComfyUI's official portable builds (NVIDIA, NVIDIA CUDA 12.6, AMD, Intel): https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.39.0
- PyTorch for Intel GPUs, supported hardware: https://docs.pytorch.org/docs/stable/notes/get_start_xpu.html
- Intel driver requirements for PyTorch: https://www.intel.com/content/www/us/en/developer/articles/tool/pytorch-prerequisites-for-intel-gpu/2-15.html
- Intel Extension for PyTorch, archived: https://github.com/intel/intel-extension-for-pytorch
- AMD PyTorch install (ROCm on Windows and Linux): https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/frameworks/pytorch/install.html
- AMD ROCm compatibility matrix: https://rocm.docs.amd.com/en/latest/compatibility/compatibility-matrix.html
- TheRock supported GPUs (gfx1030 to gfx1036 on Linux and Windows): https://github.com/ROCm/TheRock/blob/main/SUPPORTED_GPUS.md
- DirectML maintenance mode: https://github.com/microsoft/DirectML
- Vulkan feature request for ComfyUI: https://github.com/Comfy-Org/ComfyUI/discussions/5530
