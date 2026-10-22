# OpenFill bar and Filter menu, end to end (2026-10-22)

PhotoCraft 0.12.0 (Linux x86_64 tarball) on Fedora 44 under Xvfb, ComfyUI 0.40 on an RTX 2070
Max-Q (8 GB). `photocraft/e2e/run_bar_e2e.py` acted like a user: it picked OpenFill's entries
from PhotoCraft's Filter > Plug-ins menu, typed into the OpenFill bar and pressed its buttons,
and checked the document through the control channel. `bar-report.json` holds every case.

| Case | Result |
|---|---|
| Filter > Plug-ins > OpenFill Generative Fill… | the bar comes up on Fill with the prompt focused; the entry's own history step is removed |
| Fill, 3 variations (RealVisXL V5) | done in 73 s; one history step ("Duplicate Layer"); thumbnails in the bar |
| Click variation 2, Keep | only variation 2 visible, the other two deleted |
| Filter > Plug-ins > OpenFill Remove Object | runs at once (LaMa), 6 s, one history step |
| Fill without a selection | refused on the bar: "Select an area first" |
| Expand to 16:9 | 1448 x 1086 to 1931 x 1086 in 63 s; two steps (Crop, the result) |
| Create, portrait, as a new image | a new 704 x 1024 document (RealVisXL's 1024 limit, same shape) in 21 s |
| Close the bar, Filter > Plug-ins > OpenFill Panel | the bar comes back |

Also checked on 2026-10-22, outside this script: the installer in a clean home folder (PhotoCraft
downloaded and SHA-256 checked, menu entry handed to OpenFill, uninstall puts it back); opening
PhotoCraft from that menu entry with an image; a second image opened while it runs lands in the
same PhotoCraft; a PhotoCraft opened without OpenFill is detected; ComfyUI starts in the
background at launch (ready in 8 s); closing PhotoCraft stops OpenFill and ComfyUI.
