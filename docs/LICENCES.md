# Licences: what sorted is built on, and whether it can be sold

Every dependency, its licence, and why it is cleared or not. Licences here were read off the installed
package metadata in `.venv` on 2026-09-24, not from memory, with the version that was installed. Model
weights are listed separately because a weights licence is usually not the code's licence.

Two things make this document worth keeping current. A copyleft dependency in a product you sell without
source is the one defect that cannot be patched after launch. And a model whose weights forbid commercial
use is not a licence you can buy your way out of.

## Verdict

Sellable, with one question to answer.

- **pyexiv2 is gone** (2026-10-03). It was the one copyleft dependency; exifread (BSD-3-Clause) reads the
  EXIF fallback in its place. See below.
- **Question: ffmpeg.** The installer fetches a build rather than bundling one. Which build, and under
  which licence, has not been checked. See below.
- Everything else is permissive and cleared.

## The encoder, and why it is being replaced

| | licence | verdict |
|---|---|---|
| `apple/MobileCLIP-S1-OpenCLIP` weights | `apple-amlr` | **cannot ship.** Research only, excludes commercial exploitation, product development, or use in any commercial product or service. Not purchasable at any price. |
| `timm/ViT-B-16-SigLIP` weights (`webli`) | Apache-2.0 | cleared |
| `timm/ViT-B-16-SigLIP` tokenizer, shipped in `models/siglip-tokenizer/` | Apache-2.0 | cleared, 2.3 MB, redistributed with the app |

MobileCLIP-S1 was the default until 2026-09-24 and is the reason this document exists. SigLIP now ships in
its place; MobileCLIP stays selectable for comparison but cannot be sold. The replacement is measured,
not assumed: both encoders were run end to end over 955 photographs of NSG_26 Part 2 on 2026-09-24. SigLIP
sends 1.3% of that shoot to "other" against MobileCLIP's 1.7%, the two agree on 740 of the 955, and SigLIP
is clearly better on any query carrying more than one idea. It costs 25% more scan time and the weights a
buyer downloads on first run go from 325 MB to 812 MB. Full numbers in
`.superpowers/sdd/siglip/task-2-report.md`.

Switching encoders is `PHOTOSORT_CLIP_MODEL`; `photosort/config.py` holds the table. Every threshold that
depends on the encoder's similarity scale is per encoder in `classify.py`, because MobileCLIP's constants
under SigLIP send 90.7% of a shoot to "other".

## Face recognition

| | licence | verdict |
|---|---|---|
| YuNet, `models/face_detection_yunet_2023mar.onnx` | MIT | cleared, redistributed with the app |
| SFace, `models/face_recognition_sface_2021dec.onnx` | Apache-2.0 | cleared, redistributed with the app |

Both are OpenCV Zoo models and both are in the repo, so both are redistributed rather than downloaded.

## Python dependencies

Cleared, all permissive:

| package | version | licence |
|---|---|---|
| fastapi | 0.141.1 | MIT |
| ftfy | 6.3.1 | Apache-2.0 |
| google-api-python-client | 2.200.0 | Apache-2.0 |
| google-auth | 2.58.0 | Apache-2.0 |
| google-auth-oauthlib | 1.4.1 | Apache-2.0 |
| huggingface_hub | 1.32.0 | Apache-2.0 |
| exifread | 3.5.1 | BSD-3-Clause |
| ImageHash | 4.3.2 | BSD-2-Clause |
| numpy | 2.4.6 | BSD-3-Clause and others, all permissive |
| open_clip_torch | 3.3.0 | MIT |
| opencv-python | 5.0.0.93 | Apache-2.0 |
| pillow | 12.3.0 | MIT-CMU |
| pillow_heif | 1.7.0 | BSD-3-Clause |
| protobuf | 7.36.2 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| rawpy | 0.27.1 | MIT (the wrapper; LibRaw underneath, see below) |
| regex | 2026.9.10 | Apache-2.0 and CNRI-Python |
| safetensors | 0.8.0 | Apache-2.0 |
| scikit-learn | 1.9.1 | BSD-3-Clause |
| sentencepiece | 0.2.2 | Apache-2.0 |
| starlette | 1.6.0 | BSD-3-Clause |
| timm | 1.0.29 | Apache-2.0 |
| tokenizers | 0.23.2 | Apache-2.0 |
| torch | 2.14.0 | BSD-3-Clause with Apache-2.0 components |
| torchvision | 0.29.0 | BSD-3-Clause |
| tqdm | 4.70.1 | MPL-2.0 and MIT |
| transformers | 5.17.0 | Apache-2.0 |
| uvicorn | 0.53.0 | BSD-3-Clause |

Two notes on that table. `tqdm` is MPL-2.0, which is copyleft per file: shipping it unmodified is fine,
editing its source and shipping the result would oblige us to publish those files. We do not edit it.
`transformers`, `sentencepiece` and `tokenizers` are here only because open_clip builds SigLIP's tokenizer
through `transformers.AutoTokenizer`; they arrived with the encoder swap and all three are Apache-2.0.

### pyexiv2: removed 2026-10-03

`pyexiv2` 2.16.0 declared **GPLv3**, and it wraps **exiv2**, which is GPL-2.0-or-later. Either way it was
strong copyleft: linking it into software sold without source would have obliged us to release sorted
under the GPL.

It was the fallback EXIF reader in one function of `photosort/features.py`, behind Pillow. That fallback
is now `exifread` 3.5.1 (BSD-3-Clause, pure Python, nothing linked), and pyexiv2 is out of
`pyproject.toml`. A test fails if it comes back. The swap also fixed a silent bug: pyexiv2's bundled
libexiv2 linked Homebrew's `inih`, which was not installed, so the import had been failing inside a
broad `except` and every Sony ARW indexed with no date, camera or size.

### rawpy and LibRaw

`rawpy` is MIT, but the thing it wraps, **LibRaw**, is dual licensed **LGPL-2.1 or CDDL-1.0**. CDDL-1.0
permits linking into a closed product, so choosing CDDL clears it. Record the choice: it is a licence
election, and the election should be stated in the product's own notices rather than left implicit.

### ffmpeg: open question

`photosort/video.py` shells out to `ffmpeg` and `ffprobe`; nothing is linked, so no library obligation
attaches. But `Install sorted.command` does not merely require ffmpeg, it **fetches** one: Homebrew's when
Homebrew is present, otherwise a pinned static arm64 build from ffmpeg.martin-riedl.de.

The ffmpeg on this Mac right now is version 8.1.2 built with `--enable-gpl --enable-version3`, that is a
**GPL-3.0 build**. It cannot be redistributed inside a closed product. Shelling out to a copy the user
installed themselves is not distribution and carries no obligation; having our installer download it for
them sits closer to the line.

Two things to settle, neither of them yet done:

1. Which configure flags the pinned martin-riedl build carries. An LGPL build (no `--enable-gpl`) would
   remove the question entirely.
2. Whether the installer should fetch ffmpeg at all, or tell the person to run one `brew install` line.
   The second is unambiguously safe and costs one step of onboarding.

## What to do next, in order

1. ~~Drop pyexiv2.~~ Done 2026-10-03.
2. Settle the ffmpeg question, either by pinning an LGPL build or by not fetching one.
3. Re-embed the existing indexes. The default encoder is now `ViT-B-16-SigLIP`, so only the re-embed of
   shoots indexed under MobileCLIP-S1 is outstanding.
4. Record the LibRaw election (CDDL-1.0) in the product's notices.
5. Ship the notices themselves: a page in the app listing every component above with its licence text,
   which Apache-2.0, BSD and MIT all require when you redistribute.
