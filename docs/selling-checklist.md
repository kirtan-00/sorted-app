# What has to be true before sorted can be sold

Written 2026-09-22 against version 0.3.2. Everything here is either a blocker (you cannot take money
without it), a launch-day requirement (you can take money but the first week will hurt), or work for the
first 90 days. Effort is my estimate in working days.

---

## A. Blockers. Do not take a rupee until these are settled.

### A1. The dependency licence audit. Two known red flags.
sorted is built on other people's code, and at least two pieces of it may forbid what you are about to do.
This is the one item that can make the whole product unsellable, so it goes first.

Checked on 2026-09-24. Every licence below was read off installed package metadata, and the full audit is
in `docs/LICENCES.md`. The one blocker it found, pyexiv2, is now out; ffmpeg is still an open question.

- **pyexiv2 is out (2026-10-03).** The installed 2.16.0 declared GPLv3 and exiv2 underneath it is
  GPL-2.0-or-later, with no commercial licence to buy. It was the fallback metadata reader in one function
  of `features.py`; exifread (BSD-3-Clause) does that job now, and a test fails if pyexiv2 returns to
  `pyproject.toml`.
- **MobileCLIP-S1 is confirmed unsellable and the replacement now ships by default.** The weights are `apple-amlr`:
  research only, excludes commercial exploitation or use in any commercial product, and not purchasable at
  any price. `ViT-B-16-SigLIP`/`webli` is Apache-2.0 and is now wired in behind `PHOTOSORT_CLIP_MODEL`,
  with every encoder-dependent threshold in `classify.py` re-derived per encoder. Both encoders were run
  end to end over 955 photographs of NSG_26 Part 2: SigLIP sends 1.3% to "other" against MobileCLIP's 1.7%,
  the two agree on 740 of 955, and SigLIP is clearly better on compound queries. It costs 25% more scan
  time and a 812 MB first-run download instead of 325 MB. The default is now `ViT-B-16-SigLIP`;
  `PHOTOSORT_CLIP_MODEL=MobileCLIP-S1` reverses it without a code change and both suites stay green.
  What is left is one re-embed of every existing index, which the byte-width guard already handles as
  ordinary Continue work rather than a wipe.
- **rawpy wraps LibRaw** (LGPL-2.1 or CDDL). LGPL is workable but constrains how it is bundled.
- **ffmpeg**: the licence depends on the build. A GPL build (most Homebrew ones) cannot be shipped inside
  a closed product. Either ship an LGPL build you compile, or keep requiring the user to install it.
- torch, torchvision, opencv-python, Pillow, numpy, scikit-learn, fastapi, uvicorn, imagehash: permissive,
  no issue.
- YuNet and SFace ONNX (in `models/`): per-model licences in the OpenCV Zoo. Confirm both.

**Output: a LICENCES.md listing every dependency, its licence, and why it is cleared for commercial sale.**

### A2. Licensing and activation that works with no network.
The promise is that the app never phones home. A licence check that calls a server breaks the promise and
breaks the product for anyone on a location shoot with no signal.

The design that fits: you sell a signed licence file. The app carries a public key, verifies the signature
locally, and never talks to anything. Piracy is possible and you accept that, the same way Photo Mechanic
does. Decide the trial too: time-limited or file-count-limited, both enforceable offline.
**4 to 6 days including the key issuing side.**

### A3. Payment, and who is the merchant of record.
- Selling worldwide means VAT in the EU and UK, sales tax in the US. Paddle or Lemon Squeezy act as the
  merchant of record and absorb all of it for roughly 5 percent. Razorpay does not, and leaves you filing.
- Selling into India means GST and a proper invoice.
- Decide: one price forever, or a yearly licence. My read for a tool like this is a one-time price with a
  year of updates, which is what Photo Mechanic does and what shooters expect.
**2 days once the decision is made.**

### A4. A signed, notarized installer.
`curl | bash` is fine for friends and impossible for paying customers. Nobody pastes a shell command into
a terminal after handing over money, and a good number cannot.

Needs: Apple Developer Program (99 USD a year), a Developer ID certificate, a real .app with Python and
the models bundled, hardened runtime, notarization, stapling, and a .dmg. This is the single biggest
engineering item on the list. **6 to 10 days.**

### A5. The privacy claim has to be exactly true.
Right now the sentence "nothing leaves the Mac" is nearly true and that is not good enough to print on a
page someone pays against. The facts:
- The app makes no network request except the update check, which is opt-in and off by default.
- The installer pings Supabase on install.
- The website records visits and collects emails.
Write the promise precisely, say what the update check sends and when, and put it on the site and in the
app. If you keep the install ping, disclose it. **0.5 days.**

### A6. Entity, terms and policies.
Who is selling: CrayWingz, or a new entity. Then: an end user licence agreement, terms of sale, a refund
policy (14 days no questions is the norm and cheap), and a privacy policy that matches A5. A template
plus a lawyer's read. **2 days plus the lawyer.**

### A7. The name and the domain.
"sorted" is a common English word, so it cannot be defended and the good domains are gone. sorted.app and
sorted.com are taken. Either buy the best available (sorted.film and sorted.video were the recommendation)
and accept a weak trademark position, or rename before anyone knows the product. Renaming after launch is
far more expensive than renaming now. **Decision, then 1 day of changes.**

---

## B. Launch-day requirements. Sellable without them, painful without them.

### B1. Proof it works at the size a professional shoots.
There is no published measurement above a few thousand files. Before a page claims anything: a 20,000 file
run and a 50,000 file run, with timings, peak memory, index size, and what happens when the disk is pulled
out halfway. Those numbers become the landing page. **2 days.**

### B2. Flags, star ratings and bins.
Every competing tool has them, every photographer expects them, and sorted has none. Already agreed as the
next build. **2 days.**

### B3. Writing decisions back out, as XMP sidecars.
This is the difference between a toy and a tool in a professional workflow. A photographer culls in sorted
and then opens Lightroom, Bridge or Capture One, and expects the flags and stars to be there. XMP sidecars
are how that happens and it is a well defined format. Without it, sorted is a dead end in the workflow.
**3 days, and it depends on B2.**

### B4. First run without a terminal.
Double-click, a window opens, it asks for a folder. No Homebrew, no uv, no Python, no "keep the terminal
open". Falls out of A4 but has to be tested by somebody who is not you.

### B5. Real beta users who are not friends.
Dhrumil and Dhaval do not count: they will forgive things a stranger will not. Ten photographers who found
you cold, each scanning their own shoot, with the usage log telling you where they stop. The Clapper
lesson applies directly: activation is the number that matters, not signups.

### B6. A demo you are allowed to show.
Every shoot on your disks is client work under NDA. You need one shoot you own outright, shot for this
purpose, to put on the landing page and in the demo video. Without it the site cannot show the product
doing its job. **1 shooting day.**

### B7. Support that exists.
An email address a human reads, a stated response time, and a page of answers to the ten questions people
will actually ask. A changelog per release.

### B8. Updates.
A way to tell an existing customer a new version is out and let them install it in one click, without the
app phoning home unasked. The opt-in check exists; the install side does not.

---

## C. First 90 days.

- Windows. 2 to 3 days of work and a Windows machine. It doubles the market and cannot be done blind.
- Better categorisation: split "people", which holds 68 percent of a real shoot, and the learn-from-your-
  corrections pass. This is the thing that will make it feel intelligent rather than approximate.
- Pricing experiments, once there is enough traffic to measure anything.
- An affiliate or referral line for wedding photographer communities, which is how this category spreads.
- Intel Mac support, if the beta shows anyone needs it.

---

## The order I would actually do it in

1. A1 licences. Nothing else matters if the answer is no.
2. A7 name and domain. Cheapest now, most expensive later.
3. A4 signed installer, in parallel with A2 licensing.
4. B2 flags and ratings, then B3 XMP.
5. B1 scale numbers, B6 demo shoot.
6. A3 payment, A6 terms, A5 privacy wording.
7. B5 beta with strangers. Only then a price.
