# Cloud service (optional)

OpenFill runs on your own computer by default, and nothing about that changes. If you don't have a graphics card that can run the models, or don't want to set up ComfyUI, you can point OpenFill at a paid online image service instead: [OpenRouter](https://openrouter.ai), anything that speaks OpenAI's images API (OpenAI itself, a [LiteLLM](https://docs.litellm.ai) proxy, and so on), or **Google Gemini** in PhotoCraft (`generativelanguage.googleapis.com`). You bring your own API key and pay the service directly. OpenFill gets nothing from it.

**Live vs. stand-in testing.** OpenRouter and OpenAI-compatible remain tested only against a local stand-in server (and in real GIMP 3.2.8 against that stand-in). Google Gemini (`gemini-3.1-flash-image`) was tested live against Google's API from PhotoCraft on Windows 11 (`Check`, free-tier `429` quota handling, and `Fill` with both `second_image` and `overlay` mask modes), while all retry and error paths are covered against the local stand-in server (Gemini is in the PhotoCraft panel and companion only). If you try any cloud service, please tell me what happens, good or bad, in an [issue](https://github.com/beckettharriman/openfill/issues).

## What gets sent, and when

- **Nothing is sent unless you pick the cloud.** "Where images are made" is a setting with a clear choice: This computer (the default) or Cloud service. While This computer is picked, OpenFill never contacts the cloud service, even if a key is saved.
- **Nothing is sent until you press Generate.** Picking the cloud and saving your key sends nothing. Check (GIMP: Check backend) sends only your key, to OpenRouter's `GET /key`, the service's `GET /models`, or Gemini's `GET /v1beta/models/{model}`, to see that it works. No picture.
- **What a job sends:** the part of your image around the selection (the same area ComfyUI would get), a black and white mask of the selection, and your prompt. For Create, only the prompt. No file name, no layers, no metadata.
- **Every screen says so.** While the cloud is picked, the Fill, Remove, Expand and Create screens show "Cloud: your image is sent to OpenRouter." (or the address you set), and PhotoCraft's panel header shows "Cloud: OpenRouter".
- **No silent switching.** If the cloud fails, OpenFill tells you why and stops. It never runs the job on your computer instead. If ComfyUI fails, it never sends the job to the cloud instead. Each job runs on exactly the one you picked.

## Setting it up

**GIMP:** `Filters > OpenFill > Generative Fill Settings`, General tab. Set **Where images are made** to **Cloud service**. A Cloud service box appears: pick the service, paste your API key, and save. Model and Address can stay empty to use the service's defaults. If ComfyUI isn't found when you first run a tool, the "ComfyUI is required" window also has a **Use a cloud service instead...** button that opens the same form.

**PhotoCraft:** the gear in the OpenFill panel, **Engine**. Switch **Where images are made** to **Cloud service**, paste your key, and Save. The browser version of the panel has the same fields under Engine. When ComfyUI isn't found, the setup card mentions the cloud as an option, but the buttons still set up ComfyUI.

To go back, switch to **This computer** and save. Your key stays saved until you replace it or delete it from `config.json`.

**A key belongs to the service and address you entered it for.** OpenFill remembers which service, and which address (scheme, host and port), each saved key was typed for, and sends it only there. If you switch from OpenRouter to OpenAI-compatible, or change the address, the old key is not reused: the key field asks for the key of the new service, and until you paste one, Generate and Check stop with a message saying so. Nothing is sent to the new address without its own key.

### OpenRouter

- Make a key at https://openrouter.ai/keys and add credits at https://openrouter.ai/settings/credits.
- Default model: `google/gemini-2.5-flash-image`. Any OpenRouter model that makes images from text plus an input image should work. List them with `https://openrouter.ai/api/v1/models?output_modalities=image` (no key needed).
- OpenFill uses OpenRouter's image API, `POST /api/v1/images`, with your picture and the mask as two `input_references`. Some models only do images through chat. For those, set `"cloud_api": "chat"` in `config.json` and OpenFill uses `POST /api/v1/chat/completions` with `"modalities": ["image", "text"]` instead (one request per variation).

### OpenAI-compatible (OpenAI, LiteLLM, ...)

- Pick **OpenAI-compatible**, paste the key, type the **model** your service uses (OpenFill doesn't guess one), and set **Address** to the service's `/v1` address. Empty means `https://api.openai.com/v1`.
- For a LiteLLM proxy, use its address, for example `http://localhost:4000/v1` or `http://192.168.1.20:4000/v1`, and the model name you gave it in LiteLLM.
- Fill, Remove and Expand use `POST /v1/images/edits` with a real mask. Create uses `POST /v1/images/generations`.
- Plain `http://` is only accepted for an address on this computer or a private network: `localhost`, a loopback or private IP address (127.x, 10.x, 172.16-31.x, 192.168.x, fd00::/8), or a name that, when OpenFill looks it up right before each request, points only to such addresses. Anything else must be `https://`, so your key is never sent unencrypted across the internet. An address with a user name or password in it (`https://user:pass@...`) is refused. A plain-http address is always contacted directly, never through a proxy set on your system (a proxy would see the key); https may go through one, which can't see the key.

### Google Gemini (PhotoCraft)

- Pick **Google Gemini** in PhotoCraft's Engine panel (or browser settings page).
- **API key:** Paste your key in the API key field and Save (stored in `config.json` just like OpenRouter and OpenAI-compatible, bound to Gemini and its address), or set `GEMINI_API_KEY` (or `GOOGLE_API_KEY` as fallback) in your environment and restart **PhotoCraft with OpenFill**.
- **Model and endpoint:** Default model is `gemini-3.1-flash-image` (editable) and default address is `https://generativelanguage.googleapis.com/v1beta`. Requests call `POST /v1beta/models/{model}:generateContent` with the key in the `x-goog-api-key` header (never in the URL).
- **Mask method (`cloud_mask_mode`):**
  - `second_image` (default): sends the cropped image plus a black-and-white mask image and instructs the model to edit only the white area.
  - `overlay`: sends a single image with a visible semi-transparent magenta highlight over the selection and instructs the model to replace the highlighted area cleanly.
  - Either way, OpenFill composites the returned image only through your selection mask (`imaging.composite`), so pixels outside the selection never change.
- **Variations:** Switching to Google Gemini defaults Variations to `1` (up to 4 sequential requests per run).
- **Terms and SynthID watermark:** Review Google's [Gemini API Terms of Service](https://ai.google.dev/gemini-api/terms) before use. Google documents that images generated or edited with Gemini image models carry a SynthID watermark ([image generation docs](https://ai.google.dev/gemini-api/docs/image-generation)).

## How each tool maps

| Tool | OpenRouter | OpenAI-compatible | Google Gemini |
| --- | --- | --- | --- |
| Fill | picture + mask as two input images, prompt: paint *your prompt* in the white area, keep the rest | `images/edits` with the mask, your prompt | `generateContent` with `second_image` (crop + mask) or `overlay` (magenta highlight) |
| Remove | same two images, prompt: remove what is in the white area and fill it with the background | `images/edits` with the mask, the same removal prompt | `generateContent` (`second_image` or `overlay`), prompt: continue the background, no new objects |
| Expand | the picture with the new canvas area added, and a mask of that area; prompt: continue the picture into it | `images/edits` with the mask of the new area | `generateContent` (`second_image` or `overlay`), prompt: continue the scene into the new canvas |
| Create | prompt only, `aspect_ratio` closest to the size you asked for | `images/generations`, the closest of 1024x1024, 1536x1024, 1024x1536 | `generateContent` text prompt + `imageConfig.aspectRatio` |

Then, on your computer, for every tool except Create:

1. The picture the service returns is scaled back to the size of the area that was sent. Before sending, OpenFill pads that area to the nearest aspect ratio the service offers (by repeating its edge pixels), so the service has no reason to stretch or crop it. The padding is cut away again here.
2. **It's put back through your selection.** The result becomes a layer that is fully transparent everywhere outside the selection, so it can't change a single pixel there, also where your image is transparent or has 16-bit or float colours. Its layer mask is your selection as it is, never grown past it, so a feathered or anti-aliased selection blends it in exactly as it blends a local fill. A hard-edged selection also gets a short fade just inside its edge, at most a third of the selection's depth, so a thin selection (a 4 px wire) is still fully replaced in the middle.

So whatever a model does to the rest of the picture (shifting colours, redrawing faces, moving things a little), none of it reaches your image. In real GIMP, with a stand-in service that returns a solid red picture: a 600 x 4 px wire was fully replaced (2,400 of 2,400 pixels); a 60 px feathered ellipse blended exactly as a layer mask does (within 2 levels); and in all cases, a transparent image included, 0 pixels outside the selection changed.

## Limits, honestly

- **No real mask on OpenRouter.** None of the image models there takes a mask. The mask goes along as a second picture with an instruction to change only the white area. Models mostly follow that, but not always. Because of step 2 above, a model that ignores it can only spoil the inside of your selection, never the outside. Still, the result inside may not line up perfectly with what's around it. OpenAI also says its own mask is guidance, not an exact shape: "Masking with GPT Image is entirely prompt-based" ([image generation guide](https://developers.openai.com/api/docs/guides/image-generation)).
- **Resolution.** These services return pictures of about 1 megapixel (1024 x 1024 or similar). The area around a large selection is sent at reduced size (longest side at most 1536 px) and the result is scaled up into your image. A big selection in a big photo comes back softer than a local FLUX or SDXL fill at full resolution.
- **Seeds and settings.** Steps, CFG, sampler and seed don't apply. Each run is different. The Generation settings only affect local generation.
- **Variations.** Up to 4 per run, even if you ask for more (the screens say so). Each one is a separate paid picture.
- **Expand's join.** For Expand the "selection" is the new canvas plus a 32 px band along the old edge, the same as when it runs locally, so the join doesn't show. That band of the old picture is repainted; the rest of the old picture is untouched (checked in real GIMP: 0 changed pixels outside the band).
- **Seam correction** runs on Gemini Fill and Remove results before compositing (`quality.seam_field`), keeping pixels outside the selection completely unchanged while matching brightness/colour shifts against the surroundings; grain matching stays skipped for cloud results.
- **Cancel** stops OpenFill waiting right away, leaves your image as it was, and no further request is started (with several variations through chat or Gemini, each is its own request: the ones not sent yet are not sent). On an already-running request, OpenFill abandons waiting for the response in the background rather than aborting the socket at the OS level; a request the service has already received may still be finished and charged. If one variation fails after others came back, the ones you already paid for are kept.
- **Retries (Gemini):** Transient `429` rate limits (excluding best-effort detected zero-quota `429` responses) and `5xx` errors retry with backoff + jitter, honouring `Retry-After` and checking the cancel flag before waiting or retrying. `400`/`401`/`403`/`404`, safety blocks, timeouts, and unreachable hosts are not retried.
- **Time limit.** 240 seconds per request unless you set **Time limit** in PhotoCraft's Engine panel, "Generation time limit" (GIMP, Settings, Connection), or `request_timeout` in `config.json`. That is a limit for the whole job (times the number of requests in chat or Gemini mode), not per network read, so a service that answers very slowly still ends in a timeout. Check has one limit of its own, 20 seconds at most.
- **Picture format.** Only PNG, JPEG and WebP answers are read, recognised by their first bytes; anything else is refused unread. OpenFill reads PNG itself. JPEG and WebP are read with GIMP's own reader (GdkPixbuf) or, in PhotoCraft, with Pillow, in both cases limited to that one format, so an answer never reaches any other image reader. Every result is then written again as a PNG by OpenFill before GIMP or PhotoCraft gets it.
- **Size limits.** An answer larger than 64 MB is refused, and so is a picture over 25 megapixels or a PNG whose data unpacks to more than its header says. The same limits apply to pictures a service leaves at a web address; those are fetched only over https, never following a redirect, and never with your key.
- **Speed on your computer.** Putting the result back is plain Python: about 1 second for a 600 x 600 px selection and 4 seconds for 1200 x 1200 px, per variation, on a laptop.
- **Models come and go.** For example, OpenAI shut down `dall-e-2` and `dall-e-3` on 2026-05-12 and `gpt-image-1` on 2026-10-23, and `gpt-image-1-mini` goes on 2026-12-01 ([deprecations](https://developers.openai.com/api/docs/deprecations)). If a model is gone, you get a 404 message naming it. Pick another in the settings.
- **LiteLLM in front of OpenRouter** probably drops the mask: LiteLLM sends image edits for OpenRouter through chat completions, where there's no mask field ([LiteLLM OpenRouter docs](https://docs.litellm.ai/docs/providers/openrouter)). OpenFill's own OpenRouter option does the same thing on purpose, with the mask as a picture, and puts the result back through your selection either way.

## When something goes wrong

Every error says what happened in plain words and what to do. Your image is left unchanged.

| What the service says | What you see |
| --- | --- |
| 401 | The service did not accept your API key. Check the key, or make a new one (with the link for OpenRouter). |
| 402 | Your account is out of credits, with the link to add them. |
| 403 | The service refused the request: it may have flagged the picture or the prompt (with its reasons, when it gives them), or your key may not be allowed to use the model. |
| 404 | The service doesn't offer that model at that address. |
| 429 | You're sending requests too fast. Wait (the number of seconds, when the service says) and try again. |
| 408, 504, or no answer in time | The service didn't answer in time. Nothing was changed. |
| 5xx | The service or the model isn't working right now. Try later or pick another model. |
| no connection | OpenFill could not reach the service. Check your connection and the address. |

## Your API key

- Stored in one place only: `config.json` in OpenFill's settings folder (GIMP: `<GIMP profile>/openfill/config.json`; PhotoCraft: `%APPDATA%\OpenFill for PhotoCraft\openfill\config.json` on Windows, `~/.config/openfill-photocraft/openfill/config.json` on Linux, `~/Library/Application Support/OpenFill for PhotoCraft/openfill/config.json` on macOS). It's plain text in that file, not encrypted. Google Gemini can also read `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) from your environment instead of storing a key in `config.json`.
- On Linux and macOS the file is created readable by you only (mode 0600) from the start. On Windows, OpenFill does not set permissions itself: the file relies on the permissions of the folder it is in. The default folders (`%APPDATA%`) are readable only by you, SYSTEM and administrators. If you moved GIMP's profile (`GIMP3_DIRECTORY`) to a shared folder or another drive, the key is as readable as that folder.
- The settings screens never show it again after you save. They only say a key is saved (or that Gemini uses GEMINI_API_KEY from the environment). PhotoCraft's settings page and its local API never send it back.
- In GIMP the key is never a procedure argument, so it never ends up in GIMP's own plug-in settings file.
- Never logged and never in an error message: it's left out of `repr()`, OpenFill removes it from any error text a service sends back, and the log filter replaces anything that looks like a key (`sk-...`, `AIza...`, `Bearer ...`, `x-goog-api-key: ...`, `"cloud_api_key": ...`) with `[redacted]`. The debug dumps don't contain it. Unit tests check all of this over the captured logs (`tests/test_cloud.py`).
- Sent only in the `Authorization: Bearer` header (or `x-goog-api-key` for Gemini), only to the service and address it was entered for (see above), and never on a redirect: OpenFill refuses redirects from the service so the key can't be passed on to another address.
- Never attach `config.json` to a bug report. If you think the key leaked, delete it at the service (OpenRouter: https://openrouter.ai/keys; Google AI Studio: https://aistudio.google.com/apikey) and make a new one.

## Sources

Checked on 2026-11-06:

- OpenRouter image generation (`POST /api/v1/images`, `input_references`, `aspect_ratio`, `data[].b64_json`): https://openrouter.ai/docs/guides/overview/multimodal/image-generation
- OpenRouter chat completions (`modalities`, `image_config`, `message.images[].image_url.url`): https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request.md
- OpenRouter errors (401, 402 "insufficient credits", 403 moderation, 408, 429, 502, 503, the `{"error": {"code", "message", "metadata"}}` body): https://openrouter.ai/docs/api-reference/errors
- OpenRouter authentication and `GET /api/v1/key`: https://openrouter.ai/docs/api-reference/authentication
- OpenAI image edits (`image`, `mask`, `prompt`, `model`, `n`, `size`, `data[].b64_json`): https://developers.openai.com/api/reference/resources/images/methods/edit
- OpenAI image guide (mask is prompt-based guidance): https://developers.openai.com/api/docs/guides/image-generation
- LiteLLM image edits and generations on the proxy: https://docs.litellm.ai/docs/image_edits, https://docs.litellm.ai/docs/image_generation
- Google Gemini image generation, model IDs (`gemini-3.1-flash-image`, `gemini-3-pro-image`, `gemini-3.1-flash-lite-image`), `responseModalities`, `imageConfig`, and SynthID watermark: https://ai.google.dev/gemini-api/docs/image-generation
- Google Gemini REST `:generateContent` schema (`inlineData`, `thought`, `promptFeedback`, `finishReason`): https://ai.google.dev/api/generate-content
- Google Gemini Interactions API: https://ai.google.dev/gemini-api/docs/interactions
- Google Gemini model catalog and input payload limits: https://ai.google.dev/gemini-api/docs/models
- Google Gemini rate limits and troubleshooting (`429 RESOURCE_EXHAUSTED`, `400 API_KEY_INVALID`, `403`, exponential backoff): https://ai.google.dev/gemini-api/docs/rate-limits, https://ai.google.dev/gemini-api/docs/troubleshooting
- Google Gemini API Terms of Service: https://ai.google.dev/gemini-api/terms
