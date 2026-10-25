# Models and backend setup

## Models and their licenses

OpenFill doesn't ship a model. These are the models in its verified list. Each
has its own license, and the model manager links to it. **Check the license
before you use a result commercially: MAT and everything that runs on FLUX.1
Fill are non-commercial.**

| Model | Download | Graphics memory | For | License |
|---|---|---|---|---|
| LaMa | 0.2 GB | 2 GB | Remove (default) | Apache-2.0 |
| MAT Places512 | 0.1 GB | 2 GB | Remove | **CC BY-NC 4.0, non-commercial** |
| RORem (SDXL eraser) | 6.5 GB | 8 GB | Remove | Apache-2.0 |
| FLUX.1 Fill object removal LoRA | 0.3 GB, runs on FLUX.1 Fill | 8 GB | Remove | MIT for the LoRA, but FLUX.1 Fill is **non-commercial** |
| DreamShaper 8 Inpainting | 2.0 GB | 4 GB | Fill, Expand, Create | CreativeML OpenRAIL-M |
| SD 1.5 Inpainting | 4.0 GB | 4 GB | Fill, Expand, Create | CreativeML OpenRAIL-M |
| Realistic Vision V6 Inpainting | 2.0 GB | 4 GB | Fill, Expand, Create | CreativeML OpenRAIL-M |
| RealVisXL V5 | 6.5 GB | 8 GB | Fill, Expand, Create | CreativeML OpenRAIL++-M |
| Juggernaut XL v9 | 6.6 GB | 8 GB | Fill, Expand, Create | CreativeML OpenRAIL-M |
| SDXL base 1.0 | 6.5 GB | 8 GB | Fill, Expand, Create | CreativeML OpenRAIL++-M |
| Fooocus inpaint patch (used with every SDXL model) | 1.2 GB | | | OpenRAIL |
| FLUX.1 Fill dev, GGUF Q4 | 10.0 GB | 8 GB | Fill, Expand | **FLUX.1 [dev] Non-Commercial License** |
| FLUX.1 Fill dev, GGUF Q6 | 12.9 GB | 12 GB | Fill, Expand | **FLUX.1 [dev] Non-Commercial License** |
| FLUX.1 Fill dev (full) | 27.3 GB | 16 GB | Fill, Expand | **FLUX.1 [dev] Non-Commercial License** |
| Qwen Image Edit 2511, GGUF Q4 | 21.3 GB | 12 GB | Fill, Expand | Apache-2.0 |

Sizes are in GiB, as the model manager shows them. Any model can also be
picked for Remove. Your own SD 1.5 inpainting, SDXL and FLUX.1 Fill files in
ComfyUI's model folders show up too, marked "your file", under their own
licenses.

The ComfyUI node packs OpenFill adds when a model needs them are
[comfyui-inpaint-nodes](https://github.com/Acly/comfyui-inpaint-nodes) by Acly
(GPL-3.0; LaMa, MAT and the Fooocus inpaint patch) and
[ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) by city96 (Apache-2.0;
the FLUX and Qwen GGUF files). They are downloaded into your ComfyUI, not
shipped with OpenFill.

## Where the files come from

The backend and the verified model list were downloaded from the official
project pages only (GitHub releases of Comfy-Org, GitHub clone of Acly,
Hugging Face repos of the model authors). Checksums were verified with
`certutil -hashfile` against the SHA-256 values published by Hugging Face
(LFS metadata). Models found by the live search (see Models) come from
Hugging Face and Civitai uploaders and are checked by the rules described
there.

## ComfyUI (backend)

| Item | Value |
|---|---|
| Version | ComfyUI 0.39.0 (release 2026-10-05) |
| Source | https://github.com/Comfy-Org/ComfyUI/releases/download/v0.39.0/ComfyUI_windows_portable_nvidia.7z (2,003,629,308 bytes) |
| Installed at | `C:\AI\ComfyUI\ComfyUI_windows_portable\` |
| Interpreter | `C:\AI\ComfyUI\ComfyUI_windows_portable\python_embeded\python.exe` (Python 3.13.14, torch 2.14.0+cu130) |
| Start command (what the plug-in runs) | `python_embeded\python.exe -s ComfyUI\main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch --dont-print-server` |
| Port | 8188 (ComfyUI address in Settings > General > Connection and troubleshooting) |
| Logs | `%APPDATA%\GIMP\3.2\openfill\comfyui.log` (server), `openfill.log` (plug-in and supervisor) |

The official `run_nvidia_gpu.bat` passes `--windows-standalone-build`, which
opens a browser tab. The plug-in deliberately omits it and adds
`--disable-auto-launch`, so nothing visible is ever opened.

### Custom nodes

| Node pack | Commit | Purpose |
|---|---|---|
| [Acly/comfyui-inpaint-nodes](https://github.com/Acly/comfyui-inpaint-nodes) | `5b7681746fe19e563113d27366ba85236bc1ba5b` (v1.4.4, 2026-10-04), GPL-3.0 | `INPAINT_ExpandMask`, `INPAINT_MaskedFill`, `INPAINT_MaskedBlur`, `INPAINT_StabilizeMask`, Fooocus SDXL inpaint patch nodes |

Installed with `git clone` into `ComfyUI\custom_nodes\`. It needs only
`kornia`, which the portable build already ships (0.8.3), so no extra pip
install was required. ComfyUI-Manager was not needed and is not installed.

## Models

The plug-in ships with no model. The first time Generative Fill, Expand,
Text to Image or Remove object runs without a usable model, it says "No AI
model is installed yet. Download the two starting models below to begin." and opens the
Models tab (the download manager). Nothing is ever downloaded until the user
presses Download on a model (or the one button for the two starting models); the first model that finishes downloading
becomes the active model.

### Families the plug-in can drive

Every model belongs to one family, and each family has one ComfyUI workflow
template in `workflow.py`. Only models of these families are offered.

| Family | Template | Used for | Where candidates come from |
|---|---|---|---|
| Stable Diffusion 1.5 inpainting (`sd15-inpaint`) | `sd15_inpaint` | Fill, Expand, Text to Image; fast, about 2.5 GB of graphics memory | verified list; Hugging Face search "inpainting"; the inpainting version of the most downloaded Civitai SD 1.5 checkpoints |
| Stable Diffusion XL (`sdxl`) | `sdxl_fooocus` | Fill, Expand, Text to Image; any regular SDXL 1.0 checkpoint plus the Fooocus inpaint patch | verified list; Hugging Face fine-tunes of `stabilityai/stable-diffusion-xl-base-1.0`; most downloaded Civitai SDXL 1.0 checkpoints. Turbo, Lightning, LCM, Hyper and DMD distillations and SDXL inpainting models are excluded (they need other sampler settings or break the patch) |
| FLUX.1 Fill (`flux-fill`) | `flux_fill` | Fill, Expand; best quality, 8 GB and up | verified list; Hugging Face search "FLUX.1-Fill" and quantizations/fine-tunes of `black-forest-labs/FLUX.1-Fill-dev`, GGUF or single-file safetensors |
| Qwen-Image-Edit (`qwen-edit`) | `qwen_edit` | Fill, Expand, instruction edits; 12 GB and up, or slower with offloading | verified list; Hugging Face search "Qwen-Image-Edit" (GGUF or safetensors) |
| Object removal (`removal`) | `inpaint_model` | Remove object, no prompt | verified list only (LaMa, MAT) |

A FLUX or Qwen candidate from the hubs is only the diffusion model file. The
text encoders, the VAE and the custom node pack it needs are taken verbatim
from the verified entry of the same family (`flux1-fill-dev-gguf-q4`,
`qwen-image-edit-2511-gguf-q4`), and an SDXL candidate always uses the
verified Fooocus inpaint patch.

### The Models tab

A flat list in four sections; each row shows the name and its state on one
line, then size, license and whether it fits this PC in small grey text, and
one button at the right that follows the state (Download, Downloading 43% -
Cancel, Set active, Active; Ready for removal models, which Remove object
uses by itself). A small menu holds Delete downloaded files, Open the model
page and Read the license.

1. **Installed**: downloaded models, the active one first, then partly
   downloaded ones (Resume download).
2. **Recommended for this PC**: three to five models that are not installed,
   the best one per family first, then the next best overall. A model that is
   too big for this PC is never recommended.
3. **Verified**: the rest of the bundled list (`models/catalog.json`).
4. **More from Hugging Face and Civitai** (collapsed): every other live
   candidate, grouped by family, best first.

Ranking (`catalog.rank_score`): whether it fits this PC dominates (runs well
+3, runs with offloading +1, unknown +0.5, too big -10), then popularity
(0.6 x log10 of downloads per month; Hugging Face reports the last 30 days,
Civitai's all-time count of the version is divided by its age in months),
then recency (up to 2 points, fading over two years since the last upload),
plus 1 point for the verified list and minus 1 when no checksum is
published. Verified entries get the download numbers the search saw for
their Hugging Face repository, so both kinds are ranked on the same scale.

### Live search (`openfill/hub.py`)

When the Models tab opens, a worker thread asks the two public APIs (standard
library only, no account, no token sent):

- `https://huggingface.co/api/models?search=...&filter=...&sort=downloads|trendingScore&direction=-1&limit=30`
  per family, then `https://huggingface.co/api/models/<repo>?blobs=true` for
  the top ten repositories (file list with exact sizes and the LFS SHA-256);
- `https://civitai.com/api/v1/models?types=Checkpoint&baseModels=SD 1.5|SDXL 1.0&sort=Most Downloaded&nsfw=false&limit=30`
  (plus `query=inpainting` for SD 1.5).

A result becomes a candidate only when all of these hold:

- one data-only model file, `.safetensors` or `.gguf` (pickled `.ckpt`,
  `.pt` or `.bin` files are never taken from the hubs), at the repository
  root or in a `diffusion_models`, `unet` or `checkpoints` folder (diffusers
  folders and sharded files are skipped);
- its exact size is known and inside the family's range (SD 1.5 1.5 to
  5.5 GB, SDXL 5.5 to 7.5 GB, FLUX Fill 3 to 24.5 GB, Qwen edit 6 to 42 GB);
- the names are not a distillation (Turbo, Lightning, LCM, Hyper, DMD,
  Rapid), a packaging ComfyUI cannot load (SVDQuant/Nunchaku, DFloat11,
  MLX, bitsandbytes, AWQ, ONNX, all-in-one bundles), a LoRA, ControlNet or
  refiner, an "ungated" re-upload of a gated model, or adult content
  (Hugging Face tag `not-for-all-audiences`, Civitai `nsfw`, and a short
  list of words);
- a license is stated (Hugging Face card data or tag; Civitai shows the
  creator's terms and whether selling generated images is allowed);
- Civitai: public, the file passed Civitai's pickle and virus scans, and
  Civitai published its SHA-256;
- it is not the same file (SHA-256, URL or destination) as a verified
  entry or as a higher-ranked mirror.

GGUF quantizations are picked for the graphics memory: Q4_K_M, Q4_K_S, Q4_0
first below 10 GB; Q5 from 10 GB; Q8_0 or Q6_K from 16 GB. Safetensors: the
largest file that fits 1.6 x the graphics memory, else the smallest.

At most eight candidates per family and source are kept. Responses are
cached in `<profile>/openfill/hub-cache.json` for 24 hours (trimmed to the
fields used). When the hubs cannot be reached, an older cached answer is
used and the tab says so; without any cache the tab shows the verified list
only. Settings `hub_search` (default on) and `hub_civitai` (default on)
in `config.json` turn the search or the Civitai part off.

Run the search directly (prints the candidates, scores and fit verdicts):

    cd gimp-openfill
    python -m openfill.hub --models <ComfyUI>\ComfyUI\models [--record <folder>] [--out result.json]

A real run on 2026-10-09 (53 requests, 4 s) found 35 candidates: SD 1.5
inpainting 9, SDXL 14, FLUX Fill 5, Qwen-Image-Edit 7. The raw responses
are in `docs/evidence/daytime-2026-10-09/catalog/`.

### Fits this PC (`openfill/hardware.py`)

The graphics card and its memory come from `nvidia-smi`, else from a running
ComfyUI's `/system_stats`, else they are unknown; system memory and the free
space on the models drive come from the operating system. `hardware.fit`
then says:

- **Runs well on this PC**: graphics memory at least what the model needs
  (the catalog's `min_vram_gb`; for live entries the diffusion weights plus
  1.5 to 2.5 GB working memory);
- **Runs on this PC, slower (uses system memory too)**: less graphics memory
  but at least the family's floor (SD 1.5 2 GB, SDXL 4 GB, FLUX 6 GB, Qwen
  8 GB) and enough system memory for the rest of the weights plus 4 GB;
  ComfyUI offloads by itself;
- **Too big for this PC**: below the floor, too little system memory, or
  not enough free disk space for what is still to download;
- **Could not check this PC**: no graphics card information.

Hovering the grey line shows the numbers behind the verdict.

### Models that need a Hugging Face login

Some files can only be downloaded after accepting a license on Hugging Face
while logged in (the FLUX.1 [dev] files, and the FLUX VAE that every FLUX
Fill entry uses). For those the Download button opens a panel with four
steps:

1. Open the model page (button) and sign in to Hugging Face there; an
   account is free. When the files come from several pages, each page gets
   its own button.
2. Read the license on that page and press the button to agree to it.
3. Create an access key (button to the "new token" page, type Read) and
   copy it.
4. Paste it into the field in that panel and press Save.

The key field appears only in that panel. The key is stored in
`config.json` in the GIMP profile and sent only to huggingface.co over
https (see `docs/security.md`). If Hugging Face refuses the download
anyway, the panel opens again and says why that usually happens (license
not accepted yet, access still pending, key incomplete).

### How downloads are verified

- Verified entries: every file has the SHA-256 published by Hugging Face
  (or recorded once for the LaMa GitHub release); size and checksum are
  checked before the file is renamed into place.
- Hugging Face candidates: the URL is pinned to the repository commit the
  search saw, and the size and LFS SHA-256 from `?blobs=true` are checked
  the same way.
- Civitai candidates: the exact size and the SHA-256 Civitai published are
  checked.
- A live file without a published checksum is marked "unverified checksum"
  in the tab; after the download the computed SHA-256 is written into
  `hub-selected.json`, so a later download or repair is verified against it.

A live model is remembered in `<profile>/openfill/hub-selected.json` when
its Download button is pressed. The supervisor's task worker reads that file
but re-validates every entry with the live rules (`catalog.parse_live_entries`):
one file from a Hugging Face `resolve` URL or a Civitai download URL, into
`checkpoints/`, `unet/` or `diffusion_models/`, and companion files, custom
nodes and companion models only as listed in the verified catalog.

## Verified list (`models/catalog.json`)

The bundled list is short and checked by hand: every URL is an official
project page, every file has a SHA-256, and every entry was run through the
plug-in. It is the fallback when the hubs cannot be reached and the source
of the companion files that live candidates reuse.

| Catalog id | Family | Download size | Graphics memory | License | Login | Page |
|---|---|---|---|---|---|---|
| `sd15-inpainting` | SD 1.5 inpainting | 4.0 GB | 4 GB | CreativeML OpenRAIL-M | no | https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-inpainting |
| `realistic-vision-v6-inpainting` | SD 1.5 inpainting | 2.0 GB | 4 GB | CreativeML OpenRAIL-M | no | https://huggingface.co/SG161222/Realistic_Vision_V6.0_B1_noVAE |
| `dreamshaper-8-inpainting` | SD 1.5 inpainting | 2.0 GB | 4 GB | CreativeML OpenRAIL-M | no | https://huggingface.co/Lykon/dreamshaper-8-inpainting |
| `sdxl-base` | SDXL | 6.5 GB | 8 GB | CreativeML OpenRAIL++-M | no | https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0 |
| `juggernaut-xl-v9` | SDXL | 6.6 GB | 8 GB | CreativeML OpenRAIL-M (repository tag) | no | https://huggingface.co/RunDiffusion/Juggernaut-XL-v9 |
| `realvisxl-v5` | SDXL | 6.5 GB | 8 GB | CreativeML OpenRAIL++-M (repository tag) | no | https://huggingface.co/SG161222/RealVisXL_V5.0 |
| `fooocus-inpaint-patch` | SDXL helper | 1.2 GB | - | OpenRAIL (repository tag) | no | https://huggingface.co/lllyasviel/fooocus_inpaint |
| `flux1-fill-dev-gguf-q4` | FLUX.1 Fill | 10.0 GB | 8 GB | FLUX.1 [dev] Non-Commercial | yes (VAE) | https://huggingface.co/YarvixPA/FLUX.1-Fill-dev-GGUF |
| `flux1-fill-dev-gguf-q6` | FLUX.1 Fill | 12.9 GB | 12 GB | FLUX.1 [dev] Non-Commercial | yes (VAE) | https://huggingface.co/YarvixPA/FLUX.1-Fill-dev-GGUF |
| `flux1-fill-dev` | FLUX.1 Fill | 27.3 GB | 16 GB | FLUX.1 [dev] Non-Commercial | yes | https://huggingface.co/black-forest-labs/FLUX.1-Fill-dev |
| `qwen-image-edit-2511-gguf-q4` | Qwen-Image-Edit | 21.3 GB | 12 GB | Apache-2.0 | no | https://huggingface.co/unsloth/Qwen-Image-Edit-2511-GGUF |
| `lama-big` | Object removal | 196 MB | 2 GB | Apache-2.0 | no | https://github.com/advimman/lama |
| `mat-places512` | Object removal | 120 MB | 2 GB | CC BY-NC 4.0 | no | https://huggingface.co/Acly/MAT |
| `rorem-sdxl` | Object removal (SDXL eraser) | 6.5 GB | 8 GB | Apache-2.0 | no | https://huggingface.co/LetsThink/RORem |
| `flux1-fill-object-removal-lora` | Object removal (LoRA on FLUX.1 Fill) | 0.3 GB | 8 GB | MIT for the LoRA; FLUX.1 [dev] Non-Commercial for FLUX.1 Fill | no | https://huggingface.co/lrzjason/ObjectRemovalFluxFill |

(Sizes in GiB as the Models tab shows them.) Each entry records id, display
name (messages use only the part before the packaging details in
parentheses, `workflow.model_display_name`), tier, capabilities,
architecture, workflow template, page, license (name + URL), gated flag,
source type (`first-party` or `community-quantization` with the uploader
named), minimum graphics memory, required custom nodes (repo, folder, pinned
commit) and the files (URL, destination under `models/`, size, sha256).
Every URL was verified with a HEAD request (`tests/check_catalog_urls.py`,
log in `docs/evidence/catalog-url-check.txt`). Live entries use the same
schema plus `source` (`huggingface` or `civitai`), `family`, `downloads`,
`updated` and `repo`.

No reputable repository offers a Q4_K_M quantization of FLUX.1 Fill; the
catalog uses YarvixPA's Q4_0 (the most downloaded FLUX Fill GGUF) and lists
the first-party safetensors as the alternative.

Custom node packs referenced by the catalog:

| Pack | Pinned commit | License | Needed by |
|---|---|---|---|
| Acly/comfyui-inpaint-nodes | `5b7681746fe19e563113d27366ba85236bc1ba5b` | GPL-3.0 | SDXL + Fooocus, LaMa/MAT removal |
| city96/ComfyUI-GGUF | `6ea2651e7df66d7585f6ffee804b20e92fb38b8a` | Apache-2.0 | FLUX GGUF, Qwen GGUF |

### Files of the original setup

All paths are relative to `C:\AI\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\`.

| File | Location | Size (bytes) | SHA-256 | License | Source |
|---|---|---|---|---|---|
| `sd-v1-5-inpainting.ckpt` | `checkpoints/` | 4,265,437,280 | `c6bbc15e3224e6973459ba78de4998b80b50112b0ae5b5c67113d56b4e366b19` | CreativeML OpenRAIL-M | https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-inpainting |
| `sd_xl_base_1.0.safetensors` | `checkpoints/` | 6,938,078,334 | `31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b` | CreativeML OpenRAIL++-M | https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0 |
| `fooocus_inpaint_head.pth` | `inpaint/` | 52,602 | `32f7f838e0c6d8f13437ba8411e77a4688d77a2e34df8857e4ef4d51f6b97692` | OpenRAIL (repo tag) | https://huggingface.co/lllyasviel/fooocus_inpaint |
| `inpaint_v26.fooocus.patch` | `inpaint/` | 1,323,362,033 | `f8657a025104e22d70f9c060635d8e8c2196f433871a2f68dc40abd2171f0d59` | OpenRAIL (repo tag) | https://huggingface.co/lllyasviel/fooocus_inpaint |

The SD 1.5 inpainting repository publishes only the `.ckpt`; ComfyUI loads
it with `weights_only` safe unpickling. (Live candidates never use pickled
formats.)

### Which family for what

Which tier for what, from the 2026-10-09 evidence
(`docs/evidence/overnight-2026-10-09/quality-ledger.md`): SD 1.5 often
ignores the prompt on flat or featureless regions and small selections, where
the SDXL tier draws it; the SDXL tier can leave a flat tinted area on
some structured backgrounds; every model gets weaker when Generative Expand
grows a side by more than 25 % (expand in rounds); MAT removes objects cleanly from texture and flat surfaces but
cannot rebuild structured backgrounds.

- **SD 1.5 inpainting** is a real inpainting UNet (9-channel input), loads
  in seconds, samples 768 px regions in a few seconds on the RTX 4070 Laptop
  and needs roughly 2.5 GB of graphics memory.
- **SDXL + Fooocus inpaint patch**: the patch turns any regular
  (non-Turbo/Lightning) SDXL checkpoint into an inpainting model, as used by
  Fooocus and Krita AI Diffusion. At 1024 px it uses most of 8 GB; ComfyUI's
  model management offloads to system memory automatically. Expect 30 to
  60 s per fill versus 5 to 10 s for SD 1.5.
- **LaMa and MAT** are prompt-free removal models used by Remove object when
  installed. They run at a working size of at most 512 px.

### Default model per operation

The plug-in picks the model itself unless one was set active by hand
(Models tab, Set active) or chosen in a dialog. Each tier follows a fallback
list and uses the first model that is installed (`openfill/catalog.py`,
`TIER_PREFERENCE`). Scores are pass / notes / fail of 33 judged cases
(`docs/evidence/daytime-2026-10-09/judge/`).

| Operation | Order | Why |
|---|---|---|
| Fast fill | DreamShaper 8 inpainting, then SD 1.5 inpainting | 7/10/16 against 7/7/19 |
| Quality fill | RealVisXL V5, then Juggernaut XL v9, then SDXL base (with the Fooocus patch) | 9/1/23, 5/4/24, 7/0/25 |
| Remove object, empty prompt | LaMa, then MAT | 5/3/25 against 3/3/27; MAT invents glyphs and figures |
| Generative Expand (both tiers) | SD 1.5 inpainting, then DreamShaper 8 | 4 photos x 3 models x 25 % and 60 % per side, all sides: SD 1.5 best in 5 of 8 pairs, DreamShaper 2, RealVisXL 1 (it invents scenery); `docs/evidence/daytime-2026-10-09/expand-models/` |
| Automatic active model | RealVisXL V5, DreamShaper 8, SD 1.5, Juggernaut, SDXL base | the most often acceptable fills |

Generative Expand uses the model you pick. When the Expand default is
installed and you picked another model, a question offers to switch; declining
keeps your pick.

FLUX Fill and Qwen Edit are never chosen automatically; they are used only
when picked by hand. FLUX Fill (Q4) scored 5/5/18, ties RealVisXL on object
fills and takes about four times as long (median 112 s), so it is labelled
"Quality (slow)". An empty prompt or Remove object is never sent to FLUX or
Qwen (they repaint the object); without LaMa or MAT it goes to the Fast, then
the Quality default. FLUX and Qwen get a request timeout of at least 240 s, and
any progress message from the backend extends the deadline further. Qwen Edit
needs more than 8 GB of graphics memory (one job took 664 s offloaded on an
8 GB card), so the download manager marks it too big for 8 GB cards.

The first-run download manager recommends LaMa (0.2 GB) and DreamShaper 8
inpainting (2 GB) as the starting set and names RealVisXL V5 (6.5 GB) as the
quality upgrade.

### Graphics memory budget (8 GB RTX 4070 Laptop)

| Scenario | Working size | Peak graphics memory (observed/expected) |
|---|---|---|
| SD 1.5 inpaint, 1 variation | up to 768 x 768 | about 2.5 GB |
| SD 1.5 inpaint, 4 variations | up to 768 x 768 | about 3.5 GB |
| SDXL + Fooocus, 1 variation | up to 1024 x 1024 | 6.5 to 7.5 GB, partial offload |
| SDXL + Fooocus, 2+ variations | up to 1024 x 1024 | offload heavy; prefer 1 at a time |

If a request fails with an out-of-memory message, use a smaller selection,
fewer variations, or an SD 1.5 model. `--lowvram` can be added through
"Extra ComfyUI options" (Settings > General > Connection and troubleshooting).

## Adding other models by hand

Any SD 1.5 or SDXL checkpoint dropped into `models/checkpoints/` is offered
as "SD 1.5 model file" or "SDXL model file" (Settings > General > Connection
and troubleshooting) after the next Check backend; these files are used when
a script asks for the legacy `fast` or `quality` tier. For SDXL, keep the
Fooocus patch; for SD 1.5, prefer checkpoints trained for inpainting (names
usually contain `inpainting`), although regular checkpoints also work with
`InpaintModelConditioning` at reduced quality.
