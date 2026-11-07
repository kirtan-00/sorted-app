# Direct camera connection

Live used to need the maker's tether app writing to a folder. `photosort/tether.py` removes that app: the camera talks to
sorted, and finished files drop into the folder Live already watches. The watcher, RAW and JPEG pairing, sorting and the
drive copy are unchanged.

Where it stands on 2026-11-07:

| | State |
|---|---|
| Wi-Fi or Ethernet, by FTP | Built. 111 tests pass against the real server. **Never run against a real camera.** |
| USB, by ImageCaptureCore | Helper built. Finds no camera because none was connected. New-frame delivery **unproven on any body**, and doubtful on Sony. |

Wired into Live on 2026-11-07: the start screen's "Straight from the camera (Wi-Fi)". USB stays out of the UI until a camera
proves it; the helper ships only with the optional `usb` extra.

## Set up the camera

The camera needs four values: address, port, user, password. sorted shows them when a session starts. The login is random
per session, and the port is 2121, not 21, because macOS only lets an ordinary user bind port 21 on all addresses at once
(tested here: refused on one address, allowed on `0.0.0.0`).

Try a body before Live is wired: `python -m photosort.tether ftp <folder>`. It prints the four values, then every event, and
on Ctrl-C the full command log. Save that log: it is the first real record of what a body sends.

### Sony, first

The owner shoots Sony, model not yet known. Sony publishes a separate FTP Help Guide for each of these bodies (titles read
from `helpguide.sony.net/di/ftp*/v1/en/index.html`):

| Bodies | Passive setting in the guide |
|---|---|
| α7 IV, α7S III, α7R V, FX3, FX30, FX3A, α9, α9 II, α7 III, α7R III/IIIA, α7R IV/IVA, α7C | None. "The camera is not compatible with active mode FTP transfers" |
| α1, α1 II, α9 III, α7C II, α7CR, α7 V (ILCE-7M5), α7R VI (ILCE-7RM6), FX2/FX2B, ILX-LR1 | Passive Mode toggle. **Set it On** |

Some bodies need a minimum system software version: α7S III and α1 have separate guides from Ver.3 and Ver.2, FX3 from Ver.2,
and α9 differs either side of Ver.4.0. No FTP guide turned up for α6700 or ZV-E1, or for any body not listed. That is
"not found", not proof they lack it. Check `MENU → Network` for FTP Transfer Func.

Menu wording below is from the α1 and α9 III guides, and varies a little by body.

1. Join the same network as the Mac: `MENU → Network → Wi-Fi → Access Point Set.` (or WPS Push).
2. `MENU → Network → [Transfer/Remote] or [FTP Transfer] → FTP Transfer Func.`: FTP Connect Method = Wi-Fi where the body has it, then `Server Setting → Server 1`.
3. **Destination Settings**: Host Name = sorted's address. Port = sorted's port. Secure Protocol = Off.
4. **Directory Settings**: Specify Directory blank, Directory Hierarchy = Standard, Same File Name = Does Not Overwrite.
5. **User Info Settings**: User and Password from sorted.
6. **Passive Mode = On**, if the body has it.
7. `RAW+J/H Transfer Target` = RAW+J to send both, or JPEG for speed (see [Speed](#speed)).
8. `Auto FTP Transfer` = On, FTP Power Save = Off, then `FTP Function` = On.
9. Take one frame. It should appear in Live, and `Display FTP Result` should show no failures.

Sony's USB advice does not apply to any of this: the FAQ's macOS 15 and 26 warning concerns USB Remote.

### Canon

From the EOS R1, R3 and R5 manuals. Path: network settings, FTP transfer settings, FTP server.

1. FTP mode = FTP. Server = sorted's address. Port = sorted's port, in place of the usual 00021.
2. Passive mode = Enable. Target folder = Root folder. Overwrite same file = Disable. Directory structure = Default.
3. Login with the user and password from sorted, then turn on automatic transfer and choose the RAW+JPEG transfer type.

### Nikon

From the Z9 reference guide; Zf and Z6III have the same section.

1. Network menu, Connect to FTP server, Network settings, Create profile, Connection wizard, Configure manually.
2. Server type = FTP. Address and port from sorted. PASV mode = On. Anonymous login off: enter the user and password.
3. Destination folder = Home folder. "Enter folder name" only works if the folder already exists.
4. Options: Auto upload = On, Overwrite if same name = Off, **Delete after upload = Off**. Keep Connection = On if the battery allows.

Nikon's manual says "connection to FTP servers running third-party software are not supported". sorted is third-party
software, so a Nikon body working here is outside what Nikon promises.

### Network, with no router

- **A travel router** is the simple answer. It needs no internet, only Wi-Fi for the camera and the Mac.
- **Camera as access point**: the Nikon guide documents this mode. Join the camera's network from the Mac, even after Start:
  sorted looks at the Mac's addresses every 3 seconds and serves the new one on the same port. Sony's FTP guides read here
  only describe joining an access point.
- **Mac hotspot**: Apple's Internet Sharing needs a source to share from, such as Ethernet. Third-party guides say Wi-Fi cannot
  share to itself, so a Mac on Wi-Fi alone cannot do it. A shared network shows as `bridge100` and is labelled hotspot.
- **A cable**: Sony's guides cover FTP over wired LAN through a router. That is the likely answer for anyone who wanted USB for
  reliability. A direct camera-to-Mac cable is undocumented and untried, though link-local addresses are offered.
  Sony's FTP Connect Method = USB Tethering is no help here: it goes through a smartphone, not a computer.
- Guest networks often block devices from reaching each other, which looks like a dead connection.

## How a frame arrives

1. Bytes go to `<folder>/.sorted-incoming/`. The dot keeps it away from Live's walker, scans and the Finder.
2. At the end of the upload the file is flushed to disk and checked for being whole.
3. It is renamed into the folder without replacing anything.
4. **Only then** does the camera get its `226`. A body may delete from the card on that reply, so a failed landing sends
   `451` instead. pyftpdlib sends the reply first, which is one reason this server is written on the standard library.
   The other is that it adds nothing to the installer.

**Whole or not.** FTP has no length: a body dropping the data connection cleanly looks identical to one that finished. So the
file is a witness, never a judge. An upload that ends cleanly always lands, and if the file looks cut, the photographer is
told. One whose connection failed (reset, stall, control lost, ABOR) stays out, unless the file proves whole or the same body
failed the same way at the same size before. So no body ever loops on 426. The witness reads: JPEG end marker, the image offsets in TIFF directories (ARW, NEF, CR2, DNG), box sizes in ISO files (CR3,
HEIF, MP4, MOV), and `ALLO` if a body sent it. Anything it cannot read passes, RAF for one.

**Names.** A name is never reused for different bytes: `DSC00001_1.JPG`. The same bytes again (a camera that missed the reply)
land once. Two bodies with the same numbers each keep their own stem, so a RAW stays beside its own JPEG.

**Interrupted frames.** Sony: a frame interrupted and sent again gets "an underscore and numeric characters" even on Overwrite.
Canon: `_1`, or `_a1` on a resend. That would split a RAW from its JPEG, so when this body left a partial of the plain name and
nothing landed under it, the frame goes back to the plain name. No manual mentions REST or APPE, but both work anyway:
resume continues the partial, and never touches a landed frame.

**Kept files.** Dropped uploads stay as `.part`, superseded ones as `.stale`. Neither is ever deleted, and the count is in
`status()`, because the folder is hidden and they would otherwise be forgotten.

**Pairing needs no change.** A RAW after its JPEG attaches at any delay. A RAW first waits `PAIR_WINDOW_S` (2 s) from when it
settles, and a JPEG sent next is far smaller than the RAW before it.

## What each brand's manual says

| | Sony | Canon | Nikon |
|---|---|---|---|
| Passive | Toggle, or passive only | Setting; enable on Error 41 | PASV toggle |
| Port | 0 to 65535 (α9 III) | "Normally" 00021 | Typed; firewall list 21 and 32768 to 61000 |
| Encryption | Off, FTPES, SFTP on some | FTP, FTPS, SFTP on newer | FTP, FTPS, SFTP |
| Root certificate | `cacert.pem`, card slot 1 | `ROOT.CER`, `.CRT` or `.PEM` | Import from card |
| Folder | Created, or `A/DCIM/100MSDCF` | Created if missing | Must already exist |
| Duplicates | `_1`, or overwrite | `_1`, `_a1`, or overwrite | Numbers added, or overwrite |
| After a drop | Sends again, name with `_N` | Auto retry unless cancelled or off | Resumes marked pictures at power on |
| Anonymous | Not stated | Not stated | Optional |

**Not in any manual**, so the tests imitate a guess: the actual command sequence, whether a body sends SIZE, MDTM, LIST or
NLST before a file, and whether any uses RNFR. Rename is refused: if a body uploads a temp name and renames, the frame lands
under the temp name, and the command log shows it. The older Sony guide builds a folder per transfer with model, serial and time.
That needs MKD, which works. Data ports sit in 50000 to 50399, inside Nikon's range, which is probably the camera's own port range.

## Security

Plain FTP is unencrypted. Anyone on the same network can read the login and the frames, and Sony's guide says so. The limits
here are about damage, not secrecy: private addresses only, no VPN interfaces, on only during a session, random login, no
anonymous, 3 tries per connection, a 60-second lockout after 10 failures, and data ports that accept only the logged-in address.

The folder is the wall. Reads, deletes, renames and `SITE` are all refused. Climbing out with `..` is refused, not clamped, as are
symlinks out, dot names, and `PORT` to anyone but the caller.

**FTPS** is built and tested with a TLS client, explicit mode only, and adds nothing to install because `cryptography` is already
there through other packages. Pass `tls_dir` to turn it on: plain logins are then refused. No body needs it, and each must
import the certificate from a card first. Not tried on a camera. Sony's Root Certificate Error = Connect skips verification,
which gives encryption without proof of who answered. The certificate is kept between shoots with the addresses of its first
run: delete the two `.pem` files in `tls_dir` if the Mac's address changes and a body verifies it.

## macOS

- **Firewall.** With it on, macOS asks once whether python may accept incoming connections, naming python rather than sorted. It is
  off on this Mac, so that prompt was never seen. `firewall()` reads the state without changing it. Block all incoming refuses
  the camera and asks nothing, and the screen should say so.
- **Local Network permission.** Accepting incoming TCP needs none (Apple TN3179), so passive mode is clear. Active mode makes
  an outgoing connection, which does need it when launched from sorted.app, and a failure would show as a `425`. `active_mode` in
  status flags a body doing this. Sony's Passive Mode toggle on Off is the likely cause.
- **Sleep.** Live already holds the Mac awake.

## Speed

Measured on this Mac, real server and `ftplib`, 30 MB frames, flushed to disk each time. All of it is loopback: **no radio is
involved**, and the LAN address run also stays inside the Mac.

| Run | Throughput | Per frame, median | Worst |
|---|---|---|---|
| One body, internal SSD, 127.0.0.1 | 242 MB/s | 116 ms | 193 ms |
| One body, the Mac's LAN address | 233 MB/s | 115 ms | 385 ms |
| Two bodies at once | 354 MB/s total | 150 ms | 362 ms |
| FTPS, one body | 143 MB/s | 207 ms | 392 ms |
| exFAT disk image on the SSD | 103 MB/s | 284 ms | 437 ms |

The camera is the limit. No controlled test across bodies was found, so these are two users' numbers, not specifications. An R5
owner [measured about 15 MB/s](https://www.dpreview.com/forums/thread/4515653), so 2 seconds for 30 MB. An α1 user
[reports about 1.5 s for a 50 MB RAW](https://sonyalpha.blog/2021/06/11/ftp-pc-remote-wireless-transfer-on-macos-with-sony-a1-a9-a7x/),
about 33 MB/s, so about 1 second. That is 7 to 16 times below what the server takes.

So one frame per 1 to 2 seconds is the working figure for RAW. A burst queues on the camera and drains at that rate. For a
live verdict on set, sending JPEG alone is much faster, with RAW coming off the card later.

## USB

**Tested on this Mac** (macOS 26.1, Darwin 25.1):

- `ICDeviceBrowser` runs from an unbundled python through pyobjc, and from a plain ad-hoc-signed Swift binary. No sudo, no Info.plist,
  no prompt, nothing in the `tccd` log. It starts `ptpcamerad` on demand, the sanctioned path past the old gphoto2 block.
- `requestControlAuthorization` does not exist on macOS: `IC_UNAVAILABLE(macos)` in the SDK headers, and absent at runtime.
- `requestEnableTethering` is deprecated since macOS 14 and has no effect. The header says tethered capture is simply on when a
  body reports `ICCameraDeviceCanTakePicture`.
- Every call the helper makes resolves. It reports `waiting`, stops cleanly, and leaves no process behind.
- `system_profiler SPUSBDataType` lists nothing: **no camera or phone was connected, so nothing past the browser has run.**

The helper is `python -m photosort.tether usb <folder>`, or `--list`, printing one JSON event a line. It runs in its own process because it
needs a main-thread run loop and a bridge crash must not end a shoot. Frames already on the card are left alone, nothing on
the card is ever deleted, and files take the same landing path as FTP. The decisions are tested with fakes; the real delegate
callbacks and the download are not.

**Not confirmed, and only a camera can tell:**

- Whether a session opens without a prompt. A plain process had no prompt only because no device was attached.
- What `sorted.app` does differently. It is a shell launcher running python, so any prompt would likely name it or python.
- Which bodies report `ICCameraDeviceCanTakePictureUsingShutterReleaseOnCamera`, and whether a shutter press produces `didAddItems`.
- **Sony.** Its help guides describe PC Remote as control through Imaging Edge, a different mode from Mass Storage or MTP. I expect
  PC Remote frames to go through Sony's vendor commands and not as ordinary new items, in which case this helper sees the
  camera and no frames. That expectation is from memory of libgphoto2, not from a source read today. The helper prints raw
  `ptp_event` data to settle it. Whether a body can shoot at all in MTP mode is also unconfirmed.
- Canon and Nikon on plain PTP: no claim either way.

**Verdict:** the route is open and cheap to finish testing, but not something to promise. FTP is the direct connection to
ship, and a cable means Ethernet where a body has it.

**If USB must work on Sony:**

1. **ICC's PTP passthrough** (`requestSendPTPCommand`) could speak Sony's vendor protocol through the sanctioned channel, with no
   licence, but it is reverse-engineered and needs a camera to build against.
2. **[Sony Camera Remote SDK](https://support.d-imaging.sony.co.jp/app/sdk/en/)**: macOS 14.1, 15.1 and 26.0 and later, USB, LAN and Wi-Fi. Free, but apply by
   region. The [licence](https://support.d-imaging.sony.co.jp/app/sdk/licenseagreement/en.html) lets a sold app include the library, sets no fee, and
   requires explaining to end users and getting their consent, **including telling them that controlling a device through the app
   takes it out of Sony's manufacturer warranty**. Apple Silicon is not stated, and whether it escapes the macOS 26 USB trouble is unknown.
3. **Canon EDSDK**: Canon's [SDK page](https://www.canon.dk/business/imaging-solutions/sdk/) lists Mac, access is through its developer programme, and
   [one regional agreement](https://asia.canon/en/campaign/developerresources/terms-conditions-for-digital-camera-software-development-kit-sdk) is a one-year licence.
   Current macOS terms and fees were not found.

**Cheapest next test:** plug in an iPhone and run the helper with `--list`, then the camera. An iPhone is a real PTP device.
This one can raise prompts on the owner's screen: macOS's accessory approval and the phone's Trust dialog. Do it with the owner there.

## Install

- **Wi-Fi: nothing.** Standard library only, and `cryptography` is already installed for FTPS.
- **USB: optional.** `pip install ".[usb]"` adds pyobjc 13.0 (core, Cocoa, ImageCaptureCore), 24 MB on disk, 11 MB of it pyobjc's own tests. The installer
  is unchanged, and without it the helper reports `unavailable`. pyobjc belongs in `docs/LICENCES.md` if this ships.
- A Swift helper would be 57 KB instead, but `swiftc` is not on a buyer's Mac, so it would have to be built and shipped.

## What was tested, and what was not

`tests/test_tether.py`, 111 tests. `ftplib` against the real server, each passing on the sequence named: Sony Standard and Same as
in Camera, Canon target folder, 30 MB frames, a 12-frame burst, two bodies at once, cuts by FIN, RST, control loss and ABOR, resume by
REST and APPE, new-name resend, wrong password, anonymous, traversal, symlinks, refused commands, active mode, FTPS. Live's
own `photo_files` never saw a partial.

Run by hand: a real exFAT image (`RENAME_EXCL` returns `ENOTSUP` and `os.link` is unsupported, so the claim-then-replace path is the
one in use, and it held); `looks_complete` on 2,733 real files on this Mac (1,500 JPEG, 605 HEIC, 520 MOV/MP4, 108 TIFF/DNG), none falsely rejected.

**Weak spots:**

- No complete real RAW was available. The ARW and DNG fixtures are header stubs, correctly found cut, and one padded DNG passes. The check can no longer refuse a frame, so at worst a whole RAW gets a wrong "may be incomplete" note. Watch for that on the first real frame.
- The two-body case is simulated from one address, so per-body naming is tested on `Lander`, not over two real IPs.
- The firewall prompt, Wi-Fi drops and real camera timing are all untested.

## Sources

- Sony: [α1 FTP guide](https://helpguide.sony.net/di/ftp_2420/v1/en/index.html), [α9 III registration](https://helpguide.sony.net/di/ftp_2380/v1/en/contents/FTP_server_registration.html), [α7 IV guide](https://helpguide.sony.net/di/ftp_2110/v1/en/index.html), [older bodies](https://helpguide.sony.net/di/ftp/v1/en/index.html), [USB Connection](https://helpguide.sony.net/ilc/2450/v1/en/contents/0601_usb_connection.html), [Imaging Edge FAQ](https://support.d-imaging.sony.co.jp/app/imagingedge/en/faqs/)
- Canon: [EOS R5 FTP](https://cam.start.canon/en/C003/manual/html/UG-06_Network_0070.html), [EOS R3 FTP](https://cam.start.canon/en/C010/manual/html/UG-06_Network_0070.html), [EOS R3 settings](https://cam.start.canon/en/C010/manual/html/UG-06_Network_0130.html), [EOS R1 settings](https://cam.start.canon/en/C018/manual/html/UG-06_Network_0170.html)
- Nikon: [Z9 FTP servers](https://onlinemanual.nikonimglib.com/z9/en/ftp_servers_connecting_via_ethernet_or_wlan_67.html), [Z9 Connect to FTP server](https://onlinemanual.nikonimglib.com/z9/en/nwm_connect_to_ftp_server_325.html)
- Apple: [TN3179](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy), [ICDeviceBrowser](https://developer.apple.com/documentation/imagecapturecore/icdevicebrowser), the SDK headers on this Mac, [Internet Sharing](https://support.apple.com/guide/mac-help/mchlp1540/mac)
- [Tether Studio](https://www.tetherstudio.app/) claims USB and Wi-Fi tethering at $99, and says nothing on how.

## How Live uses it

- `StartReq.camera = "wifi"`: the folder is `export_root()/Live/<shoot>`, made at Start. The receiver starts after the session,
  and Finish stops it first. It waits for a frame on the wire (quiet 1.5 s, stuck 10 s, 90 s at most), then lets the watcher
  settle it, a lone RAW its pair window included, before the session stops. A second Finish press cuts nothing off.
- The login lives in `app_home()/camera-login.json` (0600): user, password, port, and the host a camera last really reached.
  `GET /api/live/camera?shoot=` gives the menu values, the folder, `moved`, `block_all`, and the running `line`.
  `POST /api/live/camera/reset` makes a new login, refused while a session runs.
- A file that lands looking cut gives a status message ("may be incomplete. Check it on the card") and a `cut` alert on its
  shot. Back to the last shoot brings the receiver back (`camera` is kept in the shoot's meta).
- Tests: tests/test_live.py (real receiver on loopback) and tests/test_tether.py.
