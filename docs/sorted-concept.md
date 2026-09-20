# sorted, the concept

For Ohm. Read this once and you know the product well enough to write about it, brief a designer,
answer a photographer's question, or hand it to your own Claude as context. Everything here is
true as of 20 September 2026 and comes from the actual build, not from a pitch.

## One line

sorted is a Mac app that plugs into a shoot disk and makes the whole shoot searchable, in plain
words, on the Mac, with nothing uploaded anywhere.

## What problem it solves

A shoot comes back as a pile. Three days of a documentary, a wedding, a brand film: 4,000 photos,
a few hundred 4K clips, RAW files, drone footage, all named DSC05455 and C0417. The editor,
the photographer or the agency then spends hours (sometimes days) just finding things: the
sunset on the beach, every frame of the client's owner, the sharp ones, the drone shots, the
interviews. Every existing tool that helps with this either uploads the material to a cloud
(slow, expensive, and a problem for client work) or only does photos, or only does culling.

sorted does the finding. It reads the disk once, understands what is in every photo and clip,
and from then on you ask for things the way you would ask a person.

## What it does

1. **Index a whole disk on the Mac.** Photos (JPEG, HEIC, RAW from Sony, DJI and others),
   4K clips, drone footage. The disk is only ever read. The index lives on the Mac.
2. **Search in plain words.** "sunset on the beach", "ring exchange", "man in a white shirt at a
   desk", "drone over the fort". Works on photos and on video (a clip is broken into scenes and
   each scene is searchable).
3. **Faces and people.** Every face in the stills is detected and grouped into people. Name a
   person once and every photo of them is one click away. Drop in one photo of someone and it
   finds every frame of them. When two groups might be the same person, it asks once, "Same or
   Different", and remembers.
4. **Categories it finds by itself.** Beach, ocean, boat, building, office, interview, night,
   food, sky, birds and animals, plus categories discovered from the shoot itself. Drone shots
   are a filter. Out of focus photos and clips can be filtered out.
5. **Deliver.** Select by person, category, search or hand-pick, then export: to a folder on the
   Mac or any disk, or straight to a Google Drive folder (paste the client's link, the files go
   from the disk to Drive without a copy on the laptop). Or reorganise the whole disk into sorted
   folders by category and person, with an Undo.
6. **Nothing leaves the room.** Fully offline. No account needed to sort. No upload, no
   telemetry. The one thing that leaves the Mac is what the user exports, where they say.

## What it is not

- Not an editor. It does not cut, colour or retouch. It finds and delivers; the edit happens in
  Premiere, Resolve, Lightroom, whatever the person already uses.
- Not a cloud service. There is no server doing the work. There never will be; that is the point.
- Not "AI" in the chatbot sense. No language model is used anywhere. It uses two small on-device
  vision models: one that understands images and words in the same space (MobileCLIP), one that
  detects and recognises faces (YuNet and SFace). They run on the Mac's own chip.
- Video faces: not yet. Faces are on photos; clips get the scene breakdown instead.

## Who it is for

Editors, designers, agencies, wedding photographers and filmmakers, documentary editors,
photographers, and anyone who deals with big piles of photo and video data. Eleven ways in on the
landing page: wedding studio, documentary edit, agency and brand films, the editor on the other
end, the photographer on delivery day, designers pulling from a shoot, drone operators, real
estate and hotel shoots, event coverage, archives with old drives, and the solo shooter.

## Numbers that are real (Apple M1, 8 GB, 2020 MacBook Air)

- 3,677 photos, 147 GB (Sony A7R V, 61 megapixel) indexed in 10 minutes 42 seconds.
- 955 items across 800 GB (630 photos, 325 4K clips, a documentary shoot) in 44 minutes 36 seconds.
- A 23 second DJI 4K clip: 1.0 second with hardware decode (9.3 seconds without).
- 36 milliseconds per photo to understand it, 83 milliseconds for the full pass including faces.
- The point of these numbers: a five year old base model Mac does the whole thing. No GPU farm,
  no upload, no subscription for compute.

## How it works, the honest short version

The app is a small local program (Python) that runs on the Mac and shows its interface in the
browser (Chrome). It walks the disk, makes a thumbnail of every photo and a handful of frames of
every clip, runs each through MobileCLIP to get a fingerprint that captures what is in it, runs a
face detector on the stills, and stores all of that in one database on the Mac. A search turns
the words into the same kind of fingerprint and finds the closest images. Categories are the
same trick with fixed descriptions plus clustering. People are face fingerprints grouped by
similarity, deliberately conservative (it prefers to ask "Same person?" over merging two people
by mistake).

## Where it is

- Landing page: https://kirtan-00.github.io/sorted/
- Download for Mac (one Terminal line, public beta): https://kirtan-00.github.io/sorted/download/
- Code: https://github.com/kirtan-00/sorted-app
- Status: public beta, testing right now. First outside tester is Dhrumil (editor). Sign-ups on
  the landing page go to a waitlist; we email when the build is ready for them.
- Next: a signed, notarised Mac app (drag to Applications, no Terminal), then a browser-only
  "try it on 200 photos" demo, then person search across shoots.

## The brand

- Name: **sorted**, always lower-case in the wordmark. Say it like the word: "it's sorted".
- Mark: a 2 by 2 contact sheet of four rounded tiles, three ink, one blush pink tile lifted out
  and tilted. The one that got picked. Never put the pink tile on a pink or peach background.
- Palette: ink #15171b, ground (off-white paper) #f7f5ef, sky #bcd8f0, mint #bfe5cf, butter
  #f2e3a2, blush #f3c8d0, lilac #d6cbee, accent blue #2b5ce0. Pastels are generous on the
  landing page and rare inside the app.
- Type: Archivo (semi-expanded, bold) for headlines, Source Sans 3 for everything else. The app
  itself is a dark Adobe Spectrum style interface, because that is what editors live in.
- Feel: calm, confident, a little dry. Chill pastel page, serious dark tool.
- Kit: `docs/brand/` in the repo (SVG mark, wordmark, favicon, social set, banner).

## Voice rules for anything written about sorted

- Concrete first. Numbers, file counts, minutes. "3,677 photos in under 11 minutes" beats "fast".
- Second person, short sentences, dry. No exclamation marks doing the work of a fact.
- Never say "AI-powered", "revolutionary", "seamless", "unleash", "supercharge", "game-changer".
  Never "leverage". If a sentence could be on any SaaS site, cut it.
- No em dashes. No "--". Use a full stop, a comma or a colon.
- The privacy line is a fact, not a feature: "Nothing leaves the Mac." Say it plainly, once.
- Say photos AND videos. Half the pitch is that it does both.
- "The only one that does it all" is the positioning line: photos, video, people, plain-word
  search, export, in one on-device app. Competitors do one or two of those, usually in the cloud.
- Do not name clients or show client photos. The Diu documentary shoot exists in our numbers as
  "a documentary shoot, 800 GB"; the client, the people in it and the location stay out of public
  material. The landing page uses free Pexels photos for exactly this reason.
- Beta honesty: say it is a public beta, say what it does not do yet (video faces, Intel Macs).

## Lines already in use (reuse freely)

- Plug in the disk. Search the whole shoot.
- The only one that does it all.
- Nothing leaves the room. / Offline. No data leak. Ever.
- A shoot arrives as a pile. (the sorting animation on the landing page)
- Every face, one person.
- Send the client their frames tonight.
- Runs on a Mac. Be first to run it.
- public beta, testing right now

## Competitors, one line each (for positioning, not for naming in public copy)

Excire Foto (on-device photo search, photos only, $199), Peakto (Mac cataloguer, photos and
some video, weaker search), Aftershoot and Narrative (wedding culling, cloud or photo-only),
Adobe Premiere's media intelligence (video search inside Premiere, cloud-assisted, Adobe
subscription). Nobody sells one on-device Mac app that does photos plus video plus people plus
plain-word search plus delivery. That gap is the product.

## If you are briefing your Claude

Paste this file and say what you want: a reel script, a LinkedIn post, an email to a wedding
studio, a comparison table, alt text, whatever. Add one line of context about the audience and
the platform. Ask it to follow the voice rules above and to keep every number exactly as written
here; if it needs a number that is not here, it should say so rather than invent one.
