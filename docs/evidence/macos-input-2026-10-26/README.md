# Mac keyboard and model menu, 2026-10-26

Tested on an M3 MacBook Air, macOS 14.7, Python 3.13.2 and Tk 8.6.15.

The old override-redirect panel reported `canBecomeKeyWindow=false` and `isKeyWindow=false` through Cocoa. Tk still reported its Text widget as focused. A synthetic Tk key produced `a`, but a native Cocoa key event posted to this test application's own queue did not append `b`.

With the managed Mac window, both native window flags were true. The same native key appended `b`, producing `ab`. This reproduces why widget-only typing tests missed the fault. macOS now keeps a standard title bar on the expanded panel; the collapsed chip stays borderless.

The model picker now uses an Aqua menu instead of a custom override-redirect window with a delayed grab. Selection callbacks run after native tracking finishes. Twelve open/select/cancel cycles completed, including a change from DreamShaper to RealVisXL. Every cycle ended with the menu destroyed and no grab. Native typing still worked afterward.

These are application-local native key events, not global keyboard injection. The probe did not require Accessibility permissions or change macOS settings. It used the actual PromptBox and Select classes. It did not run image generation or reproduce every possible backend-related freeze. Further reports of a freeze still need logs.

The [Tk platform note](https://core.tcl-lang.org/tk/timeline?r=doc-popup-platform) documents why actions are deferred until the native popup returns on macOS.

This release does not change LaMa's generation or compositing. The reported large-area smoothing remains a separate quality investigation.
